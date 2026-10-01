"""
財報快取與 Alpha Vantage 配額管理（Alpha Vantage Fallback #議題）
- 快取 commit 回 repo（data/financial_cache.json）：Actions runner 免洗，需靠 repo 持久化
- 配額按日計數：免費版 25 次/日，本系統安全上限 20 次/日
"""
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)
TZ_TAIPEI = timezone(timedelta(hours=8))

CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "financial_cache.json"
CACHE_TTL_DAYS = 40     # 財報季頻更新，40 天 TTL 足夠
AV_DAILY_QUOTA = 20     # 免費 25 次/日 的安全邊際


def _today() -> str:
    return datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")


def load_cache() -> Dict:
    """讀取快取；檔案不存在／損毀時回傳空快取（絕不中斷主流程）"""
    if CACHE_PATH.exists():
        try:
            with CACHE_PATH.open(encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("cache root is not an object")
            data.setdefault("meta", {})
            data.setdefault("stocks", {})
            return data
        except Exception as e:
            logger.warning("財報快取讀取失敗，改用空快取：%s", e)
    return {"meta": {}, "stocks": {}}


def save_cache(cache: Dict) -> None:
    """寫入快取（原子替換，避免 Actions commit 步驟讀到半寫入檔案）"""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        tmp.replace(CACHE_PATH)
    except Exception as e:
        logger.error("財報快取寫入失敗：%s", e)


def _age_days(entry: Dict) -> float:
    """entry 距上次抓取的日數；時間戳缺失／格式異常時回傳極大值（視為過期）"""
    try:
        dt = datetime.fromisoformat(entry.get("fetched_at", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_TAIPEI)
        return (datetime.now(TZ_TAIPEI) - dt).total_seconds() / 86400
    except Exception:
        return 9999.0


def fresh_entry(cache: Dict, code: str) -> Optional[Dict]:
    """TTL 內的新鮮快取条目；過期或缺失回傳 None"""
    e = cache["stocks"].get(code)
    return e if (e and _age_days(e) < CACHE_TTL_DAYS) else None


def _quota_state(cache: Dict) -> Dict:
    """取得當日配額狀態；跨日時自動重置"""
    q = cache["meta"].setdefault("av_quota", {"date": _today(), "used": 0})
    if q.get("date") != _today():
        q.update({"date": _today(), "used": 0})
    return q


def quota_remaining(cache: Dict) -> int:
    q = _quota_state(cache)
    return max(0, AV_DAILY_QUOTA - q.get("used", 0))


def consume_quota(cache: Dict) -> None:
    q = _quota_state(cache)
    q["used"] = q.get("used", 0) + 1
