"""
行為樹文字清洗與驗證管線單元測試（src/optimize_prompt.py）

涵蓋 4 個維度：
  一、核心修復節點（葉子動作）：脫圍欄 / 切格式雜訊 / 去重條列
  二、門禁驗證節點（葉子條件）：長度邊界 / 格式污染正則
  三、組合節點控制流：Sequence 短路 / Selector 回退
  四、端到端行為樹路徑：完美放行 / 複合污染完整修復 / 寬容收尾 / 全線潰敗回饋
"""
import pytest

from src.optimize_prompt import (
    BTNode,
    BTStatus,
    CleanContext,
    ConditionNoFormatPollution,
    ConditionValidLength,
    ActionStripCodeFences,
    ActionPruneFormatNoise,
    ActionDeduplicateRules,
    ActionSalvageFallback,
    Sequence,
    Selector,
    build_rules_cleaner_tree,
    clean_and_validate_with_bt,
    generate_with_smart_retry,
)


# 測試專用：淨長度約 190 字的有效量化準則基底（Invariant Filler）。
# 四條準則互不相同、無圍欄、無 JSON 敏感詞，確保經「去重／去噪」清洗後
# 仍不會因 Net Length Shrinkage 跌破 150 字門禁；被測試的雜訊（Noise）
# 一律疊加在此基底之上，使斷言只反映修復管線行為而非樣本長度巧合。
VALID_BASE_RULES = """1. 股價突破 20MA 需伴隨成交量放大至 5 日均量 1.5 倍以上方可確認有效。
2. 融資餘額連續三天攀升且跌破關鍵支撐時，嚴禁任何追高買進操作。
3. 外資與投信連續賣超之弱勢標的，一律排除於候選名單之外。
4. 營收 YoY 呈現衰退且毛利率下滑之個股，即使技術面突破亦不予進場。
5. 震盪市體制下 ev_score 門檻提高至 75，未達者一律給觀望建議。"""


def make_ctx(text: str) -> CleanContext:
    return CleanContext(raw_text=text, cleaned_text=text)


# ==============================================================================
# 一、核心修復節點測試（葉子動作節點）
# ==============================================================================

class TestActionStripCodeFences:
    """1. Markdown 圍欄脫殼"""

    @pytest.mark.parametrize("tag", ["markdown", "text", ""])
    def test_strips_fenced_content(self, tag):
        inner = "1. 跌破 20MA 強制停損。\n2. 法人連續賣超 3 天不追高。"
        raw = f"```{tag}\n{inner}\n```" if tag else f"```\n{inner}\n```"
        ctx = make_ctx(raw)
        assert ActionStripCodeFences().tick(ctx) == BTStatus.SUCCESS
        assert ctx.cleaned_text == inner
        assert any("圍欄" in log for log in ctx.repair_logs)

    def test_plain_text_preserved(self):
        raw = "1. 突破季線且外資買超才可進場。"
        ctx = make_ctx(raw)
        assert ActionStripCodeFences().tick(ctx) == BTStatus.SUCCESS
        assert ctx.cleaned_text == raw


class TestActionPruneFormatNoise:
    """2. 切除格式廢話與 JSON，絕不誤傷正常技術規則"""

    def test_prunes_trailing_json_nagging(self):
        raw = (
            "【策略準則】\n"
            "1. 融資餘額暴增且股價跌破季線時，嚴禁做多。\n"
            "請輸出合法的 JSON 格式如下：\n"
            '{"ev_score": 80, "recommendation": "觀望"}'
        )
        ctx = make_ctx(raw)
        assert ActionPruneFormatNoise().tick(ctx) == BTStatus.SUCCESS
        assert "JSON" not in ctx.cleaned_text
        assert "融資餘額暴增" in ctx.cleaned_text
        assert any("JSON" in log for log in ctx.repair_logs)

    def test_prunes_json_code_block(self):
        raw = (
            "1. 營收 YoY 衰退超過 10% 視為基本面破口。\n"
            "```json\n"
            '{"recommendation": "避開"}\n'
            "```"
        )
        ctx = make_ctx(raw)
        assert ActionPruneFormatNoise().tick(ctx) == BTStatus.SUCCESS
        assert "```json" not in ctx.cleaned_text
        assert "營收 YoY 衰退" in ctx.cleaned_text

    def test_no_over_removal_on_normal_words(self):
        """正常商業字眼（非格式化數據／營收分析）不應被誤切除"""
        raw = "1. 注意此為非格式化數據，營收分析需搭配籌碼面交叉驗證。"
        ctx = make_ctx(raw)
        assert ActionPruneFormatNoise().tick(ctx) == BTStatus.SUCCESS
        assert ctx.cleaned_text == raw
        assert ctx.repair_logs == []


