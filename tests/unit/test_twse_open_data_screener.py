"""TWSE/TPEx 開放資料海選通道單元測試（#93）

依 issue #93「風險與前置作業」要求：先寫解析測試（固定 fixture），
涵蓋欄位缺失／格式變動情境（驗收標準 3），並驗證降級路徑（驗收標準 2）。

全部使用固定 fixture／Fake client，不打真實端點（CI 可離線執行）。
"""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import screener  # noqa: E402
from twse_open_data import TwseOpenDataClient, _to_float  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures：模擬 TWSE T86 / B2i / TPEx 回應結構
# ---------------------------------------------------------------------------
T86_OK = [
    {"StockId": "2330", "InvestTrustBuy": 1000, "InvestTrustSell": 400,
     "InvestTrustOverBuy": 600},
    {"StockId": "2317", "InvestTrustBuy": 500, "InvestTrustSell": 800,
     "InvestTrustOverBuy": -300},
    {"StockId": "1504", "InvestTrustBuy": 200, "InvestTrustSell": 0,
     "InvestTrustOverBuy": None},  # 欄位缺失 → fallback buy-sell
    {"StockId": "12345", "InvestTrustOverBuy": 999},   # 非 4 碼 → 忽略
    {"InvestTrustOverBuy": 111},                        # StockId 缺失 → 忽略
]

B2I_OK = {
    "fields": [
        {"id": "0", "name": "StockCode"},
        {"id": "1", "name": "公司代號"},
        {"id": "2", "name": "本月營收"},
        {"id": "3", "name": "本月營收年增率%"},
    ],
    "data": [
        ["2330", "2330", "500,000", "12.3"],
        ["2317", "2317", "100,000", "-5"],
        ["2382", "2382", "-", "0"],      # 營收缺失 → 跳過該檔
    ],
}

TPEX_OK = {
    "fields": [
        {"id": "0", "name": "SecuCode"},
        {"id": "1", "name": "RevMonthAmnt"},
        {"id": "2", "name": "YoYChgRate"},
    ],
    "data": [
        ["6669", "30000", "20.5"],
        ["4919", "10000", "-3"],
    ],
}

# 格式變動情境：欄位名稱完全改變、無可辨識的營收欄
B2I_FORMAT_CHANGED = {
    "fields": [{"id": "0", "name": "StockCode"}, {"id": "1", "name": "SomeNewField"}],
    "data": [["2330", "xxx"]],
}


class FakeTwse(TwseOpenDataClient):
    """覆寫網路層（fetch_institutional_day）；inst_buy_streak_map 沿用真實邏輯"""

    def __init__(self, day_maps=None, fail_days=()):
        super().__init__()
        self.day_maps = day_maps or {}
        self.fail_days = set(fail_days)
        self.requested = []

    def fetch_institutional_day(self, date):
        self.requested.append(date)
        if date in self.fail_days:
            return None
        return self.day_maps.get(date, {})

    def revenue_yoy_map(self, cur, prev, include_tpex=True):
        return {}


# ---------------------------------------------------------------------------
# 1. _to_float 防呆
# ---------------------------------------------------------------------------
def test_to_float_dash_and_comma():
    assert _to_float("-") is None
    assert _to_float("") is None
    assert _to_float(None) is None
    assert _to_float("1,234.5") == 1234.5
    assert _to_float("abc") is None


# ---------------------------------------------------------------------------
# 2. T86 解析（欄位缺失情境）
# ---------------------------------------------------------------------------
def test_parse_t86_normal_missing_and_invalid():
    out = TwseOpenDataClient.parse_t86(T86_OK)
    assert out["2330"] == 600                      # 正常
    assert out["2317"] == -300                     # 賣超
    assert out["1504"] == 200                      # OverBuy 缺失 → buy-sell fallback
    assert "12345" not in out and "" not in out    # 非 4 碼／缺 StockId → 忽略


def test_parse_t86_empty_and_none():
    assert TwseOpenDataClient.parse_t86([]) == {}
    assert TwseOpenDataClient.parse_t86(None) == {}


# ---------------------------------------------------------------------------
# 3. 連買 streak（任一日失敗 → None；休市空資料 → 保守中斷）
# ---------------------------------------------------------------------------
def test_inst_streak_counts_consecutive_buy():
    d1 = {"2330": 100, "2317": -50}
    d2 = {"2330": 200, "2317": 30}
    d3 = {"2330": 50, "2317": 10}
    fake = FakeTwse(day_maps={"20261001": d1, "20260930": d2, "20260929": d3})
    streak = fake.inst_buy_streak_map(["20261001", "20260930", "20260929"],
                                      codes=["2330", "2317"])
    assert streak["2330"] == 3
    assert streak["2317"] == 0   # 最新日賣超 → 連買中斷（誠實 0，與 FinMind #92 語意一致）


def test_inst_streak_returns_none_when_any_day_fails():
    fake = FakeTwse(day_maps={"20261001": {"2330": 1}}, fail_days=["20260930"])
    assert fake.inst_buy_streak_map(["20261001", "20260930"]) is None


