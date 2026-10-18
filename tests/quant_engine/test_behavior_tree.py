"""🛡️ 鋼板五驗證：行為樹部分平倉追蹤 + 優先級 + 防護條件"""
from src.quant_engine.bt.tree import build_trading_behavior_tree
from src.quant_engine.bt.leaves import CheckTakeProfit
from src.quant_engine.bt.nodes import NodeStatus
from src.quant_engine.config import ExecutionConfig
from src.quant_engine.blackboard import Blackboard, StockData, MarketState, PortfolioState


def make_bb(stock, market_state=MarketState.SIDEWAYS, portfolio_state=PortfolioState.NORMAL):
    return Blackboard(
        market_index=1000, market_ma60=1000, market_ma200=950,
        peak_capital=1_000_000, current_capital=1_000_000,
        market_state=market_state, portfolio_state=portfolio_state,
        stock=stock,
    )


def test_tp_taken_prevents_double_close():
    """🛡️ 移動停利平倉後，不應再次觸發"""
    cfg = ExecutionConfig()
    bb = Blackboard(
        market_index=1000, market_ma60=1000, market_ma200=950,
        peak_capital=1_000_000, current_capital=1_000_000,
        stock=StockData(
            symbol="2330", current_price=105, cost_price=100,
            peak_price=120, ma20=100, has_position=True,
            tp_taken=False  # 尚未平倉
        )
    )
    checker = CheckTakeProfit(cfg)
    assert checker.tick(bb) == NodeStatus.SUCCESS  # 第一次觸發

    bb.stock.tp_taken = True  # 模擬平倉後
    assert checker.tick(bb) == NodeStatus.FAILURE  # 不再重複觸發


def test_tree_priority_stop_loss_over_take_profit():
    """止損優先於停利：同時滿足時只執行的應是 STOP_LOSS"""
    tree = build_trading_behavior_tree(ExecutionConfig())
    # 成本 100、高點 120（+20%）、現價 90（-10%）：止損與回檔停利條件都成立
    bb = make_bb(StockData(symbol="2330", current_price=90, cost_price=100,
                           peak_price=120, ma20=95, has_position=True))
    tree.tick(bb)
    assert bb.action == "STOP_LOSS"


def test_tree_take_profit_marks_tp_taken():
    """整樹執行 TAKE_PROFIT 後 tp_taken 必須被標記"""
    tree = build_trading_behavior_tree(ExecutionConfig())
    bb = make_bb(StockData(symbol="2330", current_price=105, cost_price=100,
                           peak_price=120, ma20=100, has_position=True))
    tree.tick(bb)
    assert bb.action == "TAKE_PROFIT"
    assert bb.stock.tp_taken is True

    # 第二次 tick：不再重複平倉 → HOLD
    bb.action, bb.decision = "HOLD", ""
    tree.tick(bb)
    assert bb.action == "HOLD"


def test_no_buy_outside_bull():
    """非牛市禁止開倉：突破 MA20 也只給 HOLD"""
    tree = build_trading_behavior_tree(ExecutionConfig())
    bb = make_bb(StockData(symbol="2330", current_price=110, cost_price=0,
                           peak_price=0, ma20=100, has_position=False),
                 market_state=MarketState.SIDEWAYS)
    tree.tick(bb)
    assert bb.action == "HOLD"
    assert any("非牛市" in r for r in bb.reasons)


def test_no_buy_during_circuit_breaker():
    """熔斷期間即使牛市也禁止開倉"""
    tree = build_trading_behavior_tree(ExecutionConfig())
    bb = make_bb(StockData(symbol="2330", current_price=110, cost_price=0,
                           peak_price=0, ma20=100, has_position=False),
                 market_state=MarketState.BULL,
                 portfolio_state=PortfolioState.CIRCUIT_BREAKER)
    tree.tick(bb)
    assert bb.action == "HOLD"
    assert any("熔斷" in r for r in bb.reasons)


def test_buy_in_bull_with_half_size_when_defensive():
    """牛市＋防禦狀態：可以開倉但部位由引擎端換算為 50%"""
    tree = build_trading_behavior_tree(ExecutionConfig())
    bb = make_bb(StockData(symbol="2330", current_price=110, cost_price=0,
                           peak_price=0, ma20=100, has_position=False),
                 market_state=MarketState.BULL,
                 portfolio_state=PortfolioState.DEFENSIVE)
    tree.tick(bb)
    assert bb.action == "BUY"
    assert "50%" in bb.decision
