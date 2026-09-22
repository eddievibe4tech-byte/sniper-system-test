"""
路徑 A：盤後突破 Setup 偵測器
用盤後數據找出「明天可能觸發突破」的標的，輸出 data/setup_watchlist.json
"""
import json
import os
from datetime import datetime
from typing import Dict, List
from src.finmind_client import FinMindClient

SETUP_RULES = {
    "near_high_pct": 3.0,      # 收盤距 20 日高點 < 3% = 突破在即
    "lookback_high": 20,
    "volume_ratio_min": 2.0,   # 當日量 / 20 日均量
    "tech_score_min": 60,
    "rsi_max": 70,             # 排除已超買
}

def _tech_score(rsi: float, macd: float, above_ma20: bool, vol_ratio: float) -> int:
    s = 0
    if above_ma20: s += 30
    if macd > 0:   s += 25
    if 40 <= rsi <= 65: s += 25
    if vol_ratio >= 2.0: s += 20
    return s

def detect_setups(finmind: FinMindClient, stocks: List[Dict]) -> List[Dict]:
    out = []
    for s in stocks:
        code = s["code"]
        rows = finmind._make_request("TaiwanStockPrice", code, days=40) or []
        if len(rows) < 21:
            continue
        closes = [float(r["close"]) for r in rows]
        vols   = [float(r.get("trading_volume", 0)) for r in rows]
        close = closes[-1]
        # 計算過去 20 天高點（排除今天），尋找「即將突破」的 Setup
        high20 = max(closes[-(SETUP_RULES["lookback_high"] + 1):-1])
        avg_vol = sum(vols[-21:-1]) / 20
        vol_ratio = vols[-1] / avg_vol if avg_vol > 0 else 0

        # 條件：接近突破位 + 量能擴增
        if close < high20 * (1 - SETUP_RULES["near_high_pct"] / 100):
            continue
        if vol_ratio < SETUP_RULES["volume_ratio_min"]:
            continue

        tech = finmind.get_technical_indicators(code)
        if not tech or tech["rsi"] > SETUP_RULES["rsi_max"]:
            continue
        score = _tech_score(tech["rsi"], tech["macd"], tech["price_above_ma20"], vol_ratio)
        if score < SETUP_RULES["tech_score_min"]:
            continue

        out.append({
            "code": code,
            "name": s["name"],
            "close": round(close, 2),
            "breakout_price": round(high20, 2),
            "distance_to_break_pct": round((high20 / close - 1) * 100, 2),
            "volume_ratio": round(vol_ratio, 2),
            "tech_score": score,
            "rsi": tech["rsi"],
            "warrant_confirmed": None,   # TODO: 接入證交所權證申購量後改為 bool
            "trigger_note": f"次日盤中放量 (量比>4) 突破 {round(high20,2)} 才觸發；停損＝突破價 -2%",
            "screened_at": datetime.now().isoformat(),
        })
    out.sort(key=lambda x: (-x["tech_score"], x["distance_to_break_pct"]))
    return out[:10]

def main():
    base = os.path.join(os.path.dirname(__file__), "..", "data")
    finmind = FinMindClient()
    
    # 使用 with 確保檔案正確關閉
    with open(os.path.join(base, "stock_pool.json"), encoding="utf-8") as f:
        pool = json.load(f)
    
    setups = detect_setups(finmind, pool.get("stocks", []))
    
    with open(os.path.join(base, "setup_watchlist.json"), "w", encoding="utf-8") as f:
        json.dump({"setups": setups, "rules": SETUP_RULES,
                   "updated_at": datetime.now().isoformat()},
                  f, ensure_ascii=False, indent=2)
    
    print(f"✅ Setup 偵測完成：{len(setups)} 檔")

if __name__ == "__main__":
    main()
