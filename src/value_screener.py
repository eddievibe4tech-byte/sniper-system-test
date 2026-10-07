"""
跨市場價值淘金與核心 ETF 抄底監控引擎 (Global Value & ETF Dip Monitor)
特色：
1. 多市場 ETF 矩陣診斷 (美股 VOO/QQQ/RSP/VT + 台股 0050/006208)
2. 批次下載 2 年歷史行情 (確保 MA200 計算穩定，一次請求搞定)
3. 標準 Wilder RSI 計算與嚴格防價值陷阱濾網
4. 排除金融股 FCF 限制、修補負 PEG/PE 邏輯破洞
"""
import json
import logging
import time
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
TZ_TAIPEI = timezone(timedelta(hours=8))
DATA_DIR = Path(__file__).resolve().parent.parent / 'data'

# 核心監控 ETF (跨市場矩陣)
CORE_ETFS = {
    "VOO": {"name": "S&P 500 (大型龍頭)", "market": "🇺🇸", "type": "美股大盤"},
    "QQQ": {"name": "那斯達克 100 (科技巨頭)", "market": "🇺🇸", "type": "科技成長"},
    "RSP": {"name": "標普等權重 (落後補漲)", "market": "🇺🇸", "type": "市場廣度"},
    "VT":  {"name": "全世界股票 (全球分散)", "market": "🌍", "type": "全球配置"},
    "0050.TW": {"name": "元大台灣50", "market": "🇹🇼", "type": "台股大盤"},
    "006208.TW": {"name": "富邦台50 (低內扣)", "market": "🇹🇼", "type": "台股大盤"}
}

# 精選 S&P 500 價值觀察池
SP500_VALUE_POOL = [
    "JNJ", "UNH", "PFE", "ABBV", "MRK", "LLY", "TMO", "ABT", "DHR", "BMY",
    "JPM", "BAC", "WFC", "BRK-B", "GS", "MS", "C", "BLK", "SCHW", "AXP",
    "CAT", "HON", "UNP", "BA", "GE", "RTX", "DE", "UPS", "LMT", "MMM",
    "PG", "KO", "PEP", "WMT", "MCD", "COST", "NKE", "SBUX", "TGT", "LOW",
    "XOM", "CVX", "COP", "NEE", "DUK", "SO", "D", "SLB", "EOG", "OXY"
]

def calculate_technical_metrics(series: pd.Series) -> dict:
    """計算年線乖離率、季線、以及標準 Wilder's RSI (14)"""
    if len(series) < 200:
        return {}
    
    cur_price = float(series.iloc[-1])
    ma200 = float(series.rolling(200).mean().iloc[-1])
    ma50 = float(series.rolling(50).mean().iloc[-1])
    bias_ma200 = ((cur_price - ma200) / ma200) * 100
    
    # 標準 Wilder's RSI 計算 (使用 EWM)
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    
    roll_up = up.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    roll_down = down.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    
    rs = roll_up / roll_down.replace(0, 1e-10)
    rsi = float((100 - (100 / (1 + rs))).iloc[-1])
    
    # 判斷進場訊號 (台股與美股通用邏輯)
    if cur_price <= ma200 or rsi < 35:
        advice = "🟢 罕見甜蜜點！(跌破/回踩年線或超賣)"
        strategy = "大筆加碼"
    elif bias_ma200 < 3.0:
        advice = "🟡 價格合理 (貼近年線支撐)"
        strategy = "分批佈局"
    elif bias_ma200 > 10.0:
        advice = f"🔴 嚴重超買 (乖離 +{bias_ma200:.1f}%)"
        strategy = "停止追高/減碼"
    else:
        advice = f"⏳ 乖離偏高 (+{bias_ma200:.1f}%)"
        strategy = "定期定額/觀望"
        
    return {
        "price": round(cur_price, 2),
        "ma200": round(ma200, 2),
        "ma50": round(ma50, 2),
        "bias_ma200_pct": round(bias_ma200, 2),
        "rsi14": round(rsi, 2),
        "advice": advice,
        "strategy": strategy
    }

