"""🛡️ 鋼板五：行為樹葉節點 — 含部分平倉追蹤（tp_taken 旗標）

優先級由 tree.py 的 Selector 順序決定：止損 > 停利 > 開倉 > 觀望。
移動停利平倉後以 stock.tp_taken 標記，防止同一波回檔重複觸發部分平倉。
"""
from .nodes import BTNode, NodeStatus
from ..blackboard import Blackboard, MarketState, PortfolioState
from ..config import ExecutionConfig


class CheckStopLoss(BTNode):
    def __init__(self, cfg: ExecutionConfig):
        self.cfg = cfg

    def tick(self, bb: Blackboard) -> NodeStatus:
        s = bb.stock
        if s.has_position and s.current_price <= s.cost_price * (1 - self.cfg.stop_loss_pct):
            bb.reasons.append(f"跌破成本 {self.cfg.stop_loss_pct:.0%}")
            return NodeStatus.SUCCESS
        return NodeStatus.FAILURE


class ActionStopLoss(BTNode):
    def tick(self, bb: Blackboard) -> NodeStatus:
        bb.action = "STOP_LOSS"
        bb.decision = f"【硬性止損】{bb.stock.symbol} 跌破成本，市價全數清倉！"
        return NodeStatus.SUCCESS


class CheckTakeProfit(BTNode):
    def __init__(self, cfg: ExecutionConfig):
        self.cfg = cfg

    def tick(self, bb: Blackboard) -> NodeStatus:
        s = bb.stock
        # 🛡️ 已平過 50% 不再重複觸發
        if not s.has_position or s.tp_taken:
            return NodeStatus.FAILURE
        if (s.peak_price >= s.cost_price * (1 + self.cfg.tp_trigger_profit_pct) and
                s.current_price <= s.peak_price * (1 - self.cfg.tp_trail_pct)):
            bb.reasons.append(f"自高點回檔 {self.cfg.tp_trail_pct:.0%}")
            return NodeStatus.SUCCESS
        return NodeStatus.FAILURE


class ActionTakeProfit(BTNode):
    def __init__(self, cfg: ExecutionConfig):
        self.cfg = cfg

    def tick(self, bb: Blackboard) -> NodeStatus:
        bb.stock.tp_taken = True  # 🛡️ 標記，防重複平倉
        # 🛡️ Review 🔴1：旗標綁定當前持倉成本——日後 cost_price 改變（換手／加碼）
        # 即視為新倉位，持久化還原時 tp_taken 應重置，避免該股票「終身免疫」停利保護
        bb.stock.tp_taken_cost_price = bb.stock.cost_price
        bb.action = "TAKE_PROFIT"
        bb.decision = f"【移動停利】{bb.stock.symbol} 自高點回檔，平倉 {self.cfg.tp_close_ratio:.0%}。"
        return NodeStatus.SUCCESS


class ConditionMarketAllowed(BTNode):
    """🛡️ Review R3：失敗留痕需守「髒黑板」協定——bb.action 仍為初始 HOLD
    才代表流程真的走到本節點（前面的止損/停利分支已攔截則不該留下誤導性理由）。"""
    def tick(self, bb: Blackboard) -> NodeStatus:
        if bb.market_state == MarketState.BULL:
            return NodeStatus.SUCCESS
        if bb.action == "HOLD":
            bb.reasons.append("大盤非牛市，禁止開倉")
        return NodeStatus.FAILURE


class ConditionPortfolioAllowed(BTNode):
    def tick(self, bb: Blackboard) -> NodeStatus:
        if bb.portfolio_state == PortfolioState.CIRCUIT_BREAKER:
            if bb.action == "HOLD":  # 🛡️ Review R3：同上，避免跨分支污染 reasons
                bb.reasons.append("帳戶熔斷中，禁止開倉")
            return NodeStatus.FAILURE
        return NodeStatus.SUCCESS


class ConditionBreakout(BTNode):
    def tick(self, bb: Blackboard) -> NodeStatus:
        s = bb.stock
        if not s.has_position and s.current_price > s.ma20 and s.ma20 > 0:
            bb.reasons.append(f"突破 20MA (收盤 {s.current_price} > MA20 {s.ma20})")
            return NodeStatus.SUCCESS
        # 🛡️ Review R3：技術面未達標時主動留痕（此前成功才有理由、失敗反而空白）
        if bb.action == "HOLD" and not s.has_position and s.ma20 > 0:
            bb.reasons.append(f"未突破 20MA (收盤 {s.current_price} ≤ MA20 {s.ma20})")
        return NodeStatus.FAILURE


class ActionBuy(BTNode):
    def __init__(self, cfg: ExecutionConfig):
        self.cfg = cfg

    def tick(self, bb: Blackboard) -> NodeStatus:
        # 🛡️ Review R2（DRY）：倉位大小唯一起源在此，寫入黑板供 engine 直接取用
        size = 0.5 if bb.portfolio_state == PortfolioState.DEFENSIVE else 1.0
        bb.position_size_pct = size
        bb.action = "BUY"
        size_label = "50% 縮減倉位" if size == 0.5 else "100% 標準部位"
        bb.decision = f"【開倉買進】{bb.stock.symbol} 突破 20MA，建立 {size_label}。"
        return NodeStatus.SUCCESS


class ActionHold(BTNode):
    def tick(self, bb: Blackboard) -> NodeStatus:
        bb.action = "HOLD"
        state_str = "持有待變" if bb.stock.has_position else "空手觀望"
        # 🛡️ Review R3：保底結算節點——若 reasons 為空（未達開倉技術條件、無任何失敗留痕），
        # 主動標註觀望原因，避免報表上「因何觀望」不可辨識
        if not bb.reasons:
            bb.reasons.append("未達開倉條件或無持倉事件")
        bb.decision = f"【維持現狀】{bb.stock.symbol} 無觸發事件，{state_str}。"
        return NodeStatus.SUCCESS
