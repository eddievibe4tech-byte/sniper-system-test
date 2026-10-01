"""
美股動能海選引擎
四象限決策矩陣：VIX（市場情緒）× RSI（個股動能）
情境：黃金買點 / 動量突破 / 極度危險 / 中性
財報風險守衛：財報前 3 日內自動降級為觀望
輸出：data/us_candidates.json
"""
import json
import os
from datetime import datetime
from typing import Dict, List, Optional

from us_client import USClient

US_RULES = {
    "vix_panic": 30.0,        # VIX > 30 極度恐慌
    "vix_calm": 20.0,         # VIX < 20 樂觀環境
    "vix_complacent": 15.0,   # VIX < 15 極度自滿
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "rsi_extreme": 80,
    "near_high_pct": 3.0,     # 距 52 週高點 < 3% 視為突破區
    "earnings_guard_days": 3, # 財報前 3 日不進新倉
}

SCENARIO_META = {
    "黃金買點": {
        "recommendation": "積極買入",
        "win_rate": "參考 ~70%（恐慌超賣均值回歸，歷史參考）",
        "position_size": "≤ 150 USD（約台幣 5,000），分 2 批，用零股",
        "stop_loss": "再跌 15% 停損",
        "take_profit": "RSI > 60 或 VIX < 20 分批獲利",
        "holding_period": "3-6 個月",
    },
    "動量突破": {
        "recommendation": "謹慎買入",
        "win_rate": "參考 ~55-60%（美股科技動量，歷史參考）",
        "position_size": "≤ 150 USD（約台幣 5,000），單批，用零股",
        "stop_loss": "跌破 MA50 或 -8% 停損",
        "take_profit": "MA50 移動停利，+25% 先減半",
        "holding_period": "1-3 個月",
    },
    "極度危險": {
        "recommendation": "避開",
        "win_rate": "參考 <30%（自滿超買追高）",
        "position_size": "0%",
        "stop_loss": "不適用",
        "take_profit": "已有部位考慮分批獲利",
        "holding_period": "不適用",
    },
    "中性": {
        "recommendation": "觀望",
        "win_rate": "-",
        "position_size": "等待明確訊號",
        "stop_loss": "不適用",
        "take_profit": "不適用",
        "holding_period": "不適用",
    },
    "資料不足": {
        "recommendation": "觀望（資料不足）",
        "win_rate": "-",
        "position_size": "0%（VIX 缺失，禁止進場）",
        "stop_loss": "不適用",
        "take_profit": "不適用",
        "holding_period": "不適用",
    },
}


def get_us_scenario(vix: Optional[float], rsi: float,
                    price_above_ma50: bool, dist_to_high_pct: float) -> str:
    # 🔴 P0 修正（#115）：VIX 缺失時不做任何象限判定——四象限規則全部以 VIX 為環境門檻，
    # 舊版預設 20.0 會在無資料時仍誤判「動量突破」→ 產生不該有的買入推薦。
    if vix is None:
        return "資料不足"
    # 象限 4：極度自滿 + 極度超買
    if vix < US_RULES["vix_complacent"] and rsi > US_RULES["rsi_extreme"]:
        return "極度危險"
    # 象限 1：極度恐慌 + 超賣（左側）
    if vix > US_RULES["vix_panic"] and rsi < US_RULES["rsi_oversold"]:
        return "黃金買點"
    # 象限 2：樂觀環境 + 強動能 + 接近新高（右側）
    if (vix <= US_RULES["vix_calm"]
            and US_RULES["rsi_oversold"] <= rsi <= US_RULES["rsi_overbought"]
            and price_above_ma50
            and dist_to_high_pct <= US_RULES["near_high_pct"]):
        return "動量突破"
    return "中性"


