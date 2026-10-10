"""
狙擊手系統主程式
整合每日分析、風險評估、投資組合優化等功能
"""
import argparse
import json
import logging
import math
import os
import sys
from bisect import bisect_left
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

# 🔴 最佳實踐：將 Import 移到檔案頂端
from src.finmind_client import FinMindClient
from src.groq_client import GroqClient
from src.risk_calculator import calculate_volatility, assess_risk_level
from src.trading_plan import generate_trading_plan
from src.yahoo_client import YahooFinanceClient


# 🆕 P2-1（Issue #110）：金融產業關鍵字——與 prompt v2「金融股忽略營收/EPS，
# 改看股價位置與籌碼」的規則保持一致。
FINANCIAL_INDUSTRY_KEYWORDS = ('金融', '銀行', '保險', '證券', '金控')


def is_financial_industry(industry: str) -> bool:
    """判斷產業是否屬金融類（與 prompt v2 口徑一致）。"""
    ind = industry or ''
    return any(kw in ind for kw in FINANCIAL_INDUSTRY_KEYWORDS)


def generate_rule_based_analysis(stock_data: Dict) -> Dict:
    """
    🆕 方案 1+2：規則引擎 — 當財報不完整時，用技術面＋籌碼面規則產生預設評分。
    完全不呼叫 Groq，節省 token（0 input / 0 output）。

    🆕 P2-1（Issue #110）產業調整：金融股（金控/銀行/保險/證券）不適用
    「短期漲勢加分」邏輯——金融股波動特性為長年盤整、短線暴衝多為事件驅動，
    改以「股價位置（MA20 之上）」＋「籌碼面」為主，與 prompt v2 一致。

    Args:
        stock_data: 已組裝的股票數據字典

    Returns:
        與 Groq 輸出同格式的 analysis 字典（ev_score / recommendation / reason）
    """
    score = 50  # 基礎分
    reasons = []

    # 🆕 P2-1：判定是否金融股
    financial = is_financial_industry(stock_data.get('industry', ''))

    # 技術面評分
    rsi = stock_data.get('rsi') or 50
    change_5d = stock_data.get('change_5d') or 0
    ma20 = stock_data.get('ma20') or 0
    current_price = stock_data.get('current_price') or 0
    macd = stock_data.get('macd') or 0
    volatility = stock_data.get('volatility') or 0

    if 30 < rsi < 70:
        score += 10
        reasons.append("RSI 中性")
    elif rsi <= 30:
        score += 15
        reasons.append("RSI 超賣")
    else:
        score -= 10
        reasons.append("RSI 超買")

    if financial:
        # 🔴 P2-1：金融股忽略 short-term momentum（change_5d），
        # 改以「股價位於 MA20 之上」加 10 分（趨勢位置取代動能）
        if ma20 > 0 and current_price > ma20:
            score += 10
            reasons.append("股價於 MA20 上（金融股位置加分）")
    else:
        if change_5d > 5:
            score += 10
            reasons.append("短期強勢")
        elif change_5d < -5:
            score -= 10
            reasons.append("短期弱勢")

    # MA20 乖離
    if ma20 > 0 and current_price > 0:
        gap_pct = (current_price - ma20) / ma20 * 100
        if gap_pct > 5:
            score -= 5
            reasons.append("乖離過大")

    # MACD
    if macd > 0:
        score += 5
        reasons.append("MACD 多頭")

    # 風險控管
    if volatility > 40:
        score -= 10
        reasons.append("波動過高")
    elif 0 < volatility < 20:
        score += 5
        reasons.append("波動低")

    # 籌碼面（金融股与非金融股皆納入；金融股以此為主要正面證據）
    inst_buy_days = stock_data.get('inst_buy_days') or 0
    if inst_buy_days >= 3:
        score += 15
        reasons.append("投信連買")
    elif inst_buy_days >= 1:
        score += 5
        reasons.append("投信小幅買超")

    # 限制分數範圍
    score = max(0, min(100, score))

    # 建議
    if score >= 70:
        recommendation = "積極買入"
    elif score >= 60:
        recommendation = "謹慎買入"
    elif score >= 40:
        recommendation = "觀望"
    else:
        recommendation = "避開"

    reason_str = "、".join(reasons[:3]) if reasons else "規則引擎判定"

    return {
        'ev_score': score,
        'recommendation': recommendation,
        'reason': f"（無 AI 分析）{reason_str}",
    }


# ==========================================
# 🆕 (#158) 鐵律：訊號衝突降級與時間維度標籤邏輯
# ==========================================
def _safe_float(val, default: float = 0.0) -> float:
    """Code Review (#159) 防護性修正：安全轉換數值。

    AI (Groq) 偶爾可能在數值欄位輸出 "N/A"、"資料不足" 或帶單位的 "75%"，
    直接 float() 會拋出 ValueError 導致整個每日批次分析迴圈中斷。
    本函數容錯處理：移除 % 符號與空白，轉換失敗一律回傳預設值。
    """
    try:
        if val is None:
            return default
        if isinstance(val, str):
            val = val.strip().replace('%', '').replace(',', '')
            if not val:
                return default
        return float(val)
    except (ValueError, TypeError):
        return default


