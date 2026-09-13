"""
FinMind API 客戶端模組
提供台股數據抓取功能：營收、投信籌碼、融資餘額、財報、技術指標等
"""
import os
import logging
import time
import requests
from typing import Dict, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class FinMindClient:
    """FinMind API 客戶端"""
    
    def __init__(self, token: Optional[str] = None):
        """
        初始化 FinMind 客戶端
        
        Args:
            token: FinMind API Token，若未提供則從環境變數 FINMIND_TOKEN 讀取
        """
        self.token = token or os.getenv('FINMIND_TOKEN', '')
        self.base_url = 'https://api.finmindtrade.com/api/v4/data'
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'SniperSystem/1.0',
            'Content-Type': 'application/json'
        })
        # ✅ 加入請求間隔設定（避免速率限制）
        self.request_interval = 1.0  # 每次請求間隔 1 秒
        self.max_retries = 3
        self.retry_delay = 2
    
    def _make_request(self, dataset: str, stock_id: str, days: int = 60) -> Optional[List[Dict]]:
        """
        發送 API 請求
        
        Args:
            dataset: 數據集名稱
            stock_id: 股票代號（v4 用 data_id）
            days: 日期範圍天數
            
        Returns:
            API 回應資料（data 陣列），失敗時返回 None
        """
        if not self.token:
            raise ValueError("FinMind Token 未設定")
        
        end = datetime.now()
        start = end - timedelta(days=days)
        
        request_params = {
            'dataset': dataset,
            'data_id': stock_id,
            'start_date': start.strftime('%Y-%m-%d'),
            'end_date': end.strftime('%Y-%m-%d'),
            'token': self.token,
        }
        
        for attempt in range(self.max_retries):
            try:
                # ✅ 加入請求間隔，避免觸發速率限制
                time.sleep(self.request_interval)
                
                response = self.session.get(self.base_url, params=request_params, timeout=10)
                
                if response.status_code == 429:
                    # 速率限制，等待更長時間
                    logger.warning(f"FinMind 速率限制，等待 {self.retry_delay * (attempt + 1)} 秒")
                    time.sleep(self.retry_delay * (attempt + 1))
                    continue
                
                if response.status_code != 200:
                    logger.error(f"FinMind {dataset} 失敗：{response.status_code} {response.text}")
                    return None
                
                data = response.json()
                
                if data.get('status') == 200:
                    # FinMind 回傳的 data 直接是陣列
                    return data.get('data', [])
                else:
                    logger.error(f"FinMind {dataset} 失敗：{data.get('status')} {data.get('msg', 'Unknown error')}")
                    return None
                    
            except requests.exceptions.Timeout:
                logger.warning(f"API 請求超時 (Attempt {attempt + 1})")
                time.sleep(self.retry_delay)
            except requests.exceptions.RequestException as e:
                logger.error(f"FinMind {dataset} 請求失敗 (Attempt {attempt + 1}): {e}")
                time.sleep(self.retry_delay)
        
        return None
    
    def get_revenue(self, stock_id: str, months: int = 3) -> Optional[Dict]:
        """
        取得股票月營收資料
        
        Args:
            stock_id: 股票代號
            months: 要取得的月數
            
        Returns:
            包含最新月營收年增率的字典
        """
        data = self._make_request('TaiwanStockMonthRevenue', stock_id, days=months*30) or []
        
        if not data:
            return None
        
        # 解析營收資料 (data 直接是陣列)
        revenue_data = data
        if not revenue_data:
            return None
        
        # 取最新一筆資料
        latest = revenue_data[-1]
        
        # 計算年增率
        current_revenue = float(latest.get('revenue', 0))
        
        # 簡化：假設 API 已提供 YoY 或我們用前後月比較
        # 實際應比較去年同月，此處簡化演示
        yoy_growth = 0.0
        if len(revenue_data) >= 2:
            prev_month = revenue_data[-2]
            prev_revenue = float(prev_month.get('revenue', 0))
            if prev_revenue > 0:
                yoy_growth = ((current_revenue - prev_revenue) / prev_revenue) * 100
        
        return {
            'date': latest.get('date', ''),
            'revenue': current_revenue,
            'yoy_growth': round(yoy_growth, 2)
        }
    
    def get_institutional_buy(self, stock_id: str, days: int = 10) -> Optional[int]:
        """
        取得投信連續買超天數
        
        Args:
            stock_id: 股票代號
            days: 檢查的天數範圍
            
        Returns:
            連續買超天數
        """
        data = self._make_request('TaiwanStockInstitutionalInvestorsBuySell', stock_id, days=days) or []
        
        if not data:
            return 0
        
        institutional_data = data
        if not institutional_data:
            return 0
        
        # 計算投信連續買超天數
        consecutive_buy_days = 0
        
        # 由最近往回推
        for record in reversed(institutional_data):
            buy = int(record.get('buy', 0))
            sell = int(record.get('sell', 0))
            net_buy = record.get('net_buy', buy - sell)
            
            if net_buy > 0:
                consecutive_buy_days += 1
            else:
                break
        
        return consecutive_buy_days
    
    def get_margin_balance(self, stock_id: str, days: int = 10) -> int:
        """
        計算融資增減 (今日餘額 - 昨日餘額)
        
        Args:
            stock_id: 股票代號
            days: 檢查的天數範圍
            
        Returns:
            融資增減張數
        """
        # ✅ 修正 Dataset 名稱
        data = self._make_request('TaiwanStockMarginPurchaseShortSale', stock_id, days=days)
        if not data or len(data) < 2:
            return 0
        
        # ✅ 修正欄位名稱 (取最近兩筆)
        latest = data[-1]
        prev = data[-2]
        
        today_balance = latest.get('MarginPurchaseTodayBalance', 0)
        yesterday_balance = prev.get('MarginPurchaseTodayBalance', 0)
        
        # 回傳增減張數 (FinMind 單位通常是張)
        return int(today_balance - yesterday_balance)
    
    def get_stock_price(self, stock_id: str, days: int = 30) -> Optional[List[float]]:
        """
        取得股票收盤價列表
        
        Args:
            stock_id: 股票代號
            days: 要取得的天數
            
        Returns:
            收盤價列表
        """
        data = self._make_request('TaiwanStockPrice', stock_id, days=days) or []
        
        if not data:
            return []
        
        price_data = data
        if not price_data:
            return []
        
        # 提取收盤價
        prices = []
        for record in price_data:
            close_price = float(record.get('close', 0))
            prices.append(close_price)
        
        return prices
    
    def get_financial_statements(self, code: str, days: int = 365) -> Optional[Dict]:
        """
        抓取財報數據（營收、毛利率、淨利率）
        
        Args:
            code: 股票代號
            days: 日期範圍天數
            
        Returns:
            包含最新財報數據的字典
        """
        # ✅ 使用正確的 Dataset 名稱（FinMind v4）
        # 先嘗試月營收（較穩定）
        try:
            data = self._make_request('TaiwanStockMonthRevenue', code, days=days)
            if data and len(data) > 0:
                latest = data[-1]
                return {
                    'revenue': latest.get('revenue', 0),
                    'revenue_yoy': latest.get('revenue_yoy', 0),
                    'gross_margin': 0,  # 月營收沒有毛利率
                    'net_margin': 0,
                    'eps': 0,
                }
        except Exception as e:
            logger.warning(f"TaiwanStockMonthRevenue 失敗：{e}")
        
        # 如果月營收失敗，嘗試完整財報
        try:
            data = self._make_request('TaiwanStockFinancialStatements', code, days=days)
            if data and len(data) > 0:
                latest = data[-1]
                revenue = latest.get('Revenue', 0) or 0
                gross_profit = latest.get('GrossProfit', 0) or 0
                net_income = latest.get('NetIncome', 0) or 0
                eps = latest.get('BasicEarningsPerShare', 0) or 0
                
                return {
                    'revenue': revenue,
                    'gross_margin': (gross_profit / revenue * 100) if revenue > 0 else 0,
                    'net_margin': (net_income / revenue * 100) if revenue > 0 else 0,
                    'eps': eps,
                }
        except Exception as e:
            logger.warning(f"TaiwanStockFinancialStatements 失敗：{e}")
        
        return None
    
    def get_technical_indicators(self, code: str) -> Dict:
        """
        計算技術指標（MA、RSI、MACD）
        
        Args:
            code: 股票代號
            
        Returns:
            包含技術指標的字典
        """
        prices = self.get_stock_price(code, days=60) or []
        if len(prices) < 20:
            return {'ma5': 0, 'ma20': 0, 'rsi': 50, 'macd': 0, 'price_above_ma20': False}
        
        # 計算移動平均線
        ma5 = sum(prices[-5:]) / 5
        ma20 = sum(prices[-20:]) / 20
        
        # 計算 RSI（14 日）
        rsi = self._calculate_rsi(prices, period=14)
        
        # 計算 MACD
        macd = self._calculate_macd(prices)
        
        return {
            'ma5': round(ma5, 2),
            'ma20': round(ma20, 2),
            'rsi': round(rsi, 2),
            'macd': round(macd, 2),
            'price_above_ma20': prices[-1] > ma20,
        }
    
    def _calculate_rsi(self, prices: List[float], period: int = 14) -> float:
        """計算 RSI 指標"""
        if len(prices) < period + 1:
            return 50.0
        
        changes = [prices[i] - prices[i-1] for i in range(-period, 0)]
        gains = [c for c in changes if c > 0]
        losses = [-c for c in changes if c < 0]
        
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    def _calculate_macd(self, prices: List[float]) -> float:
        """計算 MACD（簡化版）"""
        if len(prices) < 26:
            return 0.0
        
        ema12 = self._ema(prices[-12:], 12)
        ema26 = self._ema(prices[-26:], 26)
        return ema12 - ema26
    
    def _ema(self, prices: List[float], period: int) -> float:
        """計算指數移動平均線"""
        multiplier = 2 / (period + 1)
        ema = sum(prices) / len(prices)
        for price in prices:
            ema = (price - ema) * multiplier + ema
        return ema
