"""
Issue #110（P2）單元測試：
- P2-1 規則引擎產業調整：金融股忽略短期動能（change_5d），改以股價位置（MA20 之上）＋籌碼面評分
- P2-2 規則引擎 vs Groq 勝率追蹤：update_performance_metrics 產生 model_stats
"""
import json
import pathlib
import tempfile

from src.main import (
    FINANCIAL_INDUSTRY_KEYWORDS,
    generate_rule_based_analysis,
    is_financial_industry,
    update_performance_metrics,
)


class TestIsFinancialIndustry:
    def test_detects_financial_keywords(self):
        for kw in FINANCIAL_INDUSTRY_KEYWORDS:
            assert is_financial_industry(kw)
            assert is_financial_industry(f'{kw}業')

    def test_non_financial(self):
        assert not is_financial_industry('半導體')
        assert not is_financial_industry('電子')
        assert not is_financial_industry('')
        assert not is_financial_industry(None)


class TestRuleEngineFinancialAdjustment:
    """P2-1：金融股不適用「短期漲勢加分」，改用 MA20 位置＋籌碼"""

    BASE = {'rsi': 50, 'ma20': 100, 'macd': 0, 'volatility': 25, 'inst_buy_days': 0}

    def _mk(self, **over):
        d = dict(self.BASE, **over)
        return generate_rule_based_analysis(d)

    def test_financial_ignores_positive_momentum(self):
        """金融股 +5日大漲 → 不應出現「短期強勢」加分"""
        result = self._mk(industry='金融', change_5d=8, current_price=100)
        assert '短期強勢' not in result['reason']

    def test_financial_ignores_negative_momentum(self):
        """金融股 -5日大跌 → 不應出現「短期弱勢」扣分"""
        result = self._mk(industry='銀行', change_5d=-8, current_price=100)
        assert '短期弱勢' not in result['reason']

    def test_financial_above_ma20_gets_position_bonus(self):
        """金融股 + 股價在 MA20 上 → 位置加分（+10）"""
        above = self._mk(industry='金控', change_5d=0, current_price=105)
        below = self._mk(industry='金控', change_5d=0, current_price=95)
        assert above['ev_score'] == below['ev_score'] + 10
        assert '股價於 MA20 上' in above['reason']

    def test_nonfinancial_still_uses_momentum(self):
        """非金融股維持原邏輯：短期強勢 +10 / 短期弱勢 -10"""
        strong = self._mk(industry='半導體', change_5d=8, current_price=100)
        weak = self._mk(industry='半導體', change_5d=-8, current_price=100)
        assert '短期強勢' in strong['reason']
        assert '短期弱勢' in weak['reason']
        assert strong['ev_score'] == weak['ev_score'] + 20

    def test_financial_momentum_does_not_change_score(self):
        """金融股無論漲跌，分數不因 change_5d 變動（與持平相同）"""
        flat = self._mk(industry='金融', change_5d=0, current_price=100)
        surge = self._mk(industry='金融', change_5d=15, current_price=100)
        dump = self._mk(industry='金融', change_5d=-15, current_price=100)
        assert flat['ev_score'] == surge['ev_score'] == dump['ev_score']


class TestModelStats:
    """P2-2：performance_metrics.json 加入 model_stats（rule-based-engine vs groq）"""

    def _run(self, records):
        with tempfile.TemporaryDirectory() as tmp:
            import src.main as m
            data_dir = pathlib.Path(tmp)
            orig_data, orig_path = m.DATA_DIR, m.path if hasattr(m, 'path') else None
            metrics_path = data_dir / 'performance_metrics.json'
            # monkeypatch DATA_DIR 以隔離寫入路徑
            m.DATA_DIR = data_dir
            try:
                update_performance_metrics({'records': records, 'metadata': {}})
                return json.loads(metrics_path.read_text(encoding='utf-8'))
            finally:
                m.DATA_DIR = orig_data

    def test_model_stats_split_by_engine(self):
        records = [
            {'model': 'rule-based-engine', 'accuracy': 1},
            {'model': 'rule-based-engine', 'accuracy': 0},
            {'model': 'rule-based-engine', 'accuracy': None},
            {'model': 'qwen/qwen3.6-27b', 'accuracy': 1},
            {'model': 'llama-3.3-70b-versatile', 'accuracy': 1},
            {'model': 'llama-3.3-70b-versatile', 'accuracy': 0},
        ]
        metrics = self._run(records)
        ms = metrics['model_stats']
        assert ms['rule-based-engine'] == {
            'predictions': 3, 'verified': 2, 'correct': 1, 'accuracy': 50.0}
        # AI 模型名合併到 groq 鍵
        assert ms['groq']['predictions'] == 3
        assert ms['groq']['verified'] == 3
        assert ms['groq']['correct'] == 2
        assert ms['groq']['accuracy'] == round(2 / 3 * 100, 1)

    def test_no_verified_samples_accuracy_none(self):
        """無已驗證樣本 → accuracy 為 None（前端顯示資料不足），不得填 0"""
        records = [{'model': 'rule-based-engine', 'accuracy': None}]
        ms = self._run(records)['model_stats']
        assert ms['rule-based-engine']['accuracy'] is None
        assert ms['rule-based-engine']['verified'] == 0

    def test_missing_model_field_bucketed_as_unknown_into_groq(self):
        """舊記錄無 model 欄位 → 歸 unknown，併入 groq 統計不遺漏筆數"""
        records = [{'accuracy': 1}, {'model': 'rule-based-engine', 'accuracy': 1}]
        ms = self._run(records)['model_stats']
        assert ms['groq']['predictions'] == 1
        assert ms['rule-based-engine']['predictions'] == 1
