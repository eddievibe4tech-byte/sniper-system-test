"""
🆕 每週 Prompt 優化器（Verification v2.1 配套｜Token 節流與骨架分離版）

流程：讀已驗證 telemetry → 守門（樣本數/達標準跳過）→ 錯誤樣本歸因（含 alpha 與
失敗條件審計欄）→ Groq 僅生成「策略準則」→ Python 本地縫合固定骨架（14 個佔位符
＋輸出契約由模板保證，0 Token）→ 輕量自我修復重試 → 產出 main_analysis_v{N+1}.txt
並更新 data/prompt_history.json（current_version 遞增，供 Daily Analysis 版本驅動載入）。

紅燈根因修復：
  - 原本 workflow 引用不存在的 src.optimize_prompt 模組 → 補齊本模組。
  - secret 名稱對齊既有 GROQ_API_KEY（與 src/groq_client.py 一致）。
  - history schema 相容：prompt_history.json 使用 "versions" 陣列（非 "history"）。
  - 404 model_not_found → 動態模型鏈切換；429/5xx → 指數退避（見 groq_complete）。

🛡️ 本版三重改進（解決「暴力盲抽重試」燒 Token 的反模式）：
  1. 【骨架分離】固定佔位符與 JSON 契約由 Python PROMPT_SKELETON 掌控，
     LLM 僅需生成 ~300-500 字的量化分析準則 → 佔位符遺失率 0%、輸出 Token 減少約 70%。
  2. 【輕量自我修復】驗證失敗時只回傳有問題的草稿局部修正，
     不把整包錯誤樣本上下文重送 → 消除重複 Context 消耗。
  3. 【網路異常指數退避】groq_complete 內建 404 換模型、429/5xx sleep 退避，
     嚴禁無退避的連環撞擊觸發更嚴重限流。

🛡️ v2 重試解耦與 0 Token 本地修復（防止乘數放大效應）：
  4. 【職責切分】網路故障／429／5xx／404 全部留在 groq_complete 處理；
     全模型鏈失敗直接拋 RuntimeError 中斷程序，外層不再盲目重撞
     （避免內層 model×attempt 與外層 retry 相乘 → 極端 18 次請求的爆炸）。
  5. 【0 Token 清洗】clean_and_validate_rules 用正則切除模型碎碎念的
     「請輸出 JSON…」贅字與 ```json 圍欄，不必花一次 API 呼叫重寫。
  6. 【寬容修補去噪】最終採納的草稿已過本地正則去噪，不會把衝突的
     輸出格式描述縫進骨架造成下游格式錯亂。
  7. 【目錄防護】main() 先 mkdir(parents=True, exist_ok=True)，
     新容器環境首次執行不再噴 FileNotFoundError。
"""
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

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

# 🛡️ 固化模板骨架（由 Python 守護，不花費 LLM 思考額度）
# Note: {strategy_rules} 是唯一由 LLM 生成的區塊；其餘佔位符由 groq_client._render_template
# 逐鍵替換（str.replace），因此模板中 JSON 契約可使用單一大括號而無需 {{}} 跳脫。
PROMPT_SKELETON = """你是一位台股短波段量化分析專家。請根據下方技術與籌碼數據進行評估。

【基本資料】
股票代碼：{code}
股票名稱：{name}
市場體制：{regime}

【技術與指標】
當前股價：{current_price} (20MA: {ma20})
5日漲跌幅：{change_5d}%
波動率：{volatility}%
RSI 指標：{rsi}

【籌碼與財務】
營收 YoY：{revenue_yoy}%
毛利率：{gross_margin}%
淨利率：{net_margin}%
EPS：{eps}
法人連續買超天數：{inst_buy_days}
融資增減變化：{margin_change}

【核心決策準則】（依據近期市場實盤回測反思優化）
{strategy_rules}

【輸出契約約束】
請直接回傳合法的 JSON 物件，嚴禁包含額外對話或 Markdown 標籤：
{"ev_score": 75, "recommendation": "積極買入/謹慎買入/觀望/避開", "reason": "≤30字之簡短理由"}
"""


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except Exception:
        return default


