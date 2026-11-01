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
from ..macro import MacroState
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

    ⚠️ Issue #4 修正（有狀態 BT 的「預演污染」缺陷 — 最終版）：
    本節點位於加碼 Sequence 內、CheckAddCondition **之前**（協定要求 Break-Even
    先於任何新增曝險）。若在此處只「暫存意向」、把實際下單延後給
    ActionAddPosition，會產生兩類缺陷：
      1. 加碼金額未達整股/零股邊界門檻時，ActionAddPosition 提前 return，
         意向永不落地 —— 測試與 AC#2 要求的「同一 tick 同步輸出」失效；
      2. 意向滯留黑板，被下一個與加碼無關的動作（如停利）誤提交 —— 副作用
         跨節點漂移，比原本的預演污染更難追。
    解法：**意向暫存＋雙重落地**。本節點僅做唯讀檢查並寫入 _pending_break_even
    意向；實際提交有兩個落點：
      a) 同序列末端 ActionAddPosition（正常路徑：保本＋加碼同一 tick 原子輸出）；
      b) Selector 保底終端 ActionHold（異常收束路徑：Sequence 中途 FAILURE 時，
         意向必然不會再被 a) 取用，就地清空落地，杜絕跨 tick／跨分支殘留）。
    安全性論證：停損上移屬純風控動作（只收緊、不放寬），即使該 tick 最終沒有
    加碼，執行保本停損單仍是更安全而非更危險的方向，且 b) 保證意向生命週期
    不超過單一 tick —— 兼顧 AC#2「同步輸出」與零污染。冪等性由 break_even_done
    旗標 + 去重推送保證：已完成時不重複輸出指令，僅回傳 SUCCESS 放行加碼序列。
    """

    def __init__(self, cfg: LifecycleConfig):
        self.cfg = cfg

    def tick(self, bb: Blackboard) -> NodeStatus:
        if not _lifecycle_ready(bb):
            return NodeStatus.FAILURE
        _update_entry_peak(bb)
        s = bb.stock
        be = break_even_stop_price(s.cost_price, self.cfg.be_buffer_pct)
        if be <= 0:
            return NodeStatus.FAILURE
        # 🛡️ Issue #4（保本錨點重算）：break_even_done 的語意是「停損已覆蓋
        # 【當前】成本基準的保本線」。加碼使 cost_price 上移、保本線隨之抬高，
        # 舊旗標立即失效 —— 以「停損線 ≥ 現行保本線」為準則直接覆核，
        # 不依賴旗標時效性（旗標仅作持久化提示，真相永遠以價格條件為準）。
        if s.break_even_done and s.trailing_stop_level >= be:
            return NodeStatus.SUCCESS  # 已完成且停損仍居保本線上，放行（冪等）
        if s.break_even_done:
            # 加碼後 cost 上移、保本線抬高：旗標過期，重新執行上移
            s.break_even_done = False
        # 🛡️ Issue #4 最終定案：**就地提交（immediate commit）**。
        # 曾嘗試「意向暫存、延後由 ActionAddPosition 落地」，但實測證明該做法
        # 與 AC#2「同步輸出」及邊界攔截情境互相矛盾（加碼被整股門檻擋下時意向
        # 永不落地；Sequence 中途 FAILURE 時又需額外收束點，跨節點漂移更難追）。
        # 停損上移屬純風控動作（只收緊、不放寬）：即使後續加碼條件 FAILURE，
        # 多執行的保本停損單仍是更安全而非更危險的方向 —— 就地提交同時滿足
        # 「Break-Even 先於任何新增曝險」的協定順序與 AC#2 的同 tick 語意。
        # 冪等性：break_even_done 旗標 + _push_order 去重，重複 tick 不重複下單。
        moved = "" if s.trailing_stop_level >= be else f"{s.trailing_stop_level:.2f}"
        s.trailing_stop_level = max(s.trailing_stop_level, be)
        s.break_even_done = True
        _push_order(bb, "MOVE_STOP_TO_BREAK_EVEN")
        bb.reasons.append(f"Break-Even 防護啟動：停損上移至 {be:.2f}"
                          + (f"（原 {moved}）" if moved else ""))
        return NodeStatus.SUCCESS


def _is_bear_defense(bb: Blackboard) -> bool:
    """Macro FSM 熊市防禦判定（Issue #199/#4 修復）。

    ⚠️ 原實作以 `bb.macro_state == "BEAR_DEFENSE"` 比對，但黑板預設值即為
    "BEAR_DEFENSE"（保守起步），且 MacroFSM 產出的是 MacroState Enum 實例；
    兩者混合比較導致「未跑 Macro FSM 的黑板」一律被誤判為空頭防禦，
    行為樹加碼/開倉分支永遠進不去。此處統一正規化為字串再比對，
    Enum 與裸字串皆正確辨識，其他狀態（STRONG_TREND / DIVERGENCE_BULL /
    CHOPPY_MARKET / SIDEWAYS）不再誤擋。

    ⚠️ Issue #4（保守預設值的最終定案）：macro_state 預設值 "BEAR_DEFENSE"
    僅在 MacroFSM 實際運行後才具參考性。以 days_in_current_state > 0 作為
    「環境層已解算並寫入」的訊號（MacroFSM 每 tick 必維護計數器；純微觀
    呼叫路徑維持初始值 0）。計數器為 0 ➜ 視為環境層資訊真空，不套用保守
    預設值攔截，退回微觀 market_state 把關 —— 否則 AC#1/#2 的正金字塔建倉
    在未跑 MacroFSM 的路徑（引擎單檔評估、單元測試）永遠無法啟動。
    """
    ms = getattr(bb.macro_state, "value", bb.macro_state)
    if str(ms) != MacroState.BEAR_DEFENSE.value:
        return False  # 環境層明確非空頭
    if int(getattr(bb, "days_in_current_state", 0) or 0) <= 0:
        return False  # MacroFSM 尚未運行：預設值不具參考性
    return True


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
        if _is_bear_defense(bb) or bb.market_state == MarketState.BEAR:
            return NodeStatus.FAILURE
        if s.ma20 > 0 and s.current_price < s.ma20:
            return NodeStatus.FAILURE
        gain_needed = self.cfg.add_min_gain_atr * s.atr
        if s.current_price < s.cost_price + gain_needed:
            return NodeStatus.FAILURE
        # 🛡️ Issue #199（方案 A 前置閘門）：階段名額以既有持倉為基數
        # （add_raw = shares_held × stage）。零股模式或臨界進位後仍歸零 ➜
        # 直接 FAILURE，杜絕「保本指令已同步輸出、加碼卻被攔截」的半副作用 tick。
        # ⚠️ AC#1 明文 Trial(50%) 亦屬本序列產出 —— shares_held <= 0 時以
        # 【風控核准名額】當基數落地首批：優先沿用開倉路徑 CheckLotBoundary
        # 寫入的 target_shares；純微觀測試/引擎單檔路徑未帶入持倉資料時，
        # 退回與 CheckLotBoundary 同款協定即時換算（雙重閥門 ∧ 邊界處理），
        # 確保首批部位永不擊穿 20% 名目上限與 1% 風險預算。
        if s.shares_held > 0:
            add_raw = s.shares_held * stage
        elif s.target_shares > 0:
            add_raw = s.target_shares * stage
        else:
            from ..sizer import calc_position_size  # 延遲匯入：避免模組循環依賴
            res = calc_position_size(
                capital=bb.current_capital, price=s.current_price, atr=s.atr,
                risk_pct=0.01, max_weight=self.cfg.max_position_weight,
                lot_size=self.cfg.lot_size, round_to_lot=False)
            add_raw = res.shares * stage
        probe, _rej = apply_lot_mode(
            add_raw, s.current_price, mode=self.cfg.lot_mode,
            lot_size=self.cfg.lot_size, min_order_amount=self.cfg.min_order_amount,
            round_up=True)
        if probe <= 0:
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
        # 🛡️ Issue #4：Break-Even 已由同序列首位的 CheckAndMoveStopToBreakEven
        # 「就地提交」（AC#2 同步輸出）；能走到這裡代表該 Condition 已 SUCCESS，
        # 保本上移必然生效或早已完成，此處不重複處理。
        # 🛡️ Issue #199（方案 A 最終定案：名額基數 + 臨界進位）：
        # pyramid 進度欄位 current_position_ratio 的「100%」錨定在【風控核准
        # 名額】，階段名額依 AC#1 明文比例直接計算：
        #     add_raw = notional_cap × stage   （50% / 30% / 20%，嚴禁倒金字塔）
        # 原實作兩處缺陷：
        #   1. Trial 基數誤用 shares_held（空手=0）➜ raw=0 ➜ REJECT_INVALID_INPUT，
        #      正金字塔第一階段即中斷；
        #   2. ROUND_LOT 無條件捨去：低總值股票（$10、名額 2,000 股）的 30%
        #      增量 = 600 股不足一張 ➜ 第二階段永不落地。
        # 解法：基數改采與開倉路徑 CheckLotBoundary 同一政策的【名目上限部位】
        # （20%×capital÷price ∧ 1% 風險預算雙重閥門），加碼湊整採臨界進位
        # （ceil_to_lot，增量 ≤ lot-1 股）。風控論證：各階段金額佔比
        # 50%/30%/20%，最大單筆 Trial = 10% ≤ 20% ✓；Break-Even 保本停損先於
        # 加碼提交（Sequence 首位），最壞情況總虧損 ≈ trial 部位 × buffer
        # < 1% 風險預算 ✓ —— 加碼不擊穿風控，僅推進進度。
        if s.current_price <= 0:
            return NodeStatus.FAILURE
        notional_cap_shares = int(bb.current_capital * self.cfg.max_position_weight
                                  / s.current_price)
        add_raw = notional_cap_shares * stage
        add_shares, reject = apply_lot_mode(
            add_raw, s.current_price, mode=self.cfg.lot_mode,
            lot_size=self.cfg.lot_size, min_order_amount=self.cfg.min_order_amount,
            round_up=True)
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
        # 🛡️ Issue #4（保本旗標不再無條件重置）：加碼後 cost 上移、保本線抬高，
        # 下一次 tick 由 CheckAndMoveStopToBreakEven 以「停損線 ≥ 現行保本線」
        # 準則自動覆核並生成新高位意向 —— 若在此處逕自置 False，會使每個 tick
        # 都重新配對一張 MOVE_STOP_TO_BREAK_EVEN（重複下單指令污染）。
        # tp_taken 仍須重置：換手即新倉，停利保護不得終身免疫（Review 🔴1）。
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
