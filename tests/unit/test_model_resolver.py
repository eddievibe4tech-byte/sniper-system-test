"""
模型解析器與 Prompt 優化器 Groq 呼叫鏈測試

🔴 Bug 回歸：Weekly Prompt Optimization 全數 404 model_not_found 的雙重根因
  1. GitHub Actions 未設定 repo Variable 時 `GROQ_MODEL: ${{ vars.GROQ_MODEL }}`
     注入「空字串」，os.getenv 預設值不生效 → payload model=""。
  2. 舊預設 qwen/qwen3.6-27b 是 OpenRouter 風格 ID（帶斜線），
     Groq 只接受扁平 ID → 即使 env 有值也 404。
"""
import json
from unittest.mock import patch, MagicMock

import pytest
import requests

from src.model_resolver import get_dynamic_model_chain, STATIC_FALLBACKS, _cache
from src.optimize_prompt import groq_complete


@pytest.fixture(autouse=True)
def clear_models_cache():
    """每個測試清掉 TTL 快取，避免跨測試污染"""
    _cache.update(ts=0.0, models=[])
    yield
    _cache.update(ts=0.0, models=[])


class TestDynamicModelChain:
    """get_dynamic_model_chain：空字串防呆 + 動態抓取 + 靜態兜底"""

    def test_empty_env_model_is_ignored(self, monkeypatch):
        """🔴 Bug 1 核心回歸：GROQ_MODEL="" 不得進入鏈（否則送 model=""）"""
        monkeypatch.setenv('GROQ_MODEL', '')
        monkeypatch.delenv('GROQ_API_KEY', raising=False)
        chain = get_dynamic_model_chain()
        assert '' not in chain
        assert chain[0] == "qwen-2.5-32b"

    def test_whitespace_env_model_is_stripped_or_dropped(self, monkeypatch):
        """純空白 env 同樣被防呆丟棄"""
        monkeypatch.setenv('GROQ_MODEL', '   ')
        monkeypatch.delenv('GROQ_API_KEY', raising=False)
        chain = get_dynamic_model_chain()
        assert all(m.strip() for m in chain)

    def test_valid_env_model_takes_priority(self, monkeypatch):
        """有值的 GROQ_MODEL 排鏈首"""
        monkeypatch.setenv('GROQ_MODEL', 'llama-3.1-8b-instant')
        monkeypatch.delenv('GROQ_API_KEY', raising=False)
        chain = get_dynamic_model_chain()
        assert chain[0] == 'llama-3.1-8b-instant'
        # 去重：兜底鏈中的同名模型不會重複出現
        assert chain.count('llama-3.1-8b-instant') == 1

    def test_static_fallbacks_use_groq_flat_ids(self):
        """🔴 兜底鏈必須是 Groq 風格扁平 ID（無斜線）——OpenRouter 風格會 100% 404"""
        for m in STATIC_FALLBACKS:
            assert '/' not in m, f"{m} 是 OpenRouter 風格 ID，Groq 會 404"

    @patch('src.model_resolver.requests.get')
    def test_dynamic_qwen_sorted_by_created_desc(self, mock_get, monkeypatch):
        """動態抓取：依 created 時間戳降序，最新權重優先"""
        monkeypatch.delenv('GROQ_MODEL', raising=False)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [
            {"id": "qwen-2.5-32b", "created": 100},
            {"id": "qwen-3-latest-32b", "created": 300},
            {"id": "qwen-qwq-32b", "created": 200},
            {"id": "llama-3.3-70b-versatile", "created": 400},  # 非 qwen，不入動態段
        ]}
        mock_get.return_value = mock_resp

        chain = get_dynamic_model_chain(api_key='fake_key')
        assert chain[:3] == ["qwen-3-latest-32b", "qwen-qwq-32b", "qwen-2.5-32b"]

    @patch('src.model_resolver.requests.get')
    def test_fetch_failure_falls_back_to_static(self, mock_get, monkeypatch):
        """/models 抓取失敗（網路/無效 key）→ 退回靜態兜底鏈，不拋例外"""
        monkeypatch.delenv('GROQ_MODEL', raising=False)
        mock_get.side_effect = requests.exceptions.ConnectionError("no network")
        chain = get_dynamic_model_chain(api_key='fake_key')
        assert chain == STATIC_FALLBACKS


class TestOptimizePromptGroqComplete:
    """groq_complete：404 換模型、成功記錄模型、全鏈失敗才拋例外"""

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv('GROQ_API_KEY', raising=False)
        with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
            groq_complete("sys", "user")

    @patch('src.optimize_prompt.requests.post')
    def test_404_switches_model_and_succeeds(self, mock_post, monkeypatch):
        """🔴 根因回歸：第一個模型 404 → 立即切換下一个，不再整輪重試同一壞 ID"""
        monkeypatch.setenv('GROQ_API_KEY', 'test_key')
        resp_404 = MagicMock()
        resp_404.status_code = 404
        resp_404.text = '{"error":{"code":"model_not_found"}}'
        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = {
            'choices': [{'message': {'content': '新 prompt 內容'}}]}
        mock_post.side_effect = [resp_404, resp_ok]

        result = groq_complete("sys", "user")

        assert result == "新 prompt 內容"
        assert mock_post.call_count == 2
        m1 = mock_post.call_args_list[0].kwargs['json']['model']
        m2 = mock_post.call_args_list[1].kwargs['json']['model']
        assert m1 != m2 and m1 and m2  # 換了模型，且絕不送空字串

    @patch('src.optimize_prompt.requests.post')
    def test_never_sends_empty_model(self, mock_post, monkeypatch):
        """🔴 Bug 1 終極防呆：即使 workflow 注入空字串 GROQ_MODEL，payload 也不得為 """""
        monkeypatch.setenv('GROQ_API_KEY', 'test_key')
        monkeypatch.setenv('GROQ_MODEL', '')  # 模擬 repo 未設定 Variable 的情境
        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = {
            'choices': [{'message': {'content': 'ok'}}]}
        mock_post.return_value = resp_ok

        groq_complete("sys", "user")

        sent_model = mock_post.call_args.kwargs['json']['model']
        assert sent_model != ""
        assert '/' not in sent_model  # 不再是 OpenRouter 風格

    @patch('src.optimize_prompt.requests.post')
    def test_all_models_fail_raises_runtime(self, mock_post, monkeypatch):
        """全鏈 404 → 拋 RuntimeError（而非靜默）"""
        monkeypatch.setenv('GROQ_API_KEY', 'test_key')
        resp_404 = MagicMock()
        resp_404.status_code = 404
        resp_404.text = 'not found'
        mock_post.return_value = resp_404

        with pytest.raises(RuntimeError, match="全模型鏈失敗"):
            groq_complete("sys", "user")
