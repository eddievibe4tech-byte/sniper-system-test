"""
Alpha Vantage Fallback 與財報快取 單元測試

對應議題：財報資料源五層鏈——
  快取(40天TTL) → FinMind → yfinance → Alpha Vantage(配額管控) → 過期快取(cache-stale) → None(規則引擎)

驗證重點：
1. AV 客戶端解析（含 "None" 字串、HTTP 200 限流訊息欄）
2. 每日配額管控（25 次/日免費額度 → 安全上限 20；用盡時零請求）
3. 台股後綴探測只做一次（結果寫進 cache meta，不逐檔燒配額）
4. 快取命中／過期兜底（cache-stale）行為
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.alpha_vantage_client import AlphaVantageClient
from src import financial_cache as fc


def _report(revenue="1000000", gross="600000", net="300000", eps="10.5"):
    return {"quarterlyReports": [{
        "fiscalDateEnding": "2026-06-30",
        "totalRevenue": revenue, "grossProfit": gross,
        "netIncome": net, "eps": eps,
    }]}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# ─────────────────────────────────────────────
# AlphaVantageClient 解析
# ─────────────────────────────────────────────

def test_parse_quarterly(monkeypatch):
    c = AlphaVantageClient(api_key="dummy")
    monkeypatch.setattr(c.session, "get", lambda *a, **k: _FakeResp(_report()))
    out = c.get_quarterly_financials("2330.TPE")
    assert out["gross_margin"] == 60.0
    assert out["net_margin"] == 30.0
    assert out["eps"] == 10.5
    assert out["revenue"] == 1000000.0
    assert out["fiscal_quarter"] == "2026-06-30"
    assert out["source"] == "alphavantage"


def test_none_strings_handled(monkeypatch):
    """AV 缺失欄位回傳 "None" 字串——不得崩潰，該欄位應為 None"""
    c = AlphaVantageClient(api_key="dummy")
    monkeypatch.setattr(c.session, "get",
                        lambda *a, **k: _FakeResp(_report(gross="None", net="None")))
    out = c.get_quarterly_financials("2881.TPE")
    assert out["gross_margin"] is None and out["eps"] == 10.5
    assert out["net_margin"] is None


def test_note_response_is_failure(monkeypatch):
    """AV 限流是 HTTP 200 + Note 欄——必須視為失敗而非資料"""
    c = AlphaVantageClient(api_key="dummy")
    monkeypatch.setattr(c.session, "get", lambda *a, **k: _FakeResp(
        {"Note": "Thank you for using Alpha Vantage! ... rate limit ..."}))
    assert c.get_quarterly_financials("2330.TPE") is None


def test_disabled_without_api_key(monkeypatch):
    """未設定 API key → enabled=False，且發出請求前就回 None（零成本退場）"""
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    c = AlphaVantageClient()
    assert c.enabled is False
    def boom(*a, **k):
        raise AssertionError("無 key 時不應發請求")
    monkeypatch.setattr(c.session, "get", boom)
    assert c.get_quarterly_financials("2330.TPE") is None


def test_eps_falls_back_to_epsDiluted(monkeypatch):
    payload = {"quarterlyReports": [{
        "fiscalDateEnding": "2026-06-30",
        "totalRevenue": "1000000", "grossProfit": "None",
        "netIncome": "None", "eps": "None", "epsDiluted": "9.8",
    }]}
    c = AlphaVantageClient(api_key="dummy")
    monkeypatch.setattr(c.session, "get", lambda *a, **k: _FakeResp(payload))
    out = c.get_quarterly_financials("2330.TPE")
    assert out["eps"] == 9.8


# ─────────────────────────────────────────────
# 配額管控（_av_call）
# ─────────────────────────────────────────────

def test_quota_guard_blocks_without_http(monkeypatch):
    """配額用盡時絕不發請求——保護免費版 25 次/日額度"""
    cache = {"meta": {"av_quota": {"date": fc._today(), "used": fc.AV_DAILY_QUOTA}}, "stocks": {}}
    av = AlphaVantageClient(api_key="dummy")
    def boom(*a, **k):
        raise AssertionError("配額用盡時不應發請求")
    monkeypatch.setattr(av.session, "get", boom)
    from src.finmind_client import _av_call
    assert _av_call(cache, av, "2330.TPE") is None


def test_quota_consumed_on_call():
    """每次 _av_call 消耗 1 點配額，剩餘量遞減"""
    cache = {"meta": {}, "stocks": {}}
    before = fc.quota_remaining(cache)
    av = AlphaVantageClient(api_key="dummy")
    # get_quarterly_financials 直接回 None（跳過網路：模擬請求失敗）
    av.get_quarterly_financials = lambda symbol: None
    from src.finmind_client import _av_call
    _av_call(cache, av, "2330.TPE")
    assert fc.quota_remaining(cache) == before - 1


def test_quota_resets_across_days():
    """跨日自動重置配額（按台北時區計日）"""
    yesterday = (datetime.now(fc.TZ_TAIPEI) - timedelta(days=1)).strftime("%Y-%m-%d")
    cache = {"meta": {"av_quota": {"date": yesterday, "used": fc.AV_DAILY_QUOTA}}, "stocks": {}}
    assert fc.quota_remaining(cache) == fc.AV_DAILY_QUOTA


# ─────────────────────────────────────────────
# 台股後綴探測（只做一次，寫進 cache meta）
# ─────────────────────────────────────────────

def test_suffix_probe_success_cached_in_meta():
    """探測成功 → meta.tw_suffix 記錄，後續股票直接使用不再逐檔探測"""
    from src.finmind_client import _resolve_tw_symbol
    cache = {"meta": {}, "stocks": {}}
    av = AlphaVantageClient(api_key="dummy")
    calls = []
    def fake_get(symbol):
        calls.append(symbol)
        return {"eps": 1.0} if symbol.endswith(".TPE") else None
    av.get_quarterly_financials = fake_get

    sym, res = _resolve_tw_symbol(cache, av, "2330")
    assert sym == "2330.TPE" and res is not None
    assert cache["meta"]["tw_suffix"] == ".TPE"

    # 第二檔：直接沿用已探測後綴，零額外呼叫
    calls.clear()
    sym2, res2 = _resolve_tw_symbol(cache, av, "2427")
    assert sym2 == "2427.TPE" and res2 is None
    assert calls == []


def test_suffix_probe_failure_marks_unsupported():
    """三後綴全滅 → meta.tw_unsupported=True，之後 AV 自動退場不燒配額"""
    from src.finmind_client import _resolve_tw_symbol
    cache = {"meta": {}, "stocks": {}}
    av = AlphaVantageClient(api_key="dummy")
    calls = []
    av.get_quarterly_financials = lambda symbol: calls.append(symbol) or None

    sym, res = _resolve_tw_symbol(cache, av, "2330")
    assert sym is None and res is None
    assert len(calls) == 3   # .TPE / .TW / .TWO 各探測一次
    assert cache["meta"]["tw_unsupported"] is True

    calls.clear()
    sym2, res2 = _resolve_tw_symbol(cache, av, "2427")
    assert sym2 is None and res2 is None
    assert calls == []       # unsupported 之後零請求


# ─────────────────────────────────────────────
# 五層鏈整合（get_financial_statements）＋快取
# ─────────────────────────────────────────────

@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """把 CACHE_PATH 重導到 tmp_path，避免污染 repo 的 data/financial_cache.json"""
    path = tmp_path / "financial_cache.json"
    monkeypatch.setattr(fc, "CACHE_PATH", path)
    import src.finmind_client as fmc
    monkeypatch.setattr(fmc, "CACHE_PATH", path, raising=False)
    return path


def _seed_cache(path, code, age_days, source="yfinance"):
    fetched = (datetime.now(fc.TZ_TAIPEI) - timedelta(days=age_days)).isoformat()
    data = {"meta": {}, "stocks": {code: {
        "revenue": 5000.0, "gross_margin": 40.0, "net_margin": 15.0,
        "eps": 3.0, "fiscal_quarter": "2026-06-30",
        "fetched_at": fetched, "source": source}}}
    path.write_text(json.dumps(data), encoding="utf-8")


def test_fresh_cache_hit_zero_network(tmp_cache, monkeypatch):
    """第 0 層：40 天內快取命中 → 完全不碰 FinMind/yfinance/AV"""
    _seed_cache(tmp_cache, "2330", age_days=10)
    from src.finmind_client import FinMindClient
    client = FinMindClient(token="t")
    def boom(*a, **k):
        raise AssertionError("快取命中時不應有任何網路呼叫")
    monkeypatch.setattr(client, "_get_financial_from_finmind", boom)
    monkeypatch.setattr(client, "_get_financial_from_yfinance", boom)

    out = client.get_financial_statements("2330")
    assert out["source"] == "cache:yfinance"
    assert out["gross_margin"] == 40.0
    assert out["eps"] == 3.0


def test_stale_cache_used_when_all_sources_fail(tmp_cache, monkeypatch):
    """第 4 層：三源全失敗 → 過期快取撐住並標記 cache-stale（非 None）"""
    _seed_cache(tmp_cache, "2882", age_days=fc.CACHE_TTL_DAYS + 20)
    from src.finmind_client import FinMindClient
    client = FinMindClient(token="t")
    monkeypatch.setattr(client, "_get_financial_from_finmind", lambda code, days=730: None)
    monkeypatch.setattr(client, "_get_financial_from_yfinance", lambda code: None)
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)  # AV 未啟用

    out = client.get_financial_statements("2882")
    assert out is not None
    assert out["source"] == "cache-stale"
    assert out["eps"] == 3.0


def test_all_layers_fail_returns_none(tmp_cache, monkeypatch):
    """五層全滅且無任何快取 → None（維持 main.py 前置攔截契約）"""
    from src.finmind_client import FinMindClient
    client = FinMindClient(token="t")
    monkeypatch.setattr(client, "_get_financial_from_finmind", lambda code, days=730: None)
    monkeypatch.setattr(client, "_get_financial_from_yfinance", lambda code: None)
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)

    assert client.get_financial_statements("9999") is None


def test_success_persists_to_cache(tmp_cache, monkeypatch):
    """FinMind 成功取得 → 寫入快取（含 fetched_at），供 workflow commit 回 repo"""
    from src.finmind_client import FinMindClient
    client = FinMindClient(token="t")
    monkeypatch.setattr(client, "_get_financial_from_finmind", lambda code, days=730: {
        "revenue": 1000.0, "gross_margin": 50.0, "net_margin": 20.0,
        "eps": 5.0, "fiscal_quarter": "2026-06-30", "source": "FinMind"})

    out = client.get_financial_statements("2330")
    assert out["source"] == "FinMind"
    saved = json.loads(tmp_cache.read_text(encoding="utf-8"))
    assert saved["stocks"]["2330"]["gross_margin"] == 50.0
    assert "fetched_at" in saved["stocks"]["2330"]

    # 隔日再跑：應走快取命中路徑（source=cache:FinMind），不再打 FinMind
    monkeypatch.setattr(client, "_get_financial_from_finmind",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不應再呼叫")))
    out2 = client.get_financial_statements("2330")
    assert out2["source"] == "cache:FinMind"


def test_corrupted_cache_file_degrades_gracefully(tmp_cache, monkeypatch):
    """快取檔損毀 → 警告＋空快取繼續跑，不中斷主流程"""
    tmp_cache.write_text("{ this is not json", encoding="utf-8")
    from src.finmind_client import FinMindClient
    client = FinMindClient(token="t")
    monkeypatch.setattr(client, "_get_financial_from_finmind", lambda code, days=730: {
        "revenue": 1.0, "gross_margin": 1.0, "net_margin": 1.0,
        "eps": 1.0, "source": "FinMind"})
    out = client.get_financial_statements("2330")
    assert out["source"] == "FinMind"
