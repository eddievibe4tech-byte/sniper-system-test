"""🧪 Issue #4 驗收測試：行為樹（BT）正金字塔生命週期 + Break-Even + Trailing Stop。

對應 AC（Acceptance Criteria，Issue #199）：
  [x] 正金字塔建倉測試：BT 輸出部位比例嚴格為 Trial(50%) ➜ Add(30%) ➜ Add(20%)，
      絕不允許倒金字塔。
  [x] Break-Even 強制防護：觸發 30% 加碼條件 ➜ BT 必須「同步輸出」將原 50% 部位
      停損上移至 Break-Even 指令，確保最壞情況總虧損 ≤ 1%。
  [x] Trailing Stop 優先級測試：獲利展開後最高價回撤達 2.5×ATR ➜ BT 必須優先輸出
      TRAILING_STOP_EXIT，優先級高於「跌破 20MA」。
  [x] 整股/零股邊界測試：ROUND_LOT 1,342 股 ➜ 1,000 股；ODD_LOT 487 股且金額
      < 5,000 元 ➜ 觸發 MIN_ORDER_THRESHOLD 攔截。
"""
import pytest

from src.quant_engine.blackboard import Blackboard, StockData, MarketState, PortfolioState
from src.quant_engine.config import EngineConfig, ExecutionConfig, LifecycleConfig
from src.quant_engine.bt.tree import build_trading_behavior_tree
from src.quant_engine.bt.nodes import NodeStatus
from src.quant_engine.bt.leaves import (
    CheckAndMoveStopToBreakEven,
    CheckAddCondition,
    CheckLotBoundary,
    CheckTrailingStop,
)
from src.quant_engine.lifecycle import (
    PYRAMID_STAGES,
    apply_lot_mode,
    blended_cost_price,
    break_even_stop_price,
    next_pyramid_stage,
    worst_case_loss_pct,
)


def make_bb(stock, market_state=MarketState.BULL, portfolio_state=PortfolioState.NORMAL):
    return Blackboard(
        market_index=1000, market_ma60=950, market_ma200=900,
        peak_capital=1_000_000, current_capital=1_000_000,
        market_state=market_state, portfolio_state=portfolio_state,
        stock=stock,
    )


# ---------------------------------------------------------------------------
# AC#1：正金字塔建倉 —— 50% ➜ 30% ➜ 20%，嚴禁倒金字塔
# ---------------------------------------------------------------------------
class TestPyramidStages:
    def test_stage_sequence_is_forward_pyramid(self):
        """階段序列必須遞減（越加越少），杜絕倒金字塔。"""
        assert PYRAMID_STAGES == (0.50, 0.30, 0.20)
        assert all(a > b for a, b in zip(PYRAMID_STAGES, PYRAMID_STAGES[1:]))

    def test_next_stage_progression(self):
        assert next_pyramid_stage(0.0)[0] == 0.50   # 空手 ➜ Trial
        assert next_pyramid_stage(0.5)[0] == 0.30   # Trial 完成 ➜ Add#1
        assert next_pyramid_stage(0.8)[0] == 0.20   # Add#1 完成 ➜ Add#2
        stage, idx = next_pyramid_stage(1.0)        # 滿倉 ➜ 無下一階段
        assert stage is None and idx == -1

    def test_full_pyramid_via_behavior_tree(self):
        """模擬三階段建倉：BT 依序輸出部位 50% ➜ 80% ➜ 100%，絕不超額。"""
        lc = LifecycleConfig()
        tree = build_trading_behavior_tree(ExecutionConfig(), lc)
        s = StockData(symbol="2330", current_price=100, cost_price=100,
                      ma20=95, has_position=True, atr=2.0, shares_held=0,
                      current_position_ratio=0.0)
        bb = make_bb(s)
        ratios = []
        for price in (103, 106, 109):  # 每段都推進 ≥ 1×ATR，站穩 20MA
            s.current_price = price
            tree.tick(bb)
            assert "ADD_POSITION" in bb.orders, f"price={price} 應觸發加碼"
            ratios.append(s.current_position_ratio)
        assert ratios == [0.50, 0.80, 1.00]
        # 滿倉後不再有加碼指令
        bb.orders = []
        s.current_price = 112
        tree.tick(bb)
        assert "ADD_POSITION" not in bb.orders
        # 成本為加權平均（Trial@103 / Add@106 / Add@109），非最後一筆成交價
        assert 103 < s.cost_price < 109


