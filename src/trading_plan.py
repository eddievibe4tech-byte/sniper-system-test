"""
交易計畫產生器：為每檔股票計算具體買點與賣點
輸出：進場區間、停損價、停利價、風險報酬比、即時進場狀態
"""
from typing import Dict, Optional


def generate_trading_plan(stock: Dict) -> Dict:
    """
    根據技術面數據產生交易計畫

    Args:
        stock: 包含 current_price, ma20, ma5, volatility, recommendation 的字典

    Returns:
        交易計畫字典
    """
    price = stock.get('current_price') or 0
    ma20 = stock.get('ma20') or 0
    vol = stock.get('volatility') or 25  # 20日年化波動率
    rec = stock.get('recommendation') or '觀望'

    # 避開/觀望：不給進場點
    if rec in ('避開',) or price <= 0 or ma20 <= 0:
        return {
            'entry_type': 'none',
            'entry_zone': None,
            'stop_loss': None,
            'take_profit_1': None,
            'take_profit_2': None,
            'risk_reward': None,
            'action_now': '不進場',
            'status': '🔴 無交易計畫',
        }

    # 停損幅度：依波動率調整（6%~12%）
    stop_pct = max(6.0, min(12.0, vol * 0.25))

    # 與 MA20 的乖離率
    gap_pct = (price - ma20) / ma20 * 100

    # 進場策略判定
    if gap_pct > 3:
        # 乖離過大 → 等回踩 MA20
        entry_type = 'pullback'
        entry_low, entry_high = ma20, ma20 * 1.02
        action_now = '等回踩，不追價'
        status = '⏳ 等回踩 MA20'
    elif gap_pct >= -1:
        # 價格貼近 MA20 上方 → 現在可進場
        entry_type = 'now'
        entry_low, entry_high = price * 0.99, price * 1.01
        action_now = '現在可進場'
        status = '🟢 現在可進場'
    else:
        # 價格在 MA20 下方 → 等站回
        entry_type = 'reclaim'
        entry_low, entry_high = ma20, ma20 * 1.02
        action_now = '等站回 MA20'
        status = '⏳ 等站回 MA20'

    entry_mid = (entry_low + entry_high) / 2

    # 停損：進場價 - stop_pct%，且不高於 MA20*0.97
    stop_loss = entry_mid * (1 - stop_pct / 100)
    if entry_type in ('pullback', 'now'):
        stop_loss = min(stop_loss, ma20 * 0.97)

    # 停利：兩階段
    tp1 = entry_mid * 1.10
    tp2 = entry_mid * 1.20

    # 風險報酬比
    risk = entry_mid - stop_loss
    reward = tp1 - entry_mid
    rr = round(reward / risk, 2) if risk > 0 else None

    return {
        'entry_type': entry_type,
        'entry_zone': [round(entry_low, 2), round(entry_high, 2)],
        'stop_loss': round(stop_loss, 2),
        'take_profit_1': round(tp1, 2),
        'take_profit_2': round(tp2, 2),
        'risk_reward': rr,
        'action_now': action_now,
        'status': status,
        'stop_pct': round(stop_pct, 1),
    }
