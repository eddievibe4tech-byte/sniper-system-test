"""
#158 訊號衝突降級鐵律與時間維度標籤 單元測試

驗證 apply_signal_conflict_logic()：
1. 基本面強 + 跌破 MA20 → 強制降級「觀望」，保留 original_recommendation，signal_tag = 長線配置/短期觀望
2. 基本面強 + 技術面健康 → signal_tag = 長線配置，不降級
3. 基本面弱 → signal_tag = 短線狙擊
4. MA20 缺失（<=0）→ 不觸發降級（防禦性）

Code Review (#159) 防護性修正回測：
5. _safe_float：AI 輸出 "N/A" / "資料不足" / "75%" 等異常字串不拋例外
6. 字眼包含檢查：「強烈買入」/「買入」等變體也能觸發降級
"""
import pytest

from src.main import apply_signal_conflict_logic, _safe_float


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

    # ==========================================
    # 🛡️ Code Review (#159) 防護性修正回測
    # ==========================================

    def test_ai_garbage_strings_do_not_crash(self):
        """AI 輸出 "N/A" / "資料不足" / "75%" 等異常字串 → _safe_float 容錯，不拋 ValueError"""
        stock = {
            'ev_score': 'N/A',
            'recommendation': '積極買入',
            'current_price': '資料不足',
            'ma20': '100.5',
        }
        # 不應拋出例外
        result = apply_signal_conflict_logic(stock)

        # ev_score="N/A" → 預設 50；現價轉換失敗 → 0 → 0 < MA20 但 current_price=0
        # 屬無效現價，仍會因 ma20>0 且 0<100.5 觸發降級（保守風控，可接受）
        assert result['signal_tag'] in ('長線配置/短期觀望', '長線配置')

    def test_percent_string_ev_score_parsed(self):
        """ev_score 帶 % 單位（如 "75%"）→ 正確解析為 75 → 視為基本面強"""
        stock = {
            'ev_score': '75%',
            'recommendation': '觀望',
            'current_price': 105.0,
            'ma20': 100.0,
        }
        result = apply_signal_conflict_logic(stock)
        assert result['signal_tag'] == '長線配置'

    def test_fundamental_variant_wording_triggers_downgrade(self):
        """字眼包含檢查：AI 輸出「強烈買入」（非枚舉「積極買入」）跌破 MA20 → 仍降級"""
        for variant in ('強烈買入', '買入', '謹慎買入'):
            stock = {
                'ev_score': 60,
                'recommendation': variant,
                'current_price': 90.0,
                'ma20': 100.0,
            }
            result = apply_signal_conflict_logic(stock)
            assert result['recommendation'] == '觀望', f"{variant} 應觸發降級"
            assert result['original_recommendation'] == variant
            assert result['signal_tag'] == '長線配置/短期觀望'


@pytest.mark.unit
class TestSafeFloat:
    """_safe_float 工具函數單元測試"""

    def test_normal_number(self):
        assert _safe_float(80) == 80.0
        assert _safe_float(80.5) == 80.5

    def test_numeric_string(self):
        assert _safe_float('75') == 75.0
        assert _safe_float(' 75 ') == 75.0

    def test_percent_and_comma_string(self):
        assert _safe_float('75%') == 75.0
        assert _safe_float('1,234.5') == 1234.5

    def test_invalid_string_returns_default(self):
        assert _safe_float('N/A', 50.0) == 50.0
        assert _safe_float('資料不足', 50.0) == 50.0
        assert _safe_float('', 50.0) == 50.0

    def test_none_returns_default(self):
        assert _safe_float(None, 50.0) == 50.0

    def test_bool_is_accepted_by_float_semantics(self):
        # float(True)==1.0，與 Python 原生行為一致
        assert _safe_float(True) == 1.0
