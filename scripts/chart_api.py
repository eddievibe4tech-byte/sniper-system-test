"""互動式量化決策圖表後端 API（Issue: feat(dashboard) Interactive Strategy & Decision Visualizer）

提供 GET /api/v1/chart/{symbol} 資料端點，將大盤狀態機（Market FSM）、雙層濾網事件鏈、
階梯回撤網格（MDD Grid）與風控斷路器全數投影為前端 Lightweight Charts 可消費的 JSON。

設計要點（對應 Issue AC）：
- 除息無斷層：K 線強制使用 Adj Close（還原除息），杜絕 0050/006208 除息跳空假破線。
- 階梯網格動態對齊：以過去 252 交易日滾動高點計算 Stage 1~4 水平價格線。
- RSI 超賣記憶窗口：RSI(14) Wilder's 跌破 30 後的 5 個交易日標記 memory band。
- 遲滯緩衝帶：MDD 在 -7% ~ -10% 區間震盪時維持前一狀態不切換（Hysteresis Zone）。

本模組為純函式 + FastAPI router，可被 src/main.py 或獨立服務掛載；
無外部網路依賴時可用 ``build_chart_payload_from_series`` 直接餵入 OHLCV 序列（利於測試）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常數：與三層治理架構規格一致
# ---------------------------------------------------------------------------

ROLLING_WINDOW = 252          # 滾動高點視窗（約一年交易日）
MA_SHORT = 20                 # 短期突破防守線
MA_MID = 60                   # 季線（中期多空線）
MA_LONG = 200                 # 年線（宏觀牛熊分界 + 乖離率基準）
RSI_PERIOD = 14               # Wilder's RSI
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0
OVERSOLD_MEMORY_DAYS = 5      # 超賣記憶窗口（事件鏈第一階段）
HYSTERESIS_UPPER = -0.07      # 遲滯緩衝帶上緣（MDD 高於此為 Normal）
HYSTERESIS_LOWER = -0.10      # 遲滯緩衝帶下緣（MDD 低於此才允許升級 Stage 1）

# 階梯回撤網格：Stage -> (回撤起點, 回撤終點, 資金釋放比例, 顏色, 標籤)
GRID_STAGES: List[Tuple[str, float, float, float, str, str]] = [
    ("stage1", -0.10, -0.15, 0.20, "#ff9800", "釋放 20% 資金"),
    ("stage2", -0.15, -0.25, 0.30, "#e65100", "釋放 30% 資金"),
    ("stage3", -0.25, -0.35, 0.30, "#d32f2f", "釋放 30% 資金"),
    ("stage4", -0.35, -1.00, 0.20, "#4a148c", "末日彈藥 20%"),
]


# ---------------------------------------------------------------------------
# 指標計算（Wilder's 平滑，零外部依賴）
# ---------------------------------------------------------------------------

def wilder_rsi(closes: Sequence[float], period: int = RSI_PERIOD) -> List[Optional[float]]:
    """14 日 Wilder's RSI，前 period 根回傳 None 對齊時間軸。"""
    n = len(closes)
    rsi: List[Optional[float]] = [None] * n
    if n <= period:
        return rsi
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    rsi[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, n):
        delta = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        rsi[i] = _rsi_value(avg_gain, avg_loss)
    return rsi


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def moving_average(values: Sequence[float], window: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < window:
        return out
    total = sum(values[:window])
    out[window - 1] = round(total / window, 3)
    for i in range(window, len(values)):
        total += values[i] - values[i - window]
        out[i] = round(total / window, 3)
    return out


def rolling_mdd(closes: Sequence[float], window: int = ROLLING_WINDOW) -> List[float]:
    """近 252 日滾動高點回撤曲線（<=0，單位為小數比率）。"""
    out: List[float] = []
    n = len(closes)
    for i in range(n):
        start = max(0, i - window + 1)
        hh = max(closes[start:i + 1])
        mdd = (closes[i] - hh) / hh if hh > 0 else 0.0
        out.append(round(mdd, 5))
    return out


def atr20(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
          period: int = MA_SHORT) -> List[Optional[float]]:
    """20 日 ATR（Wilder），供前端 Tooltip 顯示部位權重。"""
    n = len(closes)
    trs: List[float] = []
    for i in range(n):
        prev_c = closes[i - 1] if i > 0 else closes[i]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - prev_c), abs(lows[i] - prev_c)))
    atr: List[Optional[float]] = [None] * n
    if n < period:
        return atr
    atr[period - 1] = round(sum(trs[:period]) / period, 4)
    for i in range(period, n):
        atr[i] = round((atr[i - 1] * (period - 1) + trs[i]) / period, 4)
    return atr


# ---------------------------------------------------------------------------
# 階梯網格 / 記憶窗口 / 遲滯狀態
# ---------------------------------------------------------------------------

