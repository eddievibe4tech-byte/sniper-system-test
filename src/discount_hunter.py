"""
🆕 特價獵人引擎 (Discount Hunter)
監控關注標的的現價，自動計算特價區間（小特價 -3%、大特價 -6%~-8%），
並根據現價與特價區間的距離，給出分批進場建議。
整合「二八分批法」：20-30% 先進場，70-80% 等特價。
"""
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)
TZ = timezone(timedelta(hours=8))
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 特價獵人監控池（可自由擴充）
HUNT_TARGETS = [
    {"symbol": "VOO", "name": "S&P 500 ETF", "small_discount": 0.03, "big_discount": 0.06},
    {"symbol": "JPM", "name": "摩根大通", "small_discount": 0.03, "big_discount": 0.08},
    {"symbol": "EOG", "name": "能源探勘", "small_discount": 0.03, "big_discount": 0.08},
    # 可自行加入台股標的（需加 .TW 後綴）
    # {"symbol": "006208.TW", "name": "富邦台50", "small_discount": 0.03, "big_discount": 0.06},
]


def fetch_current_prices(symbols: List[str]) -> Dict[str, float]:
    """批次抓取現價（group_by='ticker'，與 value_screener 一致；MultiIndex 層級為 ticker）"""
    prices = {}
    try:
        hist = yf.download(symbols, period="5d", group_by="ticker",
                           progress=False, threads=False, auto_adjust=True)
        for sym in symbols:
            try:
                if hist.empty:
                    break
                if isinstance(hist.columns, pd.MultiIndex):
                    if sym not in hist.columns.get_level_values(0):
                        continue
                    df = hist[sym]["Close"].dropna()
                elif sym in hist.columns:
                    df = hist[sym].dropna()
                else:
                    continue
                if not df.empty:
                    prices[sym] = round(float(df.iloc[-1]), 2)
            except Exception as e:
                logger.warning(f"{sym} 價格解析失敗: {e}")
    except Exception as e:
        logger.warning(f"價格抓取失敗: {e}")
    return prices


def calculate_discount_zones(price: float, small_pct: float, big_pct: float) -> Dict:
    """計算特價區間"""
    small_low = round(price * (1 - small_pct - 0.005), 2)   # 小特價下限（多 0.5% 緩衝）
    small_high = round(price * (1 - small_pct + 0.005), 2)  # 小特價上限
    big_low = round(price * (1 - big_pct - 0.01), 2)        # 大特價下限（多 1% 緩衝）
    big_high = round(price * (1 - big_pct + 0.01), 2)       # 大特價上限

    return {
        "small_zone": [small_low, small_high],
        "big_zone": [big_low, big_high],
        "small_discount_pct": round(small_pct * 100, 1),
        "big_discount_pct": round(big_pct * 100, 1),
    }


def judge_proximity(price: float, zones: Dict) -> Dict:
    """判定現價與特價區間的距離"""
    small_high = zones["small_zone"][1]
    big_high = zones["big_zone"][1]

    if price <= big_high:
        status = "🟢 已達大特價"
        action = "動用 70-80% 資金單筆加碼"
        proximity_pct = 0.0
    elif price <= small_high:
        status = "🟡 已達小特價"
        action = "動用 20-30% 資金分批買入"
        proximity_pct = 0.0
    else:
        # 計算現價距離小特價上限的百分比距離：(現價 - 小特價上限) / 現價 × 100%
        dist_to_small = (price - small_high) / price * 100
        proximity_pct = round(dist_to_small, 1)
        if proximity_pct < 2.0:
            status = "⚠️ 接近特價"
            action = "準備資金，設定到價通知"
        else:
            status = "⏳ 尚未特價"
            action = "繼續定期定額，耐心等待"

    return {"status": status, "action": action, "proximity_pct": proximity_pct}


def run_discount_hunter(budget: float = None):
    """執行特價獵人引擎

    預算優先級：函式參數 > 環境變數 HUNT_BUDGET > 預設 10000
    """
    logging.basicConfig(level=logging.INFO)
    logger.info("🎯 啟動特價獵人引擎...")

    if budget is None:
        budget = float(os.getenv("HUNT_BUDGET", "10000"))
    logger.info(f"預算: ${budget:,.0f}")

    symbols = [t["symbol"] for t in HUNT_TARGETS]
    prices = fetch_current_prices(symbols)

    results = []
    for target in HUNT_TARGETS:
        sym = target["symbol"]
        price = prices.get(sym)
        if not price:
            continue

        zones = calculate_discount_zones(price, target["small_discount"], target["big_discount"])
        proximity = judge_proximity(price, zones)

        # 二八分批法：20-30% 先進場，70-80% 等特價
        first_batch = round(budget * 0.25)   # 25% 先進場
        hunt_reserve = round(budget * 0.75)  # 75% 等特價

        results.append({
            "symbol": sym.replace(".TW", ""),
            "name": target["name"],
            "current_price": price,
            "zones": zones,
            "proximity": proximity,
            "budget_allocation": {
                "first_batch": first_batch,
                "hunt_reserve": hunt_reserve,
            },
            "limit_order_suggestion": {
                "small": f"限價 ${zones['small_zone'][0]}~{zones['small_zone'][1]}",
                "big": f"限價 ${zones['big_zone'][0]}~{zones['big_zone'][1]}",
            }
        })

    # 排序：優先顯示已達特價或接近特價的標的
    results.sort(key=lambda x: (
        0 if "已達" in x["proximity"]["status"] else
        1 if "接近" in x["proximity"]["status"] else 2,
        x["proximity"]["proximity_pct"]
    ))

    output = {
        "updated_at": datetime.now(TZ).isoformat(),
        "targets": results,
        "strategy_note": "二八分批法：25% 先進場避免踏空，75% 留作特價狩獵金",
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "discount_hunter.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"✅ 特價獵人完成：監控 {len(results)} 檔標的")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="🎯 特價獵人引擎 (Discount Hunter)")
    parser.add_argument("--budget", type=float, default=None,
                        help="總預算（優先於環境變數 HUNT_BUDGET，預設 10000）")
    args = parser.parse_args()
    run_discount_hunter(budget=args.budget)
