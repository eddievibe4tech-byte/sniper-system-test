"""決策紀錄（可審計）：每筆輸出都帶 config_version 與 reasons，完全可重現。"""
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class DecisionRecord:
    timestamp: str
    symbol: str
    market_state: str
    portfolio_state: str
    action: str            # STOP_LOSS / TAKE_PROFIT / BUY / HOLD
    decision_text: str
    position_size_pct: float
    config_version: str
    reasons: Tuple[str, ...]
