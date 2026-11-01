"""🛡️ Issue #4（量化引擎 v2.0）：正金字塔部位生命週期純函數。

散戶思維的致命盲點（Epic #195 痛點 #3）：
    倒金字塔加碼 —— 試單 30% ➜ 加碼 70%，瞬間墊高平均成本；一旦回踩均線，
    放大後的全額部位將擊穿初始 1% 風險底線。

機構級解法 —— 正金字塔（Pyramiding）+ Break-Even 強制防護：
    - 部位嚴格依序 Trial (50%) ➜ Add (30%) ➜ Add (20%)，越加越少；
    - 任何加碼動作之前，必須先將「原有部位」停損上移至 Break-Even（進場成本），
      確保最壞情況總虧損 ≤ 初始風險預算（1%）。
    - Trailing Stop：進場後最高價回撤達 2.5×ATR ➜ 優先出場（優先級高於跌破 20MA）。
    - 整股/零股邊界：ROUND_LOT 向下取整到張；ODD_LOT 單筆金額 < 最低門檻 ➜ 攔截。

本模組為 Pure Logic（純函數、零副作用、不拋例外），與雙重閥門計算器同層，
供行為樹葉節點與持久化層共用，杜絕散落各處的副本邏輯。
"""
import logging
import math
from enum import Enum
from typing import Sequence, Tuple

logger = logging.getLogger(__name__)


class LotMode(str, Enum):
    """下單單位模式（AC#4 整股/零股邊界測試）。

    繼承 str：與既有字串比較及 JSON 序列化完全相容（協同 SizingGate）。
    """
    ROUND_LOT = "ROUND_LOT"   # 整股：一律向下取整到張（1,000 股），絕不進位
    ODD_LOT = "ODD_LOT"       # 零股：允許 1 股为单位，但受 MIN_ORDER_THRESHOLD 攔截


# 金字塔階段佔「目標總部位」的比例（由黑板 current_position_ratio 承載進度）。
# 索引 0 = 尚未建倉時的下一階段（Trial）；最後一階段加碼完成後不再有加碼。
PYRAMID_TRIAL_RATIO: float = 0.50
PYRAMID_STAGES: Tuple[float, ...] = (0.50, 0.30, 0.20)


def next_pyramid_stage(current_ratio: float,
                       stages: Sequence[float] = PYRAMID_STAGES):
    """依現行部位比例回傳下一個金字塔階段 (stage_ratio, stage_index)。

    Args:
        current_ratio: 已建立部位佔目標總部位的「累計」比例（0~1）。
        stages:        階段序列，預設 (0.5, 0.3, 0.2)。

    Returns:
        (stage_ratio, index)；已滿倉或髒資料（負值/NaN/>1）回傳 (None, -1)。

    ⚠️ 修復（Issue #199）：原實作以「單階段比例」當門檻比較（cur < stage），
    導致 Trial 完成後（cur=0.5）對照 stage[1]=0.3 直接落空、回傳 (None, -1)，
    行為樹永遠無法進入 Add#1/Add#2 —— 正金字塔建倉在第二階段即中斷。
    正確語意是比對「累計進度門檻」cumulative[i] = Σ stages[:i+1]：
        cur < 0.5 ➜ Trial(0.5, idx 0)
        cur < 0.8 ➜ Add#1(0.3, idx 1)
        cur < 1.0 ➜ Add#2(0.2, idx 2)
    每階段門檻皆帶 1e-9 容差：浮點累加（0.5+0.3）可能得到 0.7999...，
    誤判會導致重複加碼同一階段（倒金字塔風險）。
    """
    try:
        cur = float(current_ratio)
    except (TypeError, ValueError):
        return None, -1
    if cur != cur or cur < 0 or cur > 1.0 + 1e-9:  # NaN / 負值 / 超額髒資料防護
        return None, -1
    cumulative = 0.0
    for i, stage in enumerate(stages):
        cumulative += float(stage)
        # 容差 1e-9：累計門檻的浮點誤差不得造成同階段重複加碼
        if cur < cumulative - 1e-9:
            return float(stage), i
    return None, -1


def blended_cost_price(old_shares: int, old_cost: float,
                       add_shares: int, add_price: float) -> float:
    """加碼後的加權平均成本（四捨五入至小數第 2 位）。

    非法輸入一律降級回舊成本，嚴禁拋出例外（邊界防護協定與 sizer 一致）。
    """
    try:
        if old_shares <= 0 or add_shares <= 0:
            return float(old_cost)
        total = (old_shares * old_cost + add_shares * add_price) / (old_shares + add_shares)
        return round(total, 2)
    except (TypeError, ValueError):
        return float(old_cost or 0.0)


def worst_case_loss_pct(cost: float, stop: float, weight: float) -> float:
    """單一部位的「最壞情況虧損佔帳戶比例」＝ (cost-stop)/cost × weight。

    供 AC 驗證：Break-Even 完成後的總虧損上界必須 ≤ 初始風險預算（1%）。
    """
    try:
        if cost <= 0 or weight <= 0:
            return 0.0
        return max(0.0, (cost - stop) / cost) * weight
    except (TypeError, ValueError):
        return 0.0


