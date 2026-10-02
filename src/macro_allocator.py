"""
宏觀資產配置引擎 (Macro Allocator) — Issue #136
結合 Regime 識別與動量輪動，輸出戰術資產配置 (TAA) 建議。

金融系統鐵律：權重計算必須是「確定性的數學規則」（避免 AI 幻覺），
市場解讀與報告生成才交給 AI 語言模型（Groq）——AI 不參與任何數字計算。

三層決策模型：
1. 資產宇宙 (Multi-Asset Universe)＋戰略基礎權重 SAA
2. 宏觀體制識別 (Regime Detection)：VIX × SPY 趨勢 × 股債金相對動量
3. 再平衡觸發 (Rebalancing Trigger)：偏離目標權重 >5% 才行動

CFA 視角三大風險盲點修補（對應 Issue #136 Acceptance Criteria）：
- 摩擦成本與稅務：優先用「新增資金 (new_cash)」買入欠配資產，
  而非賣出獲利部位（延後短期資本利得稅事件）。
- Whipsaw 假訊號：新体制須「連續 3 天」出現才正式切換（hysteresis），
  避免 VIX 因單一新聞（如非農數據）瞬間飆高又回落導致兩面挨耳光。
- 相關性崩壞：VIX > 35 強制觸發「Cash is King」模式，SHY 權重 ≥80%
  （極端流動性危機時股、債、黃金會同時暴跌，只有現金有效）。

輸出 data/macro_allocation.json（前端儀表板「🌍 宏觀資產配置」區塊資料源）
"""
import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

BASE = os.path.join(os.path.dirname(__file__), "..", "data")

# ============================================================
# 1. 資產宇宙與戰略基礎權重 (Strategic Asset Allocation, SAA)
# ============================================================
ASSET_UNIVERSE = {
    "QQQ": {"class": "Equity_Growth", "base_weight": 0.30, "name": "科技成長"},
    "IWM": {"class": "Equity_Value",  "base_weight": 0.10, "name": "小型價值"},
    "TLT": {"class": "Bond_Long",     "base_weight": 0.20, "name": "長天期美債"},
    "SHY": {"class": "Cash_Equiv",    "base_weight": 0.20, "name": "短期美債/現金"},
    "GLD": {"class": "Commodity",     "base_weight": 0.10, "name": "黃金"},
    "VNQ": {"class": "RealEstate",    "base_weight": 0.10, "name": "房地產"},
}

REGIME_RISK_ON = "Risk-On (追逐風險)"
REGIME_RISK_OFF = "Risk-Off (避險模式)"
REGIME_STAGFLATION = "Stagflation (停滯性通膨)"
REGIME_CASH_IS_KING = "Cash is King (流動性危機)"
REGIME_NEUTRAL = "Neutral (震盪盤整)"

# 體制指示燈（前端用）
REGIME_LIGHT = {
    REGIME_RISK_ON: "🟢",
    REGIME_NEUTRAL: "🟡",
    REGIME_RISK_OFF: "🔴",
    REGIME_STAGFLATION: "🟠",
    REGIME_CASH_IS_KING: "⚫",
}

REGIME_PARAMS = {
    "vix_risk_on": 18.0,       # VIX < 18 → 追逐風險
    "vix_risk_off": 22.0,      # VIX > 22 → 避險模式
    "vix_cash_king": 35.0,     # VIX > 35 → 相關性崩壞，現金為王
    "confirm_days": 3,         # 新体制須連續 N 天才切換（Whipsaw 過濾）
    "rebalance_band": 0.05,    # 偏離目標權重 >5% 才觸發再平衡
    "cash_king_shy_min": 0.80, # Cash is King 時 SHY 最低權重
}


