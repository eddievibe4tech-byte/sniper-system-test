"""
Groq API 客戶端模組
提供 AI 分析功能：股票評分、市場體制判斷等
"""
import os
import json
import time
import logging
import requests
from typing import Dict, Optional, Any, List
from datetime import datetime

try:  # 正式路徑（python -m src.groq_client / pytest）
    from src.model_resolver import get_dynamic_model_chain
except ImportError:  # 相容 flat import 模式（src/ 在 sys.path，如 news_sentiment/macro_allocator）
    from model_resolver import get_dynamic_model_chain

logger = logging.getLogger(__name__)


class GroqClient:
    """Groq API 客戶端"""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        """
        初始化 Groq 客戶端

        Args:
            api_key: Groq API Key，若未提供則從環境變數 GROQ_API_KEY 讀取
            model: 使用的模型名稱；若未提供則由動態模型解析鏈決定
                   （env GROQ_MODEL 空字串防呆 → /models 動態抓取 → 靜態兜底鏈）
        """
        self.api_key = api_key or os.getenv('GROQ_API_KEY', '')
        # 🔴 修正：空字串 env 不再覆蓋預設值（GitHub Actions 未設定 repo Variable
        #    時會注入空字串，os.getenv 預設值不生效 → payload model="" → 404）
        # 🔴 修正：不再硬編碼 OpenRouter 風格 ID（qwen/qwen3.6-27b 在 Groq 會 100% 404）
        self.fallback_chain = get_dynamic_model_chain(self.api_key)
        preferred = (model or os.getenv('GROQ_MODEL') or "").strip()
        self.model = preferred if preferred in self.fallback_chain else self.fallback_chain[0]
        self.base_url = 'https://api.groq.com/openai/v1/chat/completions'
        self.max_retries = 3
        self.retry_delay = 2.0

    def _make_request(self, messages: list, temperature: float = 0.3) -> Optional[Dict]:
        """
        發送 API 請求到 Groq（遍歷整個模型 fallback 鏈）

        404（model_not_found）→ 不重試，直接切換鏈中下一個模型；
        429/5xx → 指數退避重試，耗盡後換下一個模型。

        Args:
            messages: 對話訊息列表
            temperature: 溫度參數（降低至 0.3 讓輸出更穩定）

        Returns:
            API 回應資料，全鏈失敗時返回 None
        """
        if not self.api_key:
            raise ValueError("Groq API Key 未設定")

        url = self.base_url

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        # 🔴 核心修正：遍歷整個 fallback chain（而非只打單一模型）
        for current_model in self.fallback_chain:
            payload = {
                "model": current_model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": 4096,
            }

            for attempt in range(self.max_retries):
                try:
                    response = requests.post(url, headers=headers, json=payload, timeout=30)

                    # 1. 遇到 404 (Model Not Found)：不重試，直接換下一個模型
                    if response.status_code == 404:
                        logger.warning(f"Groq 404（模型 {current_model} 已下架或無權限），自動切換至下一順位...")
                        break  # 跳出 attempt 迴圈，進入 chain 的下一個 model

                    # 2. 遇到 429 (Rate Limit) 或 5xx (Server Error)：進行指數退避重試
                    if response.status_code in (429, 500, 502, 503, 504):
                        if attempt < self.max_retries - 1:
                            wait_time = self.retry_delay * (2 ** attempt)
                            logger.warning(f"Groq API 暫時性錯誤 (HTTP {response.status_code})，{wait_time}s 後重試...")
                            time.sleep(wait_time)
                            continue
                        else:
                            logger.error(f"Groq API 重試耗盡 (HTTP {response.status_code})，model={current_model}")
                            break  # 換下一個模型

                    # 3. 其他 4xx 錯誤（如 400 Bad Request）：通常是 Prompt 問題，不換模型
                    if response.status_code != 200:
                        logger.error(f"Groq API 請求失敗 (HTTP {response.status_code}):")
                        logger.error(f"URL: {url}")
                        logger.error(f"Model: {current_model}")
                        logger.error(f"Response: {response.text}")
                        return None

                    # 成功
                    self.model = current_model  # 記錄實際生效的模型
                    return response.json()

                except requests.exceptions.RequestException as e:
                    logger.warning(f"Groq 網路請求異常 (model={current_model}, Attempt {attempt + 1}/{self.max_retries}): {e}")
                    if attempt < self.max_retries - 1:
                        time.sleep(self.retry_delay)
                    else:
                        break  # 網路徹底斷線，換下一個模型試試看

        logger.error("🚨 Groq 所有候選模型皆失敗，AI 分析將跳過（降級 rule-based fallback）")
        return None

    def _render_template(self, template: str, data: dict) -> str:
        """逐鍵替換佔位符，避免模板中的 JSON 範例大括號被 format() 誤判"""
        rendered = template
        for key, value in data.items():
            rendered = rendered.replace('{' + key + '}', str(value))
        return rendered

    def _clean_stock_data(self, stock_data: Dict) -> Dict:
        """
        🔴 P0 修正：清理股票數據，處理缺失值
        
        【數據缺失處理規則】（最高優先）
        - 任何欄位為 "-"、null、空字串時：
          1. 絕對不得視為 0 或負面證據
          2. 僅用「有值的欄位」評分
          3. 在理由末尾標註「缺失欄位：xx」
        - 只有當欄位「有值且為零或負」時，才可作為負面證據
        
        Args:
            stock_data: 原始股票數據字典
            
        Returns:
            清理後的數據字典
        """
        cleaned = {}
        missing_fields = []
        
        for key, value in stock_data.items():
            # 檢查是否為缺失值
            if value is None or value == '-' or value == '':
                missing_fields.append(key)
                # 標記為「數據缺失」而非 0
                cleaned[key] = '數據缺失'
            else:
                cleaned[key] = value
        
        # 若有缺失欄位，加入標註
        if missing_fields:
            cleaned['_missing_fields'] = missing_fields
        
        return cleaned

    def analyze_stock(self, prompt_template: str, stock_data: Dict) -> Optional[Dict]:
        """
        分析股票並返回評分與建議

        Args:
            prompt_template: Prompt 模板字串
            stock_data: 股票數據字典

        Returns:
            包含 EV 評分、建議、原因的字典
        """
        # 🔴 P0 修正：在發送給 AI 前，先處理缺失數據
        cleaned_data = self._clean_stock_data(stock_data)
        
        # 渲染 Prompt 模板（使用安全替換）
        prompt = self._render_template(prompt_template, cleaned_data)

        messages = [
            {
                "role": "system",
                "content": "你是一位擁有 20 年經驗的台股分析師，專精於籌碼面與基本面分析。請僅輸出 JSON 格式，不要輸出其他內容。"
            },
            {
                "role": "user",
                "content": prompt
            }
        ]

        logger.info(f"發送請求到 Groq，模型：{self.model}")
        response_data = self._make_request(messages)

        if not response_data or 'choices' not in response_data:
            logger.error(f"Groq 未回傳有效數據")
            return None

        content = response_data['choices'][0]['message']['content']
        logger.info(f"Groq 回傳內容長度：{len(content)} 字元")

        # 關鍵修正 1：處理 </think> 標籤 - 提取標籤後的 JSON
        json_candidate = content
        if '</think>' in content:
            json_candidate = content.split('</think>')[-1].strip()
            logger.info(f"已提取 </think> 後的內容，長度：{len(json_candidate)} 字元")

        # 關鍵修正 2：尋找最後一個 '{' 和最後一個 '}' 之間的內容
        start_idx = json_candidate.rfind('{')
        end_idx = json_candidate.rfind('}')

        json_str = None
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = json_candidate[start_idx:end_idx+1]
            logger.info(f"成功提取 JSON 區塊（長度：{len(json_str)}）")
        else:
            logger.warning("無法在回應中找到有效的 JSON 區塊")
            logger.warning(f"原始內容結尾：...{content[-500:]}")
            return None

        # 解析 JSON 回應
        try:
            content_to_parse = json_str.strip()

            # 移除可能的 Markdown 標記
            if content_to_parse.startswith('```'):
                content_to_parse = content_to_parse.split('```')[1] if '```' in content_to_parse[3:] else content_to_parse[3:]
                content_to_parse = content_to_parse.rsplit('```')[0] if '```' in content_to_parse else content_to_parse
                content_to_parse = content_to_parse.strip()

            # 嘗試多種 JSON 解析方式
            try:
                result = json.loads(content_to_parse)
            except json.JSONDecodeError:
                import re
                content_clean = re.sub(r'^\s*[\{"]\s*', '{', content_to_parse)
                content_clean = re.sub(r'\s*[\}"]\s*$', '}', content_clean)
                result = json.loads(content_clean)

            # 驗證必要欄位並加入預設值
            if 'ev_score' not in result:
                logger.warning("缺少 ev_score 欄位")
                result['ev_score'] = 50
            if 'recommendation' not in result:
                logger.warning("缺少 recommendation 欄位")
                result['recommendation'] = "觀望"
            if 'reason' not in result:
                logger.warning("缺少 reason 欄位")
                result['reason'] = "AI 分析失敗"

            logger.info(f"✅ 解析成功：EV={result.get('ev_score')}, 建議={result.get('recommendation')}")

            return {
                'ev_score': int(result.get('ev_score', 50)),
                'recommendation': result.get('recommendation', '觀望'),
                'reason': result.get('reason', 'AI 分析失敗'),
                'raw_response': content
            }

        except json.JSONDecodeError as e:
            logger.error(f"❌ JSON 解析失敗：{e}")
            logger.error(f"提取到的字串：{json_str[:200] if json_str else 'N/A'}")
            return None
        except Exception as e:
            logger.error(f"❌ 解析 Groq 回應時出錯：{e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def judge_regime(self, market_data: Dict) -> Optional[Dict]:
        """
        判斷市場體制

        Args:
            market_data: 市場數據字典

        Returns:
            包含體制、信心度、理由的字典
        """
        prompt_path = os.path.join(os.path.dirname(__file__), '..', 'prompts', 'regime_judgment.txt')

        try:
            with open(prompt_path, 'r', encoding='utf-8') as f:
                prompt_template = f.read()
        except FileNotFoundError:
            prompt_template = "你是一位擁有 20 年經驗的台股總經分析師，專精於判斷市場體制。\n\n【市場體制定義】\n- 多頭：大盤在 60 日均線之上，成交量放大，外資連續買超\n- 震盪：大盤在 20-60 日均線間糾結，成交量萎縮，多空拉鋸\n- 空頭：大盤跌破 60 日均線，融資大增但股價下跌，外資連續賣超\n\n【輸入數據】\n{market_data}\n\n【輸出要求】\n請僅輸出以下 JSON 格式：\n{{\"regime\": \"多頭/震盪/空頭\", \"confidence\": \"高/中/低\", \"reason\": \"判斷理由（50 字內）\", \"operation_suggestion\": \"操作建議（30 字內）\", \"risk_level\": \"1-10 分\"}}"

        market_str = "; ".join([f"{k}: {v}" for k, v in market_data.items()])
        prompt = self._render_template(prompt_template, {'market_data': market_str})

        messages = [
            {
                "role": "system",
                "content": "你是一位擁有 20 年經驗的台股總經分析師。請僅輸出 JSON 格式。"
            },
            {
                "role": "user",
                "content": prompt
            }
        ]

        response_data = self._make_request(messages, temperature=0.2)

        if not response_data or 'choices' not in response_data:
            return None

        content = response_data['choices'][0]['message']['content']

        try:
            content = content.strip()
            if content.startswith('```json'):
                content = content.split('```json')[1].split('```')[0].strip()
            elif content.startswith('```'):
                content = content.split('```')[1].split('```')[0].strip()

            result = json.loads(content)

            return {
                'regime': result.get('regime', '震盪'),
                'confidence': result.get('confidence', '中'),
                'reason': result.get('reason', ''),
                'operation_suggestion': result.get('operation_suggestion', ''),
                'risk_level': result.get('risk_level', 5),
                'raw_response': content
            }

        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析錯誤：{e}")
            logger.error(f"原始內容：{content}")
            return None
        except Exception as e:
            logger.error(f"解析 Groq 回應時出錯：{e}")
            return None
