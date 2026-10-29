"""🌳 推播決策樹進階驗收測試（PR Review 建議稿）

涵蓋五大核心缺陷的回歸驗證：
1. 海嘯凍結 (P5) 解耦買入/出場——停損訊號不得被誤殺
2. 高波動應變 (P4) 注碼減半僅作用於 BUY，出場維持全額部位
3. 牛市高波動防禦漏洞（BULL 急殺 -2.4% 不再落入滿額常態派工）
4. 空指南自動降級 P2（避免訊息斷頭與 P4 警報疲勞）
5. 異常價格 (<=0) 短路過濾，不進入 generate_cathay_guide
"""
from unittest.mock import patch

import pytest

from src.push_behavior_tree import (
    PushBlackboard,
    tick_push_tree,
    CRASH_PCT,
    HALF_BUDGET_RATIO,
    DEFAULT_BUDGET,
)
from src.quant_engine.blackboard import MarketState, PortfolioState


# ── 測試假資料 Fixtures ──────────────────────────────────────────────

@pytest.fixture
def mock_guide_data():
    """提供標準化的國泰指南回傳結構。"""
    def _create_guide(symbol: str, method: str, shares: int = 1000):
        return {
            "symbol": symbol,
            "method": method,
            "shares_display": f"{shares}股",
            "take_profit": {"condition": "觸及 110 元限價賣出"},
            "stop_loss": {"condition": "跌破 95 元市價賣出"},
            "warning": None,
        }
    return _create_guide


# ── 核心情境 1：海嘯暴跌/熔斷時，凍結買入但強制推播緊急停損 ──────────

def test_tsunami_freeze_preserves_and_elevates_stop_loss(mock_guide_data):
    """海嘯極端防禦 (P5)：買入被凍結，但停損/停利部位必須緊急派工。"""
    decisions = [
        {
            "action": "BUY",
            "symbol": "2330",
            "current_price": 900,
            "budget": 100000,
            "trading_plan": {"entry": 900},
        },
        {
            "action": "STOP_LOSS",
            "symbol": "2454",
            "current_price": 1200,
            "budget": 120000,
            "trading_plan": {"stop": 1200},
        },
    ]

    bb = PushBlackboard(
        market_state=MarketState.BEAR,
        portfolio_state=PortfolioState.NORMAL,
        twii_change_pct=-3.2,  # 跌破 CRASH_PCT (-2.5%)
        decisions=decisions,
    )

    def fake_guide(symbol, price, plan, budget):
        return mock_guide_data(symbol, "二擇一智慧單")

    with patch("src.push_behavior_tree.generate_cathay_guide", side_effect=fake_guide):
        result = tick_push_tree(bb)

    # 1. 驗證整體狀態與優先級
    assert result.scenario == "tsunami_freeze"
    assert result.push_priority == 5
    assert result.is_blocked is True
    assert "🚨 總經海嘯預警" in result.push_title

    # 2. 驗證買入確實被凍結，但停損被保留
    assert "今日全面取消所有買入掛單" in result.push_message
    assert "緊急出場提醒" in result.push_message
    assert "2454" in result.push_message  # 停損標的必須出現
    assert "2330" not in result.push_message  # 買入標的嚴禁出現


def test_tsunami_freeze_pure_buy_has_no_exit_section():
    """海嘯極端防禦 (P5)：若只有 BUY 訊號，推播只發出防禦警告，不應出現空出場段落。"""
    decisions = [
        {
            "action": "BUY",
            "symbol": "2330",
            "current_price": 900,
            "budget": 50000,
        }
    ]

    bb = PushBlackboard(
        market_state=MarketState.BULL,
        portfolio_state=PortfolioState.CIRCUIT_BREAKER,  # 觸發帳戶熔斷
        twii_change_pct=-0.5,
        decisions=decisions,
    )

    result = tick_push_tree(bb)

    assert result.scenario == "tsunami_freeze"
    assert result.push_priority == 5
    assert result.is_blocked is True
    assert "緊急出場提醒" not in result.push_message
    assert "2330" not in result.push_message


# ── 核心情境 2：決策訊號因門檻過濾為空，自動降級為風平浪靜 ──────────

def test_normal_dispatch_with_empty_guides_falls_back_to_all_clear():
    """常態市況 (P3)：有 BUY 決策但因買不起 1 股回傳 None，自動降級為風平浪靜 (P2)。"""
    decisions = [
        {
            "action": "BUY",
            "symbol": "3008",
            "current_price": 2500,
            "budget": 1000,  # 1000 元買不起 1 股大立光
            "trading_plan": {"entry_type": "none"},
        }
    ]

    bb = PushBlackboard(
        market_state=MarketState.BULL,
        portfolio_state=PortfolioState.NORMAL,
        twii_change_pct=0.8,
        decisions=decisions,
    )

    # 模擬 generate_cathay_guide 因邊界條件過濾回傳 None
    with patch("src.push_behavior_tree.generate_cathay_guide", return_value=None):
        result = tick_push_tree(bb)

    # 驗證降級邏輯：不應停在 P3 發出空白指南，而應降級為 P2
    assert result.scenario == "all_clear"
    assert result.push_priority == 2
    assert result.push_title == "☕ 市場無事發生"
    assert "安心上班" in result.push_message
    assert "國泰智慧單/小鈴鐺指南" not in result.push_message


