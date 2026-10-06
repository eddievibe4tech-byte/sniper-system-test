"""
#158 訊號衝突降級鐵律與時間維度標籤 單元測試

驗證 apply_signal_conflict_logic()：
1. 基本面強 + 跌破 MA20 → 強制降級「觀望」，保留 original_recommendation，signal_tag = 長線配置/短期觀望
2. 基本面強 + 技術面健康 → signal_tag = 長線配置，不降級
3. 基本面弱 → signal_tag = 短線狙擊
4. MA20 缺失（<=0）→ 不觸發降級（防禦性）
"""
import pytest

from src.main import apply_signal_conflict_logic


@pytest.mark.unit
class TestApplySignalConflictLogic:

    def test_conflict_downgrade(self):
        """基本面強(EV>=70)且跌破 MA20 → 降級為觀望並標記衝突標籤"""
        stock = {
            'code': '2330',
            'ev_score': 80,
            'recommendation': '積極買入',
            'current_price': 95.0,
            'ma20': 100.0,
            'reason': 'AI 原始理由',
        }
        result = apply_signal_conflict_logic(stock)

        assert result['recommendation'] == '觀望'
        assert result['original_recommendation'] == '積極買入'
        assert result['signal_tag'] == '長線配置/短期觀望'
        assert '訊號衝突' in result['reason']
        assert 'MA20' in result['reason']

    def test_conflict_downgrade_by_active_buy_rec(self):
        """EV 未達 70 但 recommendation=積極買入 且跌破 MA20 → 仍降級"""
        stock = {
            'ev_score': 60,
            'recommendation': '積極買入',
            'current_price': 50.0,
            'ma20': 55.0,
        }
        result = apply_signal_conflict_logic(stock)

        assert result['recommendation'] == '觀望'
        assert result['original_recommendation'] == '積極買入'
        assert result['signal_tag'] == '長線配置/短期觀望'

    def test_strong_fundamental_above_ma20_no_downgrade(self):
        """基本面強且站上 MA20 → 長線配置，不降級、不覆寫 reason"""
        stock = {
            'ev_score': 75,
            'recommendation': '謹慎買入',
            'current_price': 105.0,
            'ma20': 100.0,
            'reason': '正常理由',
        }
        result = apply_signal_conflict_logic(stock)

        assert result['recommendation'] == '謹慎買入'
        assert 'original_recommendation' not in result
        assert result['signal_tag'] == '長線配置'
        assert result['reason'] == '正常理由'

    def test_weak_fundamental_short_term_tag(self):
        """基本面弱 → 短線狙擊"""
        stock = {
            'ev_score': 40,
            'recommendation': '避開',
            'current_price': 90.0,
            'ma20': 100.0,
        }
        result = apply_signal_conflict_logic(stock)

        assert result['recommendation'] == '避開'
        assert result['signal_tag'] == '短線狙擊'
        assert 'original_recommendation' not in result

    def test_missing_ma20_no_downgrade(self):
        """MA20 缺失（0）時不觸發降級（防禦性：無均線資料不誤殺）"""
        stock = {
            'ev_score': 85,
            'recommendation': '積極買入',
            'current_price': 90.0,
            'ma20': 0,
        }
        result = apply_signal_conflict_logic(stock)

        assert result['recommendation'] == '積極買入'
        assert result['signal_tag'] == '長線配置'

    def test_none_values_default_safe(self):
        """欄位為 None 時使用安全預設值，不拋例外"""
        stock = {
            'ev_score': None,
            'recommendation': None,
            'current_price': None,
            'ma20': None,
        }
        result = apply_signal_conflict_logic(stock)

        # ev 預設 50 → 基本面不算強 → 短線狙擊
        assert result['signal_tag'] == '短線狙擊'

    def test_returns_same_dict_object(self):
        """原地修改：回傳物件與輸入為同一 dict（呼叫端迴圈依賴此行為）"""
        stock = {'ev_score': 50, 'recommendation': '觀望', 'current_price': 1, 'ma20': 2}
        result = apply_signal_conflict_logic(stock)
        assert result is stock
