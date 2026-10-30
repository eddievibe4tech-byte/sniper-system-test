"""🛡️ Issue #1（量化引擎 v2.0）：雙重閥門部位計算器（Dual-Gate Position Sizer）。

散戶思維的致命盲點：只用「風險預算 ÷ 停損距離」算股數。
    → 低波動防禦股（如中華電）停損距離極小，會合法地吃掉帳戶 50%+ 資金，
      名目本金（Notional）完全失控，分散原則形同虛設。

機構級解法 —— 雙重閥門取最小值：
    shares_risk   = (Capital × risk_pct) / (atr_multiple × ATR)
    shares_notional = (Capital × max_weight) / Price
    Shares = floor(min(shares_risk, shares_notional))

高波動股被「風險閥門」縮倉；低波動股被「名目資本閥門」限制在單標上限內。
Pure Function：輸入全部顯式傳入、不碰全域狀態、不拋例外（邊界防護回傳 0）。
"""
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SizingResult:
    shares: int          # 建議股數（含整股向下取整）
    notional: float      # 名目金額
    gate: str            # 'RISK' / 'NOTIONAL_CAP' / 'REJECTED'
    reason: str = ""


def calc_position_size(
    capital: float,
    price: float,
    atr: float,
    risk_pct: float = 0.01,
    atr_multiple: float = 2.0,
    max_weight: float = 0.20,
    lot_size: int = 1000,
    round_to_lot: bool = True,
) -> SizingResult:
    """雙重閥門部位計算。

    Args:
        capital:      帳戶總資產（名目本金基數）。
        price:        當前股價。
        atr:          Wilder's ATR(n)，停損距離 = atr_multiple × atr。
        risk_pct:     單筆最大虧損佔總資產比例（預設 1%）。
        atr_multiple: 停損距離的 ATR 倍數（預設 2×）。
        max_weight:   單一標的名目資本上限佔總資產比例（預設 20%）。
        lot_size:     台股交易單位（預設 1000 股 = 1 張）。
        round_to_lot: True 時向下取整到整張；False 時允許零股（至少 1 股）。

    Returns:
        SizingResult。任何非法輸入（<=0）一律回傳 shares=0 並記錄 Warning，
        嚴禁拋出例外導致主流程崩潰。
    """
    # ---------- 邊界防護（AC: 不得拋出 Exception） ----------
    if capital <= 0 or price <= 0 or atr <= 0:
        msg = f"非法輸入已攔截: capital={capital}, price={price}, atr={atr}"
        logger.warning("[PositionSizer] %s", msg)
        return SizingResult(0, 0.0, "REJECTED", msg)

    stop_distance = atr * atr_multiple
    if stop_distance <= 0:
        msg = f"停損距離非正數: atr={atr}, multiple={atr_multiple}"
        logger.warning("[PositionSizer] %s", msg)
        return SizingResult(0, 0.0, "REJECTED", msg)

    # ---------- 閥門一：風險預算（Risk Budget Gate） ----------
    risk_budget = capital * risk_pct
    shares_risk = risk_budget / stop_distance

    # ---------- 閥門二：名目資本上限（Notional Cap Gate） ----------
    notional_budget = capital * max_weight
    shares_notional = notional_budget / price

    if shares_risk <= shares_notional:
        shares_raw, gate = shares_risk, "RISK"
    else:
        shares_raw, gate = shares_notional, "NOTIONAL_CAP"

    # ---------- 取整 ----------
    if round_to_lot and lot_size > 1:
        shares = int(shares_raw // lot_size) * lot_size
    else:
        shares = int(shares_raw)  # 零股模式：向下取整至個股

    if shares <= 0:
        msg = (f"計算股數不足最低交易單位: raw={shares_raw:.1f}, "
               f"gate={gate}, lot={lot_size}")
        logger.warning("[PositionSizer] %s", msg)
        return SizingResult(0, 0.0, "REJECTED", msg)

    return SizingResult(
        shares=shares,
        notional=round(shares * price, 2),
        gate=gate,
        reason=(f"risk_budget={risk_budget:.0f}, stop_dist={stop_distance:.2f}, "
                f"notional_cap={notional_budget:.0f}"),
    )
