"""chart_api 單元測試：對應 Issue AC（記憶窗口 / 遲滯緩衝帶 / 階梯網格 / Adj Close）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from chart_api import (  # noqa: E402
    ChartSeries,
    build_chart_payload_from_series,
    compute_grid_levels,
    hysteresis_stage,
    oversold_memory_bands,
    rolling_mdd,
    wilder_rsi,
)


def _series(closes, opens=None):
    n = len(closes)
    opens = opens or closes
    return ChartSeries(
        timestamps=[i * 86400 for i in range(n)],
        open_adj=list(opens),
        high_adj=[max(o, c) * 1.01 for o, c in zip(opens, closes)],
        low_adj=[min(o, c) * 0.99 for o, c in zip(opens, closes)],
        close_adj=list(closes),
        volume=[1000.0] * n,
    )


class TestMemoryBands:
    """AC: 記憶窗口渲染測試 — RSI 第 1 天跌破 30，第 1~5 天高亮；第 6 天起消失。"""

    def test_band_covers_exactly_5_days(self):
        rsi = [None] * 14 + [35, 28, 45, 50, 55, 60] + [50] * 20
        bands = oversold_memory_bands(rsi)
        assert len(bands) == 1
        b = bands[0]
        assert b["start"] == 15          # 跌破 30 的那一天
        assert b["end"] == 15 + 5 - 1    # 往後共 5 個交易日
        # 第 6 天（index 20）不在任何 band 內
        assert not any(bs <= 20 <= be for bs, be in [(x["start"], x["end"]) for x in bands])

    def test_no_oversold_no_band(self):
        assert oversold_memory_bands([50.0] * 30) == []


class TestHysteresis:
    """AC: 遲滯灰色帶展示測試 — MDD 於 -7%~-10% 震盪時維持前一狀態不切換。"""

    def test_buffer_zone_keeps_previous_state(self):
        mdd = [-0.05, -0.08, -0.095, -0.09, -0.08, -0.06]  # 全在緩衝區/正常區
        states = hysteresis_stage(mdd)
        # Normal -> 進入 -7%~-10% 緩衝帶仍維持 Normal，不被洗盤升級
        assert states == ["Normal"] * 6

    def test_stage_upgrade_only_past_minus_10(self):
        mdd = [-0.05, -0.09, -0.11, -0.095, -0.12]
        states = hysteresis_stage(mdd)
        assert states[0] == "Normal"
        assert states[1] == "Normal"      # 緩衝帶內不切換
        assert states[2] == "Stage 1"     # 突破 -10% 升級
        assert states[3] == "Stage 1"     # 回落緩衝帶維持 Stage 1
        assert states[4] == "Stage 1"

    def test_deep_stages(self):
        assert hysteresis_stage([-0.20]) == ["Stage 2"]
        assert hysteresis_stage([-0.30]) == ["Stage 3"]
        assert hysteresis_stage([-0.40]) == ["Stage 4"]


class TestGridLevels:
    """AC: 階梯網格動態對齊 — Stage 1~4 依過去 252 日滾動高點計算。"""

    def test_grid_from_rolling_high(self):
        closes = [100.0] + [90.0] * 10
        g = compute_grid_levels(closes)
        assert g["rolling_high"] == 100.0
        assert g["stage1_price"] == 90.0   # -10%
        assert g["stage2_price"] == 85.0   # -15%
        assert g["stage3_price"] == 75.0   # -25%
        assert g["stage4_price"] == 65.0   # -35%

    def test_grid_follows_new_high(self):
        closes = [100.0] * 50 + [120.0] + [110.0] * 5
        g = compute_grid_levels(closes)
        assert g["rolling_high"] == 120.0  # 新高後網格整體上移


class TestAdjClosePayload:
    """AC: 除息無斷層驗證 — payload 的 K 線欄位命名為 ohlc_adj，強制 Adj Close 來源。"""

    def test_payload_shape_matches_spec(self):
        closes = [100 - i * 0.1 for i in range(300)]
        p = build_chart_payload_from_series("0050.TW", _series(closes))
        assert set(p) >= {"symbol", "market_state", "risk_circuit_breaker",
                          "cooldown_remaining_hours", "series", "grid_levels", "events"}
        s = p["series"]
        assert set(s) >= {"timestamps", "ohlc_adj", "ma20", "ma200", "rsi14", "rolling_mdd"}
        assert len(s["ohlc_adj"]) == 300
        assert all(len(row) == 4 for row in s["ohlc_adj"])

    def test_rsi_drops_below_30_on_selloff(self):
        closes = [100.0] * 30 + [100 - i * 1.2 for i in range(1, 25)]
        rsi = wilder_rsi(closes)
        assert min(x for x in rsi if x is not None) < 30

    def test_rolling_mdd_negative_and_monotonic_check(self):
        closes = [100.0, 95.0, 90.0, 99.0]
        mdd = rolling_mdd(closes)
        assert mdd[0] == 0.0
        assert abs(mdd[2] - (-0.10)) < 1e-9

    def test_events_respect_position_cap(self):
        """AC(Issue3 整合): 事件部位不得出現 100%，上限 20%。"""
        closes = [100.0] * 20 + [100 - i * 0.6 for i in range(1, 40)]
        opens = [c * 1.005 for c in closes]  # 紅K情境：open < close
        series = ChartSeries(
            timestamps=[i * 86400 for i in range(len(closes))],
            open_adj=[c * 1.01 for c in closes],   # open 高於 close => 黑K，應被防接刀攔截
            high_adj=[c * 1.02 for c in closes],
            low_adj=[c * 0.99 for c in closes],
            close_adj=closes,
            volume=[2000.0] * len(closes),
        )
        p = build_chart_payload_from_series("2881.TW", series)
        # 開高走低黑 K → 行為樹裝飾器阻斷，不發出 BUY
        assert p["events"] == []
        # 改紅 K（close > open）後可有事件，且部位 ≤ 20%
        series.open_adj = [c * 0.99 for c in closes]
        p2 = build_chart_payload_from_series("2881.TW", series)
        for ev in p2["events"]:
            pct = float(ev["position_pct"].rstrip("%"))
            assert pct <= 20.0