# ============================================================
# 2. 宏觀體制識別 (Regime Detection) —— 純函數、確定性規則
# ============================================================
def detect_regime(vix: Optional[float], spy_trend: Optional[float],
                  tlt_momentum: Optional[float], gld_momentum: Optional[float]) -> str:
    """
    判斷當前宏觀體制（原始信號，未經 Whipsaw 確認）。

    Args:
        vix: VIX 現值（缺資料回傳 Neutral，誠實標記而非猜測）
        spy_trend: SPY 對 MA200 的乖離率（%），>0 表多頭排列
        tlt_momentum: TLT 20 日報酬（小數），>0 表債券走強
        gld_momentum: GLD 20 日報酬（小數），>0 表黃金走強
    """
    if vix is None or spy_trend is None or tlt_momentum is None:
        return REGIME_NEUTRAL

    # 🔴 相關性崩壞防護：VIX > 35 時股債金可能同跌，只有現金有效（最高優先）
    if vix > REGIME_PARAMS["vix_cash_king"]:
        return REGIME_CASH_IS_KING

    spy_above_ma200 = spy_trend > 0

    if vix > REGIME_PARAMS["vix_risk_off"] and not spy_above_ma200 and tlt_momentum > 0:
        return REGIME_RISK_OFF
    if vix < REGIME_PARAMS["vix_risk_on"] and spy_above_ma200 and tlt_momentum < 0:
        return REGIME_RISK_ON
    if (gld_momentum or 0) > 0 and spy_trend < 0 and tlt_momentum < 0:
        # 股債雙殺、黃金獨強 → 停滯性通膨
        return REGIME_STAGFLATION
    return REGIME_NEUTRAL


def confirm_regime(regime_history: List[str],
                   confirm_days: int = REGIME_PARAMS["confirm_days"],
                   current_regime: Optional[str] = None) -> str:
    """
    Whipsaw 過濾器（移動平均式確認）：新体制必須「連續 N 天」出現才正式切換；
    否則維持上一個已確認的体制——避免 VIX 因單一新聞（如非農數據）瞬間飆高
    又回落，導致系統頻繁在 Risk-On / Risk-Off 之間兩面挨耳光。

    Args:
        regime_history: 由舊到新的每日原始体制清單（不含今日）
        current_regime: 今日原始体制；None 時以 history 最後一筆為準
    Returns:
        已確認（生效）的体制字串
    """
    history = [r for r in (regime_history or []) if r]
    raw_today = current_regime or (history[-1] if history else REGIME_NEUTRAL)

    # 今日訊號與昨日相同 → 直接沿用已生效体制（無需重新確認）
    if history and history[-1] == raw_today:
        return raw_today

    # 從後往前數 raw_today 的連續天數（含今日）
    streak = 1
    for r in reversed(history):
        if r == raw_today:
            streak += 1
        else:
            break
    if streak >= confirm_days:
        return raw_today

    # 未達門檻 → 回退到最近的「已連續 confirm_days 天」的旧体制
    for i in range(len(history) - 1, -1, -1):
        candidate = history[i]
        window = history[max(0, i - confirm_days + 1): i + 1]
        if len(window) >= confirm_days and all(w == candidate for w in window):
            return candidate
    return REGIME_NEUTRAL


