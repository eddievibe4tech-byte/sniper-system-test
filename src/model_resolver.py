"""
🧠 Groq 模型解析器（Model Lifecycle Management）

動態偵測 + TTL 記憶體快取 + 靜態兜底鏈：
  1. 優先級 1：手動環境變數 GROQ_MODEL（空字串防呆——GitHub Actions 未設定
     repo Variable 時會注入「空字串」，os.getenv 的預設值不會生效，這是
     Weekly Prompt Optimization 全數 404 model_not_found 的根因之一）。
  2. 優先級 2：呼叫 Groq /models 端點動態取得活躍模型（依 created 時間戳降序，
     確保永遠優先使用最新釋出的權重），結果以 TTL 快取避免每次請求都打 API。
  3. 優先級 3：硬編碼靜態兜底鏈。⚠️ 必須使用 Groq 風格的扁平 ID（連字號 `-`、
     無斜線 `/`）；OpenRouter 風格 ID（如 qwen/qwen3-32b）在 Groq 會 100% 404。

任何 404（model_not_found）代表「模型不存在或已下架」，重試無意義，
呼叫端應立即切換至鏈中下一個模型（見 src/groq_client.py、src/optimize_prompt.py）。
"""
import logging
import os
import time
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"

# TTL 記憶體快取：避免每筆股票分析都多打一次 /models
_CACHE_TTL_SECONDS = int(os.getenv("GROQ_MODELS_CACHE_TTL", "3600"))
_cache = {"ts": 0.0, "models": []}  # type: ignore[var-annotated]

# 🔴 靜態兜底鏈：僅列 Groq 活躍清單中的扁平 Model ID（無斜線）
STATIC_FALLBACKS = [
    "qwen-2.5-32b",             # Groq 主力 Qwen（扁平 ID）
    "qwen-qwq-32b",             # Qwen 推理模型
    "llama-3.3-70b-versatile",  # Llama 3.3（最穩定備援）
    "llama-3.1-8b-instant",     # 極速備援（Token 便宜）
]


def fetch_active_groq_models(api_key: str) -> List[dict]:
    """抓取 Groq 活躍模型清單（含 TTL 快取）。失敗回傳空清單，不拋例外。"""
    now = time.time()
    if _cache["models"] and now - _cache["ts"] < _CACHE_TTL_SECONDS:
        return _cache["models"]
    if not api_key:
        return []
    try:
        r = requests.get(
            GROQ_MODELS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        _cache.update(ts=now, models=data)
        logger.info("📡 Groq /models 動態抓取成功：%d 個活躍模型（TTL %ds）",
                    len(data), _CACHE_TTL_SECONDS)
        return data
    except (requests.exceptions.RequestException, ValueError) as e:
        # 抓取失敗（無網路/無效 key）→ 退回靜態兜底鏈，不得讓主流程崩潰
        logger.warning("Groq /models 抓取失敗，將使用靜態兜底鏈：%s", e)
        return []


def get_dynamic_model_chain(api_key: Optional[str] = None) -> List[str]:
    """建立去重的模型候選鏈：env 首選 → 動態 Qwen（依 created 降序）→ 靜態兜底。"""
    chain: List[str] = []

    # 優先級 1：手動環境變數（🔴 空字串防呆：or "" + strip）
    preferred = (os.getenv("GROQ_MODEL") or "").strip()
    if preferred:
        chain.append(preferred)

    # 優先級 2：動態獲取最新 Qwen 模型
    key = api_key or os.getenv("GROQ_API_KEY", "")
    if key:
        all_models = fetch_active_groq_models(key)
        # 🔴 Groq /models 回傳通常沒有 active 欄位（只列當前可用者），直接檢查 id 即可
        qwen_entries = [m for m in all_models
                        if "qwen" in (m.get("id") or "").lower() and m.get("id")]
        # 依照 created 時間戳降序排序（最新建立的模型排在最前面）
        qwen_entries.sort(key=lambda m: m.get("created") or 0, reverse=True)
        chain.extend(m["id"] for m in qwen_entries)

    # 優先級 3：硬編碼兜底清單（必須是 Groq 風格扁平 ID，無斜線）
    chain.extend(STATIC_FALLBACKS)

    # 去除重複項並保持原始順序
    seen = set()
    deduped_chain = [m for m in chain if m and not (m in seen or seen.add(m))]
    logger.info("🧠 Groq 模型解析鏈完成（共 %d 個候選）：%s...",
                len(deduped_chain), deduped_chain[:5])
    return deduped_chain
