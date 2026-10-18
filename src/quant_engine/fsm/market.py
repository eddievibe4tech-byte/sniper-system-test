"""🛡️ 鋼板二：大盤滯回狀態機（Hysteresis FSM）

均線附近震盪時不跳變狀態：進牛市門檻（+1%）高於出牛市門檻（-0.5%），
熊市則需「連續 N 天跌破年線且幅度達標」才確認——單日假跌破不為所動。

🛡️ Review 修正（致命缺陷 #1/#2）：
- 「連續跌破年線天數」不再存在 FSM 實例變數（CI 無狀態 Runner 每日重置、
  且同批次多檔股票會重複累加擊穿門檻）。
- 改由 call run 傳入 `trading_days_below`（純函數：由真實歷史收盤序列計算）；
  無歷史資料時降級使用 bb.below_ma200_days（跨日持久化的計數器）。
"""
from ..blackboard import Blackboard, MarketState
from ..config import MarketFSMConfig


class MarketFSM:
    """🛡️ 滯回設計：均線附近震盪時不跳變狀態"""

    def __init__(self, cfg: MarketFSMConfig):
        self.cfg = cfg

    def update(self, bb: Blackboard, trading_days_below: int | None = None) -> None:
        idx, ma60, ma200 = bb.market_index, bb.market_ma60, bb.market_ma200
        if ma200 <= 0 or ma60 <= 0:
            return

        # 🛡️ 追蹤跌破年線天數（熊市確認用）：優先採外部傳入的歷史純計算值
        if trading_days_below is not None:
            below_days = max(0, int(trading_days_below))
        else:
            below_days = bb.below_ma200_days + 1 if idx < ma200 else 0
        bb.below_ma200_days = below_days
        cur = bb.market_state

        # 優先判斷熊市（最保守）
        if cur != MarketState.BEAR and \
           idx < ma200 * (1 + self.cfg.bear_enter_below_ma200) and \
           below_days >= self.cfg.bear_confirm_days:
            bb.market_state = MarketState.BEAR
            bb.reasons.append(f"大盤跌破年線 {below_days} 天，確認熊市")
        elif cur == MarketState.BEAR and idx > ma200 * (1 + self.cfg.bear_exit_above_ma200):
            bb.market_state = MarketState.SIDEWAYS
            bb.reasons.append("大盤回升突破年線，退出熊市")
        # 再判斷牛市
        elif cur != MarketState.BULL and \
             idx > ma60 * (1 + self.cfg.bull_enter_above_ma60) and idx > ma200:
            bb.market_state = MarketState.BULL
            bb.reasons.append("大盤站上季線與年線，進入牛市")
        elif cur == MarketState.BULL and idx < ma60 * (1 + self.cfg.bull_exit_below_ma60):
            bb.market_state = MarketState.SIDEWAYS
            bb.reasons.append("大盤跌破季線，退出牛市")
        # 其餘情況：維持 cur（滯回核心）