# ============================================================
# 3. 戰術權重計算 (Tactical Asset Allocation) —— 確定性數學
# ============================================================
def calculate_tactical_weights(regime: str,
                               asset_momentums: Optional[Dict[str, float]] = None
                               ) -> Dict[str, float]:
    """
    根據 Regime 與各資產動量，計算戰術權重 (TAA)，正規化至總和 100%。

    Args:
        regime: 已確認的体制字串
        asset_momentums: {symbol: 0~1 正規化動量分數}；>0.8 強勢加碼 10%，
                         <0.2 弱勢減碼 10%（缺失不影響權重）
    """
    weights = {k: v["base_weight"] for k, v in ASSET_UNIVERSE.items()}

    # --- 体制調整 (Regime Tilts) ---
    if regime == REGIME_RISK_OFF:
        weights["QQQ"] *= 0.5   # 減碼科技股
        weights["IWM"] *= 0.5   # 減碼小型股
        weights["TLT"] *= 1.5   # 加碼長債
        weights["SHY"] *= 1.2   # 加碼現金
    elif regime == REGIME_RISK_ON:
        weights["QQQ"] *= 1.3
        weights["IWM"] *= 1.2
        weights["TLT"] *= 0.6
    elif regime == REGIME_STAGFLATION:
        weights["QQQ"] *= 0.6
        weights["TLT"] *= 0.6   # 通膨環境長債也受傷
        weights["GLD"] *= 2.0   # 黃金為王
        weights["SHY"] *= 1.3
    elif regime == REGIME_CASH_IS_KING:
        # 極端流動性危機：強制現金為王，其餘資產一律大幅縮減
        for sym in weights:
            if sym != "SHY":
                weights[sym] *= 0.1
        weights["SHY"] = max(weights["SHY"], REGIME_PARAMS["cash_king_shy_min"])

    # --- 動量微調 (Momentum Overlay) ---
    for symbol, momentum in (asset_momentums or {}).items():
        if symbol not in weights or momentum is None:
            continue
        if momentum > 0.8:      # 強勢動能
            weights[symbol] *= 1.1
        elif momentum < 0.2:    # 弱勢動能
            weights[symbol] *= 0.9

    # --- 正規化確保總和 100%；Cash is King 時保底 SHY ≥80% ---
    total = sum(weights.values())
    normalized = {k: v / total for k, v in weights.items()}
    if regime == REGIME_CASH_IS_KING and normalized["SHY"] < REGIME_PARAMS["cash_king_shy_min"]:
        deficit = REGIME_PARAMS["cash_king_shy_min"] - normalized["SHY"]
        normalized["SHY"] = REGIME_PARAMS["cash_king_shy_min"]
        others_total = sum(v for k, v in normalized.items() if k != "SHY")
        if others_total > 0:
            for k in normalized:
                if k != "SHY":
                    normalized[k] -= deficit * normalized[k] / others_total

    return {k: round(v, 4) for k, v in normalized.items()}


