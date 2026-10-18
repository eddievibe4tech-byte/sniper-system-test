"""共享黑板（Blackboard）+ 數據來源 Protocol 抽象。

FSM 與行為樹不直接碰外部世界，一切輸入輸出都經由 Blackboard，
這是讓整顆引擎可脫線測試、可抽離成 library 的關鍵邊界。
"""
from dataclasses import dataclass, field
from typing import List, Optional, Protocol
from enum import Enum


class MarketState(Enum):
    BULL = "牛市 (可積極做多)"
    SIDEWAYS = "震盪 (控管倉位)"
    BEAR = "熊市 (禁止開多)"


class PortfolioState(Enum):
    NORMAL = "正常運行"
    DEFENSIVE = "防禦防守 (部位減半)"
    CIRCUIT_BREAKER = "熔斷停機 (鎖死交易)"


@dataclass
class StockData:
    symbol: str
    current_price: float
    cost_price: float = 0.0
    peak_price: float = 0.0
    ma20: float = 0.0
    has_position: bool = False
    tp_taken: bool = False   # 🛡️ 鋼板五：移動停利是否已平過倉（防重複部分平倉）
    # 🛡️ Review 🔴1：tp_taken 綁定的持倉成本。停利觸發時記下當時 cost_price，
    # 跨日還原時若 cost_price 改變（換手／加碼）→ 旗標作廢重置，避免「終身免疫」
    tp_taken_cost_price: float = 0.0


@dataclass
class Blackboard:
    # 大盤
    market_index: float
    market_ma60: float
    market_ma200: float
    # 個人帳戶（必填欄位集中排列，避免 dataclass 預設值順序問題）
    peak_capital: float
    current_capital: float
    # 狀態與可選欄位（皆含預設值）
    market_state: MarketState = MarketState.SIDEWAYS
    consecutive_losses: int = 0
    portfolio_state: PortfolioState = PortfolioState.NORMAL
    locked_until: Optional[str] = None   # ISO 格式時間
    below_ma200_days: int = 0            # 🛡️ Review#1/#2：熊市連日計數改由黑板承載（非 FSM 實例變數），可持久化、迴圈不重複累加
    last_market_date: Optional[str] = None  # 環境態勢最近評估日（ISO date），冪等保護用
    # 當前評估個股
    stock: Optional[StockData] = None
    decision: str = ""
    action: str = "HOLD"
    position_size_pct: float = 0.0       # 🛡️ Review R2：倉位由 ActionBuy 決定寫入，engine 直接取用（DRY）
    reasons: List[str] = field(default_factory=list)


class DataSource(Protocol):
    """🔌 數據來源抽象：可接 yfinance、FinMind 或回測 CSV"""
    def get_market_index(self) -> float: ...
    def get_market_ma(self, days: int) -> float: ...
    def get_stock(self, symbol: str) -> StockData: ...
