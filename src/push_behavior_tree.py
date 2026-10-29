"""🌳 推播決策樹 (Push Behavior Tree)：根據總經環境動態調整推播優先級與策略

將「推播通知」從單純的訊息搬運工，升級為具備環境感知與風險自適應的智能副駕。
複用 quant_engine FSM 已算好的大盤狀態 (bb.market_state) 與帳戶狀態
(bb.portfolio_state)，疊加當日市場強度指標，組裝成 Selector 式四分支決策樹：

- 分支 1（極端防禦）：熊市 / 熔斷 / 暴跌 ➜ Priority 5 警報，凍結所有買入派工，
  但強制升級推播緊急停損/停利出場指南（崩盤時持股絕不裸奔）
- 分支 2（高波動應變）：震盪市劇烈波動 或 單日急挫 ➜ Priority 4，僅買入預算打 5 折
- 分支 3（常態派工）：環境正常且有實際產出指南 ➜ Priority 3，附國泰智慧單/小鈴鐺指南
- 分支 4（風平浪靜）：無訊號或過濾後無可執行指南 ➜ Priority 2 輕量推播「安心上班」

Selector 特性：最致命的系統性風險放最左側，一旦觸發直接短路截斷，
底層個股邏輯連算都不用算，杜絕「大盤崩盤時還盲目推播買入訊號」的災難。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List

try:  # 作為 package 執行（python -m src.decision_runner）
    from src.quant_engine.blackboard import MarketState, PortfolioState
    from src.cathay_guide import generate_cathay_guide
except (ImportError, ModuleNotFoundError):  # 在 src/ 目錄內直接執行
    from quant_engine.blackboard import MarketState, PortfolioState
    from cathay_guide import generate_cathay_guide

# ── 閾值常數（集中定義，方便後續回測調參）────────────────────────
CRASH_PCT = -2.5        # 單日重挫達此幅度 → 海嘯凍結（分支 1）
VOLATILE_PCT = 1.5      # 波動達此幅度 → 注碼減半防洗（分支 2）
HALF_BUDGET_RATIO = 0.5  # 高波動情境買入預算折扣
DEFAULT_BUDGET = 50000   # decisions 未帶 budget 時的預設單筆預算

ACTION_BUY = "BUY"
EXIT_ACTIONS = ("STOP_LOSS", "TAKE_PROFIT")
ACTIONABLE_ACTIONS = (ACTION_BUY,) + EXIT_ACTIONS


@dataclass
class PushBlackboard:
    """推播專用黑板：輸入=FSM 狀態+決策清單，輸出=推播標題/內文/優先級。"""
    market_state: MarketState
    portfolio_state: PortfolioState = PortfolioState.NORMAL
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    twii_change_pct: float = 0.0       # 大盤強度代理指標（見 decision_runner 註解）

    # 輸出狀態
    push_priority: int = 3
    push_title: str = ""
    push_message: str = ""
    is_blocked: bool = False           # 是否攔截所有買入派工
    scenario: str = ""                 # 除錯用：命中的分支名稱


def _actionable(decisions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [d for d in decisions if d.get("action") in ACTIONABLE_ACTIONS]


def _render_single_guide(d: Dict[str, Any], budget_ratio: float = 1.0) -> List[str]:
    """將單筆決策轉成指南文案，自動依 action 決定是否折抵預算。"""
    action = d.get("action")
    raw_budget = d.get("budget", DEFAULT_BUDGET)
    price = d.get("current_price", 0)

    # 價格異常（<=0 或缺失）直接短路過濾，避免下游 ZeroDivisionError
    if not price or price <= 0:
        return []

    # 🔴 Review 修正：只有買入才適用預算折扣；停損/停利賣出的是既有庫存部位，
    # 若連出場預算也砍半會導致只賣一半的致命錯誤。
    budget = raw_budget * budget_ratio if action == ACTION_BUY else raw_budget

    guide = generate_cathay_guide(
        d.get("symbol", ""),
        price,
        d.get("trading_plan"),
        budget,
    )
    if not guide:
        return []

    lines = [f"\n• {guide.get('symbol')} {guide.get('method')}"
             f"（{guide.get('shares_display')}）"]

    # 🟡 Review 修正：深層鏈式取值改為 .get() 防禦，
    # 避免某些策略無停利條件時拋出 TypeError: 'NoneType' object is not subscriptable
    tp_cond = (guide.get("take_profit") or {}).get("condition")
    if tp_cond:
        lines.append(f"  綠燈停利：{tp_cond}")

    sl_cond = (guide.get("stop_loss") or {}).get("condition")
    if sl_cond:
        lines.append(f"  紅燈停損：{sl_cond}")

    if guide.get("warning"):
        lines.append(f"  ⚠️ {guide['warning']}")

    return lines


def _render_guides(items: List[Dict[str, Any]], budget_ratio: float = 1.0) -> List[str]:
    lines: List[str] = []
    for d in items:
        lines.extend(_render_single_guide(d, budget_ratio=budget_ratio))
    return lines


def tick_push_tree(bb: PushBlackboard) -> PushBlackboard:
    """執行推播決策樹（Selector 邏輯：由左至右、命中即截斷）。"""
    pct = bb.twii_change_pct or 0.0
    # 🟢 Review 修正：開頭重置輸出狀態，避免物件重用（測試/排程重試）時殘留上一輪結果
    bb.is_blocked = False
    bb.scenario = ""
    bb.push_title = ""
    bb.push_message = ""

    # ── 分支 1：極端防禦（熊市 / 熔斷停機 / 暴跌 <= -2.5%）──────────
    if (bb.market_state == MarketState.BEAR
            or bb.portfolio_state == PortfolioState.CIRCUIT_BREAKER
            or pct <= CRASH_PCT):
        bb.scenario = "tsunami_freeze"
        bb.push_priority = 5
        bb.push_title = "🚨 總經海嘯預警・全面停止買入"
        bb.is_blocked = True

        # 🔴 Review 關鍵修正：海嘯凍結不得誤殺停損訊號。
        # 崩盤當下正是最需要緊急拋售的生死關頭，解耦「買入」與「出場」：
        # 凍結新買入，但強制升級推播緊急停損/停利指南，避免持股在崩盤中裸奔。
        exit_items = [d for d in bb.decisions if d.get("action") in EXIT_ACTIONS]
        msg_lines = [
            f"大盤今日指標 {pct:+.2f}% 或處於熊市/熔斷，系統性風險過高。",
            "1. 今日全面取消所有買入掛單。",
            "2. 剩餘未進場資金轉入活存泊車，靜待止穩。",
        ]
        if exit_items:
            msg_lines.append("\n⚠️【緊急出場提醒】以下持股已達停損/停利標準，請立即執行：")
            msg_lines.extend(_render_guides(exit_items))
        bb.push_message = "\n".join(msg_lines)
        return bb

    # ── 分支 2：高波動應變（震盪盤劇烈波動 或 任何市況單日急挫 >= 1.5%）
    # 🔴 Review 修正：補上牛市高波動防禦漏洞——BULL 盤中黑天鵝急殺 -2.4%
    # （未達 CRASH_PCT）時，原條件因硬編碼 SIDEWAYS 而直接落入滿額常態派工。
    is_volatile = ((bb.market_state == MarketState.SIDEWAYS
                    and abs(pct) >= VOLATILE_PCT)
                   or pct <= -VOLATILE_PCT)

    if is_volatile:
        guides = _render_guides(_actionable(bb.decisions),
                                budget_ratio=HALF_BUDGET_RATIO)
        # 🟡 Review 修正：以「實際產出指南」而非「有無訊號」判斷。
        # 無有效指南時不再轟炸 P4 警報（避免 alert fatigue），降級至分支 4。
        if guides:
            bb.scenario = "volatile_half_budget"
            bb.push_priority = 4
            bb.push_title = "⚡ 震盪盤作戰清單・注碼減半防洗"
            bb.push_message = "\n".join([
                f"大盤今日波動達 {pct:+.2f}%，嚴禁市價追單，"
                "新買入預算自動打 5 折（停損/停利維持全額部位）：",
                *guides,
            ])
            return bb

    # ── 分支 3：常態派工（環境正常且有實際產出指南）──────────────────
    guides = _render_guides(_actionable(bb.decisions), budget_ratio=1.0)
    # 🟡 Review 修正：若所有 BUY 決策皆因「買不起 1 股」/entry_type=none 回傳 None，
    # 不應停在 P3 發出空白指南訊息，自動降級為分支 4 風平浪靜。
    if guides:
        bb.scenario = "normal_dispatch"
        bb.push_priority = 3
        bb.push_title = "📈 今日常態派工・紀律執行"
        bb.push_message = "\n".join([
            "市場環境正常，請依下列國泰智慧單/小鈴鐺指南執行：",
            *guides,
        ])
        return bb

    # ── 分支 4：風平浪靜（無訊號或過濾後無須操作）────────────────────
    bb.scenario = "all_clear"
    bb.push_priority = 2
    bb.push_title = "☕ 市場無事發生"
    bb.push_message = "無破線、無達標、無須調整。安心上班、維持紀律！"
    return bb