def apply_signal_conflict_logic(stock: dict) -> dict:
    """
    解決長線基本面與短線技術面的訊號衝突。
    鐵律：基本面強(高EV或積極買入)但技術面跌破均線(MA20)時，
    強制降級為「觀望」，並賦予專屬的時間維度標籤。

    Args:
        stock: 單檔股票的分析 record（含 ev_score / recommendation / current_price / ma20 等欄位）

    Returns:
        原地修改後的 stock dict，新增 signal_tag；衝突降級時另新增
        original_recommendation 並以風控理由覆蓋 reason。
    """
    # 1. 取得關鍵指標（🛡️ Code Review：改用 _safe_float 防止 AI 輸出異常字串導致批次中斷）
    ev_score = _safe_float(stock.get('ev_score'), 50.0)
    rec = stock.get('recommendation', '觀望')

    current_price = _safe_float(stock.get('current_price'), 0.0)
    ma20 = _safe_float(stock.get('ma20'), 0.0)

    # 定義基本面強與技術面弱的條件
    # 🛡️ Code Review：改用包含檢查——AI 可能輸出「強烈買入」/「買入」/「Buy」等變體字眼，
    # 精確匹配 == '積極買入' 會導致降級邏輯漏判。「避開買入」之类否定詞不在現行枚舉中，不須排除。
    is_fundamental_strong = (ev_score >= 70) or ('買入' in str(rec))
    is_technical_weak = (ma20 > 0 and current_price < ma20)  # 跌破 MA20

    # 2. 衝突降級邏輯 (最高優先權)
    if is_fundamental_strong and is_technical_weak:
        stock['original_recommendation'] = rec  # 保留原 AI 推薦供前端參考
        stock['recommendation'] = '觀望'         # 🚨 強制降級
        stock['reason'] = (
            f"⚠️ 訊號衝突：基本面佳(EV={ev_score:.0f})但技術修正中"
            f"(現價{current_price:.2f} < MA20 {ma20:.2f})。"
            f"短期勿追價，請等待站回均線再評估。"
        )
        stock['signal_tag'] = '長線配置/短期觀望'

    # 3. 正常情況賦予時間維度標籤
    elif is_fundamental_strong:
        stock['signal_tag'] = '長線配置'
    else:
        stock['signal_tag'] = '短線狙擊'

    return stock


# ============ 🆕 Verification v2.1：對沖基金級驗證引擎 ============
# 設計原則：華爾街評估一筆預測「對錯」不看「有沒有漲」，而是看三個條件：
#   1. Net    —— 扣除來回摩擦成本（手續費+稅+價差+滑點）後是否賺錢
#   2. Alpha  —— 是否跑贏基準（0050 大盤 ETF）
#   3. Hurdle —— 是否超過該股波動率應有的門檻（√時間縮放）
VERIFY_FRICTION_PCT = 0.5        # 來回摩擦：手續費+稅+價差+滑點
VERIFY_ALPHA_BAND_PCT = 2.0      # 觀望類：|alpha| 在此帶內視為「確實無邊際」
VERIFY_MIN_HURDLE_PCT = 2.0      # 買入類絕對門檻下限（net 口徑）
VERIFY_CRITERIA_VERSION = 2      # 評級規則版本（v2.1 修訂記於 criteria 字串）
BENCHMARK_CODE = "0050"          # 台股 alpha 基準


def vol_hurdle_pct(vol_annual_pct: float, horizon_days: int = 5) -> float:
    """波動率縮放門檻：0.5 × vol × sqrt(h/252)，下限 2%"""
    scaled = 0.5 * (vol_annual_pct or 25.0) * math.sqrt(horizon_days / 252.0)
    return max(VERIFY_MIN_HURDLE_PCT, scaled)


def windowed_return(series: List[Dict], start_date: str, horizon_days: int,
                    anchor_price: Optional[float] = None) -> Optional[float]:
    """
    🔴 v2.1 時窗鎖定：嚴格取 [T, T+H] 交易日報酬。

    series 為升序 [{"date","close"}]；anchor_price 給定時作為起始價（telemetry entry_price）。
    回傳 None ＝ 視窗尚未走完或資料缺失 → 呼叫方「延後結算」，絕不用錯誤時窗硬算。
    （修復 v2：T+15 才驗證卻用 √5 門檻、以及 regrade 拿 5 天個股報酬比 90 天大盤報酬的錯位）
    """
    if not series:
        return None
    dates = [s["date"] for s in series]
    i = bisect_left(dates, start_date)
    if i >= len(dates):
        return None
    j = i + horizon_days
    if j >= len(series):
        return None                      # 交易日未走滿，不提前結算
    p0 = anchor_price if anchor_price else series[i]["close"]
    p1 = series[j]["close"]
    if not p0 or p0 <= 0:
        return None
    return round((p1 / p0 - 1) * 100, 2)


def judge_correctness(recommendation: str, ret_pct: float, bench_pct: Optional[float],
                      vol_annual_pct: float, horizon_days: int = 5) -> Dict:
    """純函數判定（可單測）：回傳 was_correct 與完整審計欄位"""
    net = ret_pct - VERIFY_FRICTION_PCT
    alpha = (ret_pct - bench_pct) if bench_pct is not None else None
    hurdle = vol_hurdle_pct(vol_annual_pct, horizon_days)

    if recommendation in ("積極買入", "謹慎買入"):
        if alpha is None:
            ok, criteria = net > hurdle, "buy:net>hurdle(no-bench)"
        else:
            ok, criteria = (net > hurdle and alpha > 0), "buy:net>hurdle&alpha>0"
    elif recommendation == "避開":
        # 🔴 v2.1 嚴格化：必須「絕對虧損 且 相對跑輸」才算正確避開，
        #    防止多頭市靠 alpha<0 或空頭市靠 net<0 單邊刷勝率。
        #    （本系統「避開」的替代部位是現金/ETF，故以雙重價值毀滅為標準）
        if alpha is None:
            ok, criteria = net <= 0, "avoid:net<=0(no-bench)"
        else:
            ok, criteria = (net <= 0 and alpha <= 0), "avoid:net<=0&alpha<=0"
    else:  # 觀望 / 回檔觀察
        if alpha is None:
            ok, criteria = abs(net) <= VERIFY_ALPHA_BAND_PCT, "wait:|net|<=band(no-bench)"
        else:
            ok, criteria = (abs(alpha) <= VERIFY_ALPHA_BAND_PCT and net < hurdle), \
                           "wait:|alpha|<=band&net<hurdle"

    return {"was_correct": bool(ok),
            "alpha_pct": round(alpha, 2) if alpha is not None else None,
            "net_pct": round(net, 2), "hurdle_pct": round(hurdle, 2),
            "criteria": criteria, "criteria_version": VERIFY_CRITERIA_VERSION}


def _fetch_benchmark_closes(finmind, days: int = 400) -> List[Dict]:
    """基準(0050) date→close 序列；失敗回傳 []（降級為絕對報酬規則）"""
    try:
        rows = finmind._make_request("TaiwanStockPrice", BENCHMARK_CODE, days=days) or []
        series = [{"date": str(r.get("date", ""))[:10], "close": float(r.get("close", 0))}
                  for r in rows if r.get("close")]
        series.sort(key=lambda s: s["date"])
        return series
    except Exception as e:
        logger.warning("基準 %s 抓取失敗：%s", BENCHMARK_CODE, e)
        return []