def test_volatile_market_with_empty_guides_falls_back_to_all_clear():
    """高波動市況 (P4)：震盪市無任何有效產出指南，避免高警報疲勞，降為 P2。"""
    bb = PushBlackboard(
        market_state=MarketState.SIDEWAYS,
        portfolio_state=PortfolioState.NORMAL,
        twii_change_pct=-1.8,  # 符合高波動閾值
        decisions=[],          # 當天無決策
    )

    result = tick_push_tree(bb)

    assert result.scenario == "all_clear"
    assert result.push_priority == 2
    assert result.push_title == "☕ 市場無事發生"


# ── 延伸驗證：預算折扣與防禦邊界 ──────────────────────────────────

def test_volatile_budget_discount_only_affects_buy_orders(mock_guide_data):
    """高波動應變 (P4)：只有 BUY 預算打折，STOP_LOSS/TAKE_PROFIT 維持全額部位。"""
    decisions = [
        {"action": "BUY", "symbol": "2330", "current_price": 500, "budget": 100000},
        {"action": "STOP_LOSS", "symbol": "2454", "current_price": 1000, "budget": 80000},
    ]

    bb = PushBlackboard(
        market_state=MarketState.SIDEWAYS,
        twii_change_pct=1.8,
        decisions=decisions,
    )

    captured_budgets = {}

    def spy_guide(symbol, price, plan, budget):
        captured_budgets[symbol] = budget
        return mock_guide_data(symbol, "智慧單")

    with patch("src.push_behavior_tree.generate_cathay_guide", side_effect=spy_guide):
        result = tick_push_tree(bb)

    assert result.scenario == "volatile_half_budget"
    assert result.push_priority == 4
    # BUY (2330) 預算打 5 折：100,000 * 0.5 = 50,000
    assert captured_budgets["2330"] == 100000 * HALF_BUDGET_RATIO
    # STOP_LOSS (2454) 預算維持原樣：80,000
    assert captured_budgets["2454"] == 80000


def test_invalid_price_handled_safely():
    """當決策帶入異常價格 (<= 0) 時，過濾跳過而不噴出例外。"""
    decisions = [
        {"action": "BUY", "symbol": "9999", "current_price": 0, "budget": 50000},
        {"action": "BUY", "symbol": "8888", "current_price": -10, "budget": 50000},
    ]

    bb = PushBlackboard(
        market_state=MarketState.BULL,
        twii_change_pct=0.5,
        decisions=decisions,
    )

    with patch("src.push_behavior_tree.generate_cathay_guide") as mock_guide:
        result = tick_push_tree(bb)
        # 價格 <= 0 在進入 generate_cathay_guide 前就應被短路過濾
        mock_guide.assert_not_called()

    assert result.scenario == "all_clear"
    assert result.push_priority == 2


# ── 補充驗證：牛市急殺防禦漏洞 + 常數匯入正確性 ────────────────────

def test_bull_market_flash_drop_gets_half_budget(mock_guide_data):
    """牛市黑天鵝急殺 -2.4%（未達 CRASH_PCT）→ 命中分支 2 注碼減半，而非滿額派工。"""
    decisions = [
        {"action": "BUY", "symbol": "2330", "current_price": 500, "budget": 100000},
    ]

    bb = PushBlackboard(
        market_state=MarketState.BULL,
        twii_change_pct=-2.4,
        decisions=decisions,
    )

    captured_budgets = {}

    def spy_guide(symbol, price, plan, budget):
        captured_budgets[symbol] = budget
        return mock_guide_data(symbol, "智慧單")

    with patch("src.push_behavior_tree.generate_cathay_guide", side_effect=spy_guide):
        result = tick_push_tree(bb)

    assert result.scenario == "volatile_half_budget"
    assert result.push_priority == 4
    assert captured_budgets["2330"] == 100000 * HALF_BUDGET_RATIO


def test_constants_exported():
    """確認 CRASH_PCT / HALF_BUDGET_RATIO / DEFAULT_BUDGET 可被外部匯入調參。"""
    assert CRASH_PCT == -2.5
    assert HALF_BUDGET_RATIO == 0.5
    assert DEFAULT_BUDGET == 50000
