"""
FinMind API 客戶端模組
提供台股數據抓取功能：營收、投信籌碼、融資餘額、財報、技術指標等
"""
import os
import logging
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
    
    def _make_request(self, dataset: str, stock_id: str, days: int = 60, start_date: Optional[str] = None, end_date: Optional[str] = None) -> Optional[List[Dict]]:
        """
        發送 API 請求
        
        Args:
            dataset: 數據集名稱
            stock_id: 股票代號（v4 用 data_id）；空字串表示批量查詢（不帶 data_id）
            days: 日期範圍天數（當 start_date/end_date 未提供時使用）
            start_date: 開始日期（格式：YYYY-MM-DD），優先於 days
            end_date: 結束日期（格式：YYYY-MM-DD），優先於 days
            
        Returns:
            API 回應資料（data 陣列），失敗時返回 None
        """
        if not self.token:
            raise ValueError("FinMind Token 未設定")
        
        # 若有提供 start_date/end_date 則使用，否則用 days 計算
        if start_date and end_date:
            start_d = start_date
            end_d = end_date
        else:
            end = datetime.now()
            start = end - timedelta(days=days)
            start_d = start.strftime('%Y-%m-%d')
            end_d = end.strftime('%Y-%m-%d')
        
        request_params = {
            'dataset': dataset,
            'start_date': start_d,
            'end_date': end_d,
            'token': self.token,
        }
        # 🔴 關鍵修正（#90）：FinMind 的批量查詢要求「完全不帶」data_id 參數。
        # 送出 data_id=（空字串）會讓 TaiwanStockMonthRevenue /
        # TaiwanStockInstitutionalInvestorsBuySell 等端點回 400，
        # 導致海選抓不到任何資料卻被誤判為「市場太弱」。
        # 只有個股查詢（stock_id 非空）才加入 data_id。
        if stock_id:
            request_params['data_id'] = stock_id
        
        try:
            response = self.session.get(self.base_url, params=request_params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get('status') == 200:
                # FinMind 回傳的 data 直接是陣列
                return data.get('data', [])
            else:
                logger.error(f"FinMind {dataset} 失敗：{data.get('status')} {data.get('msg', 'Unknown error')}")
                return None
                
        except requests.exceptions.Timeout:
            print("API 請求超時")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"FinMind {dataset} 請求失敗：{e}")
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
    
    def get_stock_list(self) -> List[Dict]:
        """
        取得全台灣股票清單（不走日期範圍，避免被過濾）
        
        Returns:
            股票清單列表
        """
        if not self.token:
            raise ValueError("FinMind Token 未設定")
        
        params = {
            'dataset': 'TaiwanStockInfo',
            'data_id': '',
            'token': self.token,
        }
        
        try:
            response = self.session.get(self.base_url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            if data.get('status') == 200:
                return data.get('data', [])
            else:
                logger.error(f"TaiwanStockInfo 失敗：{data.get('status')}")
                return []
        except Exception as e:
            logger.error(f"取得股票清單失敗：{e}")
            return []
    
    def _get_raw_prices(self, stock_id: str, days: int = 60) -> List[float]:
        """內部使用：取得原始收盤價陣列"""
        data = self._make_request('TaiwanStockPrice', stock_id, days=days) or []
        return [float(r.get('close', 0)) for r in data]
    
    def get_stock_price(self, stock_id: str, days: int = 30) -> Optional[Dict]:
        """
        取得股票收盤價並壓縮為特徵值（降低 Token 消耗）
        
        【壓縮優化】
        - 不回傳完整價格陣列（原本 1000+ tokens）
        - 只回傳技術特徵（壓縮到 <50 tokens）
        
        Args:
            stock_id: 股票代號
            days: 要取得的天數
            
        Returns:
            包含價格特徵的字典：latest_price, ma5, ma20, price_trend
        """
        prices = self._get_raw_prices(stock_id, days)
        
        # ✅ 壓縮優化：只回傳特徵值，不回傳完整陣列
        if len(prices) < 5:
            return {
                'latest_price': prices[-1] if prices else 0,
                'ma5': 0,
                'ma20': 0,
                'price_trend': 'unknown'
            }
        
        ma5 = sum(prices[-5:]) / 5
        ma20 = sum(prices[-20:]) / 20 if len(prices) >= 20 else ma5
        price_trend = "up" if prices[-1] > prices[0] else "down"
        
        return {
            'latest_price': round(prices[-1], 2),
            'ma5': round(ma5, 2),
            'ma20': round(ma20, 2),
            'price_trend': price_trend
        }
    
    def get_financial_statements(self, code: str, days: int = 730) -> Optional[Dict]:
        """
        抓取財報數據（營收、毛利率、淨利率）——雙資料源：FinMind 主 + yfinance fallback

        🔴 422 根因修正（方案 C）：
        1. dataset 名稱錯誤：FinMind v4 正確的個股財報端點是
           「TaiwanStockFinancialStatements」，舊程式碼寫「FinancialStatements」
           （不存在的 dataset），API 直接回 422 Unprocessable Entity。
        2. 回傳格式誤判：該端點回傳的是「長表」格式
           [{date, stock_id, type, value, origin_name}, ...]，
           並非每季一筆的寬表。舊程式碼用 data[-1].get('Revenue')
           永遠取不到欄位，即使 200 也會全 None。
        3. 預設改為抓兩年，確保至少涵蓋四個完整季度
           （EPS 需要「最近四季」累计值，單季資料不足以計算）。

        🔴 方案 C 強化（本 PR）：FinMind 財報端點仍可能因 token 權限/
        服務異常回傳空資料或拋例外，此時自動 fallback 到 yfinance
        （台股代號 → {code}.TW），確保「雙重資料源」；兩邊都拿不到
        才回傳 None，讓 main.py 的前置攔截（方案 A）跳過 Groq。

        Args:
            code: 股票代號
            days: 日期範圍天數（預設 730，覆蓋最近四季財報）

        Returns:
            包含最新財報數據的字典（附 source 標記），取不到時回傳 None（而非 0.0）
        """
        try:
            result = self._get_financial_from_finmind(code, days=days)
        except Exception as e:
            logger.warning(f"FinMind FinancialStatements {code} 失敗：{e}，嘗試 yfinance fallback")
            result = None

        if result:
            return result

        # 🔴 方案 C-1B：FinMind 無資料 → fallback 到 yfinance
        logger.info(f"{code}: FinMind 財報無資料，改用 yfinance fallback")
        return self._get_financial_from_yfinance(code)

    def _get_financial_from_finmind(self, code: str, days: int = 730) -> Optional[Dict]:
        """FinMind TaiwanStockFinancialStatements 長表解析（原方案 C 邏輯）"""
        data = self._make_request('TaiwanStockFinancialStatements', code, days=days)
        if not data:
            return None

        # ── 長表 → 依季度分組 ──
        # structure: {date_str: {type: value}}
        by_date: Dict[str, Dict[str, float]] = {}
        for row in data:
            d = row.get('date')
            t = row.get('type')
            v = row.get('value')
            if not d or not t or v in (None, ''):
                continue
            try:
                by_date.setdefault(d, {})[t] = float(v)
            except (TypeError, ValueError):
                continue

        if not by_date:
            return None

        quarter_dates = sorted(by_date.keys())
        latest_date = quarter_dates[-1]
        latest = by_date[latest_date]

        # ── TTM（最近四季）加總：財報為累計制，需差分或直接加總可用季度 ──
        # 取最後 4 個季度的資料做 TTM 近似（不足 4 季時用現有季度）
        recent_quarters = [by_date[d] for d in quarter_dates[-4:]]

        def _sum_type(type_name: str) -> Optional[float]:
            vals = [q[type_name] for q in recent_quarters if type_name in q]
            return sum(vals) if vals else None

        # Revenue 欄位在不同公司可能為 'Revenue' 或其他別名
        revenue_ttm = _sum_type('Revenue')
        gross_profit_ttm = _sum_type('GrossProfit')
        # 稅後純益：FinMind 常見 type 為 IncomeAfterTaxes / NetIncomeAfterTax
        net_income_ttm = (_sum_type('IncomeAfterTaxes')
                          or _sum_type('NetIncomeAfterTax')
                          or _sum_type('NetIncome'))

        # 🔴 P0-1 修正精神延續：取不到數據時回傳 None，讓前端/prompt 顯示 '-'
        gross_margin = (gross_profit_ttm / revenue_ttm * 100) if (gross_profit_ttm is not None and revenue_ttm and revenue_ttm > 0) else None
        net_margin = (net_income_ttm / revenue_ttm * 100) if (net_income_ttm is not None and revenue_ttm and revenue_ttm > 0) else None

        # ── EPS：改用獨立的 TaiwanStockTax dataset（含每股盈餘欄位）──
        eps_val = self._get_eps(code)

        has_any = any(v is not None for v in (revenue_ttm, gross_margin, net_margin, eps_val))
        if not has_any:
            return None

        return {
            'revenue': revenue_ttm if (revenue_ttm is not None and revenue_ttm > 0) else None,
            'gross_margin': round(gross_margin, 2) if gross_margin is not None else None,
            'net_margin': round(net_margin, 2) if net_margin is not None else None,
            'eps': eps_val,
            'fiscal_quarter': latest_date,
            'source': 'FinMind',
        }

    def _get_financial_from_yfinance(self, code: str) -> Optional[Dict]:
        """
        🔴 方案 C-1B：yfinance fallback——台股代號轉 {code}.TW 抓取財報。

        FinMind 財報端點因 token 權限／服務異常拿不到資料時的最後防線。
        使用 income_stmt（損益表）最近一期計算毛利率/淨利率，
        trailingEps 取得 EPS。任何例外一律回傳 None（不拋出），
        讓上層 main.py 的前置攔截決定是否跳過 Groq。
        """
        try:
            import yfinance as yf

            yf_code = f"{code}.TW"
            stock = yf.Ticker(yf_code)

            income_stmt = stock.income_stmt
            if income_stmt is None or income_stmt.empty:
                logger.warning(f"yfinance {yf_code} 無財報數據")
                return None

            # 取最近一期（第一欄）
            latest_col = income_stmt.iloc[:, 0]

            def _num(key: str) -> float:
                try:
                    v = latest_col.get(key)
                    if v is None or (isinstance(v, float) and v != v):  # NaN check
                        return 0.0
                    return float(v)
                except (TypeError, ValueError):
                    return 0.0

            revenue = _num('Total Revenue')
            gross_profit = _num('Gross Profit')
            net_income = _num('Net Income')

            gross_margin = (gross_profit / revenue * 100) if revenue > 0 else None
            net_margin = (net_income / revenue * 100) if revenue > 0 else None

            # EPS：trailingEps；虧損（負值）也視為有效資訊，但 0/缺漏回 None
            eps_val = None
            try:
                raw_eps = (stock.info or {}).get('trailingEps')
                if raw_eps not in (None, '', 0):
                    eps_val = round(float(raw_eps), 2)
            except (TypeError, ValueError, KeyError):
                eps_val = None

            has_any = any(v is not None for v in (gross_margin, net_margin, eps_val))
            if not has_any:
                logger.warning(f"yfinance {yf_code} 財報欄位全缺")
                return None

            logger.info(f"yfinance {yf_code} 成功抓取財報（來源：yfinance）")
            return {
                'revenue': revenue if revenue > 0 else None,
                'gross_margin': round(gross_margin, 2) if gross_margin is not None else None,
                'net_margin': round(net_margin, 2) if net_margin is not None else None,
                'eps': eps_val,
                'fiscal_quarter': str(income_stmt.columns[0].date()) if hasattr(income_stmt.columns[0], 'date') else str(income_stmt.columns[0]),
                'source': 'yfinance',
            }

        except Exception as e:
            logger.error(f"yfinance fallback {code} 失敗：{e}")
            return None

    def _get_eps(self, code: str) -> Optional[float]:
        """
        取得每股盈餘（EPS，元/股）。

        🔴 免費版可用資料源：FinMind v4 沒有公开的 TaiwanStockTax dataset
        （會回 422），改用「TaiwanStockPER」端點——它提供每日 PER/PBR/殖利率，
        搭配最新收盤价即可反推 EPS = 股價 / PER。
        PER <= 0（虧損股無本益比）或資料缺失時回傳 None。
        """
        data = self._make_request('TaiwanStockPER', code, days=60) or []
        if not data:
            return None

        # 取最新一筆有效 PER
        per_val = None
        for row in reversed(data):
            raw = row.get('PER')
            if raw in (None, ''):
                continue
            try:
                per_val = float(raw)
            except (TypeError, ValueError):
                continue
            break

        if not per_val or per_val <= 0:
            return None

        prices = self._get_raw_prices(code, days=30) or []
        if not prices:
            return None

        eps = prices[-1] / per_val
        return round(eps, 2)
    
    def get_technical_indicators(self, code: str) -> Dict:
        """
        計算技術指標（MA、RSI、MACD）
        
        Args:
            code: 股票代號
            
        Returns:
            包含技術指標的字典
        """
        prices = self._get_raw_prices(code, days=60)
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
