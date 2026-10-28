"""🌳 推播決策樹 (Push Behavior Tree)：根據總經環境動態調整推播優先級與策略

將「推播通知」從單純的訊息搬運工，升級為具備環境感知與風險自適應的智能副駕。
複用 quant_engine FSM 已算好的大盤狀態 (bb.market_state) 與帳戶狀態
(bb.portfolio_state)，疊加當日市場強度指標，組裝成 Selector 式四分支決策樹：

- 分支 1（極端防禦）：熊市 / 熔斷 / 暴跌 ➜ Priority 5 警報，凍結所有買入派工
- 分支 2（高波動應變）：震盪市且波動達標 ➜ Priority 4 警告，預算打 5 折、強制限價
- 分支 3（常態派工）：環境正常且有觸發訊號 ➜ Priority 3，附國泰智慧單/小鈴鐺指南
- 分支 4（風平浪靜）：無觸發訊號 ➜ Priority 2 輕量推播「安心上班」

Selector 特性：最致命的系統性風險放最左側，一旦觸發直接短路截斷，
底層個股邏輯連算都不用算，杜絕「大盤崩盤時還盲目推播買入訊號」的災難。
"""
from dataclasses import dataclass, field
from typing import Dict, List

try:  # 作為 package 執行（python -m src.decision_runner）
    from src.quant_engine.blackboard import MarketState, PortfolioState
    from src.cathay_guide import generate_cathay_guide
except ImportError:  # 在 src/ 目錄內直接執行
    from quant_engine.blackboard import MarketState, PortfolioState
    from cathay_guide import generate_cathay_guide

# ── 閾值常數（集中定義，方便後續回測調參）────────────────────────
CRASH_PCT = -2.5        # 单日重挫達此幅度 → 海嘯凍結（分支 1）
VOLATILE_PCT = 1.5      # 震盪市振幅達此幅度 → 注碼減半（分支 2）
HALF_BUDGET_RATIO = 0.5  # 高波動情境預算折扣
DEFAULT_BUDGET = 50000   # decisions 未帶 budget 時的預設單筆預算

ACTIONABLE_ACTIONS = ("BUY", "STOP_LOSS", "TAKE_PROFIT")


@dataclass
class PushBlackboard:
    """推播專用黑板：輸入=FSM 狀態+決策清單，輸出=推播標題/內文/優先級。"""
    market_state: MarketState
    portfolio_state: PortfolioState = PortfolioState.NORMAL
    decisions: List[Dict] = field(default_factory=list)
    twii_change_pct: float = 0.0       # 大盤強度代理指標（見 decision_runner 註解）

    # 輸出狀態
    push_priority: int = 3
    push_title: str = ""
    push_message: str = ""
    is_blocked: bool = False           # 是否攔截所有買入派工
    scenario: str = ""                 # 除錯用：命中的分支名稱


def _actionable(decisions: List[Dict]) -> List[Dict]:
    return [d for d in decisions if d.get("action") in ACTIONABLE_ACTIONS]


def _render_guides(items: List[Dict], budget_ratio: float = 1.0) -> List[str]:
    """把 decisions 逐筆轉成國泰三階分流指南文案。"""
    lines: List[str] = []
    for d in items:
        budget = d.get("budget", DEFAULT_BUDGET) * budget_ratio
        guide = generate_cathay_guide(
            d.get("symbol", ""),
            d.get("current_price", 0),
            d.get("trading_plan"),
            budget,
        )
        if not guide:
            continue
        lines.append(f"\n• {guide['symbol']} {guide['method']}（{guide['shares_display']}）")
        lines.append(f"  綠燈停利：{guide['take_profit']['condition']}")
        lines.append(f"  紅燈停損：{guide['stop_loss']['condition']}")
        if guide["warning"]:
            lines.append(f"  ⚠️ {guide['warning']}")
    return lines


def tick_push_tree(bb: PushBlackboard) -> PushBlackboard:
    """執行推播決策樹（Selector 邏輯：由左至右、命中即截斷）。"""
    pct = bb.twii_change_pct or 0.0

    # ── 分支 1：極端防禦（熊市 / 熔斷停機 / 暴跌 > 2.5%）──────────
    if (bb.market_state == MarketState.BEAR
            or bb.portfolio_state == PortfolioState.CIRCUIT_BREAKER
            or pct <= CRASH_PCT):
        bb.scenario = "tsunami_freeze"
        bb.push_priority = 5
        bb.push_title = "🚨 總經海嘯預警・全面停止下單"
        bb.push_message = (
            f"大盤今日指標 {pct:+.2f}% 或處於熊市/熔斷，系統性風險過高。\n"
            "1. 今日取消所有買入掛單。\n"
            "2. 所有資金轉入活存泊車，靜待止穩。"
        )
        bb.is_blocked = True
        return bb

    # ── 分支 2：高波動應變（震盪盤 且 振幅/跌幅 >= 1.5%）──────────
    if bb.market_state == MarketState.SIDEWAYS and abs(pct) >= VOLATILE_PCT:
        bb.scenario = "volatile_half_budget"
        bb.push_priority = 4
        bb.push_title = "⚡ 震盪盤作戰清單・注碼減半防洗"
        items = _actionable(bb.decisions)
        lines = [f"大盤今日波動達 {pct:+.2f}%，嚴禁市價追單，預算自動減半："]
        lines += _render_guides(items, budget_ratio=HALF_BUDGET_RATIO)
        if not items:
            lines.append("\n目前無觸發訊號，保持空手觀望。")
        bb.push_message = "\n".join(lines)
        return bb

    # ── 分支 3：常態派工（環境正常且有 actionable 訊號）────────────
    items = _actionable(bb.decisions)
    if items:
        bb.scenario = "normal_dispatch"
        bb.push_priority = 3
        bb.push_title = "📈 今日常態派工・紀律執行"
        lines = ["市場環境正常，請依下列國泰智慧單/小鈴鐺指南執行："]
        lines += _render_guides(items)
        bb.push_message = "\n".join(lines)
        return bb

    # ── 分支 4：風平浪靜（默認安全）───────────────────────────────
    bb.scenario = "all_clear"
    bb.push_priority = 2
    bb.push_title = "☕ 市場無事發生"
    bb.push_message = "無破線、無達標、無須調整。安心上班、維持紀律！"
    return bb
