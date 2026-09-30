"""
持倉管理與進場觸發模組（#128）
輸出：具體買入觸發條件 + 持倉操作策略 + 買入準備度分數

設計契約（沿用 trading_plan.py 的 PR#121 review P2 约定）：
    - stock / trading_plan 一律以「字典」存取（.get(key, default)），
      避免建議稿中 ``stock.ma20`` / ``trading_plan.entry_zone[0]`` 等屬性寫法
      在 dict 資料源下直接 AttributeError。
    - 所有數值欄位先做 None / 非正數防護，輸出一律 round(…, 2)。
    - 本模組文字皆為模組內硬編碼字串或純數字，不含外部輸入文字；
      前端仍會統一經 escapeHTML 處理後輸出。
"""
from typing import Dict, Optional


def _num(value, default=None):
    """取得可計算數字；None／非數字 → default。"""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f


def generate_entry_triggers(stock: Dict, trading_plan: Optional[Dict]) -> Dict:
    """
    產生具體的進場觸發條件（不只是價格，還有技術確認）

    Args:
        stock: 股票數據字典（current_price, ma20, rsi, ...）
        trading_plan: generate_trading_plan() 輸出字典

    Returns:
        包含具體觸發條件、K線信號、確認指標的字典
    """
    if not isinstance(trading_plan, dict):
        trading_plan = {}
    entry_type = trading_plan.get('entry_type')
    zone = trading_plan.get('entry_zone') or [None, None]
    zone_low = _num(zone[0] if len(zone) > 0 else None)
    zone_high = _num(zone[1] if len(zone) > 1 else None)
    zone_text = (
        f'{zone_low:.2f}~{zone_high:.2f}'
        if zone_low is not None and zone_high is not None
        else '待補數據'
    )
    ma20 = _num(stock.get('ma20') if isinstance(stock, dict) else None)
    ma20_text = f'{ma20:.2f}' if ma20 is not None else '待補數據'

    if entry_type == 'now':
        return {
            'status': '🟢 現在可進場',
            'primary_trigger': f'價格在 {zone_text} 區間內',
            'confirmation_signals': [
                'RSI < 65（未進入超買區）',
                '成交量 > 5日均量（動能確認）',
                '外資或投信當日買超（籌碼確認）',
            ],
            'kline_patterns': [
                '出現紅K收盤（多方表態）',
                '或出現十字星（盤整待變，可觀望）',
            ],
            'avoid_if': [
                'RSI > 70（超買）',
                '成交量萎縮（缺乏動能）',
                '三大法人同步賣超',
            ],
            'action': '✅ 確認上述條件後，市價買入',
            'confidence': '高',
        }

    if entry_type == 'pullback':
        return {
            'status': '⏳ 等回踩 MA20',
            'primary_trigger': f'價格回踩到 {zone_text} 區間',
            'confirmation_signals': [
                '出現「長下影線」K線（下檔有支撐）',
                'RSI 回落到 40-55（中性偏弱，非超賣）',
                '成交量縮小後放大（止穩後動能恢復）',
            ],
            'kline_patterns': [
                '錘子線（Hammer）：長下影、小實體、收高',
                '吞噬形態（Bullish Engulfing）：紅K吃掉前一日的黑K',
                '十字星後紅K（盤整後突破）',
            ],
            'avoid_if': [
                '跌破 MA20 且收盤未站回（支撐失效）',
                'RSI < 30（超賣，可能繼續跌）',
                '連續 3 天黑K且放量（賣壓沈重）',
            ],
            'action': '⏰ 掛限價單，觸發後觀察 30 分鐘確認再成交',
            'patience_days': '3-7 個交易日',
            'confidence': '中（需等待）',
        }

    if entry_type == 'reclaim':
        return {
            'status': '⏳ 等站回 MA20',
            'primary_trigger': f'價格突破 {ma20_text} 並站穩',
            'confirmation_signals': [
                '收盤價 > MA20（突破確認）',
                '成交量 > 10日均量（突破動能）',
                '連續 2 天收盤 > MA20（站穩確認）',
            ],
            'kline_patterns': [
                '突破長紅K（實體大、放量）',
                '跳空缺口向上（強勢突破）',
                'W底突破頸線（底部反轉）',
            ],
            'avoid_if': [
                '假突破：盤中突破但收盤跌回',
                '突破但成交量萎縮（量能不足）',
                '突破後立即回檔破 MA20（假突破確認）',
            ],
            'action': '✅ 確認站穩後，市價追入（右側交易）',
            'patience_days': '2-5 個交易日',
            'confidence': '中（需突破確認）',
        }

    # entry_type == 'none' 或其他：不建議進場
    return {
        'status': '🔴 不建議進場',
        'primary_trigger': '無',
        'confirmation_signals': [],
        'kline_patterns': [],
        'avoid_if': ['所有情況'],
        'action': '❌ 觀望，等待明確趨勢',
        'confidence': '低',
    }