# ============================================================
# 4. 再平衡行動清單 (Rebalancing Actions) —— 含稅務優化
# ============================================================
def generate_rebalancing_actions(target_weights: Dict[str, float],
                                 current_holdings: Dict[str, float],
                                 total_portfolio_value: float,
                                 new_cash: float = 0.0) -> List[Dict]:
    """
    計算具體的再平衡買賣金額。

    參數口徑：current_holdings 為各資產「市值占組合總值比例」(0~1)；
    total_portfolio_value 為持倉現值基準。new_cash（新增資金）作為額外子彈，
    優先用於補齊欠配部位，而非賣出獲利資產（稅務優化）。

    容忍區間：偏離目標權重超過 rebalance_band (5%) 才觸發，避免頻繁交易摩擦成本。

    稅務優化（CFA 建議）：
    1. 先算出所有「欠配 >5%」的買入缺口與「超配 >5%」的賣出部位；
    2. 用 new_cash（新增資金）依缺口大小優先補齊買入需求；
    3. 若新現金已足額支應全部欠配（殘留買入需求 = 0），
       則將超配部位降載為「持有觀察」——零應稅事件；
    4. 只有現金不足時才提出賣出建議，並附短期資本利得稅提醒。
    """
    actions: List[Dict] = []
    if total_portfolio_value <= 0:
        return actions

    new_cash = max(new_cash, 0.0)
    band = REGIME_PARAMS["rebalance_band"]
    unders, overs = [], []
    for symbol, target_w in target_weights.items():
        current_w = current_holdings.get(symbol, 0.0)
        diff = (target_w - current_w) * total_portfolio_value  # >0 欠配(買), <0 超配(賣)
        if abs(diff) > total_portfolio_value * band:
            (unders if diff > 0 else overs).append((symbol, diff))

    # 欠配按缺口大到小排序，新增資金優先餵飽最大缺口
    unders.sort(key=lambda x: -x[1])
    overs.sort(key=lambda x: x[1])  # 負值，絕對值大的在前

    cash_left = max(new_cash, 0.0)
    buy_actions = []
    for symbol, diff in unders:
        use = min(cash_left, diff)
        cash_left -= use
        remainder = diff - use
        funded_part = use
        if remainder > 0.5:
            buy_actions.append({
                "symbol": symbol,
                "name": ASSET_UNIVERSE[symbol]["name"],
                "action": "買入",
                "amount": round(diff, 2),
                "funded_by_new_cash": round(funded_part, 2),
                "target_weight": f"{target_weights[symbol] * 100:.1f}%",
                "tax_note": ("部分由新增資金支應 $%s，其餘需賣出超配部位（留意資本利得稅）"
                             % round(funded_part, 2)) if funded_part > 0
                            else "無足夠新增資金：需賣出獲利部位，產生短期資本利得稅，建議保留未來入金優先補此缺口",
            })
        else:
            buy_actions.append({
                "symbol": symbol,
                "name": ASSET_UNIVERSE[symbol]["name"],
                "action": "買入",
                "amount": round(use, 2),
                "funded_by_new_cash": round(use, 2),
                "target_weight": f"{target_weights[symbol] * 100:.1f}%",
                "tax_note": "完全由新增資金支應，零應稅事件 ✅",
            })

    residual_buy_need = sum(a["amount"] - a["funded_by_new_cash"] for a in buy_actions)
    # 判定：賣出是否「必要」——只有當殘留買入缺口必須靠變現超配部位才能補齊時才賣。
    # 若新現金已足額支應全部欠配（residual_buy_need ≈ 0），
    # 代表不賣也能達成再平衡 → 超配部位改列「持有觀察」（自然稀釋），零應稅事件。
    # 防呆：僅當「確實有欠配買入需求」且新現金足額支應時，才免賣出；
    # 若無買入去向（如純超配情境），仍須賣出以回到目標配置。
    no_sell_needed = residual_buy_need <= 1e-6 and bool(buy_actions)

    actions.extend(buy_actions)
    for symbol, diff in overs:
        if no_sell_needed:
            actions.append({
                "symbol": symbol,
                "name": ASSET_UNIVERSE[symbol]["name"],
                "action": "持有觀察",
                "amount": round(-diff, 2),
                "funded_by_new_cash": 0,
                "target_weight": f"{target_weights[symbol] * 100:.1f}%",
                "tax_note": "暫緩賣出：新增資金＋買入需求可自然吸收超配，避免無謂的短期資本利得稅 ✅",
            })
        else:
            actions.append({
                "symbol": symbol,
                "name": ASSET_UNIVERSE[symbol]["name"],
                "action": "賣出",
                "amount": round(-diff, 2),
                "funded_by_new_cash": 0,
                "target_weight": f"{target_weights[symbol] * 100:.1f}%",
                "tax_note": "注意短期資本利得稅；持有滿 1 年再賣可適用較低稅率",
            })
    return actions


# ============================================================
# 5. 資料抓取（yfinance）與主流程
# ============================================================
def _momentum_score(series, lookback: int = 20) -> float:
    """把 n 日報酬映射到 0~1 動量分數（min-max 於 ±10% 區間），供 overlay 用。"""
    try:
        ret = float(series.iloc[-1] / series.iloc[-1 - lookback] - 1)
    except Exception:
        return 0.5
    return max(0.0, min(1.0, (ret + 0.10) / 0.20))