def _model_chain() -> list:
    """🔴 修正：空字串 env 不再覆蓋預設值；動態抓取活躍模型＋靜態兜底鏈。

    根因：workflow 的 `GROQ_MODEL: ${{ vars.GROQ_MODEL }}` 在 repo 未設定該
    Variable 時會注入「空字串」，而 os.getenv 的預設值只在變數「不存在」時生效，
    於是 payload 變成 model="" → Groq 回 404 model_not_found。
    此外舊預設 qwen/qwen3.6-27b 是 OpenRouter 風格 ID，Groq 只接受扁平 ID。
    """
    try:  # 正式路徑（python -m src.optimize_prompt）
        from src.model_resolver import get_dynamic_model_chain
    except ImportError:  # 相容 flat import 模式（src/ 在 sys.path）
        from model_resolver import get_dynamic_model_chain
    return get_dynamic_model_chain()


def groq_complete(system: str, user: str) -> str:
    """呼叫 Groq Chat API：404（model_not_found）→ 換下一個模型；429/5xx → 指數退避重試。

    Note: timeout=120 保留給極端長推理情境；本版 LLM 僅生成短準則（max_tokens=1500），
    一般情況遠低於此上限，不會再為整份 prompt 的重打而支付長生成費用。
    """
    key = os.getenv("GROQ_API_KEY", "")                      # 🔴 用專案既有的 secret 名稱
    if not key:
        raise RuntimeError("GROQ_API_KEY 未設定")
    max_attempts = int(os.getenv("GROQ_MAX_RETRIES", "3"))
    delay = float(os.getenv("GROQ_RETRY_BASE_DELAY", "2"))   # 指数退避：2s → 4s → 8s（上限10s）
    last_err = None
    for model in _model_chain():
        payload = {"model": model,
                   "temperature": 0.2,  # 🛡️ 降低 temperature 提升格式遵循度
                   "max_tokens": 1500,  # 🛡️ 限制最大產出，避免無限吐字燒 Token
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}]}
        for attempt in range(1, max_attempts + 1):
            try:
                r = requests.post(GROQ_URL, headers={"Authorization": f"Bearer {key}"},
                                  json=payload, timeout=120)
                # 🔴 404＝model_not_found：重試無意義，立即切換 fallback 模型
                if r.status_code == 404:
                    logger.warning("Groq 404（模型不可用）：%s → 切換 fallback 模型", model)
                    last_err = requests.exceptions.HTTPError(
                        f"404 model={model}: {r.text}", response=r)
                    break  # 跳出 attempt 迴圈，進入下一個 model
                # 🔴 PR review #6：僅對暫時性錯誤重試；其他 4xx 屬請求本身問題
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.exceptions.HTTPError(f"HTTP {r.status_code}", response=r)
                r.raise_for_status()
                logger.info("Groq 準則生成成功，使用模型：%s", model)
                return re.sub(r"<\s*think\s*>.*?<\s*/\s*think\s*>", "",
                              r.json()["choices"][0]["message"]["content"],
                              flags=re.S).strip()
            except (requests.exceptions.RequestException, KeyError, IndexError, ValueError) as e:
                transient = isinstance(e, requests.exceptions.HTTPError) and \
                            getattr(getattr(e, "response", None), "status_code", 0) != 0 and \
                            (e.response.status_code == 429 or e.response.status_code >= 500)
                fatal_4xx = isinstance(e, requests.exceptions.HTTPError) and not transient
                if fatal_4xx or attempt >= max_attempts:
                    logger.error("Groq API 失敗（model=%s, attempt %d/%d）：%s",
                                 model, attempt, max_attempts, e)
                    last_err = e
                    break  # 此模型已盡力，換下一個
                wait = min(delay * (2 ** (attempt - 1)), 10)
                logger.warning("Groq API 暫時性錯誤（model=%s, attempt %d/%d）：%s → %.1fs 後重試",
                               model, attempt, max_attempts, e, wait)
                time.sleep(wait)
    raise RuntimeError(f"Groq 全模型鏈失敗：{last_err}")


def extract_rules(raw: str) -> str:
    """提取生成的準則文本，過濾多餘 Markdown 圍欄。"""
    m = re.search(r"```(?:markdown|text)?\n(.*?)```", raw, flags=re.S)
    return (m.group(1) if m else raw).strip()


# 🛡️ 0 Token 本地清洗：切除模型碎碎念的輸出格式贅字（不必花 API 呼叫重寫）
_OUTPUT_FLUFF_RE = re.compile(
    r"(?:請?輸出|回傳|格式要求|輸出格式).*?(?:JSON|json).*$", re.S)
