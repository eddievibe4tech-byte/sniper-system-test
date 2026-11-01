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


# 🛡️ Issue #3（量化引擎 v2.0）：Macro FSM 四態狀態名以字串承載於黑板
# （STRONG_TREND / DIVERGENCE_BULL / CHOPPY_MARKET / BEAR_DEFENSE），
# 與既有 MarketState Enum 並存、互不干擾，方便 JSON 持久化與跨版本還原。
MACRO_STATE_DEFAULT = "BEAR_DEFENSE"


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
    # ---------- 🛡️ Issue #4（量化引擎 v2.0）：正金字塔生命週期欄位 ----------
    # Blackboard 擴充（工程實作提示）：以下欄位支援 Trailing Stop 與加碼判斷。
    atr: float = 0.0                        # Wilder's ATR(14)（Issue #1 數學層產出）
    highest_price_since_entry: float = 0.0  # 進場後最高價（Trailing Stop 錨點，只增不減）
    current_position_ratio: float = 0.0     # 現行部位佔目標總部位比例（0~1，金字塔進度）
    break_even_done: bool = False           # Break-Even 停損上移是否已完成（冪等防護）
    trailing_stop_level: float = 0.0        # 追蹤停損價格（由 FSM/BT 維護，供下單層展示）

    def __post_init__(self):
        # 未帶入生命週期欄位的舊呼叫點自動對齊：既有管線 peak_price 即「進場後最高價」
        # （extract_stocks 以現價初始化 peak、ActionTakeProfit 只升不降），
        # 故 highest_price_since_entry 以其為初值；尚未建倉（cost<=0）時退回現價。
        if self.highest_price_since_entry <= 0:
            anchor = self.peak_price if self.peak_price > 0 else self.current_price
            if anchor > 0 and (self.has_position or self.cost_price > 0):
                self.highest_price_since_entry = anchor


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
    # ---------- 🛡️ Issue #3：大盤狀態機（Macro FSM）與 Proxy Breadth ----------
    macro_state: str = MACRO_STATE_DEFAULT  # 四態字串（見 MacroState Enum）
    days_in_current_state: int = 0          # 防抖計數器：現況已維持的交易日數
    breadth_pct: float = 0.0                # 代理寬度：站上各自 20MA 的代理佔比 (0~1)
    macro_max_exposure: float = 0.0         # 現行狀態允許的最大曝險（下游縮倉依據）
    action_note: str = ""                   # 環境層最近一次狀態轉換留痕（Runner 抽取後清空）
    pending_state: str = ""                 # 防抖觀察佇列：候選新狀態名（"" = 無候選）
    pending_days: int = 0                   # 防抖觀察佇列：候選條件已連續成立天數
    # 當前評估個股
    stock: Optional[StockData] = None
    decision: str = ""
    action: str = "HOLD"
    position_size_pct: float = 0.0       # 🛡️ Review R2：倉位由 ActionBuy 決定寫入，engine 直接取用（DRY）
    # ---------- 🛡️ Issue #4（量化引擎 v2.0）：生命週期指令承載 ----------
    # BT 葉節點產出的結構化下單/風控指令（STOP_LOSS / TAKE_PROFIT / BUY / ADD_POSITION /
    # MOVE_STOP_TO_BREAK_EVEN / TRAILING_STOP_EXIT / REJECT_MIN_ORDER_THRESHOLD），
    # 每個 tick 起始由 engine.evaluate_stock() 重置為空列表（跨標的零污染協定同 reasons）。
    orders: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)


class DataSource(Protocol):
    """🔌 數據來源抽象：可接 yfinance、FinMind 或回測 CSV"""
    def get_market_index(self) -> float: ...
    def get_market_ma(self, days: int) -> float: ...
    def get_stock(self, symbol: str) -> StockData: ...
