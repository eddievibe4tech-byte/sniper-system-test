"""🌳 推播決策樹單元測試：驗證四種市場情境的優先級與內容攔截邏輯"""
import pytest

from src.push_behavior_tree import (
    PushBlackboard, tick_push_tree, CRASH_PCT, VOLATILE_PCT,
)
from src.quant_engine.blackboard import MarketState, PortfolioState


def _buy_decision(symbol="2330", price=50.0, budget=100000):
    return {
        "symbol": symbol,
        "action": "BUY",
        "current_price": price,
        "budget": budget,
        "trading_plan": {"entry_type": "pullback", "entry_zone": [48.0, 50.0],
                         "stop_loss": 45.0, "take_profit_1": 56.0},
    }


# ── 情境 1：海嘯凍結 ────────────────────────────────────────────
def test_tsunami_bear_blocks_all_buys():
    bb = PushBlackboard(market_state=MarketState.BEAR,
                        decisions=[_buy_decision()])
    tick_push_tree(bb)
    assert bb.push_priority == 5
    assert bb.is_blocked is True
    assert "停止買入" in bb.push_title
    # 凍結情境絕不出現個股買入指南
    assert "2330" not in bb.push_message


def test_tsunami_crash_pct_triggers_freeze():
    bb = PushBlackboard(market_state=MarketState.BULL,   # 即使 FSM 還判牛
                        twii_change_pct=-3.0,            # 但當日暴跌 > 2.5%
                        decisions=[_buy_decision()])
    tick_push_tree(bb)
    assert bb.push_priority == 5
    assert bb.is_blocked is True


def test_circuit_breaker_portfolio_freezes():
    """🔴 Review 修正：熔斷停機屬系統性風險，補進分支 1（原建議稿遺漏）。"""
    bb = PushBlackboard(market_state=MarketState.SIDEWAYS,
                        portfolio_state=PortfolioState.CIRCUIT_BREAKER,
                        decisions=[_buy_decision()])
    tick_push_tree(bb)
    assert bb.push_priority == 5
    assert bb.is_blocked is True


# ── 情境 2：震盪減半 ────────────────────────────────────────────
def test_volatile_sideways_halves_budget():
    # 預算 100000 / 現價 50 → 正常 2000 股；減半 50000/50 → 1000 股
    bb = PushBlackboard(market_state=MarketState.SIDEWAYS,
                        twii_change_pct=-VOLATILE_PCT,
                        decisions=[_buy_decision(price=50.0, budget=100000)])
    tick_push_tree(bb)
    assert bb.push_priority == 4
    assert "注碼減半" in bb.push_title
    assert "1000 股" in bb.push_message      # 減半後的股數
    assert "2000 股" not in bb.push_message  # 全額股數不得出現
    assert bb.is_blocked is False


def test_sideways_calm_goes_normal_dispatch():
    """震盪市但波動未達 1.5% → 落回分支 3 常態派工。"""
    bb = PushBlackboard(market_state=MarketState.SIDEWAYS,
                        twii_change_pct=0.5,
                        decisions=[_buy_decision()])
    tick_push_tree(bb)
    assert bb.push_priority == 3


# ── 情境 3：常態派工 ────────────────────────────────────────────
def test_normal_dispatch_with_cathay_guide():
    bb = PushBlackboard(market_state=MarketState.BULL,
                        twii_change_pct=0.3,
                        decisions=[_buy_decision(price=50.0, budget=100000)])
    tick_push_tree(bb)
    assert bb.push_priority == 3
    assert "紀律執行" in bb.push_title
    assert "二擇一智慧單" in bb.push_message  # 純整張 → 智慧單指南
    assert "停利" in bb.push_message and "停損" in bb.push_message


def test_normal_dispatch_skips_non_enterable_plan():
    """trading_plan entry_type=none（避開推薦）→ 不產出掛單指南。"""
    d = _buy_decision()
    d["trading_plan"] = {"entry_type": "none", "entry_zone": None,
                         "stop_loss": None, "take_profit_1": None}
    bb = PushBlackboard(market_state=MarketState.BULL, decisions=[d])
    tick_push_tree(bb)
    assert "2330" not in bb.push_message


# ── 情境 4：風平浪靜 ────────────────────────────────────────────
def test_all_clear_when_only_hold():
    bb = PushBlackboard(market_state=MarketState.BULL,
                        decisions=[{"symbol": "2330", "action": "HOLD"}])
    tick_push_tree(bb)
    assert bb.push_priority == 2
    assert bb.push_title == "☕ 市場無事發生"
    assert bb.is_blocked is False


def test_empty_decisions_is_all_clear():
    bb = PushBlackboard(market_state=MarketState.BULL, decisions=[])
    tick_push_tree(bb)
    assert bb.push_priority == 2


# ── Selector 短路順序驗證 ───────────────────────────────────────
@pytest.mark.parametrize("state,pct,expected", [
    (MarketState.BEAR, -9.9, 5),          # 熊市+暴跌仍命中分支 1
    (MarketState.SIDEWAYS, -1.5, 4),      # 邊界值 -1.5% 命中分支 2
    (MarketState.BULL, -2.49, 4),         # 未達 -2.5 但單日急挫 -> 牛市高波動防禦（分支 2）
])
def test_selector_boundaries(state, pct, expected):
    bb = PushBlackboard(market_state=state, twii_change_pct=pct,
                        decisions=[_buy_decision()])
    tick_push_tree(bb)
    assert bb.push_priority == expected
