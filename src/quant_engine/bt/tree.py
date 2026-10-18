"""預設交易行為樹：按優先級 止損 > 停利 > 開倉 > 觀望。"""
from .nodes import Selector, Sequence
from .leaves import (CheckStopLoss, ActionStopLoss, CheckTakeProfit, ActionTakeProfit,
                     ConditionMarketAllowed, ConditionPortfolioAllowed,
                     ConditionBreakout, ActionBuy, ActionHold)
from ..config import ExecutionConfig


def build_trading_behavior_tree(cfg: ExecutionConfig) -> Selector:
    """按優先級：止損 > 停利 > 開倉 > 觀望"""
    return Selector([
        Sequence([CheckStopLoss(cfg), ActionStopLoss()]),
        Sequence([CheckTakeProfit(cfg), ActionTakeProfit(cfg)]),
        Sequence([ConditionMarketAllowed(), ConditionPortfolioAllowed(),
                  ConditionBreakout(), ActionBuy(cfg)]),
        ActionHold(),
    ])
