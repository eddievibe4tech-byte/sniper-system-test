"""交易計畫產生器（src.trading_plan）單元測試"""
from src.trading_plan import generate_trading_plan


def test_pullback_when_gap_over_3pct():
    """乖離 >3% → 等回踩 MA20，不給市價進場"""
    plan = generate_trading_plan({
        'current_price': 3555, 'ma20': 3366,
        'volatility': 40, 'recommendation': '積極買入'
    })
    assert plan['entry_type'] == 'pullback'
    assert plan['status'] == '⏳ 等回踩 MA20'
    assert plan['action_now'] == '等回踩，不追價'
    low, high = plan['entry_zone']
    assert low == 3366  # 買點下緣 = MA20
    assert high > low
    assert plan['stop_loss'] < low  # 停損低於買點區間
    assert plan['take_profit_1'] < plan['take_profit_2']


def test_entry_now_near_ma20():
    """價格貼近 MA20 上方（-1% <= 乖離 <= 3%）→ 現在可進場"""
    plan = generate_trading_plan({
        'current_price': 100, 'ma20': 99,
        'volatility': 30, 'recommendation': '觀望'
    })
    assert plan['entry_type'] == 'now'
    assert plan['status'] == '🟢 現在可進場'
    low, high = plan['entry_zone']
    assert low < 100 < high  # 現價在進場區間內


def test_reclaim_below_ma20():
    """價格在 MA20 下方超過 1% → 等站回 MA20"""
    plan = generate_trading_plan({
        'current_price': 90, 'ma20': 100,
        'volatility': 30, 'recommendation': '觀望'
    })
    assert plan['entry_type'] == 'reclaim'
    assert plan['status'] == '⏳ 等站回 MA20'
    assert plan['entry_zone'][0] == 100  # 買點 = MA20


def test_avoid_has_no_plan():
    """建議為「避開」→ 無交易計畫，所有價格為 None"""
    plan = generate_trading_plan({
        'current_price': 100, 'ma20': 95,
        'volatility': 30, 'recommendation': '避開'
    })
    assert plan['entry_type'] == 'none'
    assert plan['entry_zone'] is None
    assert plan['stop_loss'] is None
    assert plan['risk_reward'] is None
    assert plan['status'] == '🔴 無交易計畫'


def test_missing_data_returns_none_plan():
    """缺資料（price/ma20 為 0 或 None）→ 誠實回傳無計畫"""
    assert generate_trading_plan({})['entry_type'] == 'none'
    assert generate_trading_plan({'current_price': None, 'ma20': None})['entry_type'] == 'none'
    assert generate_trading_plan({'current_price': 0, 'ma20': 0, 'recommendation': '觀望'})['entry_type'] == 'none'


def test_stop_pct_clamped_by_volatility():
    """停損幅度依波動率調整並夾在 6%~12%"""
    low_vol = generate_trading_plan({'current_price': 100, 'ma20': 100, 'volatility': 10, 'recommendation': '觀望'})
    high_vol = generate_trading_plan({'current_price': 100, 'ma20': 100, 'volatility': 100, 'recommendation': '觀望'})
    assert low_vol['stop_pct'] == 6.0   # 10*0.25=2.5 → 下限 6
    assert high_vol['stop_pct'] == 12.0  # 100*0.25=25 → 上限 12


def test_stop_loss_capped_by_ma20_for_pullback():
    """pullback/now 的停損不高於 MA20 * 0.97"""
    plan = generate_trading_plan({
        'current_price': 3555, 'ma20': 3366,
        'volatility': 24,  # stop_pct=6%，6% 停損可能高於 MA20*0.97
        'recommendation': '積極買入'
    })
    assert plan['entry_type'] == 'pullback'
    assert plan['stop_loss'] <= round(3366 * 0.97, 2)


def test_risk_reward_positive():
    """有計畫時 R/R 應為正數"""
    plan = generate_trading_plan({
        'current_price': 100, 'ma20': 99,
        'volatility': 30, 'recommendation': '觀望'
    })
    assert plan['risk_reward'] is not None
    assert plan['risk_reward'] > 0
