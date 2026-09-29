"""
交易計畫產生器：為每檔股票計算具體買點與賣點
輸出：進場區間、停損價、停利價、風險報酬比、即時進場狀態、出場策略
"""
from typing import Dict, Optional


def generate_trading_plan(stock: Dict) -> Dict:
    """
    根據技術面數據產生交易計畫（含買點＋賣點＋持倉管理）

    Args:
        stock: 包含 current_price, ma20, ma5, volatility, recommendation 的字典

    Returns:
        交易計畫字典（含 entry + exit strategy）

    Note:
        契約限制（PR#121 review P2）：``exit_strategy`` 的所有欄位僅為數字
        （價格/天數/RSI 閾值）或模組內硬編碼字串，絕不含外部輸入文字。
        前端 renderExitStrategy() 據此直接渲染數值；若未來新增任何來自
        資料源的文字欄位，必須改用 escapeHTML 處理後才可輸出。
    """
    price = stock.get('current_price') or 0
    ma20 = stock.get('ma20') or 0
    # 🆕 (PR#127 review) 改用 dict.get(key, default)：避免 `or` 陷阱（值為 0 時被預設值覆蓋）
    vol = stock.get('volatility', 25)  # 20日年化波動率；None → 預設值於下方統一處理
    if vol is None:
        vol = 25
    rec = stock.get('recommendation') or '觀望'
    # 🆕 (PR#121 review 跟進) 讀取 rsi：供技術面出場「目前 RSI」警示使用（docstring 契約欄位）
    rsi = stock.get('rsi', 50)  # PR#127 review：dict.get 預設值，RSI=0 等有效值不被覆蓋
    if rsi is None:
        rsi = 50

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
            'exit_strategy': None,
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

    # 🆕 出場策略（持倉管理：分階段出場＋移動停利＋時間停利＋技術面出場）
    exit_strategy = {
        'initial_stop': round(stop_loss, 2),
        'stage_1': {
            'target': round(tp1, 2),
            'action': '賣 1/3，停損上移至成本價',
            'trigger': f'漲幅達 +10%（{round(tp1, 2)}）',
        },
        'stage_2': {
            'target': round(tp2, 2),
            'action': '再賣 1/3，停損上移至 +10%',
            'trigger': f'漲幅達 +20%（{round(tp2, 2)}）',
        },
        'stage_3': {
            'action': '剩餘 1/3 設 MA20 移動停利',
            'trigger': '跌破 MA20 全數出場',
            'moving_stop': round(ma20, 2),
        },
        'time_stop': {
            'days': 20,
            'action': '持有 20 天未達 +10% 則出場',
            'reason': '避免機會成本',
        },
        'technical_exit': {
            'rsi_overbought': 75,
            'action': 'RSI > 75 或跌破 MA50 出場',
            'reason': '技術面轉弱',
            # 🆕 目前 RSI（數值；>=75 時前端以紅色警示「已達超買，注意減碼」）
            'rsi_current': round(rsi, 1),
            # 🆕 (PR#127 review) 解除硬編碼：直接取 stock 的 ma50（無此 key → None，前端自然顯示「待補數據」；
            #    未來後端資料管線補齊 MA50 即自動無縫接軌，無需回改此行）
            'ma50': stock.get('ma50'),
            # 🆕 (PR#127 review) ma50_available：明確布布林契約欄位，供前端判斷是否納入出場條件
            'ma50_available': stock.get('ma50') is not None,
        },
    }

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
        'exit_strategy': exit_strategy,
    }
