"""
🆕 利率週期引擎 (Rate Cycle Engine)
監控 10Y 殖利率、TLT/00697B 技術面、VIX，判斷美債降息週期階段，
作為定期定額 10年期美債 (如 00697B) 的加減碼依據。

四階段框架：
  Phase 1: 利率頂部區   → 🟡 開始定期定額（左側佈局，鎖定高殖利率）
  Phase 2: 降息確認期   → 🟢 加碼佈局（右側主升段，TLT 站上 MA200）
  Phase 3: 降息尾聲     → 🟠 停止加碼 / 轉領息（利多出盡）
  Phase 4: 通膨反撲     → 🔴 減碼避險 / 暫停扣款（殖利率回升 + VIX 恐慌）
"""
import json
import logging
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
TZ_TAIPEI = timezone(timedelta(hours=8))
DATA_DIR = Path(__file__).resolve().parent.parent / 'data'

# 核心監控標的
RATE_SYMBOLS = ["TLT", "SHY", "00697B.TW", "^TNX", "^VIX"]


def fetch_rate_data() -> dict:
    """抓取利率與債券相關數據"""
    data = {}

    # 1. 批次抓取債券 ETF 與 00697B (2年歷史確保 MA200 穩定)
    try:
        logger.info("抓取債券 ETF 歷史數據...")
        hist = yf.download(["TLT", "SHY", "00697B.TW"], period="2y",
                           group_by='ticker', threads=False, progress=False)

        for sym in ["TLT", "SHY", "00697B.TW"]:
            if isinstance(hist.columns, pd.MultiIndex) and sym in hist.columns.get_level_values(0):
                df = hist[sym]['Close'].dropna()
            elif sym in hist.columns:
                df = hist[sym].dropna()
            else:
                continue

            if len(df) >= 60:  # 至少需要 2 個月數據
                cur = float(df.iloc[-1])
                ma200 = float(df.rolling(200).mean().iloc[-1]) if len(df) >= 200 else float(df.mean())
                data[sym] = {
                    "price": round(cur, 2),
                    "ma200": round(ma200, 2),
                    "above_ma200": cur > ma200,
                    "bias_ma200_pct": round((cur - ma200) / ma200 * 100, 2),
                }
    except Exception as e:
        logger.warning(f"債券 ETF 抓取失敗: {e}")

    # 2. 抓取 10Y 殖利率 (^TNX)
    try:
        logger.info("抓取 10Y 殖利率...")
        tnx = yf.Ticker("^TNX").history(period="6mo")
        if not tnx.empty and len(tnx) >= 60:
            cur_10y = float(tnx['Close'].iloc[-1])
            avg_3m = float(tnx['Close'].rolling(60).mean().iloc[-1])
            data["^TNX"] = {
                "current": round(cur_10y, 2),
                "avg_3m": round(avg_3m, 2),
                "trend": "down" if cur_10y < avg_3m - 0.1 else ("up" if cur_10y > avg_3m + 0.1 else "flat"),
            }
    except Exception as e:
        logger.warning(f"^TNX 抓取失敗: {e}")

    # 3. 抓取 VIX
    try:
        vix = yf.Ticker("^VIX").history(period="1mo")
        if not vix.empty:
            data["^VIX"] = {"current": round(float(vix['Close'].iloc[-1]), 2)}
    except Exception as e:
        logger.warning(f"VIX 抓取失敗: {e}")

    return data


def judge_rate_cycle(data: dict) -> dict:
    """判斷利率週期階段與操作建議"""
    phase = "中性盤整"
    action = "⏳ 維持定期定額 (紀律扣款)"
    reasons = []

    tlt = data.get("TLT", {})
    tnx = data.get("^TNX", {})
    vix = data.get("^VIX", {})

    tnx_cur = tnx.get("current", 4.0)
    tnx_trend = tnx.get("trend", "flat")
    tlt_above_ma200 = tlt.get("above_ma200", False)
    vix_cur = vix.get("current", 15)

    # Phase 4: 通膨反撲 / 流動性危機 (最高優先級排雷)
    if (tnx_trend == "up" and tnx_cur > 4.5) or vix_cur > 30:
        phase = "Phase 4: 通膨反撲 / 流動性風險"
        action = "🔴 減碼避險 / 暫停扣款"
        if tnx_trend == "up":
            reasons.append(f"10Y 殖利率回升至 {tnx_cur}%，通膨壓力重現")
        if vix_cur > 30:
            reasons.append(f"VIX={vix_cur}，市場恐慌拋售無差別殺跌")

    # Phase 3: 降息尾聲 (利多出盡)
    elif tnx_cur < 3.5 and tlt_above_ma200:
        phase = "Phase 3: 降息尾聲"
        action = "🟠 停止加碼 / 轉領息"
        reasons.append(f"10Y 殖利率僅 {tnx_cur}%，降息預期已充分定價")
        reasons.append("債券價格處於高位，資本利得空間有限")

    # Phase 2: 降息確認期 (右側主升段)
    elif tlt_above_ma200 and tnx_trend == "down":
        phase = "Phase 2: 降息確認期"
        action = "🟢 加碼佈局 (單筆放大)"
        reasons.append("TLT 站上 MA200，債券多頭趨勢確認")
        reasons.append(f"10Y 殖利率 {tnx_cur}% 持續下滑，價格動能強勁")

    # Phase 1: 利率頂部區 (左側佈局)
    elif tnx_trend == "down" and tnx_cur > 3.8:
        phase = "Phase 1: 利率頂部區"
        action = "🟡 開始定期定額 (12-18 次)"
        reasons.append(f"10Y 殖利率 {tnx_cur}% 自高點回落，市場提前定價降息")
        reasons.append("此時進場可鎖定高殖利率，並享受未來降息資本利得")

    else:
        reasons.append("利率趨勢與債券動能尚未明確，建議維持紀律扣款")

    return {"phase": phase, "action": action, "reasons": reasons}


def run_rate_cycle_screener():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    logger.info("🚀 啟動利率週期引擎...")

    data = fetch_rate_data()
    cycle = judge_rate_cycle(data)

    target_etf = data.get("00697B.TW", {})

    result = {
        "updated_at": datetime.now(TZ_TAIPEI).isoformat(),
        "rate_cycle": cycle,
        "target_etf": {
            "code": "00697B",
            "name": "元大美債 7-10",
            "price": target_etf.get("price", "N/A"),
            "ma200": target_etf.get("ma200", "N/A"),
            "bias_ma200_pct": target_etf.get("bias_ma200_pct", "N/A"),
        },
        "macro_data": {
            "TLT": data.get("TLT", {}),
            "TNX_10Y": data.get("^TNX", {}),
            "VIX": data.get("^VIX", {}),
        }
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / 'rate_cycle.json'
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info(f"✅ 利率週期判斷: {cycle['phase']} → {cycle['action']}")


if __name__ == "__main__":
    run_rate_cycle_screener()