def collect_macro_data(client=None) -> Optional[Dict]:
    """自 yfinance 抓取 ^VIX / SPY MA200 / 各 ETF 20 日動量。失敗回傳 None。"""
    try:
        if client is None:
            from us_client import USClient
            client = USClient()
        vix_df = client.get_history("^VIX", period="1mo", min_rows=5)
        vix = float(vix_df["Close"].iloc[-1]) if vix_df is not None else None

        spy_df = client.get_history("SPY", period="2y")
        spy_trend = None
        if spy_df is not None and len(spy_df) >= 200:
            closes = spy_df["Close"]
            ma200 = float(closes.iloc[-200:].mean())
            spy_trend = round((float(closes.iloc[-1]) / ma200 - 1) * 100, 2)

        momentums_raw, momentum_scores, prices = {}, {}, {}
        for symbol in ASSET_UNIVERSE:
            df = client.get_history(symbol)
            if df is None or len(df) < 25:
                continue
            closes = df["Close"]
            prices[symbol] = round(float(closes.iloc[-1]), 2)
            try:
                momentums_raw[symbol] = round(float(closes.iloc[-1] / closes.iloc[-21] - 1), 4)
            except IndexError:
                continue
            momentum_scores[symbol] = round(_momentum_score(closes), 2)

        if vix is None or spy_trend is None or not momentums_raw:
            logger.warning("巨觀資料不完整（VIX=%s SPY_trend=%s），跳過本次更新", vix, spy_trend)
            return None

        return {
            "vix": round(vix, 2),
            "spy_trend": spy_trend,
            "tlt_momentum": momentums_raw.get("TLT", 0.0),
            "gld_momentum": momentums_raw.get("GLD", 0.0),
            "asset_momentums": momentum_scores,
            "momentum_raw": momentums_raw,
            "prices": prices,
        }
    except Exception as e:
        logger.error("巨觀資料抓取失敗：%s", e)
        return None


def load(name: str) -> Optional[Dict]:
    p = os.path.join(BASE, name)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def build_allocation_report(data: Dict, confirmed_regime: str,
                            target_weights: Dict[str, float],
                            actions: List[Dict]) -> Dict:
    """組裝輸出給前端的 macro_allocation.json 結構。"""
    return {
        "updated_at": datetime.now().isoformat(),
        "regime": confirmed_regime,
        "regime_light": REGIME_LIGHT.get(confirmed_regime, "⚪"),
        "raw_regime": detect_regime(data["vix"], data["spy_trend"],
                                    data["tlt_momentum"], data["gld_momentum"]),
        "confirm_days_required": REGIME_PARAMS["confirm_days"],
        "inputs": {
            "vix": data["vix"],
            "spy_vs_ma200_pct": data["spy_trend"],
            "tlt_mom_20d": data["tlt_momentum"],
            "gld_mom_20d": data["gld_momentum"],
            "prices": data.get("prices", {}),
        },
        "saa_weights": {k: v["base_weight"] for k, v in ASSET_UNIVERSE.items()},
        "taa_weights": target_weights,
        "names": {k: v["name"] for k, v in ASSET_UNIVERSE.items()},
        "classes": {k: v["class"] for k, v in ASSET_UNIVERSE.items()},
        "actions": actions,
        "ai_rationale": None,  # 由 Groq 生成後填入（AI 不解讀原始行情，只解讀以下確定性數字）
        "disclaimer": "權重由確定性規則計算；AI 僅負責文字解讀，不參與任何數字運算。本報告非投資建議。",
    }