def generate_position_strategy(stock: Dict, trading_plan: Optional[Dict],
                               holding_cost: Optional[float] = None) -> Dict:
    """
    產生持倉管理策略（如果已經持有該股票）

    Args:
        stock: 股票數據字典（current_price, ma5, ma20）
        trading_plan: 交易計畫字典（此處僅供未來擴充，目前策略以損益情境判斷）
        holding_cost: 持有成本（如果已知）

    Returns:
        持倉操作建議
    """
    if not isinstance(stock, dict):
        stock = {}
    current_price = _num(stock.get('current_price'), 0) or 0
    ma5 = _num(stock.get('ma5'))
    ma20 = _num(stock.get('ma20'))
    ma5_text = f'{ma5:.2f}' if ma5 is not None else '待補數據'
    ma20_text = f'{ma20:.2f}' if ma20 is not None else '待補數據'
    cost = _num(holding_cost)
    if cost is not None and not (cost > 0):
        cost = None  # 無效成本（0/負值/NaN）→ 視為未輸入

    # 如果不知道成本，提供一般性建議
    if cost is None:
        return {
            'scenario': 'unknown_cost',
            'recommendation': '無法判斷',
            'actions': [
                '請輸入您的持有成本，系統會給出具體建議',
                '若已獲利 > 10%：建議先賣 1/3 鎖定利潤',
                f'若虧損中：設嚴格停損 {current_price * 0.92:.2f} (-8%)',
                f'若接近 MA20 ({ma20_text})：觀察是否止穩',
            ],
            'risk_level': '需評估',
        }

    pnl_pct = (current_price - cost) / cost * 100

    # 情境 1：大幅獲利 (>15%)
    if pnl_pct > 15:
        return {
            'scenario': 'large_profit',
            'recommendation': '分批獲利了結',
            'pnl_pct': round(pnl_pct, 2),
            'actions': [
                f'✅ 已獲利 {pnl_pct:.1f}%，建議先賣 1/2 鎖定利潤',
                f'剩餘 1/2 設移動停利：跌破 MA5 ({ma5_text}) 全數出場',
                f'或設定 +25% 停利：漲到 {cost * 1.25:.2f} 全數出場',
                '不建議加碼追高，避免利潤回吐',
            ],
            'risk_level': '低（已鎖定利潤）',
            'exit_strategy': {
                'immediate': f'跌破 MA5 ({ma5_text}) 市價出場',
                'trailing': '每日更新 MA5，跌破即出場',
                'target': f'漲到 {cost * 1.25:.2f} (+25%) 出場',
            },
        }

    # 情境 2：小幅獲利 (5-15%)
    if pnl_pct > 5:
        return {
            'scenario': 'small_profit',
            'recommendation': '續抱 + 移動停利',
            'pnl_pct': round(pnl_pct, 2),
            'actions': [
                f'獲利 {pnl_pct:.1f}%，可續抱',
                f'設移動停利：跌破 MA5 ({ma5_text}) 或昨日低點出場',
                f'若跌破 MA20 ({ma20_text}) 減碼至 1/2',
                '若回測 MA20 後反彈，可考慮加碼',
            ],
            'risk_level': '中',
            'exit_strategy': {
                'trailing': f'跌破 MA5 ({ma5_text}) 出場',
                'support': f'跌破 MA20 ({ma20_text}) 減碼',
            },
        }

    # 情境 3：小幅虧損 (0 到 -8%)
    if pnl_pct > -8:
        return {
            'scenario': 'small_loss',
            'recommendation': '嚴格停損 + 觀察',
            'pnl_pct': round(pnl_pct, 2),
            'actions': [
                f'虧損 {abs(pnl_pct):.1f}%，仍在停損範圍內',
                f'嚴格停損：跌破 {cost * 0.92:.2f} (-8%) 市價出場',
                f'若跌破 MA20 ({ma20_text}) 立即出場，不攤平',
                '出場後等價格站回 MA20 再重新評估',
            ],
            'risk_level': '高',
            'exit_strategy': {
                'stop_loss': f'{cost * 0.92:.2f} (-8%)',
                'support_break': f'跌破 MA20 ({ma20_text}) 立即出場',
            },
        }

    # 情境 4：大幅虧損 (<-8%)
    return {
        'scenario': 'large_loss',
        'recommendation': '立即出場',
        'pnl_pct': round(pnl_pct, 2),
        'actions': [
            f'⚠️ 虧損 {abs(pnl_pct):.1f}%，已超過停損範圍',
            '建議立即市價出場，保存本金',
            '不要攤平，避免損失擴大',
            f'等價格站回 MA20 ({ma20_text}) 且出現買入信號再重新進場',
        ],
        'risk_level': '危險',
        'exit_strategy': {
            'immediate': '市價出場，不猶豫',
        },
    }


