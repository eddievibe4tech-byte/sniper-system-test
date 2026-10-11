"""
🆕 定期定額配置引擎 (DCA Allocator)
整合三大策略：
  1. 鐵三角配置（進攻 00981 / 收息 00878 / 降息博弈 00687B / 銀彈活存）
  2. 拆單多日扣款（平滑單月波動，避免扣在波段高點）
  3. 半額扣款＋活存低接（防守期債券減碼，留銀彈等破底單筆加碼）
讀取 rate_cycle.json 判斷債券乘數，並監控 00687B 技術面觸發「低接燈號」。
"""
import json
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

import yfinance as yf

logger = logging.getLogger(__name__)
TZ = timezone(timedelta(hours=8))
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 🟢 鐵三角＋銀彈配置（代碼集中管理，若券商顯示不同請改此處）
PORTFOLIO = [
    {"role": "growth",   "code": "00981",  "tw": "00981.TW",  "name": "凱基TOP50(進攻)",   "pct": 0.475, "days": [6, 20]},
    {"role": "income",   "code": "00878",  "tw": "00878.TW",  "name": "國泰永續高股息(收息)", "pct": 0.30,  "days": [6, 20]},
    {"role": "rate_bet", "code": "00687B", "tw": "00687B.TW", "name": "國泰20年美債(降息博弈)", "pct": 0.175, "days": [6, 16, 26]},
    {"role": "reserve",  "code": "CASH",   "tw": None,        "name": "高利活存(銀彈)",     "pct": 0.05,  "days": []},
]

# 利率週期 → 債券扣款乘數（半額+低接為防守預設）
BOND_MULT = {"Phase 1": 1.0, "Phase 2": 1.5, "Phase 3": 0.5, "Phase 4": 0.0, "中性盤整": 0.5}
# 白話模式文案（取代工程師代碼）
MODE_LABEL = {
    1.0: "全額扣款，正常佈局",
    1.5: "加碼扣款，降息確認",
    0.5: "半額扣款，其餘轉活存",
    0.0: "定期暫停，轉存活存",      # 🔴 取代 stop_reserve(乘數 0x)
}


def load_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except Exception:
        return default


def fetch_bond_technical(tw_code, rate_data):
    """抓 00687B 技術面；失敗時以 TLT 作為長債代理"""
    try:
        df = yf.Ticker(tw_code).history(period="1y")
        close = df["Close"].dropna()
        if not close.empty and len(close) >= 60:
            cur = float(close.iloc[-1])
            ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else float(close.mean())
            return {"price": round(cur, 3),
                    "bias_ma200_pct": round((cur - ma200) / ma200 * 100, 2),
                    "at_60d_low": cur <= float(close.tail(60).min()) * 1.01}
    except Exception as e:
        logger.warning("00687B 技術面抓取失敗：%s", e)
    tlt = (rate_data.get("macro_data") or {}).get("TLT") or {}
    if tlt:
        return {"price": tlt.get("price"), "bias_ma200_pct": tlt.get("bias_ma200_pct"),
                "at_60d_low": False, "proxy": "TLT"}
    return {}


def plain_signal(tech: dict, phase_key: str) -> str:
    """🟢 把技術數據翻譯成人話結論"""
    msgs = []
    bias = tech.get("bias_ma200_pct")
    if bias is not None and bias < -3:
        msgs.append(f"美債跌破年線 {bias}%（已達波段超跌甜蜜點，適合進場撿便宜）")
    elif bias is not None and bias > 5:
        msgs.append(f"美債高於年線 +{bias}%（偏貴，暫不加碼）")
    if tech.get("at_60d_low"):
        msgs.append("觸及 60 日新低，市場恐慌錯殺")
    if phase_key == "Phase 4":
        msgs.append("通膨反撲期，債價承壓＝低接時機")
    return "；".join(msgs) or "未觸發低接，維持紀律扣款"


def run_dca_allocator(monthly_budget: int = 10000):
    logging.basicConfig(level=logging.INFO)
    rate = load_json(DATA_DIR / "rate_cycle.json", {})
    phase = rate.get("rate_cycle", {}).get("phase", "中性盤整")
    key = phase.split(":")[0].strip()
    bond_mult = BOND_MULT.get(key, 0.5)

    bond = next(r for r in PORTFOLIO if r["role"] == "rate_bet")
    tech = fetch_bond_technical(bond["tw"], rate)
    price = tech.get("price") or 0

    # 低接燈號
    dip = (tech.get("bias_ma200_pct", 0) < -3) or tech.get("at_60d_low", False) or (key == "Phase 4")

    roles, calendar, bond_saved, paused_notes = [], {}, 0.0, []
    for r in PORTFOLIO:
        base = monthly_budget * r["pct"]
        orig_days = list(r["days"])

        if r["role"] == "rate_bet":
            final = base * bond_mult
            bond_saved = base - final
            if bond_mult == 0:                      # 🔴 停扣：清空扣款日，日曆自動過濾
                days, status, status_label = [], "paused", "⏸ 暫停中"
                paused_notes.append(f"{r['code']} 已暫停，{'、'.join(str(d)+'號' for d in orig_days)}本月不扣款")
            else:
                days, status, status_label = orig_days, "normal", "正常扣款"
        elif r["role"] == "reserve":
            final, days, status, status_label = base + bond_saved, [], "reserve", "留存活存備用"
        else:
            final, days, status, status_label = base, orig_days, "normal", "正常扣款"

        per_day = round(final / len(days)) if days else 0
        roles.append({"role": r["role"], "code": r["code"], "name": r["name"], "pct": r["pct"],
                      "base_amount": round(base), "final_amount": round(final),
                      "days": days, "per_day": per_day, "status": status, "status_label": status_label})
        for d in days:                              # 🟢 只有未暫停的標的會進日曆
            calendar.setdefault(d, []).append({"code": r["code"], "amount": per_day})

    # 🟢 置頂動作清單：定期 vs 手動 分離
    auto_dca = [{"code": x["code"],
                 "action": "暫停扣款（App 內點選暫停）" if x["status"] == "paused"
                           else f"維持正常扣款（${x['final_amount']:,}）"}
                for x in roles if x["role"] != "reserve"]
    manual = []
    if dip:
        amt = round((monthly_budget * 0.05 + bond_saved) * 3)
        shares = int(amt // price) if price else None
        manual.append({"code": bond["code"], "action": "手動買進", "amount": amt,
                       "detail": f"動用活存備用金，手動下單「{bond['code']} 盤中零股」約 ${amt:,}"
                                 + (f"（約 {shares} 股）" if shares else "")})

    result = {
        "updated_at": datetime.now(TZ).isoformat(),
        "monthly_budget": monthly_budget,
        "phase": phase,
        "bond_multiplier": bond_mult,
        "bond_mode_label": MODE_LABEL.get(bond_mult, "半額扣款，其餘轉活存"),
        "action_items": {"auto_dca": auto_dca, "manual": manual},
        "signal": {"triggered": dip, "plain": plain_signal(tech, key),
                   "technical": f"乖離 MA200 {tech.get('bias_ma200_pct')}%"},
        "roles": roles,
        "calendar": [{"day": d, "items": v} for d, v in sorted(calendar.items())],
        "calendar_note": "；".join(paused_notes),
        "bond_technical": tech,
    }
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "dca_plan.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("✅ DCA 產出：模式=%s｜低接=%s", result["bond_mode_label"], dip)


if __name__ == "__main__":
    run_dca_allocator()