def compute_grid_levels(closes: Sequence[float]) -> Dict[str, float]:
    """依過去 252 日滾動高點動態計算 Stage 1~4 水平價格線。"""
    if not closes:
        return {}
    rh = max(closes[-ROLLING_WINDOW:])
    levels = {"rolling_high": round(rh, 2)}
    boundaries = {"stage1_price": -0.10, "stage2_price": -0.15,
                  "stage3_price": -0.25, "stage4_price": -0.35}
    for key, depth in boundaries.items():
        levels[key] = round(rh * (1.0 + depth), 2)
    return levels


def oversold_memory_bands(rsi: Sequence[Optional[float]]) -> List[Dict[str, int]]:
    """RSI 跌破 30 後往後 5 個交易日的記憶窗口索引區間（Pane 2 陰影渲染用）。"""
    bands: List[Dict[str, int]] = []
    n = len(rsi)
    i = 0
    while i < n:
        if rsi[i] is not None and rsi[i] < RSI_OVERSOLD:
            end = min(i + OVERSOLD_MEMORY_DAYS - 1, n - 1)
            if bands and bands[-1]["end"] >= i - 1 and bands[-1]["end"] < end:
                bands[-1]["end"] = end  # 重疊窗口合併
            else:
                bands.append({"start": i, "end": end})
        i += 1
    return bands


def hysteresis_stage(mdd_series: Sequence[float]) -> List[str]:
    """Rolling MDD 的多級狀態序列。

    遲滯邏輯：
    - MDD > -7% → Normal
    - -10% < MDD ≤ -7% → 維持前一狀態（緩衝帶，防止邊界洗盤）
    - MDD ≤ -10% → Stage 1 或更深
    """
    states: List[str] = []
    prev = "Normal"
    for mdd in mdd_series:
        if mdd > HYSTERESIS_UPPER:                    # > -7%
            state = "Normal"
        elif mdd > HYSTERESIS_LOWER:                  # -7% ~ -10% 遲滯緩衝帶
            state = prev                              # 維持原狀態不切換
        elif mdd > -0.15:
            state = "Stage 1"
        elif mdd > -0.25:
            state = "Stage 2"
        elif mdd > -0.35:
            state = "Stage 3"
        else:
            state = "Stage 4"
        states.append(state)
        prev = state
    return states


# ---------------------------------------------------------------------------
# Payload 組裝
# ---------------------------------------------------------------------------

@dataclass
class ChartSeries:
    """輸入：已確認來源為 Adj Close 的 OHLCV 序列。"""
    timestamps: List[int]
    open_adj: List[float]
    high_adj: List[float]
    low_adj: List[float]
    close_adj: List[float]
    volume: List[float] = field(default_factory=list)


def build_chart_payload_from_series(
    symbol: str,
    series: ChartSeries,
    *,
    market_state: str = "Normal",
    risk_circuit_breaker: str = "Normal",
    cooldown_remaining_hours: int = 0,
    capital_pool_used_pct: float = 0.0,
) -> Dict[str, Any]:
    """由原始序列產生 Issue 規格定义的 GET /api/v1/chart/{symbol} Response Payload。"""
    closes = series.close_adj
    ma20 = moving_average(closes, MA_SHORT)
    ma60 = moving_average(closes, MA_MID)
    ma200 = moving_average(closes, MA_LONG)
    rsi14 = wilder_rsi(closes, RSI_PERIOD)
    mdd = rolling_mdd(closes)
    stages = hysteresis_stage(mdd)
    grid = compute_grid_levels(closes)
    bands = oversold_memory_bands(rsi14)
    vol_ma20 = moving_average(series.volume, MA_SHORT) if series.volume else [None] * len(closes)
    atr = atr20(series.high_adj, series.low_adj, closes) if series.high_adj else [None] * len(closes)

    events: List[Dict[str, Any]] = []
    for i in range(len(closes)):
        if stages[i] in {"Stage 1", "Stage 2", "Stage 3", "Stage 4"}:
            # 行為樹防接刀裝飾器：紅 K（Close > Open）+ 放量（Volume > MA20 Volume）
            red_k = closes[i] > series.open_adj[i]
            vol_ok = (vol_ma20[i] is not None and series.volume
                      and series.volume[i] > vol_ma20[i])
            chain_active = any(b["start"] <= i <= b["end"] for b in bands) or (
                rsi14[i] is not None and rsi14[i] < RSI_OVERSOLD)
            if red_k and vol_ok and chain_active:
                pos = _position_pct(stages[i], atr[i], closes[i])
                events.append({
                    "timestamp": series.timestamps[i],
                    "type": "BUY",
                    "stage": stages[i],
                    "position_pct": pos,
                    "reason": f"{stages[i]} 觸發且通過事件鏈+防接刀驗證（紅K+放量）",
                })

    payload: Dict[str, Any] = {
        "symbol": symbol,
        "market_state": market_state,
        "risk_circuit_breaker": risk_circuit_breaker,
        "cooldown_remaining_hours": cooldown_remaining_hours,
        "capital_pool": {"used_pct": round(capital_pool_used_pct, 1)},
        "series": {
            "timestamps": series.timestamps,
            "ohlc_adj": [
                [round(series.open_adj[i], 3), round(series.high_adj[i], 3),
                 round(series.low_adj[i], 3), round(closes[i], 3)]
                for i in range(len(closes))
            ],
            "ma20": ma20,
            "ma60": ma60,
            "ma200": ma200,
            "rsi14": rsi14,
            "rolling_mdd": mdd,
            "hysteresis_state": stages,
            "oversold_memory_bands": bands,
            "atr20": atr,
        },
        "grid_levels": grid,
        "grid_meta": [
            {"stage": s[0], "range": [s[1], s[2]], "release_pct": s[3],
             "color": s[4], "label": s[5]} for s in GRID_STAGES
        ],
        "events": events,
    }
    return payload