def test_inst_streak_resilient_mode_skips_old_failed_day():
    """韌性模式：舊日失敗跳過；最新日失敗仍回 None（無最新交易日即無意義）"""
    fake = FakeTwse(day_maps={"20261001": {"2330": 1}, "20260929": {"2330": 1}},
                    fail_days=["20260930"])
    streak = fake.inst_buy_streak_map(["20261001", "20260930", "20260929"],
                                      require_all_days=False)
    assert streak["2330"] == 2          # 跳過失敗日，連續計數
    fake2 = FakeTwse(day_maps={}, fail_days=["20261001"])
    assert fake2.inst_buy_streak_map(["20261001", "20260930"],
                                     require_all_days=False) is None


def test_inst_streak_holiday_empty_day_breaks_streak():
    # 休市日回空 dict（非 None）→ net=0 → 保守中斷連買，不虛報
    fake = FakeTwse(day_maps={"20261001": {"2330": 100}, "20260930": {}})
    streak = fake.inst_buy_streak_map(["20261001", "20260930"])
    assert streak["2330"] == 1


# ---------------------------------------------------------------------------
# 4. 月營收表解析（TWSE B2i / TPEx；欄位缺失／格式變動）
# ---------------------------------------------------------------------------
def test_parse_twse_revenue_ok_and_missing_value():
    out = TwseOpenDataClient.parse_twse_revenue(B2I_OK)
    assert out["2330"] == 500000.0     # 千分位逗號處理
    assert out["2317"] == 100000.0
    assert "2382" not in out           # '-' 營收 → 排除


def test_parse_tpex_revenue_ok():
    out = TwseOpenDataClient.parse_tpex_revenue(TPEX_OK)
    assert out == {"6669": 30000.0, "4919": 10000.0}


def test_parse_revenue_format_changed_returns_empty():
    """格式變動（無法辨識營收欄位）→ 回傳空 map，不拋錯（上層降級）"""
    assert TwseOpenDataClient.parse_twse_revenue(B2I_FORMAT_CHANGED) == {}
    assert TwseOpenDataClient.parse_twse_revenue({"fields": [], "data": []}) == {}
    assert TwseOpenDataClient.parse_twse_revenue(None) == {}


# ---------------------------------------------------------------------------
# 5. screener 高階函數：月份口徑與 raise 語意
# ---------------------------------------------------------------------------
def test_fetch_revenue_yoy_opendata_raises_when_empty():
    with pytest.raises(RuntimeError):
        screener.fetch_revenue_yoy_map_opendata(FakeTwse(), today=datetime(2026, 10, 4))


def test_twse_yyyymm_rollover():
    assert screener._twse_yyyymm(datetime(2026, 1, 15), 1) == "202512"
    assert screener._twse_yyyymm(datetime(2026, 10, 4), 13) == "202509"


def test_recent_trading_dates_weekdays_only_newest_first():
    dates = screener.recent_trading_dates(5, until=datetime(2026, 10, 5))  # 週一
    assert dates == ["20261002", "20261001", "20260930",
                     "20260929", "20260928"]  # 跳過週末，由新到舊


# ---------------------------------------------------------------------------
# 6. 開關與降級路徑（驗收標準 2）
# ---------------------------------------------------------------------------
def test_flag_off_keeps_legacy_universe(monkeypatch):
    """SCREENER_TWSE_OPEN_DATA 預設关闭 → get_universe 仍為小宇宙（#92 行為）"""
    monkeypatch.setattr(screener, "ENABLE_TWSE_FULL_MARKET", False)
    class FM:
        def get_stock_list(self):
            return [{"stock_id": f"{i:04d}", "stock_name": "x",
                     "industry_category": "y"} for i in range(1, 800)]
    uni = screener.get_universe(FM())
    assert len(uni) <= 40  # 監控池 ∪ 精選活躍股（~34 檔）


def test_flag_on_switches_to_full_market_universe(monkeypatch):
    monkeypatch.setattr(screener, "ENABLE_TWSE_FULL_MARKET", True)
    class FM:
        def get_stock_list(self):
            return [{"stock_id": f"{i:04d}", "stock_name": "x",
                     "industry_category": "y"} for i in range(1, 800)]
    uni = screener.get_universe(FM())
    assert len(uni) >= 500  # 全市場宇宙


def test_screen_falls_back_to_finmind_when_opendata_fails(monkeypatch):
    """TWSE 營收通道拋錯 → 降級 FinMind 雙路（批量→個股保底），不整場失敗"""
    monkeypatch.setattr(screener, "ENABLE_TWSE_FULL_MARKET", True)
    monkeypatch.setattr(screener, "TwseOpenDataClient", lambda *a, **k: FakeTwse())

    def boom(tw):
        raise RuntimeError("TWSE down")
    monkeypatch.setattr(screener, "fetch_revenue_yoy_map_opendata", boom)
    monkeypatch.setattr(screener, "fetch_inst_streak_map_opendata",
                        lambda tw, lookback=10: None)  # T86 失敗 → 降級
    calls = {"finmind_yoy": 0}

    def fm_yoy(finmind, codes):
        calls["finmind_yoy"] += 1
        return {"2330": 20.0}
    monkeypatch.setattr(screener, "fetch_revenue_yoy_map", fm_yoy)
    monkeypatch.setattr(screener, "fetch_inst_streak_map",
                        lambda fm, codes: {"2330": 0})

    info = {"2330": {"stock_id": "2330", "stock_name": "台積電", "industry": "半導體"}}
    rules = dict(screener.STRICT_RULES)
    out = screener._screen_with_rules(None, rules, info)
    assert calls["finmind_yoy"] == 1     # 確有降級
    assert out == []                     # 連買 0 天 → 關卡 2 淘汰，誠實空結果