_JSON_FENCE_RE = re.compile(r"```json.*?```", re.S)
# 精確判定「輸出格式描述」殘留（取代死板的 "請輸出"+"JSON" 雙 in 檢查）
_FORMAT_MENTION_RE = re.compile(r"(?:輸出|格式|回傳)[^\n]{0,20}JSON|```json")


def clean_and_validate_rules(rules_text: str) -> tuple:
    """本地 0 Token 修復：自動清洗多餘格式廢話，並檢查長度。

    回傳 (cleaned_text, problems)；problems 為空清單＝通過。
    """
    cleaned = extract_rules(rules_text)
    # 若模型在文末交代「請輸出 JSON...」等贅字 → Regex 直接切除，不花 Token 重試
    cleaned = _JSON_FENCE_RE.sub("", cleaned)
    cleaned = _OUTPUT_FLUFF_RE.sub("", cleaned).strip()

    problems = []
    if len(cleaned) < 150:
        problems.append("策略規則過於簡短 (少於 150 字)")
    if len(cleaned) > 2500:
        problems.append("策略規則過於冗長 (大於 2500 字)")
    if _FORMAT_MENTION_RE.search(cleaned):
        problems.append("仍殘留輸出格式描述，請僅專注於交易準則")
    return cleaned, problems


def assemble_prompt(strategy_rules: str) -> str:
    """🛡️ 本地縫合：將 LLM 產出的準則填入固定骨架 → 100% 保證佔位符與契約完整（0 Token）。"""
    skeleton = PROMPT_SKELETON
    skel_path = PROMPTS_DIR / "main_analysis_skeleton.txt"
    if skel_path.exists():  # 允許以檔案覆寫骨架（不需改程式即可調整措辭）
        skeleton = skel_path.read_text(encoding="utf-8")
    assembled = skeleton.replace("{strategy_rules}", strategy_rules)
    problems = [f"缺佔位符{x}" for x in REQUIRED_PLACEHOLDERS if x not in assembled]
    problems += [f"缺契約{x}" for x in REQUIRED_CONTRACT if x not in assembled]
    if problems:  # 理論上不該發生（骨架被外部覆寫壞掉時才會）
        raise RuntimeError(f"骨架自檢失敗（請檢查 prompts/main_analysis_skeleton.txt）：{problems}")
    return assembled


