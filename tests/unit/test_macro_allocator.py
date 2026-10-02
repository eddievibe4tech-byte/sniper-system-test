"""Macro Allocator 單元測試（Issue #136）

驗證鐵律：權重計算必須是確定性數學規則——本測試完全不依賴網路與 AI。
覆蓋：
1. detect_regime 四種体制 + 資料缺失回退 Neutral；
2. Cash is King：VIX>35 強制最高優先，TAA 中 SHY ≥80%；
3. confirm_regime Whipsaw 過濾：新体制未連續 3 天不切換、連續 3 天才切換；
4. calculate_tactical_weights：各体制 tilt 方向正確、動量 overlay、總和正規化 100%；
5. generate_rebalancing_actions：5% 容忍區間、新增資金優先買入（稅務優化）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import macro_allocator as m  # noqa: E402


# ---------- 1. Regime Detection ----------

def test_detect_regime_risk_on():
    assert m.detect_regime(vix=15, spy_trend=5.0, tlt_momentum=-0.03, gld_momentum=0.0) \
        == m.REGIME_RISK_ON


def test_detect_regime_risk_off():
    assert m.detect_regime(vix=25, spy_trend=-2.0, tlt_momentum=0.04, gld_momentum=0.0) \
        == m.REGIME_RISK_OFF


def test_detect_regime_stagflation():
    # 股債雙殺、黃金獨強
    assert m.detect_regime(vix=24, spy_trend=-5.0, tlt_momentum=-0.02, gld_momentum=0.06) \
        == m.REGIME_STAGFLATION


def test_detect_regime_neutral_by_default():
    assert m.detect_regime(vix=20, spy_trend=1.0, tlt_momentum=0.01, gld_momentum=0.0) \
        == m.REGIME_NEUTRAL


def test_detect_regime_missing_data_is_honest():
    # VIX 缺失 → 誠實回 Neutral，不猜測
    assert m.detect_regime(vix=None, spy_trend=5.0, tlt_momentum=-0.03, gld_momentum=0) \
        == m.REGIME_NEUTRAL


def test_detect_regime_cash_is_king_overrides_all():
    # VIX>35 時即使其他條件像 Risk-Off，也必須升級為 Cash is King（相關性崩壞防護）
    assert m.detect_regime(vix=40, spy_trend=-10.0, tlt_momentum=0.1, gld_momentum=0.1) \
        == m.REGIME_CASH_IS_KING


# ---------- 2. Whipsaw 確認機制 ----------

def test_confirm_regime_blocks_single_day_spike():
    # 已站穩 Neutral 3 天，今日 VIX 因非農數據單日飆出 Risk-Off → 不得切換
    history = [m.REGIME_NEUTRAL] * 3
    assert m.confirm_regime(history, current_regime=m.REGIME_RISK_OFF) == m.REGIME_NEUTRAL


def test_confirm_regime_switches_after_three_consecutive_days():
    history = [m.REGIME_NEUTRAL, m.REGIME_RISK_OFF, m.REGIME_RISK_OFF]
    assert m.confirm_regime(history, current_regime=m.REGIME_RISK_OFF) == m.REGIME_RISK_OFF


def test_confirm_regime_persistent_new_regime_without_history():
    # 冷啟動：歷史全是同一新体制（>=3 筆）即可生效
    history = [m.REGIME_RISK_ON] * 3
    assert m.confirm_regime(history, current_regime=m.REGIME_RISK_ON) == m.REGIME_RISK_ON


def test_confirm_regime_empty_history_defaults_neutral():
    assert m.confirm_regime([], current_regime=m.REGIME_RISK_OFF) == m.REGIME_NEUTRAL


# ---------- 3. TAA 權重計算 ----------

def _sums_to_one(w):
    assert abs(sum(w.values()) - 1.0) < 0.005


def test_weights_neutral_equals_saa():
    w = m.calculate_tactical_weights(m.REGIME_NEUTRAL)
    for sym, meta in m.ASSET_UNIVERSE.items():
        assert abs(w[sym] - meta["base_weight"]) < 1e-6
    _sums_to_one(w)


def test_weights_risk_off_tilts_growth_up_bonds_down():
    w = m.calculate_tactical_weights(m.REGIME_RISK_OFF)
    assert w["QQQ"] < m.ASSET_UNIVERSE["QQQ"]["base_weight"]   # 減碼科技
    assert w["TLT"] > m.ASSET_UNIVERSE["TLT"]["base_weight"]   # 加碼長債
    assert w["SHY"] > m.ASSET_UNIVERSE["SHY"]["base_weight"]   # 加碼現金
    _sums_to_one(w)


def test_weights_risk_on_tilts_growth_up():
    w = m.calculate_tactical_weights(m.REGIME_RISK_ON)
    assert w["QQQ"] > m.ASSET_UNIVERSE["QQQ"]["base_weight"]
    assert w["TLT"] < m.ASSET_UNIVERSE["TLT"]["base_weight"]
    _sums_to_one(w)


def test_weights_stagflation_gold_king():
    w = m.calculate_tactical_weights(m.REGIME_STAGFLATION)
    assert w["GLD"] > m.ASSET_UNIVERSE["GLD"]["base_weight"] * 1.5
    assert w["TLT"] < m.ASSET_UNIVERSE["TLT"]["base_weight"]   # 通膨傷長債
    _sums_to_one(w)


def test_weights_cash_is_king_shy_at_least_80():
    """CFA 盲點修補 #3：極端危機時 SHY 權重必須 ≥80%（TLT 無法避險，只有現金有效）"""
    w = m.calculate_tactical_weights(m.REGIME_CASH_IS_KING,
                                     {"QQQ": 0.5, "TLT": 0.5, "GLD": 0.5})
    assert w["SHY"] >= 0.80
    _sums_to_one(w)