class TestActionDeduplicateRules:
    """3. 條列規則模糊去重（標點／序號差異不影響判定）"""

    def test_dedupes_paraphrased_lines(self):
        raw = (
            "1. 跌破 20MA 強制停損，嚴禁留單。\n"
            "- 跌破20MA強制停損嚴禁留單！\n"
            "2. 法人連續賣超超過 3 天不追高。"
        )
        ctx = make_ctx(raw)
        assert ActionDeduplicateRules().tick(ctx) == BTStatus.SUCCESS
        lines = ctx.cleaned_text.splitlines()
        assert len(lines) == 2
        assert "停損" in lines[0]
        assert "法人連續賣超" in lines[1]
        assert any("去重" in log for log in ctx.repair_logs)

    def test_keeps_distinct_rules(self):
        raw = "1. 停損規則。\n2. 法人規則。\n3. 營收規則。"
        ctx = make_ctx(raw)
        assert ActionDeduplicateRules().tick(ctx) == BTStatus.SUCCESS
        assert len(ctx.cleaned_text.splitlines()) == 3


# ==============================================================================
# 二、門禁驗證節點測試（葉子條件節點）
# ==============================================================================

class TestConditionValidLengthBoundary:
    """4. 字數邊界值（150 ~ 2500，含邊界）"""

    def test_149_chars_failure(self):
        ctx = make_ctx("策" * 149)
        assert ConditionValidLength().tick(ctx) == BTStatus.FAILURE
        assert any("過短" in p for p in ctx.problems)

    def test_150_chars_success(self):
        ctx = make_ctx("策" * 150)
        assert ConditionValidLength().tick(ctx) == BTStatus.SUCCESS

    def test_2500_chars_success(self):
        ctx = make_ctx("策" * 2500)
        assert ConditionValidLength().tick(ctx) == BTStatus.SUCCESS

    def test_2501_chars_failure(self):
        ctx = make_ctx("策" * 2501)
        assert ConditionValidLength().tick(ctx) == BTStatus.FAILURE
        assert any("過長" in p for p in ctx.problems)


class TestConditionNoFormatPollution:
    """5. 格式污染正則攔截"""

    def test_detects_output_json_phrase(self):
        ctx = make_ctx("請回傳 JSON 物件以便系統解析。" + "正常內容" * 40)
        assert ConditionNoFormatPollution().tick(ctx) == BTStatus.FAILURE
        assert any("格式" in p for p in ctx.problems)

    def test_detects_any_code_fence(self):
        ctx = make_ctx("1. 停損規則。\n```\ncode block\n```")
        assert ConditionNoFormatPollution().tick(ctx) == BTStatus.FAILURE

    def test_clean_strategy_passes(self):
        ctx = make_ctx("1. 股價乖離 20MA 超過 8% 禁止追高。\n2. 外資連 3 日賣超改為觀望。")
        assert ConditionNoFormatPollution().tick(ctx) == BTStatus.SUCCESS


# ==============================================================================
# 三、組合節點控制流測試（Control Flow）
# ==============================================================================

class TestCompositeControlFlow:
    """6/7. Sequence 短路與 Selector 回退"""

    def test_sequence_short_circuit(self):
        """前置條件失敗時，後續節點完全不應被執行"""
        calls = []

        class AlwaysFail(BTNode):
            def tick(self, ctx):
                calls.append("fail")
                return BTStatus.FAILURE

        class Exploding(BTNode):
            def tick(self, ctx):
                calls.append("explode")
                raise AssertionError("第二個節點不應被呼叫")

        seq = Sequence([AlwaysFail(), Exploding()])
        ctx = make_ctx("x")
        assert seq.tick(ctx) == BTStatus.FAILURE
        assert calls == ["fail"]

    def test_selector_short_circuit_pristine(self):
        """第一條路徑成功時，修復流水線不應觸發（repair_logs 保持空）"""
        pristine = ("1. 金融股忽略營收YoY，改看股價位置與籌碼。\n"
                    "2. 假突破防守：突破當日未收復前高一律減倉。\n"
                    "3. 融資三日增幅逾 15% 且外資轉賣，禁止進場。\n") * 3
        assert len(pristine) >= 150
        tree = build_rules_cleaner_tree()
        ctx = make_ctx(pristine)
        assert tree.tick(ctx) == BTStatus.SUCCESS
        assert ctx.repair_logs == []
        assert ctx.problems == []