def compute_alpha_stats(verified_records: List[Dict]) -> Dict:
    """🔴 v2.1 純函數：多空/訊號類型隔離的績效統計（可單測，杜絕符號錯誤回歸）

    - 期望值只計「會實際執行」的買入訊號；避開/觀望屬過濾器，另以分類準確率評估，
      防止「避開正確」的 -10% 污染 avg_win（負負得正的數學幻覺）。
    - avg_loss 一律取絕對值，expectancy = hit×avg_win − miss×|avg_loss|。
    """
    v2 = [r for r in verified_records
          if (r.get("actual_result") or {}).get("criteria_version") == VERIFY_CRITERIA_VERSION]
    if not v2:
        return {}
    alphas = [r["actual_result"]["alpha_pct"] for r in v2
              if r["actual_result"].get("alpha_pct") is not None]

    trades = [r for r in v2
              if (r.get("prediction") or {}).get("recommendation") in ("積極買入", "謹慎買入")]
    if trades:
        pnls = [r["actual_result"].get("net_pct") for r in trades
                if isinstance(r["actual_result"].get("net_pct"), (int, float))]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        hit = len(wins) / len(pnls) * 100 if pnls else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0   # 🔴 取絕對值
        expectancy = (hit / 100) * avg_win - (1 - hit / 100) * avg_loss
    else:
        hit, avg_win, avg_loss, expectancy = 0.0, 0.0, 0.0, 0.0

    overall = sum(1 for r in v2 if r["actual_result"]["was_correct"]) / len(v2) * 100
    return {
        "graded_records": len(v2),
        "trade_records": len(trades),
        "classification_accuracy_pct": round(overall, 1),
        "trade_hit_rate_pct": round(hit, 1),
        "avg_alpha_pct": round(sum(alphas) / len(alphas), 2) if alphas else None,
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "expectancy_pct": round(expectancy, 2),
    }


def auto_verify_predictions(telemetry_data: Dict, finmind: FinMindClient, horizon: int = 5) -> int:
    """
    🆕 Verification v2.1：對沖基金級自動驗證

    改動重點（vs v1）：
      1. 判定改用 judge_correctness（Net/Alpha/Hurdle 三鐵律），不再只看「有沒有漲」。
      2. 🔴 時窗鎖定：個股與基準報酬一律取 [T, T+H] 交易日區間，以 entry_price 為錨；
         視窗未走完 → 延後結算（回傳 None → skip），絕不用「當前最新價」硬算。
      3. 基準 0050 一次抓取全批共用；缺失時降級為絕對報酬規則並記錄警告。
      4. 個股價格序列按 code 快取，避免重複打 API。
    """
    now = datetime.now(TZ_TAIPEI)
    bench = _fetch_benchmark_closes(finmind, days=400)
    if not bench:
        logger.warning("基準 0050 缺失：alpha 條件降級為絕對報酬規則")
    price_cache: Dict[str, List[Dict]] = {}
    verified = 0

    for rec in telemetry_data.get("records", []):
        # 跳過已驗證的記錄
        if rec.get("actual_result"):
            continue

        # 跳過 legacy 記錄（舊格式無 entry_price，永遠無法驗證）
        if rec.get("legacy"):
            continue

        entry_price = rec.get("entry_price")
        if not entry_price:
            continue

        pred_date = rec["timestamp"][:10]
        code = rec["stock_code"]

        # 🆕 按 code 快取價格序列，避免重複打 API
        if code not in price_cache:
            try:
                rows = finmind._make_request("TaiwanStockPrice", code, days=400) or []
                series = [{"date": str(r.get("date", ""))[:10],
                           "close": float(r.get("close", 0))}
                          for r in rows if r.get("close")]
                series.sort(key=lambda s: s["date"])
                price_cache[code] = series
            except Exception:
                price_cache[code] = []

        # 🔴 個股報酬鎖定 [T, T+H]，以 entry_price 為錨；視窗未滿 → 延後結算
        ret = windowed_return(price_cache[code], pred_date, horizon, anchor_price=entry_price)
        if ret is None:
            continue
        bench_ret = windowed_return(bench, pred_date, horizon) if bench else None

        rec_pred = (rec.get("prediction") or {}).get("recommendation", "")
        vol = (rec.get("input") or {}).get("volatility") \
              or (rec.get("prediction") or {}).get("volatility") or 25.0
        verdict = judge_correctness(rec_pred, ret, bench_ret, vol, horizon)

        rec["actual_result"] = {
            "profit_pct": ret,
            # 🆕 Sprint 3 (#160)：Expectancy 追蹤 — actual_return_pct 為實際報酬率欄位，
            # 與 profit_pct 同值（保留 profit_pct 供既有前端/報告相容使用）
            "actual_return_pct": ret,
            "benchmark": BENCHMARK_CODE,
            "benchmark_ret_pct": bench_ret,
            "window": f"{pred_date}+{horizon}td",     # 🆕 審計欄：結算時窗
            "horizon_days": horizon,
            "auto": True,
            "verified_at": now.isoformat(),
            **verdict,
        }
        rec["accuracy"] = 1 if verdict["was_correct"] else 0
        verified += 1

    return verified

# 載入環境變數
load_dotenv()

# 動態取得專案根目錄 (假設 main.py 在 src/ 下)
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / 'data'
LOGS_DIR = BASE_DIR / 'logs'
RESULTS_DIR = BASE_DIR / 'results'

# 設定台灣時區 (UTC+8)
TZ_TAIPEI = timezone(timedelta(hours=8))


