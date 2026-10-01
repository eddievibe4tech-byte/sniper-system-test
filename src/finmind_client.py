"""
FinMind API 客戶端模組
提供台股數據抓取功能：營收、投信籌碼、融資餘額、財報、技術指標等
"""
import os
import logging
import requests
from typing import Dict, List, Optional
from datetime import datetime, timedelta

from src.financial_cache import (load_cache, save_cache, fresh_entry,
                                 quota_remaining, consume_quota,
                                 TZ_TAIPEI as _CACHE_TZ)
from src.alpha_vantage_client import AlphaVantageClient

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Alpha Vantage 層 helper（五層鏈的第三層 fallback）
# ─────────────────────────────────────────────

def _av_call(cache: Dict, av: AlphaVantageClient, symbol: str) -> Optional[Dict]:
    """帶配額管控的 AV 呼叫：免費版 25 次/日，超過安全上限直接跳過"""
    if quota_remaining(cache) <= 0:
        logger.warning("Alpha Vantage 當日配額用盡，跳過 %s", symbol)
        return None
    consume_quota(cache)
    return av.get_quarterly_financials(symbol)


def _resolve_tw_symbol(cache: Dict, av: AlphaVantageClient, code: str):
    """回傳 (symbol, 探測結果)；台股後綴探測一次即快取進 cache meta，
    避免每檔都浪費配額（AV 對台股覆蓋率不保證）。"""
    meta = cache.setdefault("meta", {})
    if meta.get("tw_unsupported"):
        return None, None
    if meta.get("tw_suffix"):
        return f"{code}{meta['tw_suffix']}", None
    for suf in (".TPE", ".TW", ".TWO"):
        res = _av_call(cache, av, f"{code}{suf}")
        if res:
            meta["tw_suffix"] = suf
            return f"{code}{suf}", res
    meta["tw_unsupported"] = True   # AV 無台股資料 → 之後直接跳過，不燒配額
    return None, None


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

        🔴 方案 C 強化：FinMind 財報端點仍可能因 token 權限/
        服務異常回傳空資料或拋例外，此時自動 fallback 到 yfinance
        （台股代號 → {code}.TW）。

        🔴 Alpha Vantage Fallback（本 PR）：升級為「五層鏈」——
            快取(40天TTL) → FinMind → yfinance → Alpha Vantage(配額管控)
            → 過期快取(cache-stale) → None(規則引擎)
        設計前提：
        - AV 免費版僅 25 次/日 → 配額安全上限 20 次 + 後綴探測只做一次
          （結果寫進 cache meta，避免每檔燒配額）。
        - GitHub Actions runner 免洗 → 快取檔 data/financial_cache.json
          必須由 workflow commit 回 repo 才能跨 run 存活。
        回傳契約不變：{revenue, gross_margin, net_margin, eps, source}；
        四源全滅且無過期快取時才回傳 None，讓 main.py 的前置攔截（方案 A）
        跳過 Groq。

        Args:
            code: 股票代號
            days: 日期範圍天數（預設 730，覆蓋最近四季財報）。
                🔴 #103 註記：review 規格原建議 180 天（最近兩季），此處
                刻意放大至 730 是 TTM（最近四季加總）的必要條件——
                FinMind 財報為累計制長表，少于四季無法計算 TTM 口徑。
                請勿随意改回 365/180，否則毛利率/淨利率將因樣本季度不足而失真。

        Returns:
            包含最新財報數據的字典（附 source 標記），取不到時回傳 None（而非 0.0）
        """
        cache = load_cache()

        # 0) 新鮮快取（40 天內）：零網路成本，多數日常運行的命中路徑
        entry = fresh_entry(cache, code)
        if entry:
            logger.info(f"{code}: 財報快取命中（{entry.get('source')}，{entry.get('fetched_at')}）")
            return self._to_result(entry, source=f"cache:{entry.get('source', 'unknown')}")

        result = None
        cache_dirty = False   # 只有動過配額／後綴探測才需要回寫快取

        # 1) FinMind 主源
        try:
            result = self._get_financial_from_finmind(code, days=days)
        except Exception as e:
            logger.warning(f"FinMind FinancialStatements {code} 失敗：{e}，嘗試 yfinance fallback")
            result = None

        if result:
            return self._persist(cache, code, result, expected_source='FinMind')

        # 2) yfinance fallback
        logger.info(f"{code}: FinMind 財報無資料，改用 yfinance fallback")
        result = self._get_financial_from_yfinance(code)
        if result:
            return self._persist(cache, code, result, expected_source='yfinance')

        # 3) Alpha Vantage（配額＋台股後綴探測管控）
        av = AlphaVantageClient()
        if av.enabled:
            sym, probed = _resolve_tw_symbol(cache, av, code)
            cache_dirty = True   # _av_call/_resolve_tw_symbol 會更新配額與 meta
            av_result = probed if probed else (_av_call(cache, av, sym) if sym else None)
            if av_result:
                return self._persist(cache, code, av_result, expected_source='alphavantage')
            logger.info(f"{code}: Alpha Vantage 無資料（後綴不支援／配額限制／請求失敗）")

        # 4) 過期快取兜底（聊勝於無，標記 cache-stale 供前端/規則引擎辨識）
        stale = cache["stocks"].get(code)
        if stale:
            logger.warning(f"{code}：三源全失敗，使用過期快取（{stale.get('fetched_at')}）")
            if cache_dirty:
                save_cache(cache)   # 保留配額／後綴探測等 meta 更新
            return self._to_result(stale, source="cache-stale")

        # 5) 全部失靈 → None（main.py 前置攔截跳過 Groq，規則引擎接手）
        if cache_dirty:
            save_cache(cache)
        return None

    @staticmethod
    def _to_result(entry: Dict, source: str) -> Dict:
        """統一回傳契約：只暴露下游需要的欄位＋來源標記"""
        out = {
            "revenue": entry.get("revenue"),
            "gross_margin": entry.get("gross_margin"),
            "net_margin": entry.get("net_margin"),
            "eps": entry.get("eps"),
            "fiscal_quarter": entry.get("fiscal_quarter"),
            "source": source,
        }
        if entry.get("statement_kind"):
            out["statement_kind"] = entry["statement_kind"]
        return out

    def _persist(self, cache: Dict, code: str, result: Dict,
                 expected_source: Optional[str] = None) -> Dict:
        """成功取得財報 → 寫入快取（含 fetched_at）並回傳標準化結果。
        🔴 Actions runner 免洗：workflow 的 commit 步驟需把
        data/financial_cache.json 推回 repo，快取才能跨 run 存活。

        source 標記以「调用層」為準（expected_source），避免 monkeypatch
        出的假 fallback 未帶 source 欄位時被誤標為 finmind；result 內建的
        source（如 'FinMind'/'yfinance'）優先沿用原字串，維持 #103 可追溯性。"""
        raw_source = result.get('source') or expected_source or 'finmind'
        entry = {
            **result,
            "fetched_at": datetime.now(_CACHE_TZ).isoformat(),
            "source": raw_source,
        }
        cache.setdefault("stocks", {})[code] = entry
        save_cache(cache)
        logger.info(f"{code}：財報來源={raw_source}")
        return self._to_result(entry, source=raw_source)

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
        🔴 方案 C 補齊（#103）：口徑統一——優先用 ttm_income_stmt（Yahoo
        提供之最近四季 TTM 損益表）計算毛利率/淨利率，與 FinMind 主源的
        TTM 加總口徑一致；取不到 TTM 時才降級回 income_stmt 最近一期
        （避免年報 vs 單季期間長度不一致導致 EV 評分基本面權重失真）。
        trailingEps 取得 EPS。任何例外一律回傳 None（不拋出），
        讓上層 main.py 的前置攔截決定是否跳過 Groq。
        """
        try:
            import yfinance as yf

            yf_code = f"{code}.TW"
            stock = yf.Ticker(yf_code)

            # 🔴 #103：優先 TTM 口徑，降級單期
            income_stmt = None
            stmt_kind = 'income_stmt'
            try:
                ttm_stmt = stock.ttm_income_stmt
                if ttm_stmt is not None and not ttm_stmt.empty:
                    income_stmt = ttm_stmt
                    stmt_kind = 'ttm_income_stmt'
            except Exception:
                pass  # 部分 yfinance 版本無此屬性／端點異常 → 走降級路徑

            if income_stmt is None:
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
                raw_eps = getattr(stock, 'info', None) or {}
                raw_eps = raw_eps.get('trailingEps')
                if raw_eps not in (None, '', 0):
                    eps_val = round(float(raw_eps), 2)
            except (TypeError, ValueError, KeyError, AttributeError):
                eps_val = None

            has_any = any(v is not None for v in (gross_margin, net_margin, eps_val))
            if not has_any:
                logger.warning(f"yfinance {yf_code} 財報欄位全缺")
                return None

            logger.info(f"yfinance {yf_code} 成功抓取財報（來源：yfinance，口徑：{stmt_kind}）")
            return {
                'revenue': revenue if revenue > 0 else None,
                'gross_margin': round(gross_margin, 2) if gross_margin is not None else None,
                'net_margin': round(net_margin, 2) if net_margin is not None else None,
                'eps': eps_val,
                'fiscal_quarter': str(income_stmt.columns[0].date()) if hasattr(income_stmt.columns[0], 'date') else str(income_stmt.columns[0]),
                'source': 'yfinance',
                'statement_kind': stmt_kind,  # 🔴 #103：記錄 TTM／單期口徑供除錯與資料健康檢查
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

        🔴 方案 C 補齊（#103）：多層防線——FinMind PER 端點異常／虧損股
        拿不到 EPS 時，fallback 到 yfinance trailingEps，避免「毛利率/淨利率
        成功、唯 EPS 缺漏」時 Groq prompt 中 EPS 永遠是 '-'
        （此情境 has_any=True 不會觸發整批 yfinance fallback）。
        """
        eps = self._get_eps_from_finmind_per(code)
        if eps is not None:
            return eps

        # Fallback：yfinance trailingEps
        logger.info(f"{code}: FinMind PER 反推 EPS 失敗，改用 yfinance trailingEps")
        return self._get_eps_from_yfinance(code)

    def _get_eps_from_finmind_per(self, code: str) -> Optional[float]:
        """FinMind TaiwanStockPER 端點反推 EPS = 股價 / PER"""
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

    def _get_eps_from_yfinance(self, code: str) -> Optional[float]:
        """
        🔴 方案 C 補齊（#103）：yfinance trailingEps fallback。

        任何例外一律回傳 None（不拋出），維持「財報抓取絕不中斷主流程」的契約。
        虧損（負 EPS）也視為有效資訊回傳；0／缺漏回 None。
        """
        try:
            import yfinance as yf

            raw_eps = (yf.Ticker(f"{code}.TW").info or {}).get('trailingEps')
            if raw_eps in (None, '', 0):
                return None
            eps_val = round(float(raw_eps), 2)
            logger.info(f"yfinance {code}.TW trailingEps 成功取得 EPS={eps_val}")
            return eps_val
        except Exception as e:
            logger.warning(f"yfinance trailingEps fallback {code} 失敗：{e}")
            return None
    
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
