"""DecisionEngine 主體：FSM 更新 → 行為樹 tick → 產出可審計 DecisionRecord。

三層治理管線（大盤狀態機 → 風控狀態機 → 行為樹執行），全程確定性、無 LLM 幻覺。

🛡️ Review 修正（致命缺陷 #1）：環境評估與個股決策拆分為兩個呼叫點——
- evaluate_environment()：宏觀態勢（Market/Portfolio FSM），每天只跑一次
- evaluate_stock()：個股行為樹，可在迴圈中遍歷多檔而不污染宏觀狀態
"""
from datetime import datetime
from typing import Optional

from .config import EngineConfig
from .blackboard import Blackboard, StockData
from .fsm.market import MarketFSM
from .fsm.portfolio import PortfolioFSM
from .bt.tree import build_trading_behavior_tree
from .records import DecisionRecord


class DecisionEngine:
    def __init__(self, cfg: EngineConfig = EngineConfig()):
        self.cfg = cfg
        self.market_fsm = MarketFSM(cfg.market)
        self.portfolio_fsm = PortfolioFSM(cfg.portfolio)
        self.tree = build_trading_behavior_tree(cfg.execution)

    def evaluate_environment(self, bb: Blackboard, now: datetime,
                             trading_days_below: Optional[int] = None,
                             force: bool = False) -> bool:
        """每日宏觀態勢評估（一天只需且只能評估一次）。

        🛡️ 冪等保護：若黑板上 last_market_date 已等於今日（同一日重複呼叫），
        除非 force=True，否則直接跳過，防止同批次／同日多次執行造成
        狀態連續跳變或熔斷鎖到期時間被反覆推延。

        Args:
            trading_days_below: 由真實歷史收盤序列純計算出的「連續跌破年線交易日數」；
                None 時降級使用黑板持久化計數器（見 MarketFSM docstring）。
        Returns:
            True 表示本次實際執行了狀態更新。
        """
        today = now.date().isoformat()
        if not force and bb.last_market_date == today:
            return False
        self.market_fsm.update(bb, trading_days_below=trading_days_below)
        self.portfolio_fsm.update(bb, now)
        bb.last_market_date = today
        return True

    def evaluate_stock(self, bb: Blackboard, stock: StockData,
                       now: datetime) -> DecisionRecord:
        """個股行為樹評估（可在迴圈中遍歷；不觸碰任何宏觀 FSM）。"""
        bb.stock = stock
        # 🛡️ 清空前一檔標的的殘留痕跡，避免 reasons/action 跨股票污染
        bb.reasons = []
        bb.action = "HOLD"
        bb.decision = ""
        bb.position_size_pct = 0.0

        self.tree.tick(bb)

        # 🛡️ Review R2（DRY）：倉位由 ActionBuy 寫入黑板，此處直接取用，不再重複計算
        return DecisionRecord(
            timestamp=now.isoformat(),
            symbol=stock.symbol,
            market_state=bb.market_state.value,
            portfolio_state=bb.portfolio_state.value,
            action=bb.action,
            decision_text=bb.decision,
            position_size_pct=bb.position_size_pct,
            config_version=self.cfg.version,
            reasons=tuple(bb.reasons),
        )

    def evaluate(self, bb: Blackboard, now: datetime,
                  trading_days_below: Optional[int] = None) -> DecisionRecord:
        """相容性入口：環境（帶冪等保護）＋ 單檔個股評估。

        ⚠️ 批次場景請改用 evaluate_environment() + evaluate_stock() 迴圈，
        勿在 for-loop 中對同一引擎重複呼叫本方法（Review 致命缺陷 #1）。
        """
        self.evaluate_environment(bb, now, trading_days_below=trading_days_below)
        return self.evaluate_stock(bb, bb.stock, now)
