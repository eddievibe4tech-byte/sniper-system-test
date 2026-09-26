"""
狙擊手系統主程式
整合每日分析、風險評估、投資組合優化等功能
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

# 🔴 最佳實踐：將 Import 移到檔案頂端
from src.finmind_client import FinMindClient
from src.groq_client import GroqClient
from src.risk_calculator import calculate_volatility, assess_risk_level
from src.yahoo_client import YahooFinanceClient


def auto_verify_predictions(telemetry_data: Dict, finmind: FinMindClient, horizon: int = 5) -> int:
    """
    🔴 P0 修正：自動回填驗證
    
    自動回填：預測滿 horizon 個交易日後，
    用「預測當日收盤價 vs 現在價」計算實際報酬與對錯。
    
    Args:
        telemetry_data: 遙測數據字典
        finmind: FinMind 客戶端
        horizon: 驗證天數（預設 5 交易日）
        
    Returns:
        已驗證的記錄數量
    """
    from datetime import datetime
    now = datetime.now(TZ_TAIPEI)
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

        pred_dt = datetime.fromisoformat(rec["timestamp"].replace('+08:00', '+08:00'))
        # 簡化：用日曆日 * 1.5 近似交易日
        days_elapsed = (now - pred_dt).days
        if days_elapsed < int(horizon * 1.5):
            continue

        # 取得當前價格
        try:
            prices = finmind._get_raw_prices(rec["stock_code"], days=1) or []
            if not prices:
                continue
            current = prices[-1]
        except Exception:
            continue

        # 計算報酬率
        ret = round((current - entry_price) / entry_price * 100, 2)
        rec_pred = rec.get("prediction", {}).get("recommendation", "")

        # 判斷對錯
        if rec_pred in ("積極買入", "謹慎買入"):
            correct = ret > 0
        elif rec_pred == "避開":
            correct = ret <= 0  # 避開後真的沒漲＝正確
        else:  # 回檔觀察/觀望
            correct = abs(ret) < 3

        rec["actual_result"] = {
            "profit_pct": ret,
            "was_correct": correct,
            "horizon_days": horizon,
            "auto": True,
            "verified_at": now.isoformat()
        }
        rec["accuracy"] = 1 if correct else 0
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
    
    now = datetime.now(TZ_TAIPEI)
    metrics = {
        'total_predictions': total_predictions,
        'verified_count': verified_count,
        'correct_count': correct_count,
        'incorrect_count': incorrect_count,
        'pending_count': pending_count,
        'accuracy_rate': accuracy_display,
        'current_version': max(int(v) for v in version_stats.keys()) if version_stats else 1,
        'last_updated': now.isoformat(),
        'version_stats': version_stats_list
    }
    
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info(f"績效指標已更新：總預測={total_predictions}, 已驗證={verified_count}, 準確率={accuracy_display}")


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
            prompt_tpl = (BASE_DIR / 'prompts' / 'main_analysis.txt').read_text(encoding='utf-8')
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
                            "error": f"財報數據不完整（來源：{financial_source}）：跳過 AI 分析（避免浪費 Groq token）"
                        })
                        continue  # 直接跳過，不呼叫 Groq

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
