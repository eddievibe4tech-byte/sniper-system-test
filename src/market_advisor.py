"""
市場策略顧問：整合加密貨幣四象限 + 台股分析 + 突破 Setup
輸出 data/advice.json，提供 5,000 元資金配置建議
用法：python -m src.market_advisor
"""
import json
import os
from datetime import datetime
from typing import Dict, List, Optional

BASE = os.path.join(os.path.dirname(__file__), "..", "data")
CAPITAL = 5000  # 單筆紀律上限


def load(name: str) -> Optional[Dict]:
    p = os.path.join(BASE, name)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def crypto_section(crypto: Optional[Dict]) -> Dict:
    """加密貨幣：篩出右側確認 / 黃金買點，給出配置"""
    if not crypto:
        return {
            "status": "missing",
            "msg": "crypto_candidates.json 不存在：請先修復並重跑 Daily Crypto Analysis",
            "fng": None,
            "market_scenario": None,
            "right_side": [],
            "golden": [],
            "action": "資料缺失，加密貨幣預算暫持穩定幣",
            "alloc": 0,
        }

    fng = (crypto.get("fear_greed_index") or {}).get("value")
    cands = crypto.get("candidates") or []
    right_side = [c for c in cands if c.get("scenario") == "右側確認"]
    golden = [c for c in cands if c.get("scenario") == "黃金買點"]

    # 依矩陣決定配置
    if fng is not None and fng > 75:
        action, alloc = "市場極度貪婪：不追高，預算持穩定幣", 0
    elif golden:
        action, alloc = f"黃金買點出現：{golden[0]['symbol']} 分 2 批進場", CAPITAL // 2
    elif right_side:
        top = right_side[0]
        action = (f"右側確認：{top['symbol']} 可進場；"
                  f"停損={top.get('stop_loss', '跌破 MA20')}；"
                  f"停利={top.get('take_profit', 'RSI>70 或 FNG>70')}")
        alloc = CAPITAL // 2
    else:
        action, alloc = "無符合情境標的：預算持穩定幣等待", 0

    return {
        "status": "ok",
        "fng": fng,
        "market_scenario": crypto.get("market_scenario"),
        "right_side": right_side,
        "golden": golden,
        "action": action,
        "alloc": alloc,
    }


def tw_section(deep: Optional[Dict], setup: Optional[Dict]) -> Dict:
    """台股：買入級標的 + 明日突破觀察"""
    if not deep:
        return {"status": "missing", "buy_level": [], "setups": [],
                "action": "deep_analysis.json 缺失", "alloc": 0}

    results = deep.get("all_results") or []
    buy_level = [r for r in results
                 if r.get("recommendation") in ("謹慎買入", "積極買入")
                 and (r.get("ev_score") or 0) >= 60]
    setups = (setup or {}).get("setups") or []

    if setups:
        s = setups[0]
        action = (f"明日盤中：{s['name']} 放量(量比>4)突破 {s['breakout_price']} 才進場；"
                  f"停損=突破價-2%")
        alloc = CAPITAL // 2
    elif buy_level:
        b = buy_level[0]
        action = (f"{b['name']} 達買入級(EV={b['ev_score']})；"
                  f"等回踩 MA20({b.get('ma20')}) 不破再進場")
        alloc = CAPITAL // 2
    else:
        action, alloc = "台股無買入級標的：預算保留", 0

    return {"status": "ok", "buy_level": buy_level, "setups": setups,
            "action": action, "alloc": alloc}


def main():
    crypto = load("crypto_candidates.json")
    deep = load("deep_analysis.json")
    setup = load("setup_watchlist.json")
    regime = load("regime.json")

    c = crypto_section(crypto)
    t = tw_section(deep, setup)

    # 剩餘預算回歸現金/穩定幣
    used = c["alloc"] + t["alloc"]
    advice = {
        "updated_at": datetime.now().isoformat(),
        "capital": CAPITAL,
        "tw_regime": (regime or {}).get("current_regime"),
        "crypto": c,
        "tw_stock": t,
        "cash": {
            "alloc": CAPITAL - used,
            "note": "未配置預算持現金/穩定幣，等待下一個情境訊號",
        },
        "rules": [
            "單筆 ≤ 5,000 元、同時 ≤ 3 部位",
            "不符合情境就不進場",
            "停損紀律優先於一切訊號",
        ],
    }

    out = os.path.join(BASE, "advice.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(advice, f, ensure_ascii=False, indent=2)

    # 終端摘要
    print("=" * 50)
    print(f"台股 Regime：{advice['tw_regime']}｜FNG：{c.get('fng')}")
    print(f"[加密] {c['action']}（配置 {c['alloc']}）")
    for coin in c["right_side"][:3]:
        print(f"   🔵 右側確認：{coin['symbol']} RSI={coin.get('rsi')} "
              f"勝率參考={coin.get('win_rate')}")
    print(f"[台股] {t['action']}（配置 {t['alloc']}）")
    print(f"[現金] 保留 {advice['cash']['alloc']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
