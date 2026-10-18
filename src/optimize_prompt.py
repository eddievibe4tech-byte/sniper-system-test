"""
🆕 每週 Prompt 優化器（Verification v2.1 配套｜行為樹管線與骨架分離版）

架構設計：
  1. 【骨架分離】：固定佔位符與 JSON 契約由 Python PROMPT_SKELETON 掌控，LLM 僅需生成
     ~300-500 字的量化分析準則 → 佔位符遺失率 0%、輸出 Token 減少約 70%。
  2. 【行為樹本地修復】：以 Selector/Sequence 管線執行本地無損修復（脫圍欄、切格式雜訊、
     去重條列），優先在本地修復至合規（0 Token），修復失敗才向 LLM 回饋局部草稿。
  3. 【職責解耦重試】：groq_complete 負責 404 換模型與 429/5xx 指數退避；
     generate_with_smart_retry 僅負責語意合規性微調，消除乘數倍增重試問題。

紅燈根因修復（歷史沿革保留）：
  - 原本 workflow 引用不存在的 src.optimize_prompt 模組 → 補齊本模組。
  - secret 名稱對齊既有 GROQ_API_KEY（與 src/groq_client.py 一致）。
  - history schema 相容：prompt_history.json 使用 "versions" 陣列（非 "history"）。
  - 404 model_not_found → 動態模型鏈切換；429/5xx → 指數退避（見 groq_complete）。
  - 所有縮排皆為純 ASCII 空白（徹底清除 \\xa0 隱形字元地雷）。
"""
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PROMPTS_DIR = BASE_DIR / "prompts"
TZ_TAIPEI = timezone(timedelta(hours=8))
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
logger = logging.getLogger(__name__)

REQUIRED_PLACEHOLDERS = [
    "{code}", "{name}", "{regime}", "{revenue_yoy}", "{gross_margin}",
    "{net_margin}", "{eps}", "{current_price}", "{ma20}", "{rsi}",
    "{change_5d}", "{volatility}", "{inst_buy_days}", "{margin_change}"
]
REQUIRED_CONTRACT = ['"ev_score"', '"recommendation"', '"reason"']

# 🛡️ 固化模板骨架（由 Python 守護，0 Token 支出）
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


# ==============================================================================
# 1. 行為樹（Behavior Tree）文本清洗與驗證管線
# ==============================================================================

