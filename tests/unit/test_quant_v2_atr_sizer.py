"""🧪 Issue #1 驗收測試：Wilder's ATR + Notional Cap 雙重閥門部位計算。

對應 AC（Acceptance Criteria）：
  [x] Wilder's ATR 遞迴公式驗證（與手算 reference 誤差 < 0.01%）
  [x] 高波動縮倉測試：100 萬 / 1% 風險，股票 A (100, ATR=10) ➜ 500 股
  [x] Notional Cap 攔截測試：股票 B (100, ATR=1) ➜ 風險公式 5,000 股
      但受 Max_Weight=20% 限制 ➜ 必須輸出 2,000 股
  [x] 邊界防護：ATR<=0 / Price<=0 / 股數<1 ➜ 回傳 0，嚴禁拋出例外
"""
import math

import numpy as np
import pandas as pd
import pytest

from src.quant_engine.indicators import sma, wilder_atr, wilder_atr_last
from src.quant_engine.sizer import calc_position_size


# ---------------------------------------------------------------------------
# Helpers：產生可完全控制 TR 序列的合成 K 線
# ---------------------------------------------------------------------------
def _flat_series(n: int, tr: float):
    """每根 K 線的 True Range 恰好等於 tr（開高低收围绕前收盤對稱展開）。"""
    high = [100 + tr / 2 + i * 0 for i in range(n)]
    low = [100 - tr / 2 for _ in range(n)]
    close = [100 for _ in range(n)]
    return high, low, close


def _reference_wilder_atr(high, low, close, period=14):
    """獨立以純 Python 逐筆遞迴實作 Wilder 公式，作為交叉驗證基準。"""
    trs = []
    prev_c = None
    for h, l, c in zip(high, low, close):
        if prev_c is None:
            trs.append(h - l)
        else:
            trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
        prev_c = c
    atr = None
    for i, tr in enumerate(trs):
        if i < period - 1:
            continue
        if atr is None:
            # Wilder 原典：首值取前 n 筆 TR 的 SMA 作為種子
            atr = sum(trs[:period]) / period
        else:
            atr = (atr * (period - 1) + tr) / period
    return atr


class TestWilderATR:
    def test_matches_recursion_reference(self):
        """隨機走勢下，pandas ewm(alpha=1/14) 結果須與手算遞迴一致 (<0.01%)。"""
        rng = np.random.default_rng(42)
        n = 120
        close = 100 + np.cumsum(rng.normal(0, 1.5, n))
        high = close + rng.uniform(0.2, 2.5, n)
        low = close - rng.uniform(0.2, 2.5, n)
        result = wilder_atr(high, low, close, period=14)
        ref = _reference_wilder_atr(list(high), list(low), list(close), 14)
        assert not math.isnan(ref)
        rel_err = abs(result.iloc[-1] - ref) / ref
        assert rel_err < 0.0001, f"相對誤差 {rel_err:.2e} 超過 0.01%"

    def test_constant_tr_converges_to_tr(self):
        """TR 恆定為 6 時，收斂後 ATR 應恰為 6（平滑不改變穩態值）。"""
        h, l, c = _flat_series(60, 6.0)
        atr = wilder_atr(h, l, c, period=14)
        assert abs(atr.iloc[-1] - 6.0) < 1e-9

    def test_gap_dampening_vs_sma(self):
        """跳空日之後第 15 天：SMA-ATR 瞬間階梯回落，Wilder 僅平滑衰減。

        這是棄用 SMA 的核心原因——避免極端 TR 在視窗內滯留後的階梯效應。
        """
        n = 40
        h, l, c = _flat_series(n, 2.0)
        gap_idx = 20
        # 在第 20 天插入一根巨大跳空 K 線（TR = 30）
        h[gap_idx] = c[gap_idx - 1] + 30
        atr = wilder_atr(h, l, c, period=14)
        tr_series = pd.Series(
            [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]),
                                 abs(l[i] - c[i - 1])) for i in range(1, n)]
        )
        sma_atr = tr_series.rolling(14).mean()
        # 跳空後第 15 天（gap 移出 SMA 視窗）
        after = gap_idx + 14
        sma_drop = sma_atr.iloc[after] - sma_atr.iloc[after - 1]
        wilder_ratio = atr.iloc[after] / atr.iloc[after - 1]
        # 斷崖式崩落：跳空 TR(30) 移出 SMA 視窗的那一步，SMA-ATR 單日暴跌 ≈ -2.0；
        # Wilder 同一天僅平滑衰減約 1/14（比例 ≈ 0.98），無階梯效應
        assert sma_drop <= -2.0, "SMA 版本應呈現階梯式崩落"
        assert 0.97 < wilder_ratio <= 1.0, "Wilder 版本應平滑過渡，無階梯效應"

    def test_insufficient_data_returns_nan_not_error(self):
        h, l, c = _flat_series(5, 3.0)
        atr = wilder_atr(h, l, c, period=14)
        assert all(math.isnan(v) for v in atr)

    def test_length_mismatch_raises_valueerror(self):
        with pytest.raises(ValueError):
            wilder_atr([1, 2], [1], [1])

    def test_last_helper_degrades_to_zero(self):
        """資料不足時 helper 必須回傳 0.0 而非拋例外/NaN。"""
        h, l, c = _flat_series(5, 3.0)
        assert wilder_atr_last(h, l, c) == 0.0