def run_us_screener() -> List[Dict]:
    print("🇺🇸 啟動美股動能海選引擎...")
    client = USClient()

    # 🔴 修正（#129）：get_vix() 改為多來源 fallback 鏈，回傳 {'value', 'source'}；
    # VIX 缺失時一律不產生買入推薦（見 get_us_scenario「資料不足」守門）。
    vix_data = client.get_vix()
    vix = vix_data["value"] if vix_data else None
    vix_source = vix_data["source"] if vix_data else None
    spy_rsi = client.get_index_rsi("spy")
    print(f"  VIX：{vix}（來源：{vix_source}）｜SPY RSI：{spy_rsi}")

    market_scenario = {
        "scenario": get_us_scenario(vix, spy_rsi or 50.0, True, 0.0),
        "vix": vix,
        "vix_source": vix_source,                    # 🆕 透明化來源
        "spy_rsi": spy_rsi,
        "note": "市場層級象限（VIX + SPY RSI）",
    }

    scanned = client.scan()
    if not scanned:
        print("❌ 無法取得美股市場數據")
        save_us_results([], vix, market_scenario, vix_source=vix_source, error="yfinance 數據取得失敗")
        return []

    candidates = []
    for sym, t in scanned.items():
        scenario = get_us_scenario(vix, t["rsi"], t["price_above_ma50"], t["dist_to_52w_high_pct"])
        # 🔴 P0 修正（#115）：VIX 缺失 → scenario="資料不足"，一律不收集為候選，
        # 確保無 VIX 資料當日不會產生任何買入推薦（舊版因預設 VIX=20 仍會誤出「動量突破」）。
        if scenario not in ("黃金買點", "動量突破", "極度危險"):
            continue  # 中性／資料不足不收集；極度危險保留作為持倉警告

        meta = SCENARIO_META[scenario]
        rec = meta["recommendation"]
        guard = False
        dte = t.get("days_to_earnings")

        # 財報風險守衛
        if rec in ("積極買入", "謹慎買入") and dte is not None and 0 <= dte < US_RULES["earnings_guard_days"]:
            rec = "觀望（財報即將公布）"
            guard = True

        candidates.append({
            "symbol": sym,
            "price": t["price"], "ma20": t["ma20"], "ma50": t["ma50"],
            "rsi": t["rsi"], "high_52w": t["high_52w"],
            "dist_to_52w_high_pct": t["dist_to_52w_high_pct"],
            "price_above_ma50": t["price_above_ma50"],
            "change_5d": t["change_5d"], "change_20d": t["change_20d"],
            "volatility_ann_pct": t["volatility_ann_pct"],
            "days_to_earnings": dte, "earnings_guard": guard,
            "vix": vix,
            "vix_source": vix_source,
            "scenario": scenario,
            "recommendation": rec,
            "win_rate": meta["win_rate"],
            "win_rate_note": "歷史參考值，非未來保證",
            "position_size": meta["position_size"],
            "stop_loss": meta["stop_loss"],
            "take_profit": meta["take_profit"],
            "holding_period": meta["holding_period"],
            "screened_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

    order = {"動量突破": 0, "黃金買點": 1, "極度危險": 2}
    candidates.sort(key=lambda x: (order[x["scenario"]], x["dist_to_52w_high_pct"]))
    save_us_results(candidates, vix, market_scenario, vix_source=vix_source)
    print(f"✅ 美股海選完成：{len(candidates)} 檔")
    return candidates


def save_us_results(candidates, vix, market_scenario, vix_source: Optional[str] = None,
                    error: Optional[str] = None) -> None:
    data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "us_candidates.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "candidates": candidates,
            "vix": vix,
            "vix_source": vix_source,               # 🆕 yfinance / yfinance_fast / cboe / fred
            "market_scenario": market_scenario,
            "error": error,
            "updated_at": datetime.now().isoformat(),
            "rules_applied": US_RULES,
        }, f, ensure_ascii=False, indent=2)
    print(f"📁 美股結果已儲存：{path}")


if __name__ == "__main__":
    tops = run_us_screener()
    print("\n🎯 美股候選 Top 5:")
    for i, c in enumerate(tops[:5], 1):
        print(f"  {i}. {c['symbol']} ${c['price']}｜{c['scenario']}｜RSI {c['rsi']}"
              f"｜距52週高 {c['dist_to_52w_high_pct']}%｜財報 {c['days_to_earnings']} 天")
