"""預設交易行為樹：按優先級 Trailing Stop > 止損 > 停利 > 加碼 > 開倉 > 觀望。

🛡️ Issue #4（量化引擎 v2.0）AC#3：「獲利展開後最高價回撤達 2.5×ATR ➜ BT 必須
優先輸出 TRAILING_STOP_EXIT，優先級高於跌破 20MA」——追蹤停損分支置於 Selector
首位；AC#2 + 工程實作提示：加碼 Sequence 的第一個 Condition 必須是
CheckAndMoveStopToBreakEven（Break-Even 先於任何新增曝險）。
"""
from .nodes import Selector, Sequence
from .leaves import (CheckStopLoss, ActionStopLoss, CheckTakeProfit, ActionTakeProfit,
                     ConditionMarketAllowed, ConditionPortfolioAllowed,
                     ConditionBreakout, ActionBuy, ActionHold,
                     CheckTrailingStop, ActionTrailingStopExit,
                     CheckAndMoveStopToBreakEven, CheckAddCondition, ActionAddPosition,
                     CheckLotBoundary)
from ..config import ExecutionConfig, LifecycleConfig


def build_trading_behavior_tree(cfg: ExecutionConfig,
                                lifecycle_cfg: LifecycleConfig = None) -> Selector:
    """按優先級：追蹤停損 > 止損 > 停利 > 加碼（Break-Even ➜ Add）> 開倉 > 觀望"""
    lc = lifecycle_cfg if lifecycle_cfg is not None else LifecycleConfig()
    return Selector([
        # 🛡️ AC#3：Trailing Stop 優先級最高（高于硬性止損與跌破 20MA 的一切分支）
        Sequence([CheckTrailingStop(lc), ActionTrailingStopExit()]),
        Sequence([CheckStopLoss(cfg), ActionStopLoss()]),
        Sequence([CheckTakeProfit(cfg), ActionTakeProfit(cfg)]),
        # 🛡️ AC#1/#2：正金字塔加碼 —— Break-Even 必須是序列第一個 Condition
        Sequence([ConditionPortfolioAllowed(),
                  CheckAndMoveStopToBreakEven(lc),
                  CheckAddCondition(lc),
                  ActionAddPosition(lc)]),
        Sequence([ConditionMarketAllowed(), ConditionPortfolioAllowed(),
                  ConditionBreakout(), ActionBuy(cfg),
                  CheckLotBoundary(lc)]),  # 🛡️ AC#4：下單前整股/零股邊界閘門
        ActionHold(),
    ])
