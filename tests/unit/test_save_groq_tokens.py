"""
節省 Groq Token 方案 1+2 單元測試
- 方案 1：財報缺失 → generate_rule_based_analysis 規則引擎（不呼叫 Groq）
- 方案 2：Prompt 壓縮 main_analysis_v2.txt（~400 tokens，原 ~1000 tokens）
"""
import pathlib
from src.main import generate_rule_based_analysis


BASE = pathlib.Path(__file__).resolve().parents[2]


class TestRuleBasedAnalysis:
    """方案 1：規則引擎評分邏輯"""

    def test_neutral_stock_gets_watch(self):
        """中性 RSI + 短期弱勢 → 分數落在觀望區間（40-59），reason 帶「無 AI 分析」標記"""
        result = generate_rule_based_analysis({
            'rsi': 50, 'change_5d': -6, 'ma20': 100, 'current_price': 100,
            'macd': 0, 'volatility': 25, 'inst_buy_days': 0,
        })
        assert 40 <= result['ev_score'] < 60
        assert result['recommendation'] == '觀望'
        assert result['reason'].startswith('（無 AI 分析）')

    def test_neutral_plus_low_vol_bumps_to_buy(self):
        """中性 + 低波動 + 投信小買 → 謹慎買入（60-69）"""
        result = generate_rule_based_analysis({
            'rsi': 50, 'change_5d': -6, 'ma20': 100, 'current_price': 100,
            'macd': 0, 'volatility': 15, 'inst_buy_days': 2,
        })
        assert 60 <= result['ev_score'] < 70
        assert result['recommendation'] == '謹慎買入'

    def test_oversold_institutional_buy_scores_high(self):
        """RSI 超賣 + 投信連買 ≥3 天 → 高分、積極買入"""
        result = generate_rule_based_analysis({
            'rsi': 25, 'change_5d': 0, 'ma20': 100, 'current_price': 100,
            'macd': 1.5, 'volatility': 15, 'inst_buy_days': 5,
        })
        assert result['ev_score'] >= 70
        assert result['recommendation'] == '積極買入'

    def test_overbought_weak_stock_avoid(self):
        """RSI 超買 + 短期弱勢 + 高波動 → 避開"""
        result = generate_rule_based_analysis({
            'rsi': 80, 'change_5d': -8, 'ma20': 100, 'current_price': 100,
            'macd': -1, 'volatility': 50, 'inst_buy_days': 0,
        })
        assert result['ev_score'] < 40
        assert result['recommendation'] == '避開'

    def test_missing_fields_do_not_crash(self):
        """空字典（全缺欄位）→ 使用安全預設值，不拋例外"""
        result = generate_rule_based_analysis({})
        assert 0 <= result['ev_score'] <= 100
        assert 'recommendation' in result and 'reason' in result

    def test_score_clamped_range(self):
        """極端強勢 → 分數上限 100；極端弱勢 → 下限 0"""
        strong = generate_rule_based_analysis({
            'rsi': 20, 'change_5d': 20, 'ma20': 100, 'current_price': 100,
            'macd': 5, 'volatility': 10, 'inst_buy_days': 10,
        })
        assert strong['ev_score'] <= 100
        weak = generate_rule_based_analysis({
            'rsi': 95, 'change_5d': -30, 'ma20': 100, 'current_price': 100,
            'macd': -5, 'volatility': 80, 'inst_buy_days': 0,
        })
        assert weak['ev_score'] >= 0

    def test_output_format_matches_groq_contract(self):
        """輸出格式需與 Groq analysis 相容（record merge 依賴此契約）"""
        result = generate_rule_based_analysis({'rsi': 50})
        assert set(result.keys()) == {'ev_score', 'recommendation', 'reason'}
        assert isinstance(result['ev_score'], int)


class TestCompressedPromptV2:
    """方案 2：Prompt 壓縮驗證"""

    def test_v2_file_exists(self):
        assert (BASE / 'prompts' / 'main_analysis_v2.txt').exists()

    def test_v2_significantly_smaller(self):
        """v2 應比 v1 小至少 40%（字元數近似 token 數）"""
        v1 = (BASE / 'prompts' / 'main_analysis.txt').read_text(encoding='utf-8')
        v2 = (BASE / 'prompts' / 'main_analysis_v2.txt').read_text(encoding='utf-8')
        assert len(v2) < len(v1) * 0.6, \
            f"v2 ({len(v2)} chars) 未達壓縮目標（v1={len(v1)} chars）"

    def test_v2_contains_all_placeholders(self):
        """壓縮後仍需涵蓋 main.py stock_data 的全部佔位符"""
        v2 = (BASE / 'prompts' / 'main_analysis_v2.txt').read_text(encoding='utf-8')
        for ph in ['{code}', '{name}', '{regime}', '{revenue_yoy}', '{gross_margin}',
                   '{net_margin}', '{eps}', '{current_price}', '{ma20}', '{rsi}',
                   '{change_5d}', '{volatility}', '{inst_buy_days}', '{margin_change}']:
            assert ph in v2, f"壓縮版 prompt 缺少佔位符 {ph}"

    def test_v2_keeps_missing_data_and_scoring_rules(self):
        """關鍵品質守則不可在壓縮時遺失：數據缺失規則 + 四維度權重 + JSON 輸出"""
        v2 = (BASE / 'prompts' / 'main_analysis_v2.txt').read_text(encoding='utf-8')
        assert '數據缺失' in v2, "不得遺失 P0 數據缺失處理規則"
        for w in ['30%', '20%']:
            assert w in v2, "不得遺失評分權重"
        assert '"ev_score"' in v2 and '"recommendation"' in v2 and '"reason"' in v2

    def test_main_prefers_v2_with_fallback(self):
        """靜態驗證：main.py 優先讀取 v2 且保留 v1 fallback"""
        src = (BASE / 'src' / 'main.py').read_text(encoding='utf-8')
        assert 'main_analysis_v2.txt' in src
        assert 'main_analysis.txt' in src, "應保留退回完整版 Prompt 的 fallback"

    def test_rule_engine_wired_before_groq(self):
        """靜態驗證：規則引擎分支位於 Groq 呼叫之前，且跳過分支有 continue"""
        src = (BASE / 'src' / 'main.py').read_text(encoding='utf-8')
        guard_idx = src.find('financial_complete')
        rule_idx = src.find('generate_rule_based_analysis(stock_data)')
        call_idx = src.find('groq.analyze_stock(prompt_tpl, stock_data)')
        assert -1 < guard_idx < rule_idx < call_idx, \
            "攔截與規則引擎必須位於 Groq 呼叫之前"
        assert "'model': 'rule-based-engine'" in src, "telemetry 應標記非 AI 來源"
