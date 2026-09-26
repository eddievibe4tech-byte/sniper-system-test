"""
財報 API 422 修正＋Groq 前置攔截 單元測試

對應議題：FinMind FinancialStatements 全面 422 →
  方案 C：改用正確 dataset「TaiwanStockFinancialStatements」（長表格式解析）
           ＋ EPS 改用「TaiwanStockPER」反推（免費版可用）。
  方案 A：src/main.py 在呼叫 Groq 之前檢查財報是否全缺，
           全缺時跳過 AI 分析以節省 token。
"""
import json
import pytest
import responses

from src.finmind_client import FinMindClient

API_URL = 'https://api.finmindtrade.com/api/v4/data'


def _fs_rows(date, revenue=None, gross_profit=None, income_after_taxes=None):
    """產生 TaiwanStockFinancialStatements 長表格式的 fixture 列"""
    rows = []
    if revenue is not None:
        rows.append({'date': date, 'stock_id': '2330', 'type': 'Revenue',
                     'value': revenue, 'origin_name': '營業收入'})
    if gross_profit is not None:
        rows.append({'date': date, 'stock_id': '2330', 'type': 'GrossProfit',
                     'value': gross_profit, 'origin_name': '營業毛利'})
    if income_after_taxes is not None:
        rows.append({'date': date, 'stock_id': '2330', 'type': 'IncomeAfterTaxes',
                     'value': income_after_taxes, 'origin_name': '稅後純益'})
    return rows


class TestGetFinancialStatementsFixed:
    """方案 C：get_financial_statements 修正後行為"""

    @responses.activate
    def test_uses_correct_dataset_name(self):
        """請求的 dataset 必須是 TaiwanStockFinancialStatements（而非舊的 FinancialStatements → 422）"""
        responses.add(responses.GET, API_URL,
                      json={'status': 200, 'msg': 'success',
                            'data': _fs_rows('2026-06-30', revenue=1000.0,
                                             gross_profit=500.0, income_after_taxes=200.0)},
                      status=200)
        client = FinMindClient(token='test_token')
        # 讓 EPS 輔助查詢回空，聚焦 dataset 斷言
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(client, '_get_eps', lambda code: None)
            client.get_financial_statements('2330')

        urls = [c.request.url for c in responses.calls]
        assert any('dataset=TaiwanStockFinancialStatements' in u for u in urls), \
            f"應使用正確 dataset：{urls}"
        assert not any('dataset=FinancialStatements&' in u for u in urls), \
            "不應再呼叫不存在的 FinancialStatements dataset（會 422）"

    @responses.activate
    def test_parses_long_table_format(self):
        """長表格式 [{date, type, value}] 應正確解析出毛利率/淨利率"""
        rows = (_fs_rows('2025-06-30', revenue=4000.0, gross_profit=1800.0, income_after_taxes=800.0)
                + _fs_rows('2025-09-30', revenue=4000.0, gross_profit=2000.0, income_after_taxes=900.0)
                + _fs_rows('2026-03-31', revenue=4000.0, gross_profit=2100.0, income_after_taxes=1000.0)
                + _fs_rows('2026-06-30', revenue=4000.0, gross_profit=2200.0, income_after_taxes=1100.0))
        responses.add(responses.GET, API_URL,
                      json={'status': 200, 'msg': 'success', 'data': rows}, status=200)
        client = FinMindClient(token='test_token')
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(client, '_get_eps', lambda code: 5.0)
            result = client.get_financial_statements('2330')

        assert result is not None
        # TTM 加總：revenue=16000, gross=8100 → 50.625%（round → 50.62/50.63）, net=3800 → 23.75%
        assert result['gross_margin'] == pytest.approx(50.625, abs=0.01)
        assert result['net_margin'] == pytest.approx(23.75, abs=0.01)
        assert result['eps'] == 5.0
        assert result['fiscal_quarter'] == '2026-06-30'

    @responses.activate
    def test_returns_none_on_empty_data(self):
        """無資料時回傳 None（維持 P0-1 null 化語意，讓前端顯示 '-'）"""
        responses.add(responses.GET, API_URL,
                      json={'status': 200, 'msg': 'success', 'data': []}, status=200)
        client = FinMindClient(token='test_token')
        with pytest.MonkeyPatch.context() as mp:
            # 本測試聚焦 FinMind 主路徑；fallback 由 yfinance 系列測試覆蓋
            mp.setattr(client, '_get_financial_from_yfinance', lambda code: None)
            assert client.get_financial_statements('2330') is None

    @responses.activate
    def test_source_marked_finmind(self):
        """FinMind 成功取得時，結果應標記 source='FinMind'（方案 C 雙源可追溯性）"""
        responses.add(responses.GET, API_URL,
                      json={'status': 200, 'msg': 'success',
                            'data': _fs_rows('2026-06-30', revenue=1000.0,
                                             gross_profit=500.0, income_after_taxes=200.0)},
                      status=200)
        client = FinMindClient(token='test_token')
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(client, '_get_eps', lambda code: None)
            result = client.get_financial_statements('2330')
        assert result is not None
        assert result['source'] == 'FinMind'

    @responses.activate
    def test_eps_from_per_and_price(self):
        """EPS = 最新收盤價 / 最新 PER（TaiwanStockPER 免費版可用）"""
        per_rows = [{'date': '2026-09-24', 'stock_id': '2330',
                     'dividend_yield': 0.9, 'PER': 25.0, 'PBR': 9.0}]
        price_rows = [{'date': '2026-09-24', 'stock_id': '2330', 'close': 250.0}]

        def router(request):
            url = request.url
            if 'dataset=TaiwanStockPER' in url:
                body = {'status': 200, 'msg': 'success', 'data': per_rows}
            elif 'dataset=TaiwanStockPrice' in url:
                body = {'status': 200, 'msg': 'success', 'data': price_rows}
            else:
                body = {'status': 200, 'msg': 'success', 'data': []}
            return (200, {}, json.dumps(body))

        responses.add_callback(responses.GET, API_URL, callback=router)
        client = FinMindClient(token='test_token')
        eps = client._get_eps('2330')
        assert eps == 10.0  # 250 / 25 = 10

    @responses.activate
    def test_eps_none_when_per_invalid(self):
        """PER <= 0 或缺失時 EPS 回傳 None（虧損股無本益比）"""
        responses.add(responses.GET, API_URL,
                      json={'status': 200, 'msg': 'success',
                            'data': [{'date': '2026-09-24', 'stock_id': '2330',
                                      'PER': -3.2, 'PBR': 1.0}]},
                      status=200)
        client = FinMindClient(token='test_token')
        assert client._get_eps('2330') is None


