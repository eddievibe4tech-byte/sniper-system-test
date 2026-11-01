"""🛡️ 鋼板五：行為樹葉節點 — 含部分平倉追蹤（tp_taken 旗標）

優先級由 tree.py 的 Selector 順序決定：
    Trailing Stop > 止損 > 停利 > 加碼（Break-Even ➜ Add）> 開倉 > 觀望。
移動停利平倉後以 stock.tp_taken 標記，防止同一波回檔重複觸發部分平倉。

🛡️ Issue #4（量化引擎 v2.0）：正金字塔生命週期葉節點
    - CheckTrailingStop / ActionTrailingStopExit：回撤 2.5×ATR 優先出場（AC#3）
    - CheckAndMoveStopToBreakEven：加碼 Sequence 的第一個 Condition（工程實作提示）
    - CheckAddCondition / ActionAddPosition：Trial(50%) ➜ Add(30%) ➜ Add(20%)（AC#1/#2）
    - CheckLotBoundary：整股/零股邊界攔截 MIN_ORDER_THRESHOLD（AC#4）
所有計算委派至 lifecycle.py 純函數（單一實作來源），葉節點只做「判斷 + 黑板寫入」。
"""
import logging
from typing import Optional, Tuple

from .nodes import BTNode, NodeStatus
from ..blackboard import Blackboard, MarketState, PortfolioState
from ..config import ExecutionConfig, LifecycleConfig
from ..lifecycle import (
    apply_lot_mode,
    blended_cost_price,
    break_even_stop_price,
    next_pyramid_stage,
    trailing_stop_price,
)

logger = logging.getLogger(__name__)


def _push_order(bb: Blackboard, order: str) -> None:
    """指令去重寫入：同一 tick 同名下單指令只出現一次（冪等防護）。"""
    if order not in bb.orders:
        bb.orders.append(order)


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
        _push_order(bb, "STOP_LOSS")  # 🛡️ Issue #4：結構化下單指令輸出
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
        _push_order(bb, "TAKE_PROFIT")  # 🛡️ Issue #4：結構化下單指令輸出
        bb.decision = f"【移動停利】{bb.stock.symbol} 自高點回檔，平倉 {self.cfg.tp_close_ratio:.0%}。"
        return NodeStatus.SUCCESS


# ---------------------------------------------------------------------------
# 🛡️ Issue #4（量化引擎 v2.0）：正金字塔生命週期葉節點
# ---------------------------------------------------------------------------
def _lifecycle_ready(bb: Blackboard) -> bool:
    """生命週期節點前置檢查：有持倉、有成本、有 ATR（髒資料一律靜默 FAILURE）。"""
    s = bb.stock
    return s is not None and s.has_position and s.cost_price > 0 and s.atr > 0


def _update_entry_peak(bb: Blackboard) -> None:
    """進場後最高價錨點更新（只增不減）：Trailing Stop 的基準線。

    每個生命週期 Condition 入口都會呼叫，確保黑板欄位與 peak_price 同步，
    且獨立於既有 FSM/ActionTakeProfit 的 peak 維護路徑（防禦性寫入）。
    """
    s = bb.stock
    if s.current_price > s.highest_price_since_entry:
        s.highest_price_since_entry = s.current_price


class CheckTrailingStop(BTNode):
    """AC#3：獲利展開後，現價自「進場後最高價」回撤達 trail_atr_mult × ATR ➜ SUCCESS。

    未獲利（現價 ≤ 成本）時不觸發 —— 虧損中的回撤是止損的職責，不是追蹤停損。
    """

    def __init__(self, cfg: LifecycleConfig):
        self.cfg = cfg

    def tick(self, bb: Blackboard) -> NodeStatus:
        if not _lifecycle_ready(bb):
            return NodeStatus.FAILURE
        _update_entry_peak(bb)
        s = bb.stock
        if s.current_price <= s.cost_price:
            return NodeStatus.FAILURE
        level = trailing_stop_price(s.highest_price_since_entry, s.atr,
                                    self.cfg.trail_atr_mult)
        if level > 0 and s.current_price <= level:
            s.trailing_stop_level = level
            drawdown = s.highest_price_since_entry - s.current_price
            bb.reasons.append(
                f"獲利回撤 {drawdown:.2f} ≥ {self.cfg.trail_atr_mult}×ATR"
                f"（追蹤停損線 {level:.2f}）")
            return NodeStatus.SUCCESS
        return NodeStatus.FAILURE


