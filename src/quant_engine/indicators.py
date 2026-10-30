"""🧮 Issue #1（量化引擎 v2.0）：Wilder's ATR 技術指標模組。

為什麼不能用 SMA？
    傳統 ATR(14) 若以簡單移動平均實作，一旦出現單日跳空（gap），
    該極端 TR 值會在視窗內「停留整整 14 天」後才瞬間消失，造成風險
    估計的階梯效應（staircase effect）——部位大小會跟著無謂震盪。

机构級標準（TradingView / TA-Lib 預設）採用 Wilder's Smoothing：
    ATR_t = (ATR_{t-1} × (n - 1) + TR_t) / n
    等價於 pandas 的 ewm(alpha=1/n, adjust=False)。

本模組為 Pure Logic（純函數、零副作用），符合 PR 拆分計畫中
「Issue 1: 數學與過濾層」的職責邊界。
"""
from typing import List, Sequence

import pandas as pd


def wilder_atr(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    period: int = 14,
) -> pd.Series:
    """計算 Wilder's Smoothed ATR（與 TradingView 標準 ATR(n) 一致）。

    Args:
        high / low / close: 等長的价格序列（依時間由舊到新排序）。
        period: ATR 週期，預設 14。

    Returns:
        與輸入等長的 pd.Series；資料不足 period 根 K 線時前段為 NaN。

    Raises:
        ValueError: 序列長度不一致或 period < 1。
    """
    if not (len(high) == len(low) == len(close)):
        raise ValueError("high/low/close 序列長度必須一致")
    if period < 1:
        raise ValueError(f"period 必須 >= 1，收到 {period}")

    h = pd.Series(high, dtype="float64")
    l = pd.Series(low, dtype="float64")
    c = pd.Series(close, dtype="float64")

    prev_c = c.shift(1)
    # True Range = max(H-L, |H-prevC|, |L-prevC|)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    # 第一根 K 線沒有前收盤，TR 退化为 H-L
    tr.iloc[0] = h.iloc[0] - l.iloc[0]

    # Wilder's Smoothing == EMA with alpha = 1/period（非調整式）
    atr = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return atr


def wilder_atr_last(*args, **kwargs) -> float:
    """回傳最新一筆 ATR 值；資料不足或異常時回傳 0.0（嚴禁拋出例外）。

    下游 PositionSizer 對 atr <= 0 有邊界防護，回傳 0 會被安全攔截。
    """
    try:
        series = wilder_atr(*args, **kwargs)
        last = series.iloc[-1] if len(series) else float("nan")
        if pd.isna(last) or last <= 0:
            return 0.0
        return float(last)
    except Exception:  # noqa: BLE001 — 數學層任何異常都降級為 0，交由風控層攔截
        return 0.0


def sma(values: Sequence[float], period: int) -> List[float]:
    """簡單移動平均（供 MA20 斜率等過濾條件使用）。

    Returns:
        與輸入等長的列表；不足 period 筆的前段填 NaN。
    """
    s = pd.Series(values, dtype="float64")
    return list(s.rolling(window=period, min_periods=period).mean())
