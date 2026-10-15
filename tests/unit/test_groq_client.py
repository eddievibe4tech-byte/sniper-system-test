"""
Groq 客戶端單元測試
測試 AI 分析功能與 JSON 解析
"""
import pytest
import json
from unittest.mock import patch, MagicMock
from src.groq_client import GroqClient


class TestGroqClientInit:
    """測試 GroqClient 初始化"""
    
    def test_init_with_api_key(self):
        """測試提供 API Key 的初始化（🔴 修正後：預設模型由動態解析鏈決定，
        不再硬編碼 OpenRouter 風格 qwen/qwen3.6-27b——該 ID 在 Groq 會 100% 404）"""
        client = GroqClient(api_key='test_key')
        assert client.api_key == 'test_key'
        # 靜態兜底鏈首位為 Groq 風格扁平 ID（無斜線）
        assert client.model == "qwen-2.5-32b"
        assert "/" not in client.model
        assert len(client.fallback_chain) >= 4
    
    def test_init_empty_env_model_not_override(self, monkeypatch):
        """🔴 Bug 1 回歸測試：GROQ_MODEL 為空字串時不得覆蓋預設值（否則 payload model="" → 404）"""
        monkeypatch.setenv('GROQ_MODEL', '')
        client = GroqClient(api_key='test_key')
        assert client.model  # 非空字串
        assert client.model == "qwen-2.5-32b"
    
    def test_init_custom_model(self):
        """測試自訂模型：在鏈內→採用；不在鏈內（如已下架/錯誤 ID）→退回鏈首位，
        但呼叫時仍會遍歷完整 fallback 鏈，custom-model 不會被丟棄"""
        client = GroqClient(api_key='test_key', model='llama-3.1-8b-instant')
        assert client.model == 'llama-3.1-8b-instant'  # 在靜態兜底鏈中 → 直接探用

        client2 = GroqClient(api_key='test_key', model='totally-unknown-model')
        assert client2.model == "qwen-2.5-32b"          # 不在鏈中 → 退回首位防呆
        assert 'totally-unknown-model' not in client2.fallback_chain
        # 🔴 關鍵：fallback 鏈仍包含所有兜底模型，AI 不會因單一壞 ID 靜默失效
        assert len(client2.fallback_chain) >= 4
    
    def test_init_without_api_key(self, monkeypatch):
        """測試未提供 API Key 時的初始化"""
        monkeypatch.delenv('GROQ_API_KEY', raising=False)
        client = GroqClient()
        assert client.api_key == ''
    
    def test_init_from_env(self, monkeypatch):
        """測試從環境變數讀取 API Key"""
        monkeypatch.setenv('GROQ_API_KEY', 'env_key')
        client = GroqClient()
        assert client.api_key == 'env_key'


class TestMakeRequest:
    """測試 API 請求"""
    
    @patch('src.groq_client.requests.post')
    def test_successful_request(self, mock_post):
        """測試成功的 API 請求"""
        import json
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'choices': [{
                'message': {'content': json.dumps({"result": "test"})}
            }]
        }
        mock_post.return_value = mock_response
        
        client = GroqClient(api_key='test_key')
        messages = [{'role': 'user', 'content': 'test'}]
        result = client._make_request(messages)
        
        assert result is not None
        assert 'choices' in result
    
    @patch('src.groq_client.requests.post')
    def test_timeout_retry(self, mock_post):
        """測試超時重試（🔴 修正後含模型 fallback 鏈：每個模型各重試 max_retries 次）"""
        import requests
        mock_post.side_effect = requests.exceptions.Timeout()
        
        client = GroqClient(api_key='test_key', model='test')
        client.max_retries = 2
        client.retry_delay = 0.1
        
        messages = [{'role': 'user', 'content': 'test'}]
        result = client._make_request(messages)
        
        # 全鏈重試耗盡後應返回 None
        assert result is None
        # 確認重試發生，且每個候選模型都被嘗試過（chain × max_retries）
        assert mock_post.call_count == len(client.fallback_chain) * 2
        assert mock_post.call_count > 2  # 證明確實切換了多個模型，而非只打單一模型

    @patch('src.groq_client.requests.post')
    def test_404_switches_to_next_model(self, mock_post):
        """🔴 Bug 回歸測試：404 model_not_found 不重試，立即切換鏈中下一個模型"""
        client = GroqClient(api_key='test_key')
        client.retry_delay = 0.01

        resp_404 = MagicMock()
        resp_404.status_code = 404
        resp_404.text = '{"error":{"code":"model_not_found"}}'
        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = {'choices': [{'message': {'content': '{"ok": true}'}}]}
        mock_post.side_effect = [resp_404, resp_ok]

        result = client._make_request([{'role': 'user', 'content': 'test'}])

        assert result is not None
        assert mock_post.call_count == 2
        first_model = mock_post.call_args_list[0].kwargs['json']['model']
        second_model = mock_post.call_args_list[1].kwargs['json']['model']
        assert first_model != second_model  # 404 後確實換了模型
        assert client.model == second_model  # 記錄實際生效的模型
    
    def test_no_api_key(self):
        """測試未設定 API Key"""
        client = GroqClient(api_key='')
        
        with pytest.raises(ValueError, match="Groq API Key 未設定"):
            client._make_request([])


