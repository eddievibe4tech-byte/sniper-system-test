"""🏦 國泰券商智慧單三階分流指南

國泰二擇一智慧單（停損+停利）**不支援零股**，若直接把「預算/現價」的股數
塞給使用者去掛智慧單，邊角料股數會失去停損防護。本模組依持股規模做三階分流：

- 純整張（>=1000 股且無零股尾數） ➜ 二擇一智慧單（停損強制市價/跌停價）
- 混合部位（>=1000 股但有零股尾數） ➜ 1張以上用智慧單 + 尾數另設小鈴鐺
- 純零股（<1000 股）               ➜ 小鈴鐺到價警示 + 手動市價紀律警告

輸出為純 dict（數值與硬編碼字串），前端可直接渲染，無需 escape。
"""
from typing import Dict, Optional

LOT_SIZE = 1000          # 台股一張 = 1000 股
DEFAULT_BUDGET = 50000   # 預設單筆預算（NTD）


def generate_cathay_guide(symbol: str, current_price: float,
                          trading_plan: Optional[Dict] = None,
                          budget: float = DEFAULT_BUDGET) -> Optional[Dict]:
    """依預算與現價計算股數，產出對應的券商 App 設定指南。

    Args:
        symbol: 股票代號（如 '2330'）
        current_price: 現價（NTD）
        trading_plan: src/trading_plan.generate_trading_plan 的輸出（可為 None）
        budget: 本次配置預算（震盪盤情境由推播樹先打 5 折再傳入）

    Returns:
        指南 dict；價格/預算非法或 trading_plan 無進場點時回傳 None（不產出派工）。
    """
    if not current_price or current_price <= 0 or not budget or budget <= 0:
        return None

    # trading_plan 明確給出不進場（entry_type='none'）→ 不產生掛單指南
    if trading_plan and trading_plan.get("entry_type") == "none":
        return None

    total_shares = int(budget // current_price)
    if total_shares <= 0:
        return None  # 預算買不起 1 股，不派工

    full_lots_shares = (total_shares // LOT_SIZE) * LOT_SIZE  # 整張部分（含多張）
    odd_shares = total_shares - full_lots_shares             # 零股邊角料

    tp = trading_plan or {}
    stop_loss = tp.get("stop_loss")
    take_profit = tp.get("take_profit_1")
    entry_zone = tp.get("entry_zone")

    sl_cond = (f"跌破 {stop_loss:.1f} 觸發，強制市價出場"
               if stop_loss else "自訂停損價（建議 -6%~-8%）")
    tp_cond = (f"達 {take_profit:.1f} 獲利了結"
               if take_profit else "自訂停利價（建議 +10%~15%）")
    buy_cond = (f"{entry_zone[0]:.1f}~{entry_zone[1]:.1f} 區間分批進場"
                if entry_zone else f"{current_price:.1f} 附近限價買進")

    shares_display = f"{total_shares} 股"

    if odd_shares == 0:
        # ── 純整張：二擇一智慧單全coverage ────────────────────────
        method = "二擇一智慧單"
        steps = [
            f"國泰 App → 下單 → 智動單/二擇一智慧單",
            f"買進 {full_lots_shares // LOT_SIZE} 張 {symbol}（限價 {buy_cond}）",
            f"條件A 停損：{sl_cond}",
            f"條件B 停利：{tp_cond}",
        ]
        warning = ""
    elif full_lots_shares == 0:
        # ── 純零股：智慧單不可用，只能小鈴鐺 ──────────────────────
        method = "小鈴鐺到價警示"
        steps = [
            "國泰 App → 個股頁 → 小鈴鐺（到價通知）",
            f"設定的到價提醒：{buy_cond}",
            f"停損警示：{sl_cond}",
            f"停利警示：{tp_cond}",
            "⚠️ 零股僅能 9:00~15:30 限價交易，接到通知後手動下單",
        ]
        warning = "🚨 純零股無法掛智慧單！停損停利全靠小鈴鐺+手動紀律，務必即時處理。"
    else:
        # ── 混合部位：整張走智慧單、尾數另設小鈴鐺 ────────────────
        method = "混合部位（智慧單＋小鈴鐺）"
        steps = [
            f"① 整張 {full_lots_shares} 股（{full_lots_shares // LOT_SIZE} 張）："
            "二擇一智慧單（停損/停利如上）",
            f"② 剩餘 {odd_shares} 股零股：另設小鈴鐺到價警示",
            f"停損：{sl_cond}｜停利：{tp_cond}",
        ]
        warning = (f"⚠️ 智慧單不支援零股：剩餘 {odd_shares} 股請另設小鈴鐺，"
                   "否則邊角料將失去停損防護。")

    return {
        "symbol": symbol,
        "method": method,
        "shares": total_shares,
        "shares_display": shares_display,
        "full_lots": full_lots_shares // LOT_SIZE,
        "full_lots_shares": full_lots_shares,
        "odd_shares": odd_shares,
        "budget_used": round(total_shares * current_price, 0),
        "stop_loss": {"condition": sl_cond, "price": stop_loss},
        "take_profit": {"condition": tp_cond, "price": take_profit},
        "entry": {"condition": buy_cond},
        "steps": steps,
        "warning": warning,
    }