def run_value_screener():
    logger.info("🚀 啟動跨市場價值淘金與多 ETF 監控引擎...")
    
    all_symbols = list(CORE_ETFS.keys()) + SP500_VALUE_POOL
    
    # 1. 單次批次下載 2 年歷史 K 線 (確保 MA200 計算穩定)
    logger.info(f"批次下載 {len(all_symbols)} 檔 2 年歷史行情...")
    try:
        hist_batch = yf.download(all_symbols, period="2y", group_by='ticker', threads=True, progress=False)
    except Exception as e:
        logger.error(f"批次下載失敗: {e}")
        hist_batch = pd.DataFrame()

    # 2. 解析核心 ETF 狀態 (美股 + 台股)
    etf_monitor = []
    for sym, meta in CORE_ETFS.items():
        try:
            if not hist_batch.empty and sym in hist_batch.columns.get_level_values(0):
                # 防呆：處理 yfinance 回傳的 MultiIndex 結構
                df = hist_batch[sym]['Close'].dropna() if isinstance(hist_batch.columns, pd.MultiIndex) else hist_batch[sym].dropna()
                metrics = calculate_technical_metrics(df)
                if metrics:
                    metrics.update({
                        "symbol": sym.replace(".TW", ""), # 前端顯示去後綴
                        "name": meta["name"],
                        "market": meta["market"],
                        "type": meta["type"]
                    })
                    etf_monitor.append(metrics)
        except Exception as e:
            logger.warning(f"解析 ETF {sym} 失敗: {e}")

    # 3. 掃描美股個股價值名單
    candidates = []
    missing_count = 0  # 🆕 資料品質監控：統計基本面抓取失敗數量 (YF 限流/403/空字典)
    for sym in SP500_VALUE_POOL:
        try:
            if hist_batch.empty or sym not in hist_batch.columns.get_level_values(0):
                continue
                
            df = hist_batch[sym]['Close'].dropna() if isinstance(hist_batch.columns, pd.MultiIndex) else hist_batch[sym].dropna()
            if len(df) < 200: continue
                
            price = float(df.iloc[-1])
            ma200 = float(df.rolling(200).mean().iloc[-1])
            
            # 防價值陷阱：股價必須維持在年線之上
            if price < ma200: continue
                
            ticker = yf.Ticker(sym)
            info = ticker.info or {}
            
            # 🆕 攔截 Yahoo /info 端點限流導致的靜默失敗（空字典/缺 forwardPE）
            if not info or 'forwardPE' not in info:
                missing_count += 1
                logger.warning(f"⚠️ {sym} 基本面資料缺失 (可能觸發 YF 限流)")
                continue
            
            pe = info.get('forwardPE')
            peg = info.get('pegRatio')
            roe = info.get('returnOnEquity')
            fcf = info.get('freeCashflow')
            mcap = info.get('marketCap')
            sector = info.get('sector', 'Unknown')
            
            roe_pct = round(roe * 100, 2) if roe else None
            fcf_yield = round((fcf / mcap * 100), 2) if (fcf and mcap and mcap > 0) else None
            
            # 核心條件檢驗 (防範負數)
            pe_ok = (pe is not None) and (0 < pe < 20)
            peg_ok = (peg is not None) and (0 < peg < 1.2)
            roe_ok = (roe_pct is not None) and (roe_pct > 15)
            
            # 金融股豁免 FCF
            fcf_ok = True if (sector and 'Financial' in str(sector)) else ((fcf_yield is not None) and (fcf_yield > 4))
            
            if pe_ok and roe_ok:
                status = "🟢 極度低估且健康" if (peg_ok and fcf_ok) else "🟡 估值合理，列入觀察"
                candidates.append({
                    "symbol": sym,
                    "name": info.get('shortName', sym),
                    "sector": sector,
                    "price": round(price, 2),
                    "forward_pe": round(pe, 2) if pe else None,
                    "peg": round(peg, 2) if peg else None,
                    "roe_pct": roe_pct,
                    "fcf_yield_pct": fcf_yield,
                    "ma200": round(ma200, 2),
                    "status": status
                })
            time.sleep(1.5) # 🆕 拉長延遲：Yahoo /info 端點防禦嚴格，1.5s 是必要的保護成本
        except Exception as e:
            missing_count += 1
            logger.warning(f"處理個股 {sym} 時發生錯誤: {e}")
            continue

    candidates.sort(key=lambda x: (x['peg'] if (x['peg'] and x['peg'] > 0) else 99, -(x['roe_pct'] or 0)))
    
    result = {
        "updated_at": datetime.now(TZ_TAIPEI).isoformat(),
        "etf_monitor": etf_monitor,
        "candidates": candidates,
        "missing_fundamentals_count": missing_count,  # 🆕 供前端判斷資料完整性
        "total_scanned": len(SP500_VALUE_POOL)
    }
    
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / 'value_candidates.json'
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info(f"✅ 篩選完成！已監控 {len(etf_monitor)} 檔核心 ETF，篩出 {len(candidates)} 檔個股，基本面缺失 {missing_count}/{len(SP500_VALUE_POOL)} 檔")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_value_screener()