class TestAnalyzeStock:
    """測試股票分析功能"""
    
    @patch('src.groq_client.GroqClient._make_request')
    def test_analyze_stock_success(self, mock_request):
        """測試成功的股票分析"""
        mock_request.return_value = {
            'choices': [{
                'message': {
                    'content': '{"ev_score": 85, "recommendation": "積極買入", "reason": "基本面佳"}'
                }
            }]
        }
        
        client = GroqClient(api_key='test_key')
        prompt_template = "分析股票 {code}"
        stock_data = {'code': '2330'}
        
        result = client.analyze_stock(prompt_template, stock_data)
        
        assert result is not None
        assert result['ev_score'] == 85
        assert result['recommendation'] == "積極買入"
        assert result['reason'] == "基本面佳"
    
    @patch('src.groq_client.GroqClient._make_request')
    def test_analyze_stock_with_markdown(self, mock_request):
        """測試含 Markdown 標記的回應"""
        mock_request.return_value = {
            'choices': [{
                'message': {
                    'content': '```json\n{"ev_score": 75, "recommendation": "觀望", "reason": "中性"}\n```'
                }
            }]
        }
        
        client = GroqClient(api_key='test_key')
        prompt_template = "分析股票 {code}"
        stock_data = {'code': '2330'}
        
        result = client.analyze_stock(prompt_template, stock_data)
        
        assert result is not None
        assert result['ev_score'] == 75
    
    @patch('src.groq_client.GroqClient._make_request')
    def test_analyze_stock_missing_fields(self, mock_request):
        """測試缺少必要欄位時的預設值補齊機制"""
        mock_request.return_value = {
            'choices': [{
                'message': {
                    'content': '{"ev_score": 80}'  # 缺少 recommendation 和 reason
                }
            }]
        }
        
        client = GroqClient(api_key='test_key')
        prompt_template = "分析股票 {code}"
        stock_data = {'code': '2330'}
        
        result = client.analyze_stock(prompt_template, stock_data)
        
        # ✅ 修正：預期應回傳帶有預設值的字典，而非 None
        assert result is not None
        assert result['ev_score'] == 80
        assert result['recommendation'] == "觀望"
        assert result['reason'] == "AI 分析失敗"
    
    @patch('src.groq_client.GroqClient._make_request')
    def test_analyze_stock_invalid_json(self, mock_request):
        """測試無效 JSON"""
        mock_request.return_value = {
            'choices': [{
                'message': {
                    'content': '這不是 JSON 格式'
                }
            }]
        }
        
        client = GroqClient(api_key='test_key')
        prompt_template = "分析股票 {code}"
        stock_data = {'code': '2330'}
        
        result = client.analyze_stock(prompt_template, stock_data)
        
        assert result is None
    
    @patch('src.groq_client.GroqClient._make_request')
    def test_analyze_stock_api_failure(self, mock_request):
        """測試 API 失敗"""
        mock_request.return_value = None
        
        client = GroqClient(api_key='test_key')
        prompt_template = "分析股票 {code}"
        stock_data = {'code': '2330'}
        
        result = client.analyze_stock(prompt_template, stock_data)
        
        assert result is None


class TestJudgeRegime:
    """測試市場體制判斷"""
    
    @patch('src.groq_client.GroqClient._make_request')
    def test_judge_regime_success(self, mock_request):
        """測試成功的體制判斷"""
        mock_request.return_value = {
            'choices': [{
                'message': {
                    'content': '{"regime": "震盪", "confidence": "高", "reason": "大盤整理", "operation_suggestion": "區間操作", "risk_level": 5}'
                }
            }]
        }
        
        client = GroqClient(api_key='test_key')
        market_data = {'twii': 18000, 'volume': 3000}
        
        result = client.judge_regime(market_data)
        
        assert result is not None
        assert result['regime'] == "震盪"
        assert result['confidence'] == "高"
        assert result['risk_level'] == 5
    
    @patch('src.groq_client.GroqClient._make_request')
    def test_judge_regime_default_values(self, mock_request):
        """測試預設值處理"""
        mock_request.return_value = {
            'choices': [{
                'message': {
                    'content': '{}'  # 空物件，應使用預設值
                }
            }]
        }
        
        client = GroqClient(api_key='test_key')
        market_data = {'twii': 18000}
        
        result = client.judge_regime(market_data)
        
        assert result is not None
        assert result['regime'] == "震盪"  # 預設值
        assert result['confidence'] == "中"  # 預設值
        assert result['risk_level'] == 5  # 預設值
    
    @patch('src.groq_client.GroqClient._make_request')
    def test_judge_regime_api_failure(self, mock_request):
        """測試 API 失敗"""
        mock_request.return_value = None
        
        client = GroqClient(api_key='test_key')
        market_data = {'twii': 18000}
        
        result = client.judge_regime(market_data)
        
        assert result is None
