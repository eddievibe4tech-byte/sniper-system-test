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
    log_file = LOGS_DIR / f"sniper_system_{datetime.now(TZ_TAIPEI).strftime('%Y%m%d')}.log"
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
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
    accuracy_rate = 0.0
    if verified_records:
        correct_count = sum(1 for r in verified_records if r.get('accuracy') == 1)
        accuracy_rate = (correct_count / len(verified_records)) * 100
    
    # 計算各版本統計
    version_stats: Dict[str, Dict] = {}
    for record in records:
        version = str(record.get('prompt_version', 1))
        if version not in version_stats:
            version_stats[version] = {'version': int(version), 'predictions': 0, 'correct': 0}
        version_stats[version]['predictions'] += 1
        if record.get('accuracy') == 1:
            version_stats[version]['correct'] += 1
    
    # 計算各版本準確率
    version_stats_list = []
    for version, stats in version_stats.items():
        stats['accuracy'] = round((stats['correct'] / stats['predictions']) * 100, 1) if stats['predictions'] > 0 else 0.0
        version_stats_list.append(stats)
    
    now = datetime.now(TZ_TAIPEI)
    metrics = {
        'total_predictions': total_predictions,
        'accuracy_rate': round(accuracy_rate, 1),
        'current_version': max(int(v) for v in version_stats.keys()) if version_stats else 1,
        'last_updated': now.isoformat(),
        'version_stats': version_stats_list
    }
    
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info(f"績效指標已更新：總預測={total_predictions}, 準確率={accuracy_rate:.1f}%")


def run_daily_analysis(mode: str = 'full') -> Dict:
    """
    執行每日盤後分析
    
    Args:
        mode: 分析模式 ('full', 'analysis', 'risk', 'optimize')
    
    Returns:
        分析結果字典
    """
    logger.info(f"開始執行每日分析，模式：{mode}")
    
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
        
        if stocks:
            finmind = FinMindClient()
            groq = GroqClient()
            prompt_tpl = (BASE_DIR / 'prompts' / 'main_analysis.txt').read_text(encoding='utf-8')
            regime = '震盪'  # 進階可改呼叫 groq.judge_regime(market_data)
            
            for stock in stocks:
                code = stock['code']
                try:
                    # 抓取基本數據
                    prices = finmind.get_stock_price(code) or []
                    revenue = finmind.get_revenue(code) or {}
                    
                    # ✅ 財報數據加入容錯（失敗時使用預設值）
                    try:
                        financials = finmind.get_financial_statements(code) or {}
                    except Exception as e:
                        logger.warning(f"{code} 財報數據抓取失敗，使用預設值：{e}")
                        financials = {
                            'gross_margin': 0,
                            'net_margin': 0,
                            'eps': 0,
                        }
                    
                    # ✅ 技術指標加入容錯
                    try:
                        technicals = finmind.get_technical_indicators(code)
                    except Exception as e:
                        logger.warning(f"{code} 技術指標計算失敗：{e}")
                        technicals = {'ma5': 0, 'ma20': 0, 'rsi': 50, 'macd': 0, 'price_above_ma20': False}
                    
                    # 🔴 修正：除以零風險防護
                    change_5d = round((prices[-1] / prices[-6] - 1) * 100, 2) if (len(prices) >= 6 and prices[-6] != 0) else 0.0
                    
                    stock_data = {
                        'code': code,
                        'name': stock['name'],
                        'industry': stock.get('industry', ''),
                        'regime': regime,
                        # 基本面數據
                        'revenue_yoy': revenue.get('yoy_growth', 0),
                        'gross_margin': financials.get('gross_margin', 0),
                        'net_margin': financials.get('net_margin', 0),
                        'eps': financials.get('eps', 0),
                        # 籌碼面數據
                        'inst_buy_days': finmind.get_institutional_buy(code) or 0,
                        'margin_change': finmind.get_margin_balance(code) or 0,
                        # 技術面數據
                        'change_5d': change_5d,
                        'volatility': calculate_volatility(prices),
                        'ma5': technicals['ma5'],
                        'ma20': technicals['ma20'],
                        'rsi': technicals['rsi'],
                        'macd': technicals['macd'],
                        'price_above_ma20': technicals['price_above_ma20'],
                        'current_price': prices[-1] if prices else None,
                    }
                    
                    # ✅ 加入容錯機制：如果 AI 分析失敗，使用預設值
                    analysis = groq.analyze_stock(prompt_tpl, stock_data)
                    if not analysis:
                        logger.warning(f"{code} AI 分析失敗，使用預設值")
                        analysis = {
                            'ev_score': 50,
                            'recommendation': '觀望',
                            'reason': f"AI 分析失敗，請手動檢視 {stock['name']} 的技術面與籌碼面數據",
                            'raw_response': None
                        }
                    
                    record = {**stock_data, **analysis,
                              'risk_level': assess_risk_level(stock_data['volatility'])}
                    all_results.append(record)

                    # 🔴 修正：記憶體中 Append (不再每次寫入檔案)
                    telemetry_data['records'].append({
                        'id': f"{now.strftime('%Y%m%d')}-{code}",
                        'timestamp': now.isoformat(),
                        'stock_code': code,
                        'stock_name': stock['name'],
                        'prompt_version': current_prompt_version,  # 🔴 修正：動態版本
                        'model': groq.model,
                        'regime': regime,
                        'input': stock_data,
                        'prediction': analysis,
                        'actual_result': None,
                        'accuracy': None,
                    })
                    logger.info(f"{code} 分析完成：EV={analysis.get('ev_score')}")
                except Exception as e:
                    logger.error(f"分析 {code} 失敗：{e}")
                    failed_count += 1
            
            # 🔴 修正：一次性寫入 telemetry (批次處理)
            if 'metadata' not in telemetry_data:
                telemetry_data['metadata'] = {}
            
            telemetry_data['metadata']['created_at'] = telemetry_data['metadata'].get('created_at') or now.isoformat()
            telemetry_data['metadata']['total_records'] = len(telemetry_data['records'])
            telemetry_path.write_text(json.dumps(telemetry_data, ensure_ascii=False, indent=2), encoding='utf-8')
            
            # 🔴 修正：呼叫績效指標更新函數
            update_performance_metrics(telemetry_data)
            
            # 🟡 修正：狀態判定更嚴謹
            if failed_count > 0:
                results['status'] = 'partial'
                logger.warning(f"部分股票分析失敗：成功 {len(all_results)}/{len(stocks)}")
            
            deep = {'analyzed_at': now.isoformat(), 'regime': regime,
                    'high_score_targets': [r for r in all_results if (r.get('ev_score') or 0) >= 70],
                    'all_results': all_results}
            (DATA_DIR / 'deep_analysis.json').write_text(
                json.dumps(deep, ensure_ascii=False, indent=2), encoding='utf-8')
            results['data']['stock_analysis'] = deep
        
    if mode in ['full', 'risk']:
        logger.info("執行風險評估...")
        # TODO: 呼叫風險計算器
        
    if mode in ['full', 'optimize']:
        logger.info("檢查是否需要優化 Prompt...")
        # TODO: 呼叫 Prompt Optimizer

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