class BTStatus(Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


@dataclass
class CleanContext:
    raw_text: str
    cleaned_text: str = ""
    problems: List[str] = field(default_factory=list)
    repair_logs: List[str] = field(default_factory=list)


class BTNode:
    def tick(self, ctx: CleanContext) -> BTStatus:
        raise NotImplementedError


class Sequence(BTNode):
    """AND 節點：所有子節點成功才返回成功"""
    def __init__(self, children: List[BTNode]):
        self.children = children

    def tick(self, ctx: CleanContext) -> BTStatus:
        for child in self.children:
            if child.tick(ctx) == BTStatus.FAILURE:
                return BTStatus.FAILURE
        return BTStatus.SUCCESS


class Selector(BTNode):
    """OR 節點：任一子分支成功即返回成功"""
    def __init__(self, children: List[BTNode]):
        self.children = children

    def tick(self, ctx: CleanContext) -> BTStatus:
        for child in self.children:
            if child.tick(ctx) == BTStatus.SUCCESS:
                return BTStatus.SUCCESS
        return BTStatus.FAILURE


class ConditionValidLength(BTNode):
    def __init__(self, min_len: int = 150, max_len: int = 2500):
        self.min_len = min_len
        self.max_len = max_len

    def tick(self, ctx: CleanContext) -> BTStatus:
        length = len(ctx.cleaned_text)
        if length < self.min_len:
            ctx.problems.append(f"字數過短 (目前 {length} 字，需 >= {self.min_len})")
            return BTStatus.FAILURE
        if length > self.max_len:
            ctx.problems.append(f"字數過長 (目前 {length} 字，需 <= {self.max_len})")
            return BTStatus.FAILURE
        return BTStatus.SUCCESS


class ConditionNoFormatPollution(BTNode):
    """確認文字中沒有殘留格式說明或代碼區塊"""
    def tick(self, ctx: CleanContext) -> BTStatus:
        if re.search(r"(?:輸出|回傳|格式要求).*?JSON|```", ctx.cleaned_text, flags=re.I):
            ctx.problems.append("包含多餘的格式說明或代碼區塊")
            return BTStatus.FAILURE
        return BTStatus.SUCCESS


class ActionStripCodeFences(BTNode):
    """🛡️ 加強解析：處理各種 Markdown 圍欄格式（```json / ```text / ```python 等）

    根因修復：LLM 偶爾會模仿範例而把整份準則包進 ```json ... ``` 代碼塊，
    舊正則未容許圍欄標籤後的首行空白/縮排，導致脫圍欄失敗、測試紅燈。
    策略：先以多行模式逐行拔除圍欄行（含前後空白），再兜底清除任何殘留 ```。
    """
    _FENCE_LINE_RE = re.compile(r"^[ \t]*```[a-zA-Z0-9_-]*[ \t]*$", flags=re.M)
    # 成對內嵌圍欄：開圍欄必須「整行僅含 ```（可帶語言標籤與前後空白）」，
    # 內容非貪婪、收圍欄同樣須整行成立 → 絕不會跨越/誤吞有效準則文字。
    _FENCE_PAIR_RE = re.compile(
        r"(?:^|(?<=\n))[ \t]*```[a-zA-Z0-9_-]*[ \t]*\n.*?\n[ \t]*```[ \t]*(?=\n|$)",
        flags=re.S)
    # 開圍欄「前面」允許任意行內前綴（如碎碎念 "…JSON 格式如下：```json"）：
    # 先錨定行首/換行、僅容許同行內文字，再非貪婪吞掉整段雜訊；合法準則文字不含 ``` → 不會跨越誤刪。
    _FENCE_INLINE_OPEN_RE = re.compile(
        r"(?:^|(?<=\n))[^\n]*?[ \t]*```[a-zA-Z0-9_-]*[ \t]*\n.*?\n[ \t]*```[ \t]*(?=\n|$)",
        flags=re.S)

    # ⚠️ CI Review 修復：外殼剝除必須用 \A/\Z（字串絕對開頭/結尾），
    # 絕不可用 re.M —— 否則「正文 + 文末 ```json 範例」情境會誤匹配文末圍欄，
    # 把整篇核心準則丟棄、只剩 JSON（嚴重資料遺失 Bug）。
    _OUTER_FENCE_RE = re.compile(
        r"\A\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n\s*```\s*\Z", flags=re.S)

    def tick(self, ctx: CleanContext) -> BTStatus:
        original = ctx.cleaned_text or ctx.raw_text
        text = original

        # 策略 1：僅當「整個文本」被最外殼代碼塊包裹時才提取內部內容（\A/\Z 錨定，零誤吞）
        fence_match = self._OUTER_FENCE_RE.match(text)
        if fence_match:
            inner = fence_match.group(1).strip()
            if inner and "```" not in inner:
                text = inner
                ctx.repair_logs.append("提取代碼塊內部內容")

        # 策略 2：切除內嵌 ```json ... ``` 成對區塊（純格式雜訊，整塊丟棄）。
        #     必須在通用圍欄行拔除「之前」執行：若先拔圍欄行，碎碎念（如
        #     "請輸出合法的 JSON 格式如下："）會與 JSON 資料行失去邊界而殘留。
        pruned = self._FENCE_PAIR_RE.sub("\n", text).strip()
        if pruned and "```" not in pruned:
            text = pruned
        else:
            # 內嵌圍欄前帶行內前綴（如碎碎念 "…JSON 格式如下：```json\n{...}\n```"）：
            # 連同前綴整塊切除；合法準則文字不含 ``` → 非貪婪匹配不會跨越誤刪。
            pruned2 = self._FENCE_INLINE_OPEN_RE.sub("\n", text).strip()
            if pruned2 and "```" not in pruned2:
                text = pruned2
            else:
                # 黏行兜底：碎碎念與開圍欄同行且無收圍欄等罕見變體，
                # 以「JSON 字眼 + 圍欄」共現為強訊號，從該行起整段切除
                # （合法範本絕不會同時出現兩者 → 不誤刪；僅當保留正文 >120 字才採用）。
                m = re.search(r"^.*?\bJSON\b.*?```.*$", text, flags=re.S | re.M)
                if m:
                    head = text[:m.start()].rstrip()
                    if len(head) > 120:
                        text = head
        # 1) 逐行拔除「整行圍欄」（```json / ```text / ``` / ```python，容忍前後空白與縮排）
        stripped = self._FENCE_LINE_RE.sub("", text)
        # 2) 拔除「行首開圍欄」但同行有內文的情況（如 "```json {...}"）：只去圍欄標記、留內容
        stripped = re.sub(r"^[ \t]*```[a-zA-Z0-9_-]*(?=[ \t]|\b)[ \t]*", "", stripped, flags=re.M)
        # 3) 拔除「行尾收圍欄」（如 "{...} ```"）
        stripped = re.sub(r"[ \t]*```[ \t]*$", "", stripped, flags=re.M)
        # 4) ⚠️ CI Review 修復：移除「通用成對正則提取圍欄內文」兜底（原 re.sub(r'```...\n(.*?)\n...```', r'\1')）。
        #    該寫法與策略 2「整塊切除格式雜訊」互相矛盾，會把漏網的無效 JSON／格式說明「還原放回」污染正文。
        #    至此所有合法路徑均已處理，直接暴力清除一切殘留 ``` 標記即可。
        stripped = stripped.replace('```', '')
        # 5) 順序調整修復：若中途步驟已把「內嵌圍欄＋碎碎念」退化為裸 JSON 行，
        #    補做一次文末格式雜訊剔除（NAG 行掃描），避免碎碎念文字與 JSON 失去邊界而殘留。
        _nag = ActionPruneFormatNoise._NAG_RE
        _ls = stripped.splitlines()
        if any(_nag.search(l) for l in _ls[-6:]):
            pre_prune = ctx.cleaned_text
            ctx.cleaned_text = stripped.strip()
            ActionPruneFormatNoise().tick(ctx)
            stripped = ctx.cleaned_text
            ctx.cleaned_text = pre_prune  # 還原，交由下方統一賦值
        if stripped.strip() != original.strip():
            ctx.cleaned_text = stripped.strip()
            ctx.repair_logs.append("已移除所有 Markdown 代碼塊／圍欄標記")
        else:
            ctx.cleaned_text = stripped.strip()
        # 🛡️ 最終檢查：若清洗後仍包含 ```，視為失敗（理論上不該發生，因步驟 4 已兜底清除）
        if "```" in ctx.cleaned_text:
            ctx.problems.append("仍包含 Markdown 代碼塊標記 (```)")
            return BTStatus.FAILURE
        return BTStatus.SUCCESS


class ActionPruneFormatNoise(BTNode):
    """切除文末模型碎碎念的 JSON 要求或格式範例（錨定行尾，避免誤刪整段）"""
    _NAG_RE = re.compile(
        r"(?:請?(?:直接)?(?:輸出|回傳)|格式要求)[^\n]{0,40}JSON"
        r"|JSON[^\n]{0,40}(?:如下|以下|格式)",
        re.I)

    def tick(self, ctx: CleanContext) -> BTStatus:
        original = ctx.cleaned_text
        lines = original.splitlines()
        cut = len(lines)
        for i in range(len(lines) - 1, -1, -1):
            if self._NAG_RE.search(lines[i]):
                cut = i
                continue
            # 容忍碎碎念後方跟著的純 JSON 資料行（如 {"ev_score": 80...}）
            stripped = lines[i].strip()
            if stripped and (stripped.startswith("{") or stripped.startswith("```")):
                cut = i
                continue
            break
        cleaned = "\n".join(lines[:cut]).strip()

        if cleaned != original.strip():
            ctx.cleaned_text = cleaned
            ctx.repair_logs.append("已剔除多餘 JSON 格式說明")
        return BTStatus.SUCCESS


class ActionDeduplicateRules(BTNode):
    """去除語意或字面重複的條列項目（模糊比對：忽略序號、標點與空白差異）"""

    @staticmethod
    def _normalize(line: str) -> str:
        # 去掉行首序號（1. / - / 2) 等）、所有標點與空白，僅留英數字與中文字
        body = re.sub(r"^\s*(?:\d+[.)、]|[-*•])\s*", "", line)
        return re.sub(r"[^\w\u4e00-\u9fa5]", "", body).lower()

    def tick(self, ctx: CleanContext) -> BTStatus:
        lines = [line.strip() for line in ctx.cleaned_text.splitlines()]
        kept: List[str] = []
        seen = set()

        for line in lines:
            if not line:
                continue  # 丟棄空行
            norm = self._normalize(line)
            if not norm:
                kept.append(line)
                continue
            if norm in seen:
                continue  # 重複條列 → 剔除
            if kept and len(norm) >= 8:
                prev_norm = self._normalize(kept[-1])
                if norm == prev_norm:
                    continue
                if len(norm) >= 20 and (norm in prev_norm or prev_norm in norm):
                    # 包含關係（同一長句換句話說重複出現）→ 保留較長者，就地取代
                    if len(norm) > len(prev_norm):
                        kept[-1] = line
                    continue
            kept.append(line)
            seen.add(norm)

        if len(kept) < len(lines):
            ctx.cleaned_text = "\n".join(kept)
            ctx.repair_logs.append(f"已去重，剔除 {len(lines) - len(kept)} 行重複條列")
        return BTStatus.SUCCESS


class ActionSalvageFallback(BTNode):
    """寬容降級兜底：修復後若具備基本長度則採用，避免呼叫 API"""
    def __init__(self, min_salvage_len: int = 120):
        self.min_salvage_len = min_salvage_len

    def tick(self, ctx: CleanContext) -> BTStatus:
        if len(ctx.cleaned_text) >= self.min_salvage_len:
            ctx.repair_logs.append(f"觸發寬容收尾，採用核心片段 ({len(ctx.cleaned_text)} 字)")
            return BTStatus.SUCCESS
        return BTStatus.FAILURE


def build_rules_cleaner_tree() -> BTNode:
    """建構規則清洗驗證樹"""
    return Selector([
        # 分支 1：原始文字已經非常乾淨，直接驗證通過
        Sequence([
            ConditionValidLength(),
            ConditionNoFormatPollution()
        ]),
        # 分支 2：本地自動修復流水線（拔圍欄 -> 切除雜訊 -> 去重 -> 重新檢驗）
        Sequence([
            ActionStripCodeFences(),
            ActionPruneFormatNoise(),
            ActionDeduplicateRules(),
            ConditionValidLength(),
            ConditionNoFormatPollution()
        ]),
        # 分支 3：修復後仍有些微長度瑕疵，但達到底線門檻
        ActionSalvageFallback()
    ])


def clean_and_validate_with_bt(raw_text: str) -> Tuple[Optional[str], List[str]]:
    """以行為樹驅動文字清洗與驗證，回傳 (清洗後字串, 錯誤清單)"""
    ctx = CleanContext(raw_text=raw_text, cleaned_text=raw_text)
    tree = build_rules_cleaner_tree()
    status = tree.tick(ctx)

    if status == BTStatus.SUCCESS:
        if ctx.repair_logs:
            logger.info("🛠️ 本地行為樹修復成功：%s", " -> ".join(ctx.repair_logs))
        return ctx.cleaned_text, []

    return None, ctx.problems


# ==============================================================================
# 2. 輔助函式與 Groq API 連線層
# ==============================================================================

def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except Exception:
        return default


def _model_chain() -> list:
    try:
        from src.model_resolver import get_dynamic_model_chain
    except ImportError:
        from model_resolver import get_dynamic_model_chain
    return get_dynamic_model_chain()


def groq_complete(system: str, user: str) -> str:
    """呼叫 Groq Chat API：404 換模型；429/5xx 指數退避（網路職責全在本層）"""
    key = os.getenv("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("GROQ_API_KEY 未設定")

    max_attempts = int(os.getenv("GROQ_MAX_RETRIES", "3"))
    delay = float(os.getenv("GROQ_RETRY_BASE_DELAY", "2"))
    last_err = None

    for model in _model_chain():
        payload = {
            "model": model,
            "temperature": 0.2,
            "max_tokens": 1500,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ]
        }
        for attempt in range(1, max_attempts + 1):
            try:
                r = requests.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                    timeout=120
                )
                if r.status_code == 404:
                    logger.warning("Groq 404（模型不可用）：%s -> 切換 fallback 模型", model)
                    last_err = requests.exceptions.HTTPError(
                        f"404 model={model}: {r.text}", response=r)
                    break

                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.exceptions.HTTPError(f"HTTP {r.status_code}", response=r)

                r.raise_for_status()
                logger.info("Groq 準則生成成功，使用模型：%s", model)
                content = r.json()["choices"][0]["message"]["content"]
                return re.sub(r"<\s*think\s*>.*?<\s*/\s*think\s*>", "", content, flags=re.S).strip()

            except (requests.exceptions.RequestException, KeyError, IndexError, ValueError) as e:
                transient = isinstance(e, requests.exceptions.HTTPError) and \
                            getattr(getattr(e, "response", None), "status_code", 0) != 0 and \
                            (e.response.status_code == 429 or e.response.status_code >= 500)
                fatal_4xx = isinstance(e, requests.exceptions.HTTPError) and not transient

                if fatal_4xx or attempt >= max_attempts:
                    logger.error("Groq API 失敗（model=%s, attempt %d/%d）：%s",
                                 model, attempt, max_attempts, e)
                    last_err = e
                    break

                wait = min(delay * (2 ** (attempt - 1)), 10)
                logger.warning("Groq API 暫時性錯誤（model=%s, attempt %d/%d）：%s -> %.1fs 後重試",
                               model, attempt, max_attempts, e, wait)
                time.sleep(wait)

    raise RuntimeError(f"Groq 全模型鏈失敗：{last_err}")