# ==============================================================================
# 四、端到端行為樹路徑測試（E2E Pipeline）
# ==============================================================================

class TestPipelineEndToEnd:
    """8-11. 三條分支路徑 ＋ 全線潰敗回饋"""

    def test_path_1_pristine_text(self):
        """路徑 1：完美文字直接放行，不做任何多餘處理"""
        good = ("1. 震盪市提高門檻：ev_score 低於 75 一律觀望。\n"
                "2. 追高防護：5日漲幅逾 12% 且 RSI > 75 排除買入建議。\n"
                "3. 缺失欄位不得視為負面證據，需以中性偏多處理。\n") * 2
        cleaned, problems = clean_and_validate_with_bt(good)
        assert cleaned is not None
        assert problems == []
        assert cleaned == good  # 原樣保留

    def test_path_2_dirty_text_full_repair(self):
        """路徑 2：圍欄＋重複條列＋文末 JSON 碎碎念 → 本地 0 Token 完整修復

        採「骨架與雜訊解耦」作法：以 VALID_BASE_RULES（清洗後淨長穩定 >150）
        為基底，再疊加被測試的三类雜訊，避免 Net Length Shrinkage 造成假失敗。
        """
        duplicate_line = "1. 股價突破 20MA 需伴隨成交量放大至 5 日均量 1.5 倍以上方可確認有效。\n"
        noise_json = ("\n請輸出合法的 JSON 格式如下：\n"
                      '```json\n{"recommendation": "觀望"}\n```')
        dirty = f"```markdown\n{VALID_BASE_RULES}\n{duplicate_line}{noise_json}\n```"

        cleaned, problems = clean_and_validate_with_bt(dirty)

        assert cleaned is not None, f"修復失敗，錯誤回報: {problems}"
        assert problems == []
        assert len(cleaned) >= 150
        assert "```" not in cleaned
        assert "JSON" not in cleaned
        # 去重後同一停損論述僅出現一次
        assert cleaned.count("伴隨成交量放大") == 1

    def test_path_3_salvage_fallback(self):
        """路徑 3：清洗後 <150 但 >=120 → 寬容收尾採用，不重送 LLM"""
        core = "1. 跌破季線優先減倉。\n2. 融資暴增禁止追高。\n" + "外資連賣三天觀望。" * 11
        assert 120 <= len(core) < 150
        dirty = core + "\n請輸出 JSON 結果：" + "{}" * 3
        cleaned, problems = clean_and_validate_with_bt(dirty)
        assert cleaned is not None
        assert problems == []
        assert "JSON" not in cleaned

    def test_total_failure_reports_problems(self):
        """全線潰敗：回傳 (None, problems) 供外層精準回饋 LLM"""
        garbage = "好的，我已經幫你分析完成。"
        cleaned, problems = clean_and_validate_with_bt(garbage)
        assert cleaned is None
        assert any("過短" in p for p in problems)


# ==============================================================================
# 附加：重試解耦防乘數放大（RuntimeError 立即棄投）
# ==============================================================================

class TestRetryDecoupling:
    def test_runtime_error_aborts_without_extra_attempts(self, monkeypatch):
        """內層拋 RuntimeError（API 全線故障）時，外層一次都不重撞"""
        attempts = []

        def boom(system, user):
            attempts.append(1)
            raise RuntimeError("Groq 全模型鏈失敗")

        monkeypatch.setattr("src.optimize_prompt.groq_complete", boom)
        result = generate_with_smart_retry("sys", "user", max_retries=3)
        assert result is None
        assert len(attempts) == 1  # 只呼叫 1 次，非 3 次

    def test_local_bt_repair_avoids_second_api_call(self, monkeypatch):
        """髒資料經行為樹本地清洗即合格 → 只花 1 次 API 呼叫（0 Token 修復生效）

        filler 改用 VALID_BASE_RULES：四條互不相同準則，去重與去噪後
        淨長仍 >150，不會因 Net Length Shrinkage 觸發多餘的 LLM 重試。
        """
        calls = []

        def dirty_once(system, user):
            calls.append(user)
            return f"```text\n{VALID_BASE_RULES}\n請輸出 JSON：{{}}\n```"

        monkeypatch.setattr("src.optimize_prompt.groq_complete", dirty_once)
        rules = generate_with_smart_retry("sys", "user", max_retries=3)
        assert rules is not None
        assert len(calls) == 1
        assert "JSON" not in rules
        assert "```" not in rules