def test_momentum_overlay_boosts_and_penalizes():
    base = m.calculate_tactical_weights(m.REGIME_NEUTRAL)
    tilted = m.calculate_tactical_weights(m.REGIME_NEUTRAL,
                                          {"QQQ": 0.95, "TLT": 0.05})
    assert tilted["QQQ"] > base["QQQ"]   # 強勢動能 +10%
    assert tilted["TLT"] < base["TLT"]   # 弱勢動能 -10%
    _sums_to_one(tilted)


# ---------- 4. 再平衡行動（5% 容忍區間 + 稅務優化）----------

def test_no_action_within_tolerance_band():
    target = m.calculate_tactical_weights(m.REGIME_NEUTRAL)
    actions = m.generate_rebalancing_actions(target, dict(target), 100000)
    assert actions == []  # 完全吻合目標權重 → 零交易（免摩擦成本）


def test_trigger_only_beyond_five_percent():
    target = m.calculate_tactical_weights(m.REGIME_NEUTRAL)
    holdings = dict(target)
    holdings["QQQ"] = target["QQQ"] - 0.04   # 偏差 4% → 不動
    holdings["TLT"] = target["TLT"] + 0.07   # 偏差 7% → 觸發賣出
    actions = m.generate_rebalancing_actions(target, holdings, 100000)
    syms = {(a["symbol"], a["action"]) for a in actions}
    assert ("QQQ", "買入") not in syms
    assert ("TLT", "賣出") in syms


def test_new_cash_prefers_buying_underweights_tax_optimization():
    """CFA 盲點修補 #1：優先用新增資金買欠配資產，而非賣出獲利部位。

    情境（組合 $100,000）：QQQ 超配 20%、SHY 超配 10%；IWM/GLD/VNQ 各欠配 10%
    （TLT diff=0 於容忍帶內）。給 30,000 新增資金 → 恰好足額支應全部欠配（$30k），
    無需任何賣出。此時「不建議賣出 QQQ」——避免無謂的短期資本利得稅事件。
    """
    target = {k: v["base_weight"] for k, v in m.ASSET_UNIVERSE.items()}
    holdings = {"QQQ": 0.50, "SHY": 0.30, "TLT": 0.20}
    # 精算：QQQ -20k / SHY -10k(超配)；IWM +10k / GLD +10k / VNQ +10k(欠配共 30k)；TLT diff=0 於容忍帶內
    actions = m.generate_rebalancing_actions(target, holdings, 100000, new_cash=30000)
    buys = [a for a in actions if a["action"] == "買入"]
    sells = [a for a in actions if a["action"] == "賣出"]
    assert {b["symbol"] for b in buys} == {"IWM", "GLD", "VNQ"}  # 三個欠配標的
    assert all(a["funded_by_new_cash"] == a["amount"] for a in buys)  # 新現金 30k 完全支應欠配 30k
    assert all("零應稅事件" in a["tax_note"] for a in buys)
    # 新增資金已足額補齊欠配 → 不應提議賣出 QQQ（延後應稅事件）
    assert not any(a["symbol"] == "QQQ" and a["action"] == "賣出" for a in sells)


def test_insufficient_new_cash_still_lists_sell_with_tax_warning():
    """新增資金不足時，仍列出賣出建議但附稅務提醒（透明度優先）。"""
    target = {k: v["base_weight"] for k, v in m.ASSET_UNIVERSE.items()}
    holdings = {"QQQ": 0.50, "SHY": 0.50}  # 欠配 > 新現金 20k → 必須變現超配部位
    actions = m.generate_rebalancing_actions(target, holdings, 100000, new_cash=20000)
    funded_buys = [a for a in actions if a["action"] == "買入" and a["funded_by_new_cash"] > 0]
    assert funded_buys, "有新增資金時必須優先支應最大欠配缺口"
    assert any(a["action"] == "賣出" and "稅" in a["tax_note"] for a in actions)


def test_zero_portfolio_value_returns_empty():
    target = {k: v["base_weight"] for k, v in m.ASSET_UNIVERSE.items()}
    assert m.generate_rebalancing_actions(target, {}, 0) == []


# ---------- 5. 輸出結構 ----------

def test_build_allocation_report_shape():
    data = {"vix": 25.0, "spy_trend": -2.0, "tlt_momentum": 0.04,
            "gld_momentum": 0.0, "prices": {"QQQ": 500.0}}
    target = m.calculate_tactical_weights(m.REGIME_RISK_OFF)
    report = m.build_allocation_report(data, m.REGIME_RISK_OFF, target, [])
    assert report["regime_light"] == "🔴"
    assert abs(sum(report["taa_weights"].values()) - 1.0) < 0.005
    assert report["ai_rationale"] is None  # AI 欄位預留，數字不由 AI 產生
    assert "確定性" in report["disclaimer"]