def assemble_prompt(strategy_rules: str) -> str:
    """本地縫合：將準則填入固定骨架，保證佔位符與契約完整（0 Token）"""
    skeleton = PROMPT_SKELETON
    skel_path = PROMPTS_DIR / "main_analysis_skeleton.txt"
    if skel_path.exists():
        skeleton = skel_path.read_text(encoding="utf-8")

    assembled = skeleton.replace("{strategy_rules}", strategy_rules)
    problems = [f"缺佔位符{x}" for x in REQUIRED_PLACEHOLDERS if x not in assembled]
    problems += [f"缺契約{x}" for x in REQUIRED_CONTRACT if x not in assembled]
    if problems:
        raise RuntimeError(f"骨架自檢失敗（請檢查 prompts/main_analysis_skeleton.txt）：{problems}")
    return assembled


def generate_with_smart_retry(system_prompt: str, user_prompt: str,
                              max_retries: int = 3) -> Optional[str]:
    """
    外層僅處理【語意內容修復】，內層 groq_complete 負責【網路與模型退避】。
    兩者職責切開，避免無謂的 API 呼叫放大（消除乘數爆炸）。
    """
    current_user_prompt = user_prompt

    for attempt in range(1, max_retries + 1):
        try:
            logger.info("📡 呼叫 Groq API 生成策略準則（第 %d/%d 次）...", attempt, max_retries)
            # groq_complete 內部已有退避與 fallback，徹底失敗會拋 RuntimeError
            raw = groq_complete(system_prompt, current_user_prompt)
        except RuntimeError as e:
            # API 全線故障或限流，外層直接棄投，絕不再盲目撞擊
            logger.error("🚨 Groq 底層連線徹底失敗，終止優化流程: %s", e)
            return None

        # 透過行為樹進行本地清洗與合規檢驗（0 Token 優先修復）
        cleaned_rules, problems = clean_and_validate_with_bt(raw)

        # 🛡️ 最終檢查：確保沒有 Markdown 代碼塊（雙重保險，防止清洗管線漏網）
        if cleaned_rules:
            if "```" in cleaned_rules:
                logger.warning("⚠️ 清洗後仍包含 Markdown，視為失敗")
                problems = list(problems) + ["最終輸出仍包含 Markdown 代碼塊"]
                cleaned_rules = None
            elif len(cleaned_rules) < 150:
                logger.warning("⚠️ 清洗後長度不足 (%d 字)", len(cleaned_rules))
                problems = list(problems) + [f"長度不足 ({len(cleaned_rules)} 字)"]
                cleaned_rules = None

        if cleaned_rules:
            logger.info("✅ 第 %d 次嘗試通過準則驗證", attempt)
            return cleaned_rules

        logger.warning("⚠️ 第 %d 次準則內容未達標：%s", attempt, problems)

        # 輕量自我修復：只把有問題的草稿局部回饋，不重新發送龐大歷史樣本
        current_user_prompt = (
            f"你先前產出的策略規則存在以下問題：{', '.join(problems)}。\n"
            f"【先前草稿】：\n{raw}\n\n"
            "請在維持原有良好邏輯的前提下修正上述問題。\n"
            "⚠️ 重要提醒：\n"
            "1. 請輸出純文本的策略規則條列\n"
            "2. 嚴禁使用任何 Markdown 代碼塊（```）\n"
            "3. 嚴禁提及任何格式或 JSON\n"
            "4. 只需輸出策略規則內容本身"
        )

    return None