def generate_macro_rationale(report: Dict) -> Optional[str]:
    """呼叫 Groq 生成 80 字內的華爾街宏觀策略師解讀（失敗回傳 None，不影響確定性輸出）。"""
    try:
        from groq_client import GroqClient
        top = sorted(report["taa_weights"].items(), key=lambda x: -x[1])[:2]
        weak = sorted(report["taa_weights"].items(), key=lambda x: x[1])[:2]
        prompt = (
            "你是一位華爾街宏觀策略師。請根據以下數據，用繁體中文寫一段 80 字以內的「資產配置建議報告」。"
            f"當前體制：{report['regime']}；VIX：{report['inputs']['vix']}；"
            f"SPY vs MA200：{report['inputs'].get('spy_vs_ma200_pct')}%；"
            f"強勢資產：{top}；弱勢資產：{weak}；"
            f"再平衡動作：{[(a['action'], a['symbol'], a['amount']) for a in report['actions']]}。"
            "要求：語氣專業、直接給出結論，解釋為什麼要這樣配置（例如：因為 VIX 飆升，建議減碼 QQQ 加碼 TLT 避險）。"
            "僅輸出純文字，不要 JSON、不要 Markdown。"
        )
        client = GroqClient()
        resp = client._make_request([
            {"role": "system", "content": "你是专业的宏观资产配置策略师，只輸出繁體中文純文字。"},
            {"role": "user", "content": prompt},
        ], temperature=0.3)
        if resp and "choices" in resp:
            text = resp["choices"][0]["message"]["content"].strip()
            return text[:200] if text else None
    except Exception as e:
        logger.warning("Groq 宏觀解讀生成失敗（略過，確定性輸出不受影響）：%s", e)
    return None


def main():
    logging.basicConfig(level=logging.INFO)
    out_path = os.path.join(BASE, "macro_allocation.json")
    state_path = os.path.join(BASE, "macro_regime_state.json")

    data = collect_macro_data()
    if data is None:
        print("⚠️ 巨觀資料不足（VIX/SPY/ETF 歷史抓取失敗），本日不更新 macro_allocation.json")
        return

    raw_regime = detect_regime(data["vix"], data["spy_trend"],
                               data["tlt_momentum"], data["gld_momentum"])

    # Whipsaw 過濾：讀取歷史原始体制序列，連續 3 天才切換
    state = load("macro_regime_state.json") or {"history": [], "confirmed": REGIME_NEUTRAL}
    history = list(state.get("history") or [])
    today = datetime.now().strftime("%Y-%m-%d")
    if not history or history[-1]["date"] != today:
        history.append({"date": today, "raw": raw_regime})
    else:
        history[-1]["raw"] = raw_regime
    history = history[-30:]  # 只保留近 30 筆

    confirmed = confirm_regime([h["raw"] for h in history], current_regime=raw_regime)

    target_weights = calculate_tactical_weights(confirmed, data["asset_momentums"])

    # 持倉來源：現有 advice.json 的現金配置視為 SHY；若 operator 提供 holdings.json 則以其為準
    holdings = load("holdings.json") or {}
    current = holdings.get("weights") or {"SHY": 1.0}
    portfolio_value = holdings.get("total_value") or 100000.0
    new_cash = holdings.get("new_cash") or 0.0

    actions = generate_rebalancing_actions(target_weights, current, portfolio_value, new_cash)

    report = build_allocation_report(data, confirmed, target_weights, actions)
    report["ai_rationale"] = generate_macro_rationale(report)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({"history": history, "confirmed": confirmed}, f, ensure_ascii=False, indent=2)

    print("=" * 56)
    print(f"{report['regime_light']} 体制：{confirmed}（今日原始：{raw_regime}，需連續 "
          f"{REGIME_PARAMS['confirm_days']} 天才切換）｜VIX {data['vix']}｜SPY vs MA200 {data['spy_trend']}%")
    for sym, w in sorted(target_weights.items(), key=lambda x: -x[1]):
        print(f"  {sym} {ASSET_UNIVERSE[sym]['name']}：SAA {ASSET_UNIVERSE[sym]['base_weight']*100:.0f}% → TAA {w*100:.1f}%")
    for a in actions:
        print(f"  {'🟢' if a['action']=='買入' else '🔴'} {a['action']} {a['symbol']} ${a['amount']} → {a['target_weight']}")
    if report["ai_rationale"]:
        print(f"🤖 AI 策略師：{report['ai_rationale']}")
    print("=" * 56)


if __name__ == "__main__":
    main()