def setup_logging():
    """設定日誌 (🟡 修正：避免重複設定 Handler)"""
    logger = logging.getLogger()
    if logger.hasHandlers():
        logger.handlers.clear()  # 清除舊的 Handler
    
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    
    # 🔴 P1 修正：支援 LOG_LEVEL 環境變數，預設 INFO 避免 DEBUG log 塞爆
    log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
    numeric_level = getattr(logging, log_level, logging.INFO)
    
    logging.basicConfig(
        level=numeric_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(LOGS_DIR / f"sniper_system_{datetime.now(TZ_TAIPEI).strftime('%Y%m%d')}.log", encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )


logger = logging.getLogger(__name__)


def ensure_directories():
    """確保必要的目錄存在"""
    for directory in [DATA_DIR, LOGS_DIR, RESULTS_DIR]:
        directory.mkdir(parents=True, exist_ok=True)


def load_config(config_path: str = 'stock_pool.json') -> Dict:
    """載入股票池配置"""
    full_path = DATA_DIR / config_path
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"配置文件 {full_path} 未找到，使用預設配置")
        return {"stocks": [], "sectors": {}}


def update_performance_metrics(telemetry_data: Optional[Dict] = None):
    """更新績效指標到 performance_metrics.json"""
    path = DATA_DIR / 'performance_metrics.json'
    
    # 若未提供 telemetry_data，則嘗試讀取現有的
    if telemetry_data is None:
        telemetry_path = DATA_DIR / 'telemetry.json'
        if telemetry_path.exists():
            telemetry_data = json.loads(telemetry_path.read_text(encoding='utf-8'))
        else:
            telemetry_data = {'records': [], 'metadata': {}}
    
    records = telemetry_data.get('records', [])
    total_predictions = len(records)
    
    # 計算已驗證的準確率
    verified_records = [r for r in records if r.get('accuracy') is not None]
    verified_count = len(verified_records)

    # P0 修正：權威計數欄位（前端卡片直接讀取，免現算）
    #   - correct_count / incorrect_count：以已驗證記錄為準
    #   - pending_count：待驗證但排除 legacy（無 entry_price、永遠無法驗證的舊記錄），
    #     否則「待驗證」數字會虛高。
    correct_count = sum(1 for r in verified_records if r.get('accuracy') == 1)
    incorrect_count = verified_count - correct_count
    pending_count = sum(1 for r in records
                        if r.get('actual_result') is None and not r.get('legacy'))
    
    # 🟡 P0 修正：驗證數 < 10 時顯示「資料不足」，不顯示 0.0%
    accuracy_rate = None
    accuracy_display = "資料不足"
    if verified_count >= 10:
        accuracy_rate = (correct_count / verified_count) * 100
        accuracy_display = round(accuracy_rate, 1)
    elif verified_count > 0:
        accuracy_display = f"已驗證 {verified_count}/{total_predictions}"

    # 🆕 Verification v2.1：分類準確率——買入訊號與過濾訊號（避開/觀望）分開統計，
    # 避免多頭市中「觀望佔多數、天然容易對」掩蓋買入訊號的真實品質。
    BUY_RECOMMENDATIONS = ("積極買入", "謹慎買入")
    buy_verified = [r for r in verified_records
                    if (r.get('prediction') or {}).get('recommendation') in BUY_RECOMMENDATIONS]
    filter_verified = [r for r in verified_records
                       if (r.get('prediction') or {}).get('recommendation') not in BUY_RECOMMENDATIONS]
    category_accuracy = {
        'buy': {'verified': len(buy_verified),
                'correct': sum(1 for r in buy_verified if r.get('accuracy') == 1),
                'accuracy': round(sum(1 for r in buy_verified if r.get('accuracy') == 1)
                                  / len(buy_verified) * 100, 1) if buy_verified else None},
        'filter': {'verified': len(filter_verified),
                   'correct': sum(1 for r in filter_verified if r.get('accuracy') == 1),
                   'accuracy': round(sum(1 for r in filter_verified if r.get('accuracy') == 1)
                                     / len(filter_verified) * 100, 1) if filter_verified else None},
    }
    
    # 計算各版本統計
    # 🔴 P0 修正：準確率分母必須是「已驗證筆數」而非「全部預測筆數（含待驗證）」，
    # 否則趨勢圖會把 46/234≈19.7% 畫出來，與卡片的 46/98≈46.9% 不一致。
    version_stats: Dict[str, Dict] = {}
    for record in records:
        v = str(record.get('prompt_version', 1))
        st = version_stats.setdefault(v, {'version': int(v), 'predictions': 0, 'verified': 0, 'correct': 0})
        st['predictions'] += 1
        if record.get('accuracy') is not None:
            st['verified'] += 1
            if record.get('accuracy') == 1:
                st['correct'] += 1

    # 計算各版本準確率（以已驗證為分母；無已驗證樣本時保持 None → 前端顯示「資料不足」）
    version_stats_list = []
    for st in version_stats.values():
        st['accuracy'] = round(st['correct'] / st['verified'] * 100, 1) if st['verified'] else None
        version_stats_list.append(st)
    # 🟡 顯式依版本排序，確保趨勢線 V1 -> V2 -> V3...
    version_stats_list.sort(key=lambda s: s['version'])

    # 🆕 P2-2（Issue #110）：規則引擎 vs Groq（AI 模型）分開的勝率追蹤。
    # telemetry record 已帶 model 欄位（'rule-based-engine' 或 Groq 模型名），
    # 此處彙整成 model_stats，長期觀察規則引擎是否比 AI 更穩定，
    # 作為後續是否擴大「跳過 Groq」範圍的依據。
    RULE_BASED_MODEL = 'rule-based-engine'
    model_agg: Dict[str, Dict] = {}
    for record in records:
        m = record.get('model') or 'unknown'
        st = model_agg.setdefault(m, {'model': m, 'predictions': 0, 'verified': 0, 'correct': 0})
        st['predictions'] += 1
        if record.get('accuracy') is not None:
            st['verified'] += 1
            if record.get('accuracy') == 1:
                st['correct'] += 1

    model_stats: Dict[str, Dict] = {}
    # 規則引擎固定以 'rule-based-engine' 為鍵；AI 模型合併到 'groq' 鍵下
    # （即使換模型名，也能與規則引擎直接比較）。
    rule_st = model_agg.pop(RULE_BASED_MODEL, None)
    groq_predictions = sum(st['predictions'] for st in model_agg.values())
    groq_verified = sum(st['verified'] for st in model_agg.values())
    groq_correct = sum(st['correct'] for st in model_agg.values())
    if rule_st or groq_predictions:
        def _mk(preds: int, ver: int, corr: int) -> Dict:
            return {
                'predictions': preds,
                'verified': ver,
                'correct': corr,
                # 分母一律用「已驗證筆數」（與 version_stats 口徑一致）；
                # 無已驗證樣本時保持 None → 前端顯示「資料不足」。
                'accuracy': round(corr / ver * 100, 1) if ver else None,
            }
        model_stats = {
            RULE_BASED_MODEL: _mk(
                rule_st['predictions'] if rule_st else 0,
                rule_st['verified'] if rule_st else 0,
                rule_st['correct'] if rule_st else 0,
            ),
            'groq': _mk(groq_predictions, groq_verified, groq_correct),
        }

    now = datetime.now(TZ_TAIPEI)

    # 🆕 Sprint 3 (#160)：期望值 (Expectancy) 追蹤 — 量化基金等級績效指標
    # 依「已驗證且含 actual_result」的記錄，以對錯劃分勝/敗組，計算數學期望值：
    #   expectancy = win_rate × avg_win + loss_rate × avg_loss
    # 注意：avg_loss 為負值（虧損報酬），故相加即為每筆交易的期望報酬率(%)。
    # 舊記錄可能沒有 actual_return_pct 欄位 → 退回 profit_pct；兩者皆無則不納入計算。
    def _record_return(r: Dict) -> Optional[float]:
        ar = r.get('actual_result') or {}
        val = ar.get('actual_return_pct')
        if val is None:
            val = ar.get('profit_pct')
        return float(val) if isinstance(val, (int, float)) else None

    returns_verified = [r for r in verified_records if _record_return(r) is not None]
    wins = [_record_return(r) for r in returns_verified if r.get('accuracy') == 1]
    losses = [_record_return(r) for r in returns_verified if r.get('accuracy') == 0]

    avg_win_pct = round(sum(wins) / len(wins), 2) if wins else 0.0
    avg_loss_pct = round(sum(losses) / len(losses), 2) if losses else 0.0

    expectancy = None
    if returns_verified:
        win_rate = len(wins) / len(returns_verified)
        loss_rate = 1 - win_rate
        expectancy = round(win_rate * avg_win_pct + loss_rate * avg_loss_pct, 2)

    # 🆕 v2.1 alpha 統計與期望值（僅統計 criteria_version==2 的 v2 評級記錄）
    # 🔴 修訂：期望值只計買入訊號、avg_loss 取絕對值——详见 compute_alpha_stats()。
    alpha_stats = compute_alpha_stats(verified_records)

    metrics = {
        'total_predictions': total_predictions,
        'verified_count': verified_count,
        'correct_count': correct_count,
        'incorrect_count': incorrect_count,
        'pending_count': pending_count,
        'accuracy_rate': accuracy_display,
        'current_version': max(int(v) for v in version_stats.keys()) if version_stats else 1,
        'last_updated': now.isoformat(),
        'version_stats': version_stats_list,
        'model_stats': model_stats,  # 🆕 P2-2：rule-based-engine vs groq 勝率
        # 🆕 Sprint 3 (#160)：Expectancy 核心指標
        'expectancy': expectancy,          # 每筆交易期望報酬率(%)；無資料時 None
        'avg_win_pct': avg_win_pct,        # 正確預測的平均報酬率(%)
        'avg_loss_pct': avg_loss_pct,      # 錯誤預測的平均報酬率(%)（負值）
        'expectancy_sample_count': len(returns_verified),  # 參與期望值計算的樣本數
        # 🆕 Verification v2.1
        'alpha_stats': alpha_stats,
        'category_accuracy': category_accuracy,
        'verify_criteria_version': VERIFY_CRITERIA_VERSION,
        'accuracy_definition': ("v2.1 對沖基金級：買入需扣 0.5% 摩擦後超越波動率門檻且跑贏 0050；"
                                "避開需「絕對虧損且相對跑輸」雙重條件才為正確；"
                                "觀望以 |alpha|≤2% 無邊際為正確"),
    }
    
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info(f"績效指標已更新：總預測={total_predictions}, 已驗證={verified_count}, "
                f"準確率={accuracy_display}, 期望值={expectancy}, "
                f"v2期望值={alpha_stats.get('expectancy_pct')}")