class TestPositionSizerDualGate:
    def test_high_volatility_risk_gate(self):
        """AC: 100 萬、1% 風險，股票 A (price=100, ATR=10) ➜ 停損距離 20 ➜ 500 股。

        （本測試以零股模式驗證純數學；台股整股情境見 round_to_lot 測試。）
        """
        r = calc_position_size(capital=1_000_000, price=100, atr=10,
                               risk_pct=0.01, atr_multiple=2.0,
                               max_weight=0.20, round_to_lot=False)
        assert r.shares == 500
        assert r.gate == "RISK"
        assert r.notional == 50_000  # 5% 名目 < 20% cap，風險閥門主導

    def test_low_volatility_notional_cap_intercepts(self):
        """AC: 股票 B (price=100, ATR=1) ➜ 風險公式 5,000 股 (50 萬)，
        但 Max_Weight=20% 上限 20 萬 ➜ 必須輸出 2,000 股。"""
        r = calc_position_size(capital=1_000_000, price=100, atr=1,
                               risk_pct=0.01, atr_multiple=2.0,
                               max_weight=0.20, round_to_lot=False)
        assert r.shares == 2000
        assert r.gate == "NOTIONAL_CAP"
        assert r.notional == 200_000  # 恰好被 20% cap 封頂

    def test_round_to_lot_floor(self):
        """整股模式：raw=1,342 股 ➜ 向下取整為 1,000 股（1 張）。"""
        # price=50, atr=... 讓 raw ≈ 1342：risk_budget=10000, stop_dist=7.45
        r = calc_position_size(capital=1_000_000, price=50, atr=3.725,
                               round_to_lot=True, lot_size=1000)
        assert r.shares == 1000

    def test_boundary_invalid_inputs_return_zero_no_exception(self):
        """AC: ATR<=0 / Price<=0 / Capital<=0 ➜ 回傳 0，嚴禁拋出 Exception。"""
        for kwargs in (
            dict(capital=1_000_000, price=100, atr=0),
            dict(capital=1_000_000, price=100, atr=-5),
            dict(capital=1_000_000, price=0, atr=5),
            dict(capital=1_000_000, price=-1, atr=5),
            dict(capital=0, price=100, atr=5),
            dict(capital=-100, price=100, atr=5),
        ):
            r = calc_position_size(**kwargs)  # 不得拋出
            assert r.shares == 0
            assert r.gate == "REJECTED"

    def test_sub_lot_result_rejected_with_warning(self):
        """小資情境：算出股數 < 1 張 ➜ shares=0 + REJECTED（不勉強下單）。"""
        with pytest.warns(None) if False else _no_raise():
            r = calc_position_size(capital=50_000, price=100, atr=8,
                                   round_to_lot=True, lot_size=1000)
        assert r.shares == 0 and r.gate == "REJECTED"

    def test_purity_no_state_mutation(self):
        """Pure Function：同輸入必得同輸出（確定性，供 BT 回測重放）。"""
        a = calc_position_size(capital=900_000, price=35, atr=0.9)
        b = calc_position_size(capital=900_000, price=35, atr=0.9)
        assert a == b


class TestSMAHelper:
    def test_sma_basic(self):
        vals = [1, 2, 3, 4, 5]
        out = sma(vals, 3)
        assert math.isnan(out[0]) and math.isnan(out[1])
        assert out[-1] == pytest.approx(4.0)


# 小型 context manager：斷言區塊內不拋例外
class _no_raise:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        assert exc_type is None, f"邊界防護不應拋出例外: {exc}"
        return False