# ---------------------------------------------------------------------------
# AC#2：Break-Even 強制防護 —— 加碼前同步輸出停損上移指令
# ---------------------------------------------------------------------------
class TestBreakEvenGuard:
    def test_add_position_requires_break_even_order(self):
        """觸發 30% 加碼的同一 tick，orders 必須同時含 MOVE_STOP_TO_BREAK_EVEN。"""
        lc = LifecycleConfig()
        tree = build_trading_behavior_tree(ExecutionConfig(), lc)
        s = StockData(symbol="2330", current_price=103, cost_price=100,
                      ma20=95, has_position=True, atr=2.0, shares_held=1000,
                      current_position_ratio=0.5)
        bb = make_bb(s)
        tree.tick(bb)
        assert "ADD_POSITION" in bb.orders
        assert "MOVE_STOP_TO_BREAK_EVEN" in bb.orders  # 🛡️ 同步輸出，非事後補救
        be = break_even_stop_price(100, lc.be_buffer_pct)
        assert s.trailing_stop_level >= be  # 原 50% 部位停損已上移至保本線

    def test_worst_case_loss_after_break_even_within_budget(self):
        """Break-Even 完成後，全部位以保本線出場：總虧損 ≤ 初始風險預算 1%。"""
        lc = LifecycleConfig()
        trial_shares, add_shares = 1000, 600
        cost = blended_cost_price(trial_shares, 100.0, add_shares, 103.0)
        be = break_even_stop_price(cost, lc.be_buffer_pct)
        # 以帳戶佔比權重計算最壞情況虧損
        weight = (trial_shares + add_shares) * cost / 1_000_000
        loss = worst_case_loss_pct(cost, be, weight)
        assert be >= cost  # 保本線不低于成本
        assert loss <= 0.01  # ≤ 1% 風險預算

    def test_break_even_idempotent(self):
        """已完成 Break-Even 後重複 tick：放行但不重複輸出指令。"""
        cfg = LifecycleConfig()
        node = CheckAndMoveStopToBreakEven(cfg)
        s = StockData(symbol="2330", current_price=103, cost_price=100,
                      ma20=95, has_position=True, atr=2.0)
        bb = make_bb(s)
        assert node.tick(bb) == NodeStatus.SUCCESS
        assert bb.orders.count("MOVE_STOP_TO_BREAK_EVEN") == 1
        assert node.tick(bb) == NodeStatus.SUCCESS  # 冪等放行
        assert bb.orders.count("MOVE_STOP_TO_BREAK_EVEN") == 1

    def test_no_add_without_break_even_first(self):
        """工程實作提示驗證：CheckAndMoveStopToBreakEven 是加碼 Sequence 首位 Condition。"""
        from src.quant_engine.bt.nodes import Sequence as SeqNode
        tree = build_trading_behavior_tree(ExecutionConfig(), LifecycleConfig())
        add_seq = next(c for c in tree.children
                       if isinstance(c, SeqNode) and
                       any(isinstance(g, CheckAddCondition) for g in c.children))
        assert isinstance(add_seq.children[1], CheckAndMoveStopToBreakEven)


# ---------------------------------------------------------------------------
# AC#3：Trailing Stop 優先級 —— 2.5×ATR 回撤 > 跌破 20MA
# ---------------------------------------------------------------------------
class TestTrailingStopPriority:
    def test_drawdown_2p5_atr_beats_ma20_exit(self):
        """獲利展開後回撤 2.5×ATR：即使未跌破 20MA，也必須優先 TRAILING_STOP_EXIT。"""
        lc = LifecycleConfig()
        tree = build_trading_behavior_tree(ExecutionConfig(), lc)
        # 進場後最高 120、ATR=2 ➜ 追蹤線 = 120 - 5 = 115；現價 114 < 115
        s = StockData(symbol="2330", current_price=114, cost_price=100,
                      peak_price=120, highest_price_since_entry=120,
                      ma20=110, has_position=True, atr=2.0)
        bb = make_bb(s)
        tree.tick(bb)
        assert bb.action == "TRAILING_STOP_EXIT"
        assert "TRAILING_STOP_EXIT" in bb.orders

    def test_trailing_stop_priority_over_hard_stop_and_tp(self):
        """同時滿足硬性止損情境時，Selector 首位仍是追蹤停損分支（AC 明文優先級）。"""
        tree = build_trading_behavior_tree(ExecutionConfig(), LifecycleConfig())
        first_branch = tree.children[0]
        from src.quant_engine.bt.leaves import ActionTrailingStopExit
        assert any(isinstance(n, ActionTrailingStopExit) for n in first_branch.children)

    def test_no_trail_trigger_while_underwater(self):
        """尚未獲利（現價 ≤ 成本）的回撤屬止損職責，不得誤觸追蹤停損。"""
        node = CheckTrailingStop(LifecycleConfig())
        s = StockData(symbol="2330", current_price=99, cost_price=100,
                      peak_price=101, highest_price_since_entry=101,
                      ma20=100, has_position=True, atr=2.0)
        bb = make_bb(s)
        assert node.tick(bb) == NodeStatus.FAILURE
        assert bb.orders == []

    def test_peak_anchor_updates_monotonically(self):
        """highest_price_since_entry 只增不減（Blackboard 擴充欄位語意）。"""
        s = StockData(symbol="2330", current_price=105, cost_price=100,
                      ma20=100, has_position=True, atr=2.0,
                      highest_price_since_entry=103)
        bb = make_bb(s)
        CheckTrailingStop(LifecycleConfig()).tick(bb)
        assert s.highest_price_since_entry == 105  # 新高推進
        s.current_price = 101
        CheckTrailingStop(LifecycleConfig()).tick(bb)
        assert s.highest_price_since_entry == 105  # 回落不改寫錨點