class ActionTrailingStopExit(BTNode):
    """AC#3：優先級高於「跌破 20MA」的出場 —— 輸出 TRAILING_STOP_EXIT 全數了結。"""

    def tick(self, bb: Blackboard) -> NodeStatus:
        s = bb.stock
        bb.action = "TRAILING_STOP_EXIT"
        _push_order(bb, "TRAILING_STOP_EXIT")
        bb.decision = (f"【追蹤停損出場】{s.symbol} 自高點 {s.highest_price_since_entry} "
                       f"回撤達停損線 {s.trailing_stop_level:.2f}，市價全數了結。")
        return NodeStatus.SUCCESS


class CheckAndMoveStopToBreakEven(BTNode):
    """AC#2 + 工程實作提示：加碼 Sequence 的第一個 Condition。

    任何加碼動作之前，必須先將原有部位停損上移至 Break-Even（進場成本 ×(1+buffer)），
    確保最壞情況總虧損 ≤ 初始風險預算。冪等：break_even_done 已成立時直接 SUCCESS
    （不重複輸出指令，但放行加碼序列繼續）。
    """

    def __init__(self, cfg: LifecycleConfig):
        self.cfg = cfg

    def tick(self, bb: Blackboard) -> NodeStatus:
        if not _lifecycle_ready(bb):
            return NodeStatus.FAILURE
        _update_entry_peak(bb)
        s = bb.stock
        if s.break_even_done:
            return NodeStatus.SUCCESS  # 已完成，放行（冪等）
        be = break_even_stop_price(s.cost_price, self.cfg.be_buffer_pct)
        if be <= 0:
            return NodeStatus.FAILURE
        # 停損只能上移、絕不下移；僅在確需移動時輸出指令
        moved = "" if s.trailing_stop_level >= be else f"{s.trailing_stop_level:.2f}"
        s.trailing_stop_level = max(s.trailing_stop_level, be)
        s.break_even_done = True
        _push_order(bb, "MOVE_STOP_TO_BREAK_EVEN")
        bb.reasons.append(
            f"Break-Even 防護啟動：停損上移至 {be:.2f}"
            + (f"（原 {moved}）" if moved else ""))
        return NodeStatus.SUCCESS


class CheckAddCondition(BTNode):
    """AC#1：正金字塔下一階段加碼條件。

    全部成立才 SUCCESS：
      1. 尚未滿倉（next_pyramid_stage 有下一階段）；
      2. 現價 ≥ 成本 + add_min_gain_atr × ATR（趨勢延續才准加碼）；
      3. 站上 20MA（微觀趨勢確認）；
      4. 大盤非熊市（Macro FSM 相容閘門：BEAR_DEFENSE 禁止新增曝險）。
    """

    def __init__(self, cfg: LifecycleConfig):
        self.cfg = cfg

    def tick(self, bb: Blackboard) -> NodeStatus:
        if not _lifecycle_ready(bb):
            return NodeStatus.FAILURE
        _update_entry_peak(bb)
        s = bb.stock
        stage, idx = next_pyramid_stage(s.current_position_ratio)
        if stage is None:
            return NodeStatus.FAILURE  # 已滿倉或髒資料
        if bb.macro_state == "BEAR_DEFENSE" or bb.market_state == MarketState.BEAR:
            return NodeStatus.FAILURE
        if s.ma20 > 0 and s.current_price < s.ma20:
            return NodeStatus.FAILURE
        gain_needed = self.cfg.add_min_gain_atr * s.atr
        if s.current_price < s.cost_price + gain_needed:
            return NodeStatus.FAILURE
        # 暫存本階段決策（stage/index），供同 Sequence 的 ActionAddPosition 取用
        bb._pending_stage = (stage, idx)  # type: ignore[attr-defined]
        return NodeStatus.SUCCESS


