"""美股海選引擎 VIX 缺失修正 單元測試（#115）

驗證三件事：
1. get_us_scenario(vix=None, ...) → 「資料不足」，不做任何象限判定
   （舊版缺陷：VIX 缺失時預設 20.0，仍會誤判「動量突破」→ 產生不該有的買入推薦）。
2. run_us_screener()：VIX=None 當日不收集任何候選（不產生買入推薦）；
   VIX 正常時四象限收集行為不變（回歸）。
3. SCENARIO_META 含「資料不足」條目，且不在買入收集清單中。
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import us_screener as us  # noqa: E402


# ── get_us_scenario：VIX 缺失 ────────────────────────────────

def test_vix_none_returns_insufficient_data():
    # 即使其他條件完全符合「動量突破」（rsi=60、站上 MA50、接近新高），
    # VIX=None 也必須回傳「資料不足」而非買入情境
    assert us.get_us_scenario(None, 60.0, True, 1.0) == "資料不足"


def test_vix_none_not_extreme_danger():
    # VIX=None + RSI 極端超買 → 也不能給「極度危險」（一致性地不判定）
    assert us.get_us_scenario(None, 90.0, True, 0.0) == "資料不足"


# ── get_us_scenario：VIX 正常（回歸）─────────────────────────

def test_normal_quadrants_unchanged():
    assert us.get_us_scenario(12.0, 85.0, True, 0.5) == "極度危險"    # 自滿+超買
    assert us.get_us_scenario(35.0, 25.0, False, 20.0) == "黃金買點"   # 恐慌+超賣
    assert us.get_us_scenario(18.0, 60.0, True, 1.5) == "動量突破"     # 樂觀+強動能
    assert us.get_us_scenario(25.0, 50.0, True, 10.0) == "中性"        # 其餘中性


def test_insufficient_data_in_meta_and_not_buy_candidate():
    assert "資料不足" in us.SCENARIO_META
    rec = us.SCENARIO_META["資料不足"]["recommendation"]
    assert "買入" not in rec  # 資料不足絕不可帶「買入」字樣


# ── run_us_screener：端到端候选收集邏輯 ─────────────────────

_MOMENTUM_TICKER = {
    "price": 200.0, "ma20": 190.0, "ma50": 180.0, "rsi": 60.0,
    "high_52w": 202.0, "dist_to_52w_high_pct": 1.0,
    "price_above_ma50": True, "change_5d": 2.0, "change_20d": 5.0,
    "volatility_ann_pct": 25.0, "days_to_earnings": 30,
}


def _run_with_vix(vix):
    fake_client = mock.MagicMock()
    # 新格式（#129）：get_vix() 回傳 {'value', 'source'}；None 代表四來源全失敗
    fake_client.get_vix.return_value = ({"value": vix, "source": "yfinance"} if vix is not None else None)
    fake_client.get_index_rsi.return_value = 55.0
    fake_client.scan.return_value = {"AAPL": dict(_MOMENTUM_TICKER)}
    with mock.patch.object(us, "USClient", return_value=fake_client), \
         mock.patch.object(us, "save_us_results"):
        return us.run_us_screener()


def test_no_candidates_when_vix_missing():
    # 🔴 核心修正：VIX 缺失 → 零候選（不再因預設 20.0 誤出 AAPL「謹慎買入」）
    assert _run_with_vix(None) == []


def test_candidates_kept_when_vix_present():
    # 回歸：VIX=18 正常 → 動量突破照常收集
    cands = _run_with_vix(18.0)
    assert len(cands) == 1
    assert cands[0]["symbol"] == "AAPL"
    assert cands[0]["scenario"] == "動量突破"
    assert cands[0]["recommendation"] == "謹慎買入"