# ---------------------------------------------------------------------------
# AC#4：整股/零股邊界測試
# ---------------------------------------------------------------------------
class TestLotBoundary:
    def test_round_lot_floors_to_thousand(self):
        """ROUND_LOT：1,342 股 ➜ 向下取整 1,000 股（1 張），絕不進位。"""
        shares, reject = apply_lot_mode(1342, 50, mode="ROUND_LOT")
        assert shares == 1000 and reject == ""

    def test_odd_lot_below_threshold_rejected(self):
        """ODD_LOT：487 股 × $10 = 4,870 < 5,000 ➜ MIN_ORDER_THRESHOLD 攔截。"""
        shares, reject = apply_lot_mode(487, 10, mode="ODD_LOT",
                                        min_order_amount=5000)
        assert shares == 0 and reject == "MIN_ORDER_THRESHOLD"

    def test_odd_lot_above_threshold_allowed(self):
        """ODD_LOT：487 股 × $20 = 9,740 ≥ 5,000 ➜ 放行零股單。"""
        shares, reject = apply_lot_mode(487, 20, mode="ODD_LOT",
                                       min_order_amount=5000)
        assert shares == 487 and reject == ""

    def test_round_lot_less_than_one_lot_rejected(self):
        shares, reject = apply_lot_mode(999, 50, mode="ROUND_LOT")
        assert shares == 0 and reject == "REJECT_MIN_LOT"

    def test_bt_open_gate_rejects_small_odd_lot(self):
        """行為樹整合：突破 20MA 但單筆金額不足 ➜ REJECT_MIN_ORDER_THRESHOLD、不下 BUY。"""
        lc = LifecycleConfig(lot_mode="ODD_LOT", min_order_amount=5000)
        tree = build_trading_behavior_tree(ExecutionConfig(), lc)
        # 高 ATR 壓縮風控股數：capital 1M, risk 1%, atr 30 ➜ ~333 股 @ $10 = 3,330 < 5,000
        s = StockData(symbol="6xxx", current_price=10, cost_price=0,
                      ma20=9, has_position=False, atr=30.0)
        bb = make_bb(s)
        tree.tick(bb)
        assert bb.action != "BUY"
        assert "REJECT_MIN_ORDER_THRESHOLD" in bb.orders

    def test_check_lot_boundary_passes_valid_round_lot(self):
        lc = LifecycleConfig()
        node = CheckLotBoundary(lc)
        s = StockData(symbol="2330", current_price=100, cost_price=0,
                      ma20=95, has_position=False, atr=2.0)
        bb = make_bb(s)
        bb.position_size_pct = 1.0
        assert node.tick(bb) == NodeStatus.SUCCESS
        assert s.target_shares % lc.lot_size == 0 and s.target_shares > 0


# ---------------------------------------------------------------------------
# 引擎整合：orders 跨標的零污染 + DecisionRecord 管線不退化
# ---------------------------------------------------------------------------
def test_engine_resets_orders_between_symbols():
    from datetime import datetime
    from src.quant_engine.engine import DecisionEngine

    engine = DecisionEngine(EngineConfig())
    bb = Blackboard(market_index=1000, market_ma60=950, market_ma200=900,
                    peak_capital=1_000_000, current_capital=1_000_000,
                    market_state=MarketState.BULL)
    now = datetime(2026, 11, 1, 9, 30)
    stopper = StockData(symbol="9999", current_price=90, cost_price=100,
                        ma20=95, has_position=True, atr=2.0)
    rec1 = engine.evaluate_stock(bb, stopper, now)
    assert rec1.action == "STOP_LOSS"
    bystander = StockData(symbol="0000", current_price=100, cost_price=0,
                          ma20=100, has_position=False, atr=2.0)
    rec2 = engine.evaluate_stock(bb, bystander, now)
    assert rec2.action == "HOLD"
    assert bb.orders == []  # 前一檔 STOP_LOSS 指令不得殘留污染