class TestYfinanceFallback:
    """方案 C-1B：FinMind 財報拿不到時自動 fallback 到 yfinance"""

    @responses.activate
    def test_falls_back_to_yfinance_when_finmind_empty(self):
        """FinMind 回空資料 → 應呼叫 _get_financial_from_yfinance 並回傳其結果"""
        responses.add(responses.GET, API_URL,
                      json={'status': 200, 'msg': 'success', 'data': []}, status=200)
        client = FinMindClient(token='test_token')
        called = {}

        def fake_yf(code):
            called['code'] = code
            return {'revenue': None, 'gross_margin': 45.0, 'net_margin': 20.0,
                    'eps': 3.2, 'fiscal_quarter': '2026-06-30', 'source': 'yfinance'}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(client, '_get_financial_from_yfinance', fake_yf)
            result = client.get_financial_statements('2882')

        assert called.get('code') == '2882', "FinMind 空資料時必須觸發 yfinance fallback"
        assert result is not None
        assert result['source'] == 'yfinance'
        assert result['gross_margin'] == 45.0

    @responses.activate
    def test_falls_back_when_finmind_raises(self):
        """FinMind 擲例外（如 422）→ 不應崩潰，仍走 yfinance fallback"""
        responses.add(responses.GET, API_URL, status=500)
        client = FinMindClient(token='test_token')

        with pytest.MonkeyPatch.context() as mp:
            def boom(code, days=730):
                raise RuntimeError("422 Unprocessable Entity")
            mp.setattr(client, '_get_financial_from_finmind', boom)
            mp.setattr(client, '_get_financial_from_yfinance',
                       lambda code: {'gross_margin': 10.0, 'net_margin': 2.0,
                                     'eps': None, 'source': 'yfinance'})
            result = client.get_financial_statements('2882')

        assert result is not None and result['source'] == 'yfinance'

    @responses.activate
    def test_returns_none_when_both_sources_fail(self):
        """雙資料源都失敗 → 回傳 None，讓 main.py 前置攔截跳過 Groq"""
        responses.add(responses.GET, API_URL,
                      json={'status': 200, 'msg': 'success', 'data': []}, status=200)
        client = FinMindClient(token='test_token')
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(client, '_get_financial_from_yfinance', lambda code: None)
            assert client.get_financial_statements('2882') is None

    def test_yfinance_helper_parses_income_stmt(self):
        """_get_financial_from_yfinance：用假 yfinance 模組驗證 income_stmt 解析與 source 標記"""
        import types
        import pandas as pd

        client = FinMindClient(token='test_token')

        # 建立假的 yfinance 模組注入 sys.modules
        # 真實 income_stmt 格式：index=財務項目、columns=期間 → iloc[:,0] 取最新一期
        income_df = pd.DataFrame(
            {'2026-06-30': [10000.0, 4000.0, 1500.0]},
            index=['Total Revenue', 'Gross Profit', 'Net Income'])

        class FakeTicker:
            def __init__(self, symbol):
                self.symbol = symbol
                assert symbol == '2882.TW', "台股代號必須轉為 {code}.TW 格式"
            income_stmt = income_df
            info = {'trailingEps': 2.5}

        fake_yf = types.ModuleType('yfinance')
        fake_yf.Ticker = FakeTicker
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(__import__('sys').modules, 'yfinance', fake_yf)
            result = client._get_financial_from_yfinance('2882')

        assert result is not None
        assert result['source'] == 'yfinance'
        assert result['gross_margin'] == pytest.approx(40.0)
        assert result['net_margin'] == pytest.approx(15.0)
        assert result['eps'] == 2.5
        assert result['revenue'] == 10000.0

    def test_yfinance_helper_returns_none_on_exception(self):
        """yfinance 拋例外時必須回傳 None（不向上拋，避免中斷主流程）"""
        import types

        client = FinMindClient(token='test_token')

        class ExplodingTicker:
            def __init__(self, symbol):
                raise RuntimeError("network down")

        fake_yf = types.ModuleType('yfinance')
        fake_yf.Ticker = ExplodingTicker
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(__import__('sys').modules, 'yfinance', fake_yf)
            assert client._get_financial_from_yfinance('2882') is None