def break_even_stop_price(cost: float, buffer_pct: float = 0.003) -> float:
    """Break-Even 停損價 = 進場成本 × (1 + buffer_pct)。

    buffer 用於覆蓋手續費/交易稅，確保平倉後真正「保本」。
    非法輸入（<=0 / NaN / None）回傳 0.0，呼叫端據此判斷不可移動。
    """
    try:
        c, b = float(cost), float(buffer_pct)
    except (TypeError, ValueError):
        return 0.0
    if c != c or b != b or c <= 0:
        return 0.0
    return round(c * (1.0 + b), 2)


def trailing_stop_price(highest_since_entry: float, atr: float,
                        multiple: float = 2.5) -> float:
    """Trailing Stop 參考價 = 進場後最高價 − multiple × ATR（回撤 2.5×ATR 出場）。

    非法輸入（<=0 / NaN / None）回傳 0.0，交由呼叫端走「無追蹤停損」路徑。
    """
    try:
        hi, a, m = float(highest_since_entry), float(atr), float(multiple)
    except (TypeError, ValueError):
        return 0.0
    if hi != hi or a != a or m != m or hi <= 0 or a <= 0 or m <= 0:
        return 0.0
    return round(max(0.0, hi - m * a), 2)


def floor_to_lot(shares: float, lot_size: int = 1000) -> int:
    """整股模式向下取整到張（AC：1,342 股 ➜ 1,000 股，絕不進位）。

    加 1e-9 epsilon 消除浮點截斷誤差（與 calc_position_size 同款協定）；
    非法輸入回傳 0。
    """
    try:
        n, lot = float(shares), int(lot_size)
    except (TypeError, ValueError):
        return 0
    if n != n or n < 0 or lot <= 0:
        return 0
    return int(math.floor((n + 1e-9) / lot)) * lot


def apply_lot_mode(raw_shares: float, price: float, mode: str = "ROUND_LOT",
                   lot_size: int = 1000, min_order_amount: float = 5000.0):
    """整股/零股邊界處理（AC#4）。

    Args:
        raw_shares:      風控層算出的原始股數（可含小數）。
        price:           成交參考價。
        mode:            "ROUND_LOT" 向下取整到張；"ODD_LOT" 允許零股。
        lot_size:        一張股數（預設 1,000）。
        min_order_amount: 零股最低單筆金額（預設 5,000 元，防手續費低消侵蝕）。

    Returns:
        (final_shares, reject_reason)：
          - ROUND_LOT：1,342 ➜ 1,000；不足一張 ➜ (0, "REJECT_MIN_LOT")
          - ODD_LOT：487 股且金額 < 5,000 ➜ (0, "MIN_ORDER_THRESHOLD")
          - 合法 ➜ (shares, "")
        任何非法輸入一律降級 (0, reason)，嚴禁拋出例外。
    """
    try:
        n = float(raw_shares)
        p = float(price)
    except (TypeError, ValueError):
        return 0, "REJECT_INVALID_INPUT"
    if n != n or p != p or n <= 0 or p <= 0:
        return 0, "REJECT_INVALID_INPUT"

    mode_name = getattr(mode, "value", mode)  # LotMode Enum 或裸字串皆可
    if str(mode_name).upper() == LotMode.ROUND_LOT.value:
        shares = floor_to_lot(n, lot_size)
        if shares <= 0:
            logger.warning("[Lifecycle] ROUND_LOT 不足最低交易單位: raw=%s", n)
            return 0, "REJECT_MIN_LOT"
        return shares, ""

    # ---- ODD_LOT：零股模式，仍受最低單筆金額攔截 ----
    shares = int(n)
    if shares <= 0:
        return 0, "REJECT_MIN_LOT"
    if shares * p < min_order_amount:
        logger.warning("[Lifecycle] ODD_LOT 單筆金額 %.0f < %s，觸發 MIN_ORDER_THRESHOLD 攔截",
                       shares * p, min_order_amount)
        return 0, "MIN_ORDER_THRESHOLD"
    return shares, ""


def check_min_order_threshold(raw_shares: float, price: float,
                              lot_mode: str = "ROUND_LOT",
                              lot_size: int = 1000,
                              min_order_amount: float = 5000.0):
    """整股/零股邊界檢查（AC#4）：回傳 (可下單股數, 拒絕原因)。

    - ROUND_LOT：一律向下取整到張（1,342 ➜ 1,000）；不足一張 ➜ 拒单。
    - ODD_LOT：允許零股，但單筆金額 < min_order_amount ➜
      回傳 (0, "MIN_ORDER_THRESHOLD")，杜絕手續費低消侵蝕報酬。

    ⚠️ 相容性包裝：核心邏輯委派至 apply_lot_mode（單一實作來源，DRY）。
    """
    return apply_lot_mode(raw_shares, price, mode=lot_mode,
                          lot_size=lot_size, min_order_amount=min_order_amount)