# ==============================================================================
# 3. 主排程流程
# ==============================================================================

def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # 防護：確保輸出目錄存在（新容器首次執行不噴 FileNotFoundError）
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

    logger.info("verified=%d accuracy=%.1f%% -> 啟動 prompt 優化", len(verified), acc)

    history = load_json(DATA_DIR / "prompt_history.json", {"current_version": 1, "versions": []})
    cur = int(history.get("current_version", 1))
    tpl_path = PROMPTS_DIR / f"main_analysis_v{cur}.txt"
    if not tpl_path.exists():
        tpl_path = PROMPTS_DIR / "main_analysis.txt"
    tpl = tpl_path.read_text(encoding="utf-8") if tpl_path.exists() else ""

    # 取前 8 筆典型錯誤樣本（控制 Input Tokens）
    errors = [{
        "code": r.get("stock_code"), "name": r.get("stock_name"),
        "recommendation": (r.get("prediction") or {}).get("recommendation"),
        "ret": r["actual_result"].get("profit_pct"),
        "net": r["actual_result"].get("net_pct"),
        "alpha": r["actual_result"].get("alpha_pct"),
        "hurdle": r["actual_result"].get("hurdle_pct"),
        "failed_criteria": r["actual_result"].get("criteria"),
    } for r in verified if not r["actual_result"].get("was_correct")][:8]

    system = (
        "你是台股量化策略工程師。你的任務是審查系統近期的預測失敗樣本，"
        "針對『追高破線』、『營收陷阱』或『忽視法人轉折』等問題，"
        "撰寫一份結構清晰的【量化準則條列】。\n\n"
        "【輸出規範】：\n"
        "1. 只需輸出準則文字（含條列點），嚴禁輸出程式碼、佔位符或 JSON 範例。\n"
        "2. 必須涵蓋：金融股例外處理、假突破防守、法人/散戶融資逆向警訊。\n"
        "3. 缺失欄位不得視為 0 或負面證據。\n"
        "4. 長度控制在 150-2500 字，具體可操作（附數值門檻）。\n"
        "5. 輸出必須是純文本的策略規則，嚴禁使用任何 Markdown 代碼塊（```）。\n"
        "6. 只需輸出策略規則內容本身，不要附加任何開場白、結語或格式說明。"
    )
    user = (
        f"當前模型勝率：{acc:.1f}%（已驗證 {len(verified)} 筆）。\n"
        f"現行 V{cur} 決策規則參考：\n{tpl[:1200]}\n\n"
        f"近期典型虧損樣本（系統建議 vs 實際結果）：\n"
        f"{json.dumps(errors, ensure_ascii=False, indent=1)}\n\n"
        "請提煉出 4~6 條具體、可操作的決策檢驗準則，供分析模型作為判斷指引。"
        "只輸出準則條列本身，不要包含任何 Markdown 標記。"
    )

    strategy_rules = generate_with_smart_retry(system, user, max_retries=3)
    if not strategy_rules:
        logger.error("🚨 重試後仍無法取得可用策略準則，維持舊版本（V%d）", cur)
        return 1

    # 🛡️ 最終檢查：確保沒有 Markdown 代碼塊（寫入前最後防線）
    if "```" in strategy_rules:
        logger.error("🚨 最終檢查失敗：策略規則仍包含 Markdown 代碼塊")
        logger.error("內容預覽（前 500 字）：%s", strategy_rules[:500])
        logger.error("...")
        logger.error("內容預覽（後 500 字）：%s", strategy_rules[-500:])
        return 1

    # 本地縫合：保證 14 個佔位符 100% 完整
    try:
        new_tpl = assemble_prompt(strategy_rules)
    except RuntimeError as e:
        logger.error("🚨 骨架自檢失敗：%s", e)
        return 1

    nv = cur + 1
    (PROMPTS_DIR / f"main_analysis_v{nv}.txt").write_text(new_tpl, encoding="utf-8")

    # 🛡️ 最終防線：組裝完成仍殘留 Markdown 圍欄 → 就地剝除，避免污染下游 LLM 輸出
    if "```" in new_tpl:
        logger.warning("⚠️ 組裝後模板仍含 Markdown 圍欄，執行最終剝除（V%d）", nv)
        new_tpl = re.sub(r"[ \t]*```[a-zA-Z0-9_-]*[ \t]*\n?", "", new_tpl).strip() + "\n"
        (PROMPTS_DIR / f"main_analysis_v{nv}.txt").write_text(new_tpl, encoding="utf-8")

    history["current_version"] = nv
    versions = history.setdefault("versions", [])
    versions.append({
        "version": nv, "prev_version": cur,
        "created_at": datetime.now(TZ_TAIPEI).isoformat(),
        "description": f"自動優化·行為樹清洗版（觸發時 v2 分類準確率 {acc:.1f}%）",
        "prompt_file": f"prompts/main_analysis_v{nv}.txt",
        "prev_accuracy": round(acc, 1),
        "verified_samples": len(verified),
        "trigger": "weekly_optimization",
    })
    meta = history.setdefault("metadata", {})
    meta["last_updated"] = datetime.now(TZ_TAIPEI).isoformat()
    meta["total_runs"] = int(meta.get("total_runs", 0)) + 1

    (DATA_DIR / "prompt_history.json").write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("🎉 成功以低 Token 消耗合成 main_analysis_v%d.txt（V%d -> V%d）", nv, cur, nv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