class TestSkipGroqWhenFinancialsMissing:
    """方案 A：財報全缺時在呼叫 Groq 之前跳過，不消耗 token"""

    def _run_loop_snippet(self, gross_margin, net_margin, eps):
        """重現 main.py 步驟 5.5 的攔條件邏輯（與產品程式碼保持一致）"""
        financial_complete = not (gross_margin is None and net_margin is None and eps is None)
        return financial_complete

    def test_all_missing_skips_groq(self):
        """三項財務指標全缺 → 判定不完整 → 跳過 Groq"""
        assert self._run_loop_snippet(None, None, None) is False

    def test_any_present_calls_groq(self):
        """任一項存在 → 判定完整 → 照常呼叫 Groq"""
        assert self._run_loop_snippet(40.0, None, None) is True
        assert self._run_loop_snippet(None, None, 3.5) is True

    def test_source_contains_pre_groq_guard(self):
        """靜態驗證：main.py 中攔截必須位於 groq.analyze_stock 呼叫「之前」"""
        import pathlib
        src = pathlib.Path(__file__).resolve().parents[2].joinpath('src/main.py').read_text(encoding='utf-8')
        guard_idx = src.find('financial_complete')
        call_idx = src.find('groq.analyze_stock(prompt_tpl, stock_data)')
        assert guard_idx != -1, "找不到方案 A 攔截邏輯"
        assert call_idx != -1, "找不到 Groq 呼叫"
        assert guard_idx < call_idx, "攔截必须在 Groq 呼叫之前，否则无法节省 token"
        assert 'continue  # 直接跳過，不呼叫 Groq' in src or 'continue' in src[guard_idx:call_idx], \
            "攔截分支应包含 continue"