def calculate_buy_readiness_score(stock: Dict, trading_plan: Optional[Dict]) -> Dict:
    """
    計算買入準備度分數 (0-100)

    考慮：價格位置、RSI、成交量、籌碼面
    """
    if not isinstance(stock, dict):
        stock = {}
    if not isinstance(trading_plan, dict):
        trading_plan = {}

    score = 50  # 基礎分
    factors = []

    current_price = _num(stock.get('current_price'), 0) or 0
    ma20 = _num(stock.get('ma20'), 0) or 0
    rsi = _num(stock.get('rsi'), 50)
    if rsi is None:
        rsi = 50
    entry_type = trading_plan.get('entry_type')

    # 價格位置（MA20 缺失時不計乖離，誠實扣分於因素中反映）
    gap_pct = (current_price - ma20) / ma20 * 100 if ma20 > 0 else 0.0

    if entry_type == 'now' and -1 <= gap_pct <= 3:
        score += 20
        factors.append('價格在理想進場區間 (+20)')
    elif entry_type == 'pullback' and -1 <= gap_pct <= 1:
        score += 15
        factors.append('價格接近 MA20 (+15)')
    elif gap_pct > 5:
        score -= 20
        factors.append('乖離過大 (-20)')
    elif gap_pct < -3:
        score -= 10
        factors.append('價格過低，可能續跌 (-10)')

    # RSI
    if 40 <= rsi <= 60:
        score += 15
        factors.append('RSI 中性 (+15)')
    elif rsi < 30:
        score -= 10
        factors.append('RSI 超賣，可能續跌 (-10)')
    elif rsi > 70:
        score -= 15
        factors.append('RSI 超買 (-15)')

    # 成交量（簡化判斷：與 5 日均量比較；資料管線尚未提供 volume_avg_5 時不計分）
    volume = _num(stock.get('volume'))
    avg_volume = _num(stock.get('volume_avg_5'))
    if volume is not None and avg_volume is not None and avg_volume > 0:
        if volume > avg_volume * 1.2:
            score += 10
            factors.append('成交量放大 (+10)')
        elif volume < avg_volume * 0.7:
            score -= 5
            factors.append('成交量萎縮 (-5)')

    # 限制範圍
    score = max(0, min(100, score))

    # 判定等級
    if score >= 80:
        level = '極佳'
        action = '✅ 可立即進場'
    elif score >= 60:
        level = '良好'
        action = '✅ 可進場，但需確認觸發條件'
    elif score >= 40:
        level = '普通'
        action = '⏳ 建議等待更好的進場點'
    else:
        level = '不佳'
        action = '❌ 不建議進場'

    return {
        'score': score,
        'level': level,
        'action': action,
        'factors': factors,
    }