class ActionAddPosition(BTNode):
    """AC#1/#2/#4：執行加碼 —— 權重平均成本、推進金字塔進度、整股/零股邊界攔截。

    部位比例嚴格依序 Trial(50%) ➜ Add(30%) ➜ Add(20%)；加碼股數經
    apply_lot_mode() 邊界處理，被攔截時輸出 REJECT_MIN_ORDER_THRESHOLD 並
    退回 HOLD（絕不帶病下單）。
    """

    def __init__(self, cfg: LifecycleConfig):
        self.cfg = cfg

    def tick(self, bb: Blackboard) -> NodeStatus:
        from ..sizer import calc_position_size  # 延遲匯入：避免模組循環依賴

        s = bb.stock
        stage, idx = getattr(bb, "_pending_stage", (None, -1))
        if stage is None:
            return NodeStatus.FAILURE
        target_shares = calc_position_size(
            capital=bb.current_capital, price=s.current_price, atr=s.atr,
            risk_pct=0.01, max_weight=0.20, lot_size=self.cfg.lot_size,
            round_to_lot=False).shares
        add_raw = target_shares * stage
        add_shares, reject = apply_lot_mode(
            add_raw, s.current_price, mode=self.cfg.lot_mode,
            lot_size=self.cfg.lot_size, min_order_amount=self.cfg.min_order_amount)
        if reject:
            # AC#4：邊界攔截 —— 拒絕下單、不留半成品指令，轉為觀望留痕
            _push_order(bb, "REJECT_MIN_ORDER_THRESHOLD")
            bb.action = "HOLD"
            bb.reasons.append(f"加碼遭攔截 [{reject}]：raw={add_raw:.0f} 股 @ {s.current_price}")
            bb.decision = f"【加碼攔截】{s.symbol} 未達最低單筆門檻（{reject}），維持現狀。"
            return NodeStatus.SUCCESS
        old_shares = s.shares_held
        s.cost_price = blended_cost_price(old_shares, s.cost_price,
                                          add_shares, s.current_price)
        s.shares_held = old_shares + add_shares
        s.current_position_ratio = round(s.current_position_ratio + stage, 6)
        s.break_even_done = False  # 🛡️ Review 🔴1 協定：換手即新倉，停利/保本旗標重置
        s.tp_taken = False
        s.tp_taken_cost_price = 0.0
        _push_order(bb, "ADD_POSITION")
        bb.action = "ADD_POSITION"
        bb.position_size_pct = s.current_position_ratio
        label = "試單 Trial" if idx == 0 else f"加碼 Add#{idx}"
        bb.decision = (f"【{label}】{s.symbol} 加碼 {add_shares} 股 @ {s.current_price}，"
                       f"金字塔進度 {s.current_position_ratio:.0%}，均價 {s.cost_price:.2f}。")
        return NodeStatus.SUCCESS


class CheckLotBoundary(BTNode):
    """AC#4：開倉前的整股/零股邊界檢查（Sequence 末端風控閘門）。

    以黑板 position_size_pct 對目標部位做邊界換算；被攔截時輸出
    REJECT_MIN_ORDER_THRESHOLD 並 FAILURE（阻斷 ActionBuy）。
    """

    def __init__(self, cfg: LifecycleConfig):
        self.cfg = cfg

    def tick(self, bb: Blackboard) -> NodeStatus:
        from ..sizer import calc_position_size  # 延遲匯入：避免模組循環依賴

        s = bb.stock
        if s is None or s.atr <= 0 or s.current_price <= 0:
            return NodeStatus.SUCCESS  # 無足夠資料時不誤攔（降級放行，交由上層風控）
        size_pct = bb.position_size_pct if bb.position_size_pct > 0 else 1.0
        res = calc_position_size(
            capital=bb.current_capital, price=s.current_price, atr=s.atr,
            risk_pct=0.01, max_weight=0.20, lot_size=self.cfg.lot_size,
            round_to_lot=False)
        raw = res.shares * size_pct
        shares, reject = apply_lot_mode(
            raw, s.current_price, mode=self.cfg.lot_mode,
            lot_size=self.cfg.lot_size, min_order_amount=self.cfg.min_order_amount)
        if reject:
            _push_order(bb, "REJECT_MIN_ORDER_THRESHOLD")
            bb.reasons.append(f"開倉股數遭攔截 [{reject}]：raw={raw:.0f} 股 @ {s.current_price}")
            return NodeStatus.FAILURE
        s.target_shares = shares  # 通過邊界驗證的實際可下單股數
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
        _push_order(bb, "BUY")  # 🛡️ Issue #4：結構化下單指令輸出
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
