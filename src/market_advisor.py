"""
市場策略顧問 v2：加密四象限 + 美股動能 + 台股分析 + 突破 Setup
統一資金紀律：總 5,000 台幣、單筆 ≤ 2,500、最多 3 部位
輸出 data/advice.json
"""
import json
import os
from datetime import datetime
from typing import Dict, List, Optional

BASE = os.path.join(os.path.dirname(__file__), "..", "data")
CAPITAL = 5000
PER_POSITION = 2500
MAX_POSITIONS = 3


def load(name: str) -> Optional[Dict]:
    p = os.path.join(BASE, name)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def collect_opportunities(crypto, deep, setup, us) -> List[Dict]:
    opps = []

    # 加密貨幣
    if crypto:
        fng = (crypto.get("fear_greed_index") or {}).get("value")
        for c in crypto.get("candidates") or []:
            if c.get("scenario") == "黃金買點":
                opps.append({"prio": 0, "market": "加密", "label": f"黃金買點 {c.get('symbol')}",
                             "detail": f"FNG={fng}；{c.get('stop_loss')}；{c.get('take_profit')}"})
            elif c.get("scenario") == "右側確認":
                opps.append({"prio": 1, "market": "加密", "label": f"右側確認 {c.get('symbol')}",
                             "detail": f"FNG={fng}；RSI={c.get('rsi')}；{c.get('stop_loss')}"})

    # 美股
    if us:
        vix = us.get("vix")
        for c in us.get("candidates") or []:
            if c.get("earnings_guard"):
                continue
            if c.get("scenario") == "黃金買點":
                opps.append({"prio": 0, "market": "美股", "label": f"黃金買點 {c.get('symbol')}",
                             "detail": f"VIX={vix}；{c.get('stop_loss')}；{c.get('take_profit')}"})
            elif c.get("scenario") == "動量突破" and c.get("recommendation") == "謹慎買入":
                opps.append({"prio": 1, "market": "美股", "label": f"動量突破 {c.get('symbol')}",
                             "detail": f"VIX={vix}；RSI={c.get('rsi')}；{c.get('stop_loss')}"})

    # 台股：突破 Setup 優先於買入級
    if setup:
        for s in (setup.get("setups") or [])[:2]:
            opps.append({"prio": 2, "market": "台股", "label": f"突破觀察 {s.get('name')}",
                         "detail": f"盤中放量突破 {s.get('breakout_price')} 才進場；停損=突破價-2%"})
    if deep:
        for r in deep.get("all_results") or []:
            if r.get("recommendation") in ("謹慎買入", "積極買入") and (r.get("ev_score") or 0) >= 60:
                opps.append({"prio": 3, "market": "台股", "label": f"買入級 {r.get('name')}",
                             "detail": f"EV={r.get('ev_score')}；回踩 MA20({r.get('ma20')}) 不破再進場"})

    opps.sort(key=lambda x: x["prio"])
    return opps


def allocate(opps: List[Dict], capital: int = CAPITAL,
             per: int = PER_POSITION, max_pos: int = MAX_POSITIONS):
    plan, remaining = [], capital
    for o in opps:
        if len(plan) >= max_pos or remaining <= 0:
            break
        amt = min(per, remaining)
        plan.append({**o, "alloc": amt})
        remaining -= amt
    return plan, remaining


def main():
    crypto = load("crypto_candidates.json")
    deep = load("deep_analysis.json")
    setup = load("setup_watchlist.json")
    us = load("us_candidates.json")
    regime = load("regime.json")

    opps = collect_opportunities(crypto, deep, setup, us)
    plan, cash = allocate(opps)

    warnings = []
    if us:
        dangers = [c["symbol"] for c in us.get("candidates") or [] if c.get("scenario") == "極度危險"]
        if dangers:
            warnings.append(f"🔴 美股極度危險象限：{', '.join(dangers)} — 已有部位考慮分批獲利")
        guards = [f"{c['symbol']}({c['days_to_earnings']}天後)" for c in us.get("candidates") or [] if c.get("earnings_guard")]
        if guards:
            warnings.append(f"⚠️ 財報 3 日內不進新倉：{', '.join(guards)}")
    if crypto and (crypto.get("fear_greed_index") or {}).get("value", 0) > 75:
        warnings.append("🔴 加密 FNG>75 極度貪婪：不追高")

    advice = {
        "updated_at": datetime.now().isoformat(),
        "capital": CAPITAL,
        "tw_regime": (regime or {}).get("current_regime"),
        "vix": (us or {}).get("vix"),
        "fng": ((crypto or {}).get("fear_greed_index") or {}).get("value"),
        "plan": plan,
        "cash": {"alloc": cash, "note": "未配置預算持現金/穩定幣，等待下一個情境訊號"},
        "warnings": warnings,
        "rules": [
            f"總預算 {CAPITAL} 台幣、單筆 ≤ {PER_POSITION}、最多 {MAX_POSITIONS} 部位",
            "不符合情境就不進場",
            "停損紀律優先於一切訊號",
            "美股用零股；財報前 3 日不進新倉",
        ],
    }

    out = os.path.join(BASE, "advice.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(advice, f, ensure_ascii=False, indent=2)

    print("=" * 56)
    print(f"台股 Regime {advice['tw_regime']}｜FNG {advice['fng']}｜VIX {advice['vix']}")
    for w in warnings:
        print(w)
    if not plan:
        print("今日無符合情境：全數持現金")
    for p in plan:
        print(f"[{p['market']}] {p['label']} → {p['alloc']} 元｜{p['detail']}")
    print(f"[現金] 保留 {cash} 元")
    print("=" * 56)


if __name__ == "__main__":
    main()
