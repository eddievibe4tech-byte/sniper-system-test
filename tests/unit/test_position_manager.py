"""持倉管理與進場觸發模組（src.position_manager）單元測試（#128）

涵蓋：
  1. generate_entry_triggers：now / pullback / reclaim / none 四種 entry_type
  2. generate_position_strategy：unknown_cost / large_profit / small_profit /
     small_loss / large_loss 五種損益情境 + 無效成本防護
  3. calculate_buy_readiness_score：加減分因素、0-100 範圍、等級判定
  4. 缺數／None 輸入不拋例外（建議稿 stock.ma20 屬性寫法會 AttributeError，
     本實作改用 dict.get，此處回歸驗證）
  5. 前端 JS 鏡像一致性：抽出 index.html <script> 區塊以 node 執行，
     比對三個函式輸出與 Python 完全一致（無 node 環境時自動 skip）
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.position_manager import (
    calculate_buy_readiness_score,
    generate_entry_triggers,
    generate_position_strategy,
)


# ---------------------------------------------------------------------------
# 1. generate_entry_triggers
# ---------------------------------------------------------------------------

def test_entry_triggers_now():
    t = generate_entry_triggers(
        {'current_price': 100, 'ma20': 99},
        {'entry_type': 'now', 'entry_zone': [99, 101]},
    )
    assert t['status'] == '🟢 現在可進場'
    assert '99.00~101.00' in t['primary_trigger']
    assert t['confidence'] == '高'
    assert len(t['confirmation_signals']) == 3
    assert 'patience_days' not in t


def test_entry_triggers_pullback():
    t = generate_entry_triggers(
        {'current_price': 45.5, 'ma20': 43.8},
        {'entry_type': 'pullback', 'entry_zone': [43.65, 44.0]},
    )
    assert t['status'] == '⏳ 等回踩 MA20'
    assert '43.65~44.00' in t['primary_trigger']
    assert t['patience_days'] == '3-7 個交易日'
    assert any('長下影線' in s for s in t['confirmation_signals'])


def test_entry_triggers_reclaim_uses_ma20_from_dict():
    t = generate_entry_triggers(
        {'current_price': 40, 'ma20': 45},
        {'entry_type': 'reclaim', 'entry_zone': [45, 45.9]},
    )
    assert t['status'] == '⏳ 等站回 MA20'
    assert '45.00' in t['primary_trigger']
    assert t['patience_days'] == '2-5 個交易日'


def test_entry_triggers_none_and_missing_plan():
    t = generate_entry_triggers({}, {'entry_type': 'none'})
    assert t['status'] == '🔴 不建議進場'
    assert t['avoid_if'] == ['所有情況']

    # trading_plan 為 None／非字典 → 不拋例外，走不建議進場分支
    assert generate_entry_triggers({}, None)['primary_trigger'] == '無'


def test_entry_triggers_missing_data_shows_placeholder():
    """entry_zone/ma20 缺失時顯示「待補數據」而非崩潰或 NaN"""
    t = generate_entry_triggers({'ma20': None}, {'entry_type': 'reclaim', 'entry_zone': None})
    assert '待補數據' in t['primary_trigger']
    t2 = generate_entry_triggers({}, {'entry_type': 'now', 'entry_zone': [None, None]})
    assert '待補數據' in t2['primary_trigger']


# ---------------------------------------------------------------------------
# 2. generate_position_strategy
# ---------------------------------------------------------------------------

STOCK_2890 = {'code': '2890', 'current_price': 45.5, 'ma5': 44.2, 'ma20': 43.8}


def test_position_unknown_cost():
    s = generate_position_strategy(STOCK_2890, {}, None)
    assert s['scenario'] == 'unknown_cost'
    assert s['risk_level'] == '需評估'
    assert 'exit_strategy' not in s


def test_position_invalid_cost_treated_as_unknown():
    """0／負值／NaN／字串 → 一律視為未輸入（避免除零）"""
    for bad in (0, -5, float('nan'), 'abc'):
        assert generate_position_strategy(STOCK_2890, {}, bad)['scenario'] == 'unknown_cost'


def test_position_large_profit():
    s = generate_position_strategy(STOCK_2890, {}, 38.0)  # +19.7%
    assert s['scenario'] == 'large_profit'
    assert s['risk_level'] == '低（已鎖定利潤）'
    assert s['exit_strategy']['target'].endswith('出場')
    assert '47.50' in s['exit_strategy']['target']  # 38 * 1.25


def test_position_small_profit():
    s = generate_position_strategy(STOCK_2890, {}, 42.0)  # +8.3%
    assert s['scenario'] == 'small_profit'
    assert s['risk_level'] == '中'
    assert '44.20' in s['exit_strategy']['trailing']  # MA5


def test_position_small_loss():
    s = generate_position_strategy(STOCK_2890, {}, 47.0)  # -3.2%
    assert s['scenario'] == 'small_loss'
    assert s['risk_level'] == '高'
    assert '43.24' in s['exit_strategy']['stop_loss']  # 47 * 0.92


def test_position_large_loss():
    s = generate_position_strategy(STOCK_2890, {}, 60.0)  # -24.2%
    assert s['scenario'] == 'large_loss'
    assert s['risk_level'] == '危險'
    assert s['exit_strategy'] == {'immediate': '市價出場，不猶豫'}


def test_position_strategy_missing_stock_fields():
    s = generate_position_strategy({}, {}, 10.0)
    # current_price 缺失 → 視為 0 → 大虧損，但不拋例外
    assert s['scenario'] == 'large_loss'


# ---------------------------------------------------------------------------
# 3. calculate_buy_readiness_score
# ---------------------------------------------------------------------------

def test_readiness_now_near_ma20_neutral_rsi():
    r = calculate_buy_readiness_score(
        {'current_price': 100, 'ma20': 99, 'rsi': 50},
        {'entry_type': 'now'},
    )
    assert r['score'] == 85  # 50 + 20(進場區間) + 15(RSI 中性)
    assert r['level'] == '極佳'  # score >= 80 → 極佳
    assert '價格在理想進場區間 (+20)' in r['factors']


def test_readiness_overbought_high_gap():
    r = calculate_buy_readiness_score(
        {'current_price': 120, 'ma20': 100, 'rsi': 80},
        {'entry_type': 'pullback'},
    )
    # elif 鏈互斥：gap=+20% 屬「乖離過大」，不會再疊加 pullback 接近 MA20 加分
    assert r['score'] == 15  # 50 - 20(乖離過大) - 15(RSI 超買)
    assert r['level'] == '不佳'
    assert r['action'] == '❌ 不建議進場'
    assert '乖離過大 (-20)' in r['factors'] and 'RSI 超買 (-15)' in r['factors']


def test_readiness_volume_factors():
    base = {'current_price': 100, 'ma20': 99, 'rsi': 50}
    up = calculate_buy_readiness_score({**base, 'volume': 1500, 'volume_avg_5': 1000}, {'entry_type': 'now'})
    down = calculate_buy_readiness_score({**base, 'volume': 500, 'volume_avg_5': 1000}, {'entry_type': 'now'})
    plain = calculate_buy_readiness_score(base, {'entry_type': 'now'})
    assert up['score'] == plain['score'] + 10
    assert down['score'] == plain['score'] - 5
    assert '成交量放大 (+10)' in up['factors']
    assert '成交量萎縮 (-5)' in down['factors']


def test_readiness_score_clamped_0_100():
    r = calculate_buy_readiness_score(
        {'current_price': 200, 'ma20': 100, 'rsi': 95},
        {'entry_type': 'none'},
    )
    assert 0 <= r['score'] <= 100
    r2 = calculate_buy_readiness_score({}, {})
    assert 0 <= r2['score'] <= 100


def test_readiness_missing_rsi_defaults_50():
    """rsi=None → 預設 50（中性 +15），不被 `or` 陷阱覆蓋成詭異值"""
    r = calculate_buy_readiness_score({'current_price': 100, 'ma20': 99, 'rsi': None}, {'entry_type': 'now'})
    assert 'RSI 中性 (+15)' in r['factors']


# ---------------------------------------------------------------------------
# 5. 前後端鏡像一致性（node 可用才跑）
# ---------------------------------------------------------------------------

NODE_AVAILABLE = shutil.which('node') is not None

PARITY_CASES = [
    ({'code': '2890', 'name': '永豐金', 'current_price': 45.5, 'ma5': 44.2, 'ma20': 43.8, 'rsi': 55},
     {'entry_type': 'pullback', 'entry_zone': [43.65, 44.0], 'stop_loss': 42.0}, 42.0),
    ({'code': '2330', 'current_price': 500, 'ma5': 495, 'ma20': 498, 'rsi': 50},
     {'entry_type': 'now', 'entry_zone': [495, 505]}, None),
    ({'code': '2317', 'current_price': 40, 'ma5': 42, 'ma20': 45, 'rsi': 25},
     {'entry_type': 'reclaim', 'entry_zone': [45, 45.9]}, 50.0),
    ({'code': '0000', 'current_price': 10, 'ma5': 10, 'ma20': 10, 'rsi': 75},
     {'entry_type': 'none', 'entry_zone': None}, 20.0),
    ({'code': '1101', 'current_price': 200, 'ma5': 190, 'ma20': 195, 'rsi': 45},
     {'entry_type': 'now', 'entry_zone': [198, 202]}, 100.0),
    ({'code': '6505', 'current_price': 30, 'ma5': 31, 'ma20': 32, 'rsi': 60},
     {'entry_type': 'pullback', 'entry_zone': [32, 32.6]}, 20.0),
    ({'code': '2882', 'current_price': None, 'ma5': None, 'ma20': None, 'rsi': None},
     {'entry_type': 'now', 'entry_zone': [None, None]}, -5),
]


def _extract_logic_block():
    """取出 index.html 中三個純邏輯函式的原始碼（不含 DOM 相依的 render*）。

    整段 <script> 含視窗／DOM 初始化代碼，在 node 直接執行會拋
    ReferenceError: window is not defined，故只截取
    generateEntryTriggers → calculateBuyReadinessScore 這段（至
    renderEntryTriggers 註解區塊前）作為鏡像測試標的。
    """
    html = Path(__file__).resolve().parents[2].joinpath('index.html').read_text(encoding='utf-8')
    start = html.index('const numOr')  # 三個邏輯函式共用的數字防護小工具
    end = html.index('function renderEntryTriggers', start)
    block = html[start:end]
    # 去掉尾端的 JSDoc／區塊註解，保留函式本體
    marker = block.rfind('/**')
    if marker != -1 and block.count('\n', marker) < 10:
        block = block[:marker]
    return block


@pytest.mark.skipif(not NODE_AVAILABLE, reason="需要 node 執行前端鏡像邏輯")
def test_frontend_js_mirrors_python_output():
    js = _extract_logic_block()
    driver = f"""\
"use strict";
let localStorage = {{ getItem: () => null }};
{js}
const cases = {json.dumps(PARITY_CASES)};
const out = cases.map(([stock, plan, cost]) => ({{
    entry: generateEntryTriggers(stock, plan),
    position: generatePositionStrategy(stock, plan, cost),
    readiness: calculateBuyReadinessScore(stock, plan),
}}));
console.log(JSON.stringify(out));
"""
    res = subprocess.run(['node', '-e', driver], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    js_results = json.loads(res.stdout)

    for (stock, plan, cost), got in zip(PARITY_CASES, js_results):
        want = json.loads(json.dumps({
            'entry': generate_entry_triggers(stock, plan),
            'position': generate_position_strategy(stock, plan, cost),
            'readiness': calculate_buy_readiness_score(stock, plan),
        }, ensure_ascii=False))
        assert want == got, f"前後端不一致（{stock.get('code')}）：\nPY={want}\nJS={got}"