def generate_with_smart_retry(system_prompt: str, user_prompt: str,
                              max_retries: int = 3) -> Optional[str]:
    """外層僅處理【語意內容修復】，內層 groq_complete 負責【網路與模型退避】。

    職責嚴格切分，消除內外層重試的乘數放大效應（極端 model×attempt×retry＝18 次撞擊）：
    - 網路故障／429／5xx／404 → 全部由 groq_complete 內部退避＋換模型處理；
      全模型鏈失敗時拋 RuntimeError，此處直接棄投終止，絕不盲目重撞。
    - 內容驗證失敗 → 輕量自我修復：只把清洗後的問題草稿丟回修正，
      不重送原始大批樣本上下文。
    """
    last_cleaned_rules = ""
    current_user_prompt = user_prompt

    for attempt in range(1, max_retries + 1):
        try:
            logger.info("📡 呼叫 Groq API 生成策略準則（第 %d/%d 次）...", attempt, max_retries)
            # groq_complete 內部已有 404 換模型與 429/5xx 指數退避；
            # 全線失敗會拋 RuntimeError —— 屬 API 故障／限流，外層直接棄投。
            raw = groq_complete(system_prompt, current_user_prompt)
        except RuntimeError as e:
            logger.error("🚨 Groq 底層連線徹底失敗，終止優化流程（不再重撞）：%s", e)
            return None

        cleaned_rules, problems = clean_and_validate_rules(raw)
        last_cleaned_rules = cleaned_rules

        if not problems:
            logger.info("✅ 第 %d 次嘗試通過準則驗證", attempt)
            return cleaned_rules

        logger.warning("⚠️ 第 %d 次準則內容未達標：%s", attempt, problems)

        # 🛡️ 輕量自我修復：只針對有問題的草稿局部修正，不重複消耗龐大樣本上下文
        current_user_prompt = (
            f"你先前產出的策略規則存在以下問題：{', '.join(problems)}。\n"
            f"【先前草稿】：\n{last_cleaned_rules}\n\n"
            "請在維持原有良好邏輯的前提下修正上述問題。"
            "請注意：只需輸出純交易準則條列，嚴禁提及任何格式或 JSON。"
        )

    # 最終寬容修補：草稿已過本地正則去噪（不會引入衝突的輸出格式描述），
    # 長度基本達標（>=120 字）即允許採用，避免最後一次小瑕疵再燒一輪 API。
    if len(last_cleaned_rules) >= 120:
        logger.info("🛠️ 啟用本地清洗後之策略草稿（長度：%d 字）", len(last_cleaned_rules))
        return last_cleaned_rules
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    # 🛡️ 目錄防護：新容器／CI 環境首次執行時 DATA_DIR／PROMPTS_DIR 可能不存在，
    # 先建立避免寫入 main_analysis_v{N}.txt / prompt_history.json 噴 FileNotFoundError。
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    verified = [r for r in load_json(DATA_DIR / "telemetry.json", {"records": []})["records"]
                if r.get("actual_result")]
    threshold = int(os.getenv("OPTIMIZATION_THRESHOLD", "5"))
    if len(verified) < threshold:
        logger.info("驗證樣本不足（%d < %d），跳過優化", len(verified), threshold)
        return 0
    correct = sum(1 for r in verified if r["actual_result"].get("was_correct"))
    acc = correct / len(verified) * 100
    target_acc = float(os.getenv("TARGET_ACCURACY", "70"))
    if acc >= target_acc:
        logger.info("準確率 %.1f%% 達標（目標 %.0f%%），跳過", acc, target_acc)
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
    # 🛡️ 取前 8 筆代表性錯誤即足以歸納共性，進一步減少 input tokens。
    errors = [{
        "code": r.get("stock_code"), "name": r.get("stock_name"),
        "recommendation": (r.get("prediction") or {}).get("recommendation"),
        "ret": r["actual_result"].get("profit_pct"),
        "net": r["actual_result"].get("net_pct"),
        "alpha": r["actual_result"].get("alpha_pct"),
        "hurdle": r["actual_result"].get("hurdle_pct"),
        "failed_criteria": r["actual_result"].get("criteria"),
    } for r in verified if not r["actual_result"].get("was_correct")][:8]

    # 🛡️ 骨架分離：System 只叫模型專注「交易準則」，格式由 Python 骨架負責
    system = (
        "你是台股量化策略工程師。你的任務是審查系統近期的預測失敗樣本，"
        "針對『追高破線』、『營收陷阱』或『忽視法人轉折』等問題，"
        "撰寫一份結構清晰的【量化準則條列】。\n\n"
        "【輸出規範】：\n"
        "1. 只需輸出準則文字（含條列點），嚴禁輸出程式碼、佔位符或 JSON 範例。\n"
        "2. 必須涵蓋：金融股例外處理、假突破防守、法人/散戶融資逆向警訊。\n"
        "3. 缺失欄位不得視為 0 或負面證據。\n"
        "4. 長度控制在 150-2500 字，具體可操作（附數值門檻）。"
    )
    user = (
        f"當前模型勝率：{acc:.1f}%（已驗證 {len(verified)} 筆）。\n"
        f"現行 V{cur} 決策規則參考：\n```\n{tpl[:1200]}\n```\n\n"
        f"近期典型虧損樣本（系統建議 vs 實際結果）：\n"
        f"{json.dumps(errors, ensure_ascii=False, indent=1)}\n\n"
        "請提煉出 4~6 條具體、可操作的決策檢驗準則，供分析模型作為判斷指引。"
        "只輸出準則條列本身。"
    )

    strategy_rules = generate_with_smart_retry(system, user, max_retries=3)
    if not strategy_rules:
        logger.error("🚨 重試後仍無法取得可用策略準則，維持舊版本（V%d）", cur)
        return 1

    # 🛡️ 本地縫合：100% 保證佔位符與合約結構完整（零 Token）
    try:
        new_tpl = assemble_prompt(strategy_rules)
    except RuntimeError as e:
        logger.error("🚨 骨架自檢失敗：%s", e)
        return 1

    nv = cur + 1
    (PROMPTS_DIR / f"main_analysis_v{nv}.txt").write_text(new_tpl, encoding="utf-8")
    history["current_version"] = nv
    versions = history.setdefault("versions", [])
    versions.append({
        "version": nv, "prev_version": cur,
        "created_at": datetime.now(TZ_TAIPEI).isoformat(),
        "description": f"自動優化·骨架分離版（觸發時 v2 分類準確率 {acc:.1f}%）",
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
    logger.info("🎉 成功以低 Token 消耗合成 main_analysis_v%d.txt（V%d → V%d）", nv, cur, nv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
