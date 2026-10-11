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
MODE_NAME = {1.0: "full(全額)", 1.5: "boost(加碼)", 0.5: "half_split(半額+活存低接)", 0.0: "stop_reserve(停扣轉活存)"}


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


def run_dca_allocator(monthly_budget: int = 10000):
    logging.basicConfig(level=logging.INFO)
    rate = load_json(DATA_DIR / "rate_cycle.json", {})
    phase = rate.get("rate_cycle", {}).get("phase", "中性盤整")
    key = phase.split(":")[0].strip()
    bond_mult = BOND_MULT.get(key, 0.5)

    bond = next(r for r in PORTFOLIO if r["role"] == "rate_bet")
    tech = fetch_bond_technical(bond["tw"], rate)

    # 🟢 低接燈號：乖離<-3% 或 60日新低 或 Phase4 → 觸發
    dip, reasons = False, []
    if tech.get("bias_ma200_pct", 0) < -3:
        dip, _ = True, reasons.append(f"00687B 乖離 MA200 達 {tech['bias_ma200_pct']}%，波段低檔")
    if tech.get("at_60d_low"):
        dip, _ = True, reasons.append("00687B 觸及 60 日新低，恐慌錯殺")
    if key == "Phase 4":
        dip, _ = True, reasons.append("利率週期 Phase4 通膨反撲，債價承壓即低接時機")

    roles, calendar, bond_saved = [], {}, 0.0
    for r in PORTFOLIO:
        base = monthly_budget * r["pct"]
        if r["role"] == "rate_bet":
            final = base * bond_mult
            bond_saved = base - final          # 債券省下的轉活存
        elif r["role"] == "reserve":
            final = base + bond_saved          # 銀彈＝基礎＋債券省下
        else:
            final = base
        per_day = round(final / len(r["days"])) if r["days"] else 0
        roles.append({**{k: r[k] for k in ("role", "code", "name", "pct", "days")},
                      "base_amount": round(base), "final_amount": round(final), "per_day": per_day})
        for d in r["days"]:
            calendar.setdefault(d, []).append({"code": r["code"], "amount": per_day})

    result = {
        "updated_at": datetime.now(TZ).isoformat(),
        "monthly_budget": monthly_budget,
        "phase": phase,
        "bond_mode": MODE_NAME.get(bond_mult, "half_split(半額+活存低接)"),
        "bond_multiplier": bond_mult,
        "roles": roles,
        "dip_buy_signal": {"triggered": dip, "reason": "；".join(reasons) or "未觸發，維持紀律扣款",
                           "suggested_amount": round((monthly_budget * 0.05 + bond_saved) * 3)},
        "calendar": [{"day": d, "items": v} for d, v in sorted(calendar.items())],
        "bond_technical": tech,
    }
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "dca_plan.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("✅ DCA 計畫產出：債券模式=%s，低接燈號=%s", result["bond_mode"], "🟢觸發" if dip else "⚪未觸發")


if __name__ == "__main__":
    run_dca_allocator()
