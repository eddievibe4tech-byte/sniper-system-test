"""
市場策略顧問 v3：加密四象限 + 美股動能 + 台股 Alpha 候選 + 台股分析 + 突破 Setup
統一資金紀律：總 5,000 台幣、單筆 ≤ 2,500、最多 3 部位
權證為「執行載具」而非信號來源：僅在股票信號觸發後附權證選項（≤3,000 元、≤10 天、-30% 停損）
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


def _ma20_gap_pct(current_price, ma20):
    """計算現價對 MA20 的乖離率（%）；資料缺損回傳 None。"""
    try:
        if not ma20 or current_price is None:
            return None
        return (float(current_price) / float(ma20) - 1) * 100
    except (TypeError, ValueError):
        return None


def attach_warrant_options(opps: List[Dict], warrants: Optional[Dict]) -> List[Dict]:
    """權證是「執行載具」不是「信號來源」：只為已存在的股票機會附上權證選項，
    絕不憑權證資料自行新增部位。warrant_candidates.json 不存在時自動略過。"""
    if not warrants or warrants.get("error"):
        return opps
    wmap = {w.get("stock_code"): w for w in warrants.get("candidates") or []}
    for o in opps:
        w = wmap.get(o.get("code"))
        if not w:
            continue
        parts = []
        if w.get("effective_leverage") is not None:
            parts.append(f"槓桿{w['effective_leverage']}x")
        if w.get("days_to_expiry") is not None:
            parts.append(f"到期{w['days_to_expiry']}天")
        if w.get("bid_ask_spread_pct") is not None:
            parts.append(f"價差{w['bid_ask_spread_pct']}%")
        o["warrant_option"] = (
            f"權證載具：{w.get('warrant_code')} " + "、".join(parts) +
            "；≤3,000 元、≤10 個交易日、-30% 無條件停損（需股票信號已觸發才進場）")
    return opps


def get_etf_migration_opportunities(deep, etf_radar):
    """
    🆕 資產搬家引擎（#166）：評估個股是否過熱，並尋找 ETF 抄底機會
    - 個股 RSI > 70 或 MA20 乖離 > 10% → 停利訊號
    - 核心台股 ETF (0050/006208) 策略含「加碼」或「佈局」→ 甜蜜點買進訊號
    三情境：🔄 資產搬家 (prio=0) / ⚠️ 個股停利擁抱現金 (prio=0) / 🇹🇼 逢低佈局 ETF (prio=1)
    """
    opps = []
    if not etf_radar or not deep:
        return opps

    # 1. 找出需要停利的過熱個股 (RSI > 70 或 乖離 > 10%)
    hot_stocks = []
    for r in deep.get('all_results', []) or []:
        rsi = r.get('rsi', 50)
        price = r.get('current_price', 0)
        ma20 = r.get('ma20', 0)
        bias = ((price - ma20) / ma20 * 100) if ma20 > 0 else 0

        # 只要 RSI 超買或乖離過大，就列入潛在停利名單
        if rsi > 70 or bias > 10:
            hot_stocks.append({
                "code": r.get('code'),
                "name": r.get('name'),
                "rsi": rsi,
                "bias": round(bias, 1)
            })

    # 2. 找出適合抄底的台股核心 ETF (0050, 006208)
    cheap_etfs = []
    for etf in etf_radar.get('etf_monitor', []) or []:
        if etf.get('symbol') in ['0050', '006208']:
            strategy = etf.get('strategy', '')
            # 只有出現「加碼」或「佈局」才視為買點
            if '加碼' in strategy or '佈局' in strategy:
                cheap_etfs.append(etf)

    # 3. 生成搬家建議 (最高優先級 prio=0)
    if hot_stocks:
        # 🆕 P1 排序：RSI 越高、乖離越大，越優先需要停利搬家
        # （all_results 順序為分析完成序/代碼序，不代表過熱程度）
        hot_stocks.sort(key=lambda x: x['rsi'] + x['bias'], reverse=True)
        target = hot_stocks[0]  # 取過熱程度最高者作為代表
        if cheap_etfs:
            # 找乖離最低（最便宜）的 ETF
            best_etf = min(cheap_etfs, key=lambda x: x.get('bias_ma200_pct', 999))
            opps.append({
                "prio": 0,
                "market": "🔄 資產搬家",
                "label": f"停利 {target['name']} ➔ 買入 {best_etf['symbol']}",
                "detail": f"{target['name']} RSI={target['rsi']} 過熱/乖離+{target['bias']}%；"
                          f"{best_etf['symbol']} 乖離 {best_etf['bias_ma200_pct']}% 具備甜蜜點。",
            })
        else:
            opps.append({
                "prio": 0,
                "market": "⚠️ 個股停利",
                "label": f"停利 {target['name']} ➔ 擁抱現金",
                "detail": f"{target['name']} RSI={target['rsi']} 過熱，但核心 ETF 目前乖離偏高，"
                          f"建議先停利泊現金，勿追高 ETF。",
            })
    elif cheap_etfs:
        # 如果沒有過熱個股，但 ETF 便宜，則建議用閒置資金佈局
        best_etf = min(cheap_etfs, key=lambda x: x.get('bias_ma200_pct', 999))
        opps.append({
            "prio": 1,
            "market": "🇹🇼 台股 ETF",
            "label": f"逢低佈局 {best_etf['symbol']} ({best_etf['name']})",
            "detail": f"核心 ETF 出現甜蜜點，年線乖離 {best_etf['bias_ma200_pct']}%，RSI {best_etf['rsi14']}。",
        })

    return opps


def collect_opportunities(crypto, deep, setup, us, alpha=None, warrants=None) -> List[Dict]:
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
                             "detail": f"VIX={vix if vix is not None else '-'}；{c.get('stop_loss')}；{c.get('take_profit')}"})
            elif c.get("scenario") == "動量突破" and c.get("recommendation") == "謹慎買入":
                opps.append({"prio": 1, "market": "美股", "label": f"動量突破 {c.get('symbol')}",
                             "detail": f"VIX={vix if vix is not None else '-'}；RSI={c.get('rsi')}；{c.get('stop_loss')}"})

    # 台股：突破 Setup 優先於買入級
    if setup:
        for s in (setup.get("setups") or [])[:2]:
            opps.append({"prio": 2, "market": "台股", "code": s.get("code"),
                         "label": f"突破觀察 {s.get('name')}",
                         "detail": f"盤中放量突破 {s.get('breakout_price')} 才進場；停損=突破價-2%"})

    # 🆕 台股 Alpha 候選（週頻硬規則：YoY>15%＋投信連買＋站 MA20）
    # 接上信號層 A → 配置層的缺口：Alpha 表「誰合格」必須進入資金分配視野，
    # 但「現在這個價格能不能買」由 MA20 乖離率把關（乖離 >3% 不追價）。
    if alpha and not alpha.get("error"):
        for c in (alpha.get("candidates") or [])[:3]:
            gap = _ma20_gap_pct(c.get("current_price"), c.get("ma20"))
            base = f"YoY={c.get('revenue_yoy')}%、投信連買{c.get('inst_buy_days')}天；"
            if gap is None:
                note = base + "MA20 資料缺失：先人工確認均線再決定進場價"
            elif gap > 3:
                note = base + f"乖離+{gap:.1f}%>3%：不追價，等回踩 MA20({c.get('ma20')}) 止穩限價"
            else:
                note = base + f"乖離{gap:+.1f}%溫和，可於 {c.get('ma20')} 上方限價"
            opps.append({"prio": 2, "market": "台股", "code": c.get("code"),
                         "label": f"Alpha 候選 {c.get('name')}", "detail": note})

    # 原有 deep 買入級改為 prio 3（讓位給 Alpha 週頻硬規則）
    if deep:
        for r in deep.get("all_results") or []:
            if r.get("recommendation") in ("謹慎買入", "積極買入") and (r.get("ev_score") or 0) >= 60:
                opps.append({"prio": 3, "market": "台股", "code": r.get("code"),
                             "label": f"買入級 {r.get('name')}",
                             "detail": f"EV={r.get('ev_score')}；回踩 MA20({r.get('ma20')}) 不破再進場"})

    opps.sort(key=lambda x: x["prio"])
    # 🆕 權證載具提示（warrant_candidates.json 存在時；不存在自動隱藏）
    return attach_warrant_options(opps, warrants)


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
    # 🆕 v3：接上信號層 A（Alpha 週頻候選）與權證執行載具（模組未合併時為 None，自動隱藏）
    alpha = load("screener_candidates.json")
    warrants = load("warrant_candidates.json")

    # 🆕 載入跨市場 ETF 抄底雷達（#166：資產搬家引擎的資料來源）
    etf_radar = load("value_candidates.json")

    # 🆕 優先評估資產搬家機會 (prio=0)，排在所有機會最前面
    migration_opps = get_etf_migration_opportunities(deep, etf_radar)

    # 原有的機會收集
    core_opps = collect_opportunities(crypto, deep, setup, us, alpha=alpha, warrants=warrants)

    # 合併機會列表（搬家建議排最前面）
    opps = migration_opps + core_opps
    plan, cash = allocate(opps)

    warnings = []
    if us:
        dangers = [c["symbol"] for c in us.get("candidates") or [] if c.get("scenario") == "極度危險"]
        if dangers:
            warnings.append(f"🔴 美股極度危險象限：{', '.join(dangers)} — 已有部位考慮分批獲利")
        guards = [f"{c['symbol']}({c['days_to_earnings']}天後)" for c in us.get("candidates") or [] if c.get("earnings_guard")]
        if guards:
            warnings.append(f"⚠️ 財報 3 日內不進新倉：{', '.join(guards)}")
    # 🆕 VIX 缺失：動量突破情境判定用了保守預設值，需人工確認後再執行
    # 🔴 修正（#129）：海選端已改為「VIX 缺失 → 情境＝資料不足 → 零候選」，
    # 此處區分三種情況：完全無數據檔 vs. 舊版格式（#131 合併前產出、無 vix_source 欄位，
    # 屬「資料過期」而非今日抓取失敗）vs. 新版格式下四來源全失敗。
    # 🆕 修正（#134）：舊版格式若直接誤報「四來源全失敗」會混淆成因；
    # 並附上 updated_at 讓 operator 一眼看出資料新鮮度（來源透明化原則）。
    _stale_note = f"（最後更新：{us.get('updated_at') or '未知'}）" if us else ""
    if not us:
        warnings.append("⚠️ 美股 VIX 缺失：動量突破訊號請先人工確認 VIX<20 再執行")
    elif "vix_source" not in us:
        warnings.append(f"⚠️ 美股數據檔為舊版格式（無 vix_source 欄位）{_stale_note}："
                        "非今日四來源抓取失敗，請先手動觸發 Daily US Momentum Analysis 更新資料後重跑")
    elif us.get("vix") is None or (us.get("market_scenario") or {}).get("scenario") == "資料不足":
        warnings.append(f"⚠️ 美股 VIX 四來源全失敗{_stale_note}：本日不產出美股買入訊號（collect_opportunities 已自動排除）")
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
            "Alpha 候選只答「誰合格」：MA20 乖離 >3% 不追價，等回踩止穩",
            "權證是執行載具非信號來源：股票信號觸發後才進權證，≤3,000 元、≤10 個交易日、-30% 無條件停損",
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
        if p.get("warrant_option"):
            print(f"    🎟️ {p['warrant_option']}")
    print(f"[現金] 保留 {cash} 元")
    print("=" * 56)


if __name__ == "__main__":
    main()
