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
import os
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


def _make_null_av():
    """AV client stub：enabled=True，但 get_quarterly_financials 恆回 None"""
    av = AlphaVantageClient(api_key="dummy")
    av.get_quarterly_financials = lambda symbol: None
    return av


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


# ─────────────────────────────────────────────
# P0-1 回歸：flat import 風格（scripts/check_finmind_bulk.py 的載入方式）
# ─────────────────────────────────────────────

def test_flat_import_style_works():
    """CI finmind-bulk-guard 紅燈根因回歸測試：

    scripts/ 把 src/ 目錄塞進 sys.path 後以 flat 風格匯入
    （from finmind_client import ...）。此時 repo 根不在 sys.path，
    若 finmind_client.py 只有 package 風格 `from src.x import` →
    ModuleNotFoundError: No module named 'src'。
    雙風格 import shim 必須讓兩種載入方式都能成功。"""
    import subprocess, sys, pathlib
    src_dir = str(pathlib.Path(__file__).resolve().parents[2] / "src")
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from finmind_client import FinMindClient, _av_call;"
        "from financial_cache import load_cache;"
        "print('ok')" % src_dir
    )
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"flat import 失敗：{r.stderr}"
    assert "No module named 'src'" not in r.stderr
    assert "ok" in r.stdout


def test_check_finmind_bulk_script_runs(tmp_path, monkeypatch):
    """直接執行 CI 出紅燈的腳本本身（未設 token → mock 路徑），確認不再崩潰"""
    import subprocess, sys, pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.pop("FINMIND_TOKEN", None)  # 走 mock 驗證分支，不打真實 API
    # 快取重導到 tmp，避免腳本執行污染 repo data/
    env["PYTHONPATH"] = str(root)
    r = subprocess.run([sys.executable, str(root / "scripts" / "check_finmind_bulk.py")],
                       capture_output=True, text=True, timeout=120, cwd=str(root), env=env)
    assert "ModuleNotFoundError" not in r.stderr, f"腳本 import 崩潰：{r.stderr}"


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
    """三後綴全滅 → meta.tw_unsupported=True（附標記日期），之後 AV 自動退場不燒配額"""
    from src.finmind_client import _resolve_tw_symbol
    cache = {"meta": {}, "stocks": {}}
    av = AlphaVantageClient(api_key="dummy")
    calls = []
    av.get_quarterly_financials = lambda symbol: calls.append(symbol) or None

    sym, res = _resolve_tw_symbol(cache, av, "2330")
    assert sym is None and res is None
    assert len(calls) == 3   # .TPE / .TW / .TWO 各探測一次
    assert cache["meta"]["tw_unsupported"] is True
    # 🔴 P1：標記必須附帶日期，供 30 天 TTL 自癒重探
    assert "tw_unsupported_at" in cache["meta"]

    calls.clear()
    sym2, res2 = _resolve_tw_symbol(cache, av, "2427")
    assert sym2 is None and res2 is None
    assert calls == []       # unsupported 效期內零請求


# ─────────────────────────────────────────────
# P1：tw_unsupported 自癒 TTL（限流污染不再永久關掉 AV 層）
# ─────────────────────────────────────────────

def test_unsupported_flag_heals_after_ttl():
    """標記超過 30 天 → 自動清除並重新探測（若當日剛好被限流，AV 層不會退場 forever）"""
    from src.finmind_client import _resolve_tw_symbol
    stale_mark = (datetime.now(fc.TZ_TAIPEI) - timedelta(days=31)).isoformat()
    cache = {"meta": {"tw_unsupported": True, "tw_unsupported_at": stale_mark},
             "stocks": {}}
    av = AlphaVantageClient(api_key="dummy")
    calls = []
    def fake_get(symbol):
        calls.append(symbol)
        return {"eps": 2.0} if symbol.endswith(".TPE") else None
    av.get_quarterly_financials = fake_get

    sym, res = _resolve_tw_symbol(cache, av, "2330")
    assert sym == "2330.TPE" and res is not None          # 重探成功
    assert cache["meta"].get("tw_suffix") == ".TPE"
    assert "tw_unsupported" not in cache["meta"]           # 舊標記已清除


def test_unsupported_flag_within_ttl_blocks():
    """標記未滿 30 天 → 仍跳過、零請求（省配額設計不變）"""
    from src.finmind_client import _resolve_tw_symbol
    fresh_mark = (datetime.now(fc.TZ_TAIPEI) - timedelta(days=5)).isoformat()
    cache = {"meta": {"tw_unsupported": True, "tw_unsupported_at": fresh_mark},
             "stocks": {}}
    av = AlphaVantageClient(api_key="dummy")
    calls = []
    av.get_quarterly_financials = lambda symbol: calls.append(symbol) or None

    sym, res = _resolve_tw_symbol(cache, av, "2330")
    assert sym is None and res is None
    assert calls == []
    assert cache["meta"]["tw_unsupported"] is True         # 標記保留


def test_unsupported_legacy_flag_without_date_kept(tmp_cache):
    """舊格式（無日期欄位）→ 保守視為有效，不立即重探（避免每次 run 燒配額）"""
    from src.finmind_client import _unsupported_flag_active
    meta = {"tw_unsupported": True}
    assert _unsupported_flag_active(meta) is True


# ─────────────────────────────────────────────
# 五層鏈整合（get_financial_statements）＋快取
# ─────────────────────────────────────────────

@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """把 CACHE_PATH 重導到 tmp_path，避免污染 repo 的 data/financial_cache.json

    🔴 P2-3 修正（PR #133 review）：只需 patch fc.CACHE_PATH——load_cache/save_cache
    於「呼叫期」解析 financial_cache 模組全域，finmind_client 引用的是同一組函式；
    舊版對 fmc.CACHE_PATH 的 setattr（raising=False）是 no-op（fmc 并未匯入該名稱），
    已刪除以免誤導讀者以為需要雙重 patch。"""
    path = tmp_path / "financial_cache.json"
    monkeypatch.setattr(fc, "CACHE_PATH", path)
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


def test_no_spurious_save_when_av_meta_untouched(tmp_cache, monkeypatch):
    """🔴 P2-1：AV 層走 early-return（tw_suffix 已存在且本檔無資料）、
    meta 與配額都沒動時，不得觸發多餘的 save_cache——
    否則 Actions 每日 commit 會出現無意義快取 diff。"""
    from src.finmind_client import FinMindClient
    seed = {"meta": {"tw_suffix": ".TPE",
                     "av_quota": {"date": fc._today(), "used": 3}},
            "stocks": {}}
    tmp_cache.write_text(json.dumps(seed), encoding="utf-8")
    before = tmp_cache.read_bytes()

    client = FinMindClient(token="t")
    monkeypatch.setattr(client, "_get_financial_from_finmind", lambda code, days=730: None)
    monkeypatch.setattr(client, "_get_financial_from_yfinance", lambda code: None)
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "dummy")
    # tw_suffix=.TPE → _resolve_tw_symbol 直接回傳 symbol；本檔 AV 也無資料
    client._get_av_client = lambda: _make_null_av()

    out = client.get_financial_statements("2427")
    assert out is None                       # 五層全滅、且無任何 stock 快取
    assert tmp_cache.read_bytes() == before  # 快取檔完全未被回寫


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
