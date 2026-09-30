"""USClient.get_vix 多來源 fallback 鏈 單元測試（#129）

驗證三件事：
1. 回歸：VIX 用 period="1mo"（約 21 根日K）不應再被 get_history 的 60 行門檻擋掉
   （舊版缺陷：len(df) < 60 硬編碼 → get_vix() 永遠回傳 None）。
2. fallback 鏈順序：yfinance 日K → yfinance fast_info → CBOE → FRED，
   前一來源失敗時自動切換下一來源，且回傳 {'value', 'source'} 透明化來源。
3. 四來源全失敗 → 回傳 None（搭配海選端「資料不足」不給買入訊號的誠實規則）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pandas as pd  # noqa: E402

from us_client import USClient  # noqa: E402


class _Resp:
    """模擬 requests.Response（依各來源端點回不同內容）"""

    def __init__(self, url):
        self.url = url

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": {"price": 21.5}}

    @property
    def text(self):
        return "DATE,VIXCLS\n2026-09-29,18.73\n2026-09-30,.\n"


class _NoFastInfo:
    def __init__(self, *a, **k):
        raise RuntimeError("fast_info unavailable")


# ── 1. min_rows 修正（核心 bug）──────────────────────────────

def test_get_vix_uses_min_rows_not_60():
    """回歸：1 個月約 21 根日K 不應被 60 行門檻擋掉"""
    c = USClient()
    fake = pd.DataFrame({"Close": [18.0] * 21})
    calls = {}

    def fake_get_history(symbol, period="1y", interval="1d", min_rows=60):
        calls["args"] = (symbol, period, min_rows)
        return fake

    c.get_history = fake_get_history
    out = c.get_vix()
    assert out == {"value": 18.0, "source": "yfinance"}
    # 🔴 關鍵：VIX 路徑必須放寬門檻（<21），否則 1mo 數據永遠過不了 60 行檢查
    sym, period, min_rows = calls["args"]
    assert sym == "^VIX" and period == "1mo" and min_rows <= 21


def test_get_history_default_threshold_still_60():
    """回歸：其他標的（個股/指數）預設仍維持 60 行品質門檻"""
    c = USClient()
    short = pd.DataFrame({"Close": [1.0] * 30})  # 30 行 < 60

    class T:
        def history(self, **kw):
            return short

    import yfinance
    orig_ticker = yfinance.Ticker
    yfinance.Ticker = lambda s: T()
    try:
        assert c.get_history("SPY") is None               # 預設 60 門檻照擋
        assert c.get_history("SPY", min_rows=5) is not None  # 明確放寬才通過
    finally:
        yfinance.Ticker = orig_ticker


# ── 2. fallback 鏈 ───────────────────────────────────────────

def test_get_vix_falls_back_to_cboe(monkeypatch):
    c = USClient()
    c.get_history = lambda *a, **k: None
    monkeypatch.setattr("yfinance.Ticker", lambda s: _NoFastInfo())
    monkeypatch.setattr(c.session, "get", lambda url, timeout=None, **kw: _Resp(url))
    out = c.get_vix()
    assert out["value"] == 21.5 and out["source"] == "cboe"


def test_get_vix_falls_back_to_fred(monkeypatch):
    """CBOE 也失靈（回傳非 JSON 內容拋錯）→ 自動切 FRED CSV 最後一筆有效值"""
    c = USClient()
    c.get_history = lambda *a, **k: None
    monkeypatch.setattr("yfinance.Ticker", lambda s: _NoFastInfo())

    class _BadCboe(_Resp):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(c.session, "get", lambda url, timeout=None, **kw: _BadCboe(url))
    out = c.get_vix()
    assert out["value"] == 18.73 and out["source"] == "fred"


def test_get_vix_returns_none_when_all_sources_fail(monkeypatch):
    c = USClient()
    c.get_history = lambda *a, **k: None
    monkeypatch.setattr("yfinance.Ticker", lambda s: _NoFastInfo())

    def _fail(url, timeout=None, **kw):
        raise OSError("network down")

    monkeypatch.setattr(c.session, "get", _fail)
    assert c.get_vix() is None
