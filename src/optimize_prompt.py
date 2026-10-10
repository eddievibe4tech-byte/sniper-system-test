"""
🆕 每週 Prompt 優化器（Verification v2.1 配套）

流程：讀已驗證 telemetry → 守門（樣本數/達標準跳過）→ 錯誤樣本歸因（含 alpha 與
失敗條件審計欄）→ Groq 重寫 → 佔位符/契約驗證 → 產出 main_analysis_v{N+1}.txt
並更新 data/prompt_history.json（current_version 遞增，供 Daily Analysis 版本驅動載入）。

紅燈根因修復：
  - 原本 workflow 引用不存在的 src.optimize_prompt 模組 → 補齊本模組。
  - secret 名稱對齊既有 GROQ_API_KEY（與 src/groq_client.py 一致）。
  - history schema 相容：prompt_history.json 使用 "versions" 陣列（非 "history"）。
"""
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PROMPTS_DIR = BASE_DIR / "prompts"
TZ_TAIPEI = timezone(timedelta(hours=8))
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
logger = logging.getLogger(__name__)

REQUIRED_PLACEHOLDERS = ["{code}", "{name}", "{regime}", "{revenue_yoy}", "{gross_margin}",
                         "{net_margin}", "{eps}", "{current_price}", "{ma20}", "{rsi}",
                         "{change_5d}", "{volatility}", "{inst_buy_days}", "{margin_change}"]
REQUIRED_CONTRACT = ['"ev_score"', '"recommendation"', '"reason"']


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except Exception:
        return default