def run_daily_analysis(mode: str = 'full') -> Dict:
    """
    執行每日盤後分析
    
    Args:
        mode: 分析模式 ('full', 'analysis', 'risk', 'optimize')
    
    Returns:
        分析結果字典
    """
    logger.info(f"開始執行每日分析，模式：{mode}")
    
    # 🔴 P0 修正：執行分析前先自動回填驗證
    telemetry_path = DATA_DIR / 'telemetry.json'
    if telemetry_path.exists():
        telemetry_data = json.loads(telemetry_path.read_text(encoding='utf-8'))
        finmind = FinMindClient()
        verified_count = auto_verify_predictions(telemetry_data, finmind)
        if verified_count > 0:
            telemetry_path.write_text(json.dumps(telemetry_data, ensure_ascii=False, indent=2), encoding='utf-8')
            logger.info(f"自動驗證完成：已更新 {verified_count} 筆預測")
            # 更新績效指標
            update_performance_metrics(telemetry_data)
    
    # 使用台灣時間
    now = datetime.now(TZ_TAIPEI)
    results = {
        'timestamp': now.isoformat(),
        'mode': mode,
        'status': 'success',
        'data': {}
    }
    
    if mode in ['full', 'analysis']:
        logger.info("執行市場體制與個股分析...")
        
        # 🔴 修正：批次處理 telemetry (效能優化)
        config = load_config()
        stocks = config.get('stocks', [])
        
        # 讀取當前 Prompt 版本 (🔴 修正：不再硬編碼為 1)
        history_path = DATA_DIR / 'prompt_history.json'
        history = json.loads(history_path.read_text(encoding='utf-8')) if history_path.exists() else {}
        current_prompt_version = history.get('current_version', 1)
        
        # 🔴 修正：批次讀取 telemetry (避免頻繁 I/O)
        telemetry_path = DATA_DIR / 'telemetry.json'
        telemetry_data = json.loads(telemetry_path.read_text(encoding='utf-8')) if telemetry_path.exists() else {'records': [], 'metadata': {}}
        
        all_results = []
        failed_count = 0
        skipped_stocks = []  # 🔴 P0-2 修正：記錄被跳過的股票
        
        if stocks:
            finmind = FinMindClient()
            groq = GroqClient()
            yahoo = YahooFinanceClient()  # ✅ 初始化備援客戶端
            # 🆕 方案 2＋v2.1：Prompt 載入改為「版本驅動」——優化器產出 V{N+1} 後，
            # Daily Analysis 必須真的使用它（否則原硬編碼 v2 会让自動優化永不生效）。
            prompt_tpl = None
            for _cand in (BASE_DIR / 'prompts' / f'main_analysis_v{current_prompt_version}.txt',
                          BASE_DIR / 'prompts' / 'main_analysis_v2.txt',
                          BASE_DIR / 'prompts' / 'main_analysis.txt'):
                if _cand.exists():
                    prompt_tpl = _cand.read_text(encoding='utf-8')
                    logger.info("使用 Prompt：%s（V%d）", _cand.name, current_prompt_version)
                    break
            if prompt_tpl is None:
                raise FileNotFoundError("找不到任何 main_analysis prompt 模板")
            regime = '震盪'  # 進階可改呼叫 groq.judge_regime(market_data)
            
            for stock in stocks:
                code = stock['code']
                try:
                    # ✅ 1. 初始化所有變數為安全預設值（🔴 P0-1 修正：財務欄位改為 None）
                    prices = []
                    revenue_yoy = 0.0
                    gross_margin = None  # 🔴 改為 None，避免評分失真
                    net_margin = None    # 🔴 改為 None，避免評分失真
                    eps = None           # 🔴 改為 None，避免評分失真
                    inst_buy_days = 0
                    margin_change = 0
                    ma5, ma20, rsi, macd = 0.0, 0.0, 50.0, 0.0
                    price_above_ma20 = False
                    change_5d = 0.0
                    volatility = 0.0
                    use_yahoo_fallback = False
                    financial_source = 'finmind'  # 財報資料源標記（FinMind / yfinance）
                    
                    # ✅ 2. 嘗試使用 FinMind (主力)
                    try:
                        prices = finmind._get_raw_prices(code) or []
                        revenue = finmind.get_revenue(code) or {}
                        revenue_yoy = revenue.get('yoy_growth', 0.0)
                        
                        # 🔴 P0-1 修正精神延續：取不到財報時設為 None，讓 prompt 顯示 '-'
                        financial_source = 'finmind'
                        try:
                            financials = finmind.get_financial_statements(code) or {}
                            gross_margin = financials.get('gross_margin')
                            net_margin = financials.get('net_margin')
                            eps = financials.get('eps')
                            financial_source = financials.get('source', 'finmind')
                        except Exception as e:
                            logger.warning(f"{code} 財報數據抓取失敗，使用預設值：{e}")
                            gross_margin = None
                            net_margin = None
                            eps = None
                        
                        # 技術指標
                        try:
                            tech = finmind.get_technical_indicators(code) or {}
                            ma5 = tech.get('ma5', 0.0)
                            ma20 = tech.get('ma20', 0.0)
                            rsi = tech.get('rsi', 50.0)
                            macd = tech.get('macd', 0.0)
                            price_above_ma20 = tech.get('price_above_ma20', False)
                        except Exception as e:
                            logger.warning(f"{code} 技術指標計算失敗：{e}")
                        
                        # ✅ 關鍵：在這裡一次性取得籌碼數據，避免後續重複呼叫 API
                        inst_buy_days = finmind.get_institutional_buy(code) or 0
                        margin_change = finmind.get_margin_balance(code) or 0
                        
                        logger.info(f"{code}: 使用 FinMind 數據成功")
                        
                    except Exception as e:
                        # ✅ 3. FinMind 失敗，切換到 Yahoo (備援)
                        logger.warning(f"{code}: FinMind 失敗 ({e})，切換至 Yahoo Finance 備援")
                        use_yahoo_fallback = True
                        
                        yahoo_data = yahoo.get_stock_data(code)
                        if yahoo_data:
                            prices = yahoo_data['prices']
                            # 直接使用 Yahoo 算好的指標，避免重複計算
                            change_5d = yahoo_data['change_5d']
                            volatility = yahoo_data['volatility']
                            ma5 = yahoo_data['ma5']
                            ma20 = yahoo_data['ma20']
                            rsi = yahoo_data['rsi']
                            macd = yahoo_data['macd']
                            price_above_ma20 = yahoo_data['price_above_ma20']
                            
                            logger.info(f"{code}: Yahoo Finance 備援成功")
                        else:
                            raise Exception("Yahoo Finance 備援也失敗，無歷史數據")
                    
                    # ✅ 4. 若使用 FinMind 成功，且尚未計算 change_5d 與 volatility，則在此計算
                    if not use_yahoo_fallback:
                        # 🔴 修正：確保有足夠的價格數據才計算
                        if len(prices) >= 6 and prices[-6] and prices[-6] > 0:
                            change_5d = round((prices[-1] / prices[-6] - 1) * 100, 2)
                        else:
                            # 數據不足時嘗試用更少天數計算
                            if len(prices) >= 2 and prices[-1] and prices[0] and prices[0] > 0:
                                days_available = len(prices) - 1
                                change_5d = round((prices[-1] / prices[0] - 1) * 100, 2) if days_available > 0 else 0.0
                                logger.warning(f"{code}: 價格數據不足 6 天，改用 {days_available} 天計算 5 日漲幅")
                            else:
                                change_5d = 0.0
                                logger.warning(f"{code}: 價格數據不足以計算漲幅")
                        volatility = calculate_volatility(prices) if len(prices) >= 2 else 0.0

                    # ✅ 5. 組裝最終的 stock_data（加入 data_source 標記）
                    stock_data = {
                        'code': code,
                        'name': stock['name'],
                        'industry': stock.get('industry', ''),
                        'regime': regime,
                        'revenue_yoy': revenue_yoy,
                        'gross_margin': gross_margin,
                        'net_margin': net_margin,
                        'eps': eps,
                        'inst_buy_days': inst_buy_days,
                        'margin_change': margin_change,
                        'change_5d': change_5d,
                        'volatility': volatility,
                        'ma5': ma5,
                        'ma20': ma20,
                        'rsi': rsi,
                        'macd': macd,
                        'price_above_ma20': price_above_ma20,
                        'current_price': float(prices[-1]) if prices else 0.0,
                        'data_source': 'yahoo' if use_yahoo_fallback else 'finmind',  # ✅ 加入數據來源標記
                    }
                    
                    # ✅ 5.5 🔴 方案 A：財報全缺時在呼叫 Groq「之前」攔截，節省 token
                    # 原問題：FinancialStatements 全面 422 時，14 檔股票仍各消耗一次
                    # Groq API call（~4-5 秒/次），且 EV 評分基於殘缺數據（無基本面）。
                    # 🔴 方案 C 強化後：FinMind 失敗會自動 fallback yfinance，
                    #    只有「雙資料源都拿不到」才會觸發此攔截。
                    financial_complete = not (gross_margin is None and net_margin is None and eps is None)
                    if not financial_complete:
                        logger.warning(
                            f"{code}: 財報數據不完整（毛利率/淨利率/EPS 皆為 None，"
                            f"來源：{financial_source}，FinMind+yfinance 雙源皆失敗），"
                            f"跳過 AI 分析以節省 Groq token"
                        )
                        skipped_stocks.append({
                            "code": code,
                            "name": stock.get('name', ''),
                            "error": f"財報數據不完整（來源：{financial_source}）：跳過 AI 分析（改用規則引擎，避免浪費 Groq token）"
                        })

                        # 🆕 方案 1：不呼叫 Groq，改用規則引擎預設評分，仍完整產出 record
                        analysis = generate_rule_based_analysis(stock_data)
                        record = {**stock_data, **analysis, 'risk_level': assess_risk_level(volatility)}
                        record['trading_plan'] = generate_trading_plan(record)
                        all_results.append(record)

                        # 規則引擎結果也寫入 telemetry，供儀表板顯示「（無 AI 分析）」標記
                        telemetry_data['records'].append({
                            'id': f"{now.strftime('%Y%m%d')}-{code}",
                            'timestamp': now.isoformat(),
                            'stock_code': code,
                            'stock_name': stock['name'],
                            'prompt_version': current_prompt_version,
                            'model': 'rule-based-engine',  # 🆕 標記非 AI 來源
                            'regime': regime,
                            'input': stock_data,
                            'prediction': {
                                'ev_score': analysis['ev_score'],
                                'recommendation': analysis['recommendation'],
                                'reason': analysis['reason'],
                                'stock_code': code,
                                'stock_name': stock['name'],
                                'close_price': prices[-1] if prices else None,
                                'change_5d': stock_data.get('change_5d'),
                                'volatility': stock_data.get('volatility'),
                                'rsi': stock_data.get('rsi'),
                            },
                            'actual_result': None,
                            'accuracy': None,
                            'entry_price': prices[-1] if prices else None,
                        })
                        continue  # 跳過 Groq 呼叫

                    # ✅ 6. 呼叫 Groq 分析 (加入容錯)
                    analysis = groq.analyze_stock(prompt_tpl, stock_data)
                    if not analysis:
                        logger.warning(f"{code} AI 分析失敗，使用預設值")
                        analysis = {
                            'ev_score': 50,
                            'recommendation': '觀望',
                            'reason': f"AI 分析失敗，請手動檢視 {stock['name']} 數據"
                        }
                    
                    record = {**stock_data, **analysis, 'risk_level': assess_risk_level(volatility)}

                    # 🆕 加入交易計畫（買點/賣點：進場區間、停損、停利、R/R），與規則引擎分支保持一致
                    record['trading_plan'] = generate_trading_plan(record)

                    all_results.append(record)

                    # ✅ 7. 記憶體中 Append
                    # 🔴 修正：prediction 應該包含完整的分析結果，而不只是 AI 回應
                    # 這樣儀表板才能正確顯示股票代碼、名稱、建議等資訊
                    analysis_dict = analysis if isinstance(analysis, dict) else {}
                    prediction_record = {
                        'ev_score': analysis_dict.get('ev_score'),
                        'recommendation': analysis_dict.get('recommendation', '-'),
                        'reason': analysis_dict.get('reason', 'AI 分析失敗'),
                        # 加入完整的股票數據供儀表板使用
                        'stock_code': code,
                        'stock_name': stock['name'],
                        'industry': stock_data.get('industry', '-'),
                        'close_price': prices[-1] if prices else None,
                        'change_5d': stock_data.get('change_5d', None),
                        'volatility': stock_data.get('volatility', None),
                        'rsi': stock_data.get('rsi', None),
                    }
                    
                    # 🔴 修正：確保 prediction 不會是空物件
                    telemetry_record = {
                        'id': f"{now.strftime('%Y%m%d')}-{code}",
                        'timestamp': now.isoformat(),
                        'stock_code': code,
                        'stock_name': stock['name'],
                        'prompt_version': current_prompt_version,
                        'model': groq.model,
                        'regime': regime,
                        'input': stock_data,
                        'prediction': prediction_record,
                        'actual_result': None,
                        'accuracy': None,
                        # 🔴 P0 修正：記錄 entry_price 供自動驗證使用
                        'entry_price': prices[-1] if prices else None,
                    }
                    
                    # 🔴 防禦性檢查：如果 prediction 為空，則直接使用 record 的數據
                    if not prediction_record.get('recommendation') or prediction_record.get('recommendation') == '-':
                        telemetry_record['prediction'] = {
                            'recommendation': record.get('recommendation', '觀望'),
                            'ev_score': record.get('ev_score', 50),
                            'reason': record.get('reason', '無 AI 理由'),
                            'stock_code': code,
                            'stock_name': stock['name'],
                        }
                    
                    telemetry_data['records'].append(telemetry_record)
                    logger.info(f"{code} 分析完成：EV={analysis_dict.get('ev_score') if analysis_dict else 'N/A'}")
                    
                except Exception as e:
                    logger.error(f"分析 {code} 完全失敗：{e}")
                    failed_count += 1
                    # 🔴 P0-2 修正：記錄被跳過的股票與原因
                    skipped_stocks.append({"code": code, "name": stock.get('name', ''), "error": str(e)})
                
                # 🔴 P1 加固：檢查部分失敗（關鍵欄位全缺）
                # 注意：「財報全缺」已在步骤 5.5 於呼叫 Groq 前攔截並 continue，
                # 這裡只需補記「無股價資料」（例如 Yahoo 備援回傳空價格但未拋例外）。
                if code not in [s.get('code') for s in skipped_stocks]:
                    partial_issues = []
                    if not prices:
                        partial_issues.append("無股價資料")
                    if partial_issues:
                        skipped_stocks.append({
                            "code": code,
                            "name": stock.get('name', ''),
                            "error": "部分失敗：" + ", ".join(partial_issues)
                        })
                        logger.warning(f"{code}: {partial_issues}")
            
            # 🔴 P0-2 修正：輸出 skipped 清單，避免靜默丟股
            if skipped_stocks:
                logger.warning(f"共有 {len(skipped_stocks)} 檔股票被跳過：{skipped_stocks}")
            # 🔴 修正：一次性寫入 telemetry (批次處理)
            if 'metadata' not in telemetry_data:
                telemetry_data['metadata'] = {}
            
            telemetry_data['metadata']['created_at'] = telemetry_data['metadata'].get('created_at') or now.isoformat()
            telemetry_data['metadata']['total_records'] = len(telemetry_data['records'])
            telemetry_data['metadata']['skipped_stocks'] = skipped_stocks  # 🔴 P0-2 新增
            telemetry_path.write_text(json.dumps(telemetry_data, ensure_ascii=False, indent=2), encoding='utf-8')
            
            # 🔴 修正：呼叫績效指標更新函數
            update_performance_metrics(telemetry_data)
            
            # 🟡 修正：狀態判定更嚴謹
            if failed_count > 0:
                results['status'] = 'partial'
                logger.warning(f"部分股票分析失敗：成功 {len(all_results)}/{len(stocks)}")
            
            # 🆕 (#158) 鐵律：寫入 JSON 前套用訊號衝突降級與時間維度標籤
            # 基本面強但跌破 MA20 → 強制降級「觀望」；同時賦予 signal_tag
            downgraded = []
            for stock_record in all_results:
                before_rec = stock_record.get('recommendation')
                apply_signal_conflict_logic(stock_record)
                if stock_record.get('original_recommendation'):
                    downgraded.append(
                        f"{stock_record.get('code')}({before_rec}→觀望)"
                    )
            if downgraded:
                logger.info(f"🚨 訊號衝突降級：{', '.join(downgraded)}")

            deep = {'analyzed_at': now.isoformat(), 'regime': regime,
                    'high_score_targets': [r for r in all_results if (r.get('ev_score') or 0) >= 70],
                    'all_results': all_results, 'skipped_stocks': skipped_stocks}
            (DATA_DIR / 'deep_analysis.json').write_text(
                json.dumps(deep, ensure_ascii=False, indent=2), encoding='utf-8')
            results['data']['stock_analysis'] = deep
            results['regime'] = regime  # ✅ 將 regime 存入 results 供報告生成使用
        
    if mode in ['full', 'risk']:
        logger.info("執行風險評估...")
        # TODO: 呼叫風險計算器
        
    if mode in ['full', 'optimize']:
        logger.info("檢查是否需要優化 Prompt...")
        # TODO: 呼叫 Prompt Optimizer
    
    # ✅ 生成靜態報告（Markdown + HTML）供 GitHub Pages 展示
    if mode in ['full', 'analysis']:
        try:
            from src.report_generator import generate_static_review, generate_html_report
            
            # ✅ 直接使用記憶體中的 results 變數，避免讀取硬碟上的舊檔案
            analysis_results = results.get('data', {}).get('stock_analysis', {}).get('all_results', [])
            regime = results.get('data', {}).get('stock_analysis', {}).get('regime', {})
            
            # ✅ 正規化 regime：可能是字串（如 '震盪'），轉為 Dict
            if isinstance(regime, str):
                regime = {'regime': regime, 'confidence': '中'}
            
            if analysis_results or regime:
                generate_static_review(analysis_results, regime)
                generate_html_report(analysis_results, regime)
                logger.info("✅ 靜態報告已生成至 docs/ 目錄")
        except Exception as e:
            logger.warning(f"生成靜態報告失敗：{e}")

    # 儲存結果 (使用台灣時間命名)
    output_file = RESULTS_DIR / f"analysis_{now.strftime('%Y%m%d_%H%M%S')}.json"
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        logger.info(f"分析結果已儲存至 {output_file}")
    except Exception as e:
        logger.error(f"儲存結果失敗：{e}")
        results['status'] = 'partial'
    
    return results


def main():
    """主程式進入點"""
    parser = argparse.ArgumentParser(description='狙擊手系統主程式')
    parser.add_argument(
        '--mode',
        type=str,
        default='full',
        choices=['full', 'analysis', 'risk', 'optimize'],
        help='執行模式：full(完整), analysis(僅分析), risk(僅風險), optimize(僅優化)'
    )
    parser.add_argument(
        '--config',
        type=str,
        default='stock_pool.json',
        help='配置文件路徑'
    )
    
    args = parser.parse_args()
    
    # 設定日誌
    setup_logging()
    
    # 確保目錄存在
    ensure_directories()
    
    # 載入配置
    config = load_config(args.config)
    
    # 執行分析
    results = run_daily_analysis(mode=args.mode)
    
    # 輸出結果摘要 (供 GitHub Actions Log 查看)
    print("\n" + "="*50)
    print("狙擊手系統執行完成")
    print(f"時間：{results['timestamp']}")
    print(f"狀態：{results['status']}")
    print("="*50 + "\n")
    
    return 0 if results['status'] == 'success' else 1


if __name__ == '__main__':
    exit(main())