def _position_pct(stage: str, atr: Optional[float], close: float) -> str:
    """ATR 動態部位：單檔上限 20%，依階段與波動率調整。

    修正 Code Review Critical Bug 2：原本 base 30% 被 min(base, 0.20) 一律截斷，
    導致各 Stage 失去分級差異。現改為定義時即確保 ≤ 20% 上限：
    - Stage 1 初步進場保守 15%；Stage 2/3 深度回撤加碼 20%；Stage 4 末日彈藥 15%。
    """
    base = {
        "Stage 1": 0.15,  # 初步進場，保守
        "Stage 2": 0.20,  # 深度回撤，加碼（單檔上限 20%）
        "Stage 3": 0.20,  # 深度回撤，加碼（單檔上限 20%）
        "Stage 4": 0.15,  # 末日彈藥，保守
    }[stage]

    capped = base  # 已在定義時確保 ≤ 20%

    if atr and close > 0:
        # ATR% 2% 為基準：高波動（ATR% > 2%）減少部位，低波動最多加成至 1.2x
        atr_pct = atr / close
        vol_factor = max(0.5, min(1.2, 0.02 / atr_pct))
        capped = round(capped * vol_factor, 3)
        capped = min(capped, 0.20)  # 確保不超過單檔 20% 上限

    return f"{round(capped * 100, 1)}%"


# ---------------------------------------------------------------------------
# FastAPI Router（選用；未安裝 fastapi 時不影響純函式測試）
# ---------------------------------------------------------------------------

try:  # pragma: no cover
    from fastapi import APIRouter, HTTPException, Query

    router = APIRouter(prefix="/api/v1", tags=["chart"])

    @router.get("/chart/{symbol}")
    def get_chart(
        symbol: str,
        timeframe: str = Query("1d", pattern="^1d$"),
        range: str = Query("1y", pattern="^(1y|3y|5y)$"),
    ) -> Dict[str, Any]:
        """圖表聚合端點。資料管線須從 FinMind/Yahoo 取 Adj Close 後餵入。"""
        loader = getattr(get_chart, "_series_loader", None)
        if loader is None:
            raise HTTPException(503, "Chart data loader not configured")
        try:
            series: ChartSeries = loader(symbol, timeframe=timeframe, range=range)
        except KeyError:
            raise HTTPException(404, f"Unknown symbol {symbol}")
        years = {"1y": 252, "3y": 756, "5y": 1260}[range]
        s = ChartSeries(
            timestamps=series.timestamps[-years:],
            open_adj=series.open_adj[-years:],
            high_adj=series.high_adj[-years:],
            low_adj=series.low_adj[-years:],
            close_adj=series.close_adj[-years:],
            volume=series.volume[-years:],
        )
        return build_chart_payload_from_series(symbol, s)

except ImportError:  # pragma: no cover
    router = None


if __name__ == "__main__":  # smoke test
    import json as _json
    ts = list(range(0, 300 * 86400, 86400))
    closes = [100 + 10 * math.sin(i / 20) - i * 0.02 for i in range(300)]
    demo = ChartSeries(ts, [c * 0.99 for c in closes], [c * 1.01 for c in closes],
                       [c * 0.98 for c in closes], closes, [1000 + i % 700 for i in range(300)])
    p = build_chart_payload_from_series("0050.TW", demo, market_state="Bull Pullback")
    print(_json.dumps({k: v for k, v in p.items() if k != "series"}, ensure_ascii=False, indent=2))
    print("events:", len(p["events"]), "| bands:", p["series"]["oversold_memory_bands"][:3])