def groq_complete(system: str, user: str) -> str:
    """呼叫 Groq Chat API（含重試：429/5xx 暫時性錯誤以指數退避重試，最多 3 次）。"""
    key = os.getenv("GROQ_API_KEY", "")                      # 🔴 用專案既有的 secret 名稱
    if not key:
        raise RuntimeError("GROQ_API_KEY 未設定")
    payload = {"model": os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b"),
               "temperature": 0.4,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    max_attempts = int(os.getenv("GROQ_MAX_RETRIES", "3"))
    delay = float(os.getenv("GROQ_RETRY_BASE_DELAY", "2"))   # 指数退避：2s → 4s → 8s（上限10s）
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.post(GROQ_URL, headers={"Authorization": f"Bearer {key}"},
                              json=payload, timeout=120)
            # 🔴 PR review #6：僅對暫時性錯誤重試；4xx（除 429）屬請求本身問題，直接失敗
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.exceptions.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return re.sub(r"<think>.*?</think>", "", r.json()["choices"][0]["message"]["content"],
                          flags=re.S).strip()
        except (requests.exceptions.RequestException, KeyError, IndexError, ValueError) as e:
            transient = isinstance(e, requests.exceptions.HTTPError) and \
                        getattr(getattr(e, "response", None), "status_code", 0) != 0 and \
                        (e.response.status_code == 429 or e.response.status_code >= 500)
            fatal_4xx = isinstance(e, requests.exceptions.HTTPError) and not transient
            if fatal_4xx or attempt >= max_attempts:
                logger.error("Groq API 失敗（attempt %d/%d）：%s", attempt, max_attempts, e)
                raise
            wait = min(delay * (2 ** (attempt - 1)), 10)
            logger.warning("Groq API 暫時性錯誤（attempt %d/%d）：%s → %.1fs 後重試",
                           attempt, max_attempts, e, wait)
            time.sleep(wait)
    raise RuntimeError("Groq API 重試耗盡")                   # 理論不可達，保險起見


def validate(text: str):
    """契約驗證：佔位符、輸出 JSON 契約欄位、長度範圍。回傳問題清單（空＝通過）。"""
    problems = [f"缺佔位符{x}" for x in REQUIRED_PLACEHOLDERS if x not in text]
    problems += [f"缺契約{x}" for x in REQUIRED_CONTRACT if x not in text]
    if not (200 < len(text) < 4000):
        problems.append("長度異常")
    return problems


def extract_new_prompt(raw: str) -> str:
    """優先取 ``` 圍欄內文；否則退回再次請求／原文去首尾空白。"""
    m = re.search(r"```(?:text|markdown)?\n(.*?)```", raw, flags=re.S)
    return (m.group(1) if m else raw).strip()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    verified = [r for r in load_json(DATA_DIR / "telemetry.json", {"records": []})["records"]
                if r.get("actual_result")]
    if len(verified) < int(os.getenv("OPTIMIZATION_THRESHOLD", "5")):
        logger.info("驗證樣本不足（%d < %s），跳過優化",
                    len(verified), os.getenv("OPTIMIZATION_THRESHOLD", "5"))
        return 0
    correct = sum(1 for r in verified if r["actual_result"].get("was_correct"))
    acc = correct / len(verified) * 100
    if acc >= float(os.getenv("TARGET_ACCURACY", "70")):
        logger.info("準確率 %.1f%% 達標，跳過", acc)
        return 0
    logger.info("verified=%d accuracy=%.1f%% → 啟動 prompt 優化", len(verified), acc)

    history = load_json(DATA_DIR / "prompt_history.json",
                        {"current_version": 1, "versions": []})
    cur = int(history.get("current_version", 1))
    tpl_path = PROMPTS_DIR / f"main_analysis_v{cur}.txt"
    if not tpl_path.exists():
        tpl_path = PROMPTS_DIR / "main_analysis.txt"
    tpl = tpl_path.read_text(encoding="utf-8") if tpl_path.exists() else ""

    # 🆕 v2.1：錯誤樣本帶入 alpha/net/hurdle/criteria 審計欄，
    # 讓優化器吃到的是「alpha 級錯誤」（例如追高跑輸大盤），而非單純跌漲。
    errors = [{
        "code": r.get("stock_code"), "name": r.get("stock_name"),
        "recommendation": (r.get("prediction") or {}).get("recommendation"),
        "reason": (r.get("prediction") or {}).get("reason"),
        "ret": r["actual_result"].get("profit_pct"),
        "net": r["actual_result"].get("net_pct"),
        "alpha": r["actual_result"].get("alpha_pct"),
        "hurdle": r["actual_result"].get("hurdle_pct"),
        "failed_criteria": r["actual_result"].get("criteria"),
    } for r in verified if not r["actual_result"].get("was_correct")][:20]

    system = ("你是量化 prompt 工程師。重寫台股分析 prompt 以提升 alpha 級勝率。硬約束："
              f"保留佔位符 {', '.join(REQUIRED_PLACEHOLDERS)}；"
              '僅輸出含 "ev_score","recommendation","reason" 的 JSON；'
              "缺失欄位不得視為 0；金融股忽略營收/EPS；只輸出新 prompt 本文。")
    user = (f"現行 V{cur} prompt：\n```\n{tpl}\n```\n勝率 {acc:.1f}%（{len(verified)} 筆）。\n"
            f"錯誤樣本（含 alpha 與失敗條件）：\n{json.dumps(errors, ensure_ascii=False, indent=2)}\n"
            "請歸因錯誤模式（追高、忽略籌碼、震盪市誤判、避開標準過鬆）並產出改進版。")

    new_tpl = extract_new_prompt(groq_complete(system, user))
    problems = validate(new_tpl)
    if problems:
        logger.error("契約驗證失敗：%s", problems)
        return 1

    nv = cur + 1
    (PROMPTS_DIR / f"main_analysis_v{nv}.txt").write_text(new_tpl, encoding="utf-8")
    history["current_version"] = nv
    versions = history.setdefault("versions", [])
    versions.append({
        "version": nv, "prev_version": cur,
        "created_at": datetime.now(TZ_TAIPEI).isoformat(),
        "description": f"自動優化（觸發時 v2 分類準確率 {acc:.1f}%）",
        "prompt_file": f"prompts/main_analysis_v{nv}.txt",
        "prev_accuracy": round(acc, 1),
        "verified_samples": len(verified),
        "trigger": "weekly_optimization",
    })
    meta = history.setdefault("metadata", {})
    meta["last_updated"] = datetime.now(TZ_TAIPEI).isoformat()
    meta["total_runs"] = int(meta.get("total_runs", 0)) + 1
    (DATA_DIR / "prompt_history.json").write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("✅ 產出 main_analysis_v%d.txt（V%d → V%d）", nv, cur, nv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
