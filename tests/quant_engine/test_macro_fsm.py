"""🧪 Issue #3 驗收測試：大盤狀態機（Macro FSM）與 Proxy Breadth。

對應 AC（Acceptance Criteria，Issue #198）：
  [x] 背離攔截（DIVERGENCE_BULL）：0050 > 60MA 且創 60 日高，但 00733 < 20MA
      ➜ FSM 輸出 DIVERGENCE_BULL，並設定 max_exposure = 0.3
  [x] 糾結狀態判定（CHOPPY_MARKET）：大盤 > 60MA，但 MA20/MA60 距離 < 2%
      ➜ 輸出 CHOPPY_MARKET，禁止追高
  [x] 防抖（Anti-Whipsaw）：第一天跌破、第二天站回 ➜ 狀態維持不變
  [x] 狀態轉移矩陣完備性：4 狀態間無 Deadlock / 無法逃離的狀態
  [x] 狀態真空區填補：寬度 45% ➜ 穩定輸出震盪緩衝態，不拋例外、不殘留預設值
"""
import pytest

from src.quant_engine.blackboard import Blackboard, MarketState
from src.quant_engine.macro import (
    DEFAULT_MAX_EXPOSURE,
    MacroFSM,
    MacroFSMConfig,
    MacroState,
    compute_breadth,
)


# ---------------------------------------------------------------------------
# Helpers：合成可直接控制條件的 breadth 快照 + 黑板
# ---------------------------------------------------------------------------
def make_bb(macro_state: str = "STRONG_TREND", days: int = 5,
            pending_days: int = 0) -> Blackboard:
    """pending_days：候選新條件已連續成立天數（防抖觀察佇列）。"""
    return Blackboard(
        market_index=1000, market_ma60=950, market_ma200=900,
        peak_capital=1_000_000, current_capital=1_000_000,
        macro_state=macro_state, days_in_current_state=days,
        pending_days=pending_days,
    )


def breadth(valid=True, lg_above60=True, lg_high60=False,
            lg_ma20=103.0, lg_ma60=100.0, mid_above20=None,
            breadth_pct=None) -> dict:
    """mid_above20=None ➜ 依預設推導：lg_above60 and not lg_high60（健康デフォルト）。
    breadth_pct=None ➜ 由 large/mid 兩旗標自動計算（與 compute_breadth 語義一致），
    避免手填寬度與結構旗標互相矛盾。"""
    if mid_above20 is None:
        mid_above20 = bool(lg_above60 and not lg_high60)
    return {
        "valid": valid,
        "large_price": 105.0, "large_ma20": lg_ma20, "large_ma60": lg_ma60,
        "large_above_ma60": lg_above60, "large_is_60d_high": lg_high60,
        "mid_price": 60.0, "mid_ma20": 62.0,
        "mid_above_ma20": mid_above20,
        "breadth_pct": (breadth_pct if breadth_pct is not None
                        else (1.0 if mid_above20 else 0.5)),
    }


@pytest.fixture
def fsm():
    return MacroFSM(MacroFSMConfig())


# ---------------------------------------------------------------------------
# compute_breadth：Proxy Breadth 純函數
# ---------------------------------------------------------------------------
class TestComputeBreadth:
    def test_insufficient_data_returns_invalid(self):
        """資料不足（<60 根）➜ valid=False，下游走「維持現狀」路徑。"""
        cfg = MacroFSMConfig()
        b = compute_breadth([100.0] * 30, [50.0] * 30, cfg)
        assert b["valid"] is False

    def test_empty_series_no_crash(self):
        cfg = MacroFSMConfig()
        b = compute_breadth([], [], cfg)
        assert b["valid"] is False

    def test_flags_and_breadth_pct(self):
        """0050 上升序列站上均線、00733 下跌序列跌破 20MA ➜ 一上一下。"""
        cfg = MacroFSMConfig()
        large = [100 + i * 0.5 for i in range(120)]   # 末端創高、遠高於 MA60
        mid = [100 - i * 0.5 for i in range(120)]     # 末端遠低於自身 MA20
        b = compute_breadth(large, mid, cfg)
        assert b["valid"] is True
        assert b["large_above_ma60"] is True
        assert b["large_is_60d_high"] is True          # 單調上升必創 60 日高
        assert b["mid_above_ma20"] is False
        assert b["breadth_pct"] == 0.5                 # 2 檔中僅 1 檔站上 20MA


# ---------------------------------------------------------------------------
# AC1：背離攔截 DIVERGENCE_BULL
# ---------------------------------------------------------------------------
class TestDivergenceBull:
    def test_divergence_sets_max_exposure_030(self, fsm):
        """0050 > 60MA 且創 60 日高，但 00733 < 20MA ➜ DIVERGENCE_BULL / 0.3。"""
        # 防抖語義：條件須連續成立 confirm_days 日。此處以 pending_days=1
        # 模擬「昨日已首次偵見背離」，本次 update 為第 2 日 ➜ 放行切換。
        bb = make_bb(macro_state="STRONG_TREND", days=5, pending_days=1)
        out = fsm.update(bb, breadth(lg_high60=True, mid_above20=False))
        assert out == "DIVERGENCE_BULL"
        assert bb.macro_state is MacroState.DIVERGENCE_BULL
        assert bb.macro_max_exposure == pytest.approx(0.3)
        assert any("拉權值殺中小" in r for r in bb.reasons)

    def test_default_map_value(self):
        assert DEFAULT_MAX_EXPOSURE[MacroState.DIVERGENCE_BULL] == 0.3


# ---------------------------------------------------------------------------
# AC2：糾結狀態 CHOPPY_MARKET
# ---------------------------------------------------------------------------
class TestChoppyMarket:
    def test_choppy_when_ma_gap_below_2pct(self, fsm):
        """大盤 > 60MA，但 |MA20-MA60|/MA60 < 2% ➜ CHOPPY_MARKET（禁止追高）。"""
        bb = make_bb(macro_state="STRONG_TREND", days=5, pending_days=1)
        # gap = |101-100|/100 = 1% < 2%
        out = fsm.update(bb, breadth(lg_ma20=101.0, lg_ma60=100.0, mid_above20=True))
        assert out == "CHOPPY_MARKET"
        assert bb.macro_max_exposure == pytest.approx(0.5)
        assert any("糾結" in r for r in bb.reasons)

    def test_not_choppy_when_gap_above_2pct(self, fsm):
        """gap = 5% ➜ 健康強趨勢 STRONG_TREND（100% 曝險）。"""
        bb = make_bb(macro_state="BEAR_DEFENSE", days=5, pending_days=1)
        out = fsm.update(bb, breadth(lg_ma20=105.0, lg_ma60=100.0, mid_above20=True))
        assert out == "STRONG_TREND"
        assert bb.macro_max_exposure == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# AC3：防抖 Anti-Whipsaw
# ---------------------------------------------------------------------------
class TestHysteresis:
    def test_one_day_dip_then_recover_never_flips(self, fsm):
        """第一天跌破 60MA、第二天站回 ➜ 全程維持原狀態，絕不 BEAR 化。"""
        bb = make_bb(macro_state="STRONG_TREND", days=10)
        # Day 1：假跌破 —— 新條件第 1 次成立（pending_days=1 < 2）➜ 擋下
        out1 = fsm.update(bb, breadth(lg_above60=False))
        assert out1 == "STRONG_TREND"          # 防抖擋下：條件僅成立 1 天
        assert bb.macro_state == "STRONG_TREND"
        # Day 2：站回 60MA —— 條件鏈斷裂，pending 歸零，永遠不會切換
        out2 = fsm.update(bb, breadth(lg_above60=True, lg_high60=False, mid_above20=True))
        assert out2 == "STRONG_TREND"
        assert bb.macro_state == "STRONG_TREND"
        assert bb.pending_days == 0
        # Day 3~4：再假跌破一次 —— 依然單日即返，不得跳變
        fsm.update(bb, breadth(lg_above60=False))
        fsm.update(bb, breadth(lg_above60=True, lg_high60=False, mid_above20=True))
        assert bb.macro_state == "STRONG_TREND"

    def test_sustained_breakdown_switches_after_confirm_days(self, fsm):
        """連續跌破：條件連續成立 >= 2 天才真正切至 BEAR_DEFENSE。"""
        bb = make_bb(macro_state="STRONG_TREND", days=99)
        bear = breadth(lg_above60=False)
        assert fsm.update(bb, bear) == "STRONG_TREND"   # 條件第 1 日：pending=1 < 2 擋下
        assert fsm.update(bb, bear) == "BEAR_DEFENSE"   # 條件第 2 日：pending=2 放行
        assert bb.days_in_current_state == 1            # 切換後計數器重置
        assert bb.macro_max_exposure == pytest.approx(0.0)

    def test_invalid_breadth_keeps_state(self, fsm):
        """資料缺失 ➜ 維持現狀、不跳變、不拋例外。"""
        bb = make_bb(macro_state="CHOPPY_MARKET", days=3)
        out = fsm.update(bb, breadth(valid=False))
        assert out == "CHOPPY_MARKET"
        assert bb.macro_state == "CHOPPY_MARKET"


# ---------------------------------------------------------------------------
# AC4：狀態轉移矩陣完備性 —— 無 Deadlock
# ---------------------------------------------------------------------------
class TestTransitionMatrixCompleteness:
    ALL_STATES = ["STRONG_TREND", "DIVERGENCE_BULL", "CHOPPY_MARKET", "BEAR_DEFENSE"]

    @pytest.mark.parametrize("src", ALL_STATES)
    @pytest.mark.parametrize("dst", ALL_STATES)
    def test_any_pair_reachable(self, fsm, src, dst):
        """任取 (src ➜ dst)：給足穩定天數與目標條件，一次 update 即可抵達。"""
        if src == dst:
            return
        # pending_days=confirm_days-1：模擬目標條件已於昨日首次成立，
        # 本次 update 為確認日第 2 天 ➜ 防抖放行，一次抵達 dst。
        bb = make_bb(macro_state=src, days=99, pending_days=fsm.cfg.confirm_days - 1)
        kwargs = {
            "STRONG_TREND": dict(lg_above60=True, lg_high60=False,
                                 lg_ma20=105.0, lg_ma60=100.0, mid_above20=True),
            "DIVERGENCE_BULL": dict(lg_above60=True, lg_high60=True, mid_above20=False),
            "CHOPPY_MARKET": dict(lg_above60=True, lg_high60=False,
                                  lg_ma20=100.5, lg_ma60=100.0, mid_above20=True),
            "BEAR_DEFENSE": dict(lg_above60=False),
        }[dst]
        out = fsm.update(bb, breadth(**kwargs))
        assert out == dst
        assert bb.macro_state is MacroState(dst)
        assert bb.macro_max_exposure == pytest.approx(
            DEFAULT_MAX_EXPOSURE[MacroState(dst)])


# ---------------------------------------------------------------------------
# AC5：狀態真空區填補
# ---------------------------------------------------------------------------
class TestVacuumZoneFill:
    def test_breadth_45pct_falls_to_sideways_buffer(self, fsm):
        """AC 字面情境：大盤在 60MA 上、MA20/MA60 距離 > 2%，但代理寬度僅 45%
        （< 50%）➜ 四態條件不完整，FSM 必須穩定輸出 SIDEWAYS 緩衝態，
        不得拋例外、不得殘留髒預設值。"""
        bb = make_bb(macro_state="STRONG_TREND", days=99, pending_days=1)
        out = fsm.update(bb, breadth(lg_above60=True, lg_high60=False,
                                     lg_ma20=105.0, lg_ma60=100.0,
                                     mid_above20=False, breadth_pct=0.45))
        assert out == "SIDEWAYS"                      # 震盪緩衝填補
        assert bb.macro_state is MarketState.SIDEWAYS  # 復用既有枚舉，型別安全
        assert bb.macro_max_exposure == pytest.approx(0.5)
        assert any("真空" in r or "震盪緩衝" in r or "寬度" in r for r in bb.reasons)

    def test_discrete_vacuum_combo_also_buffered(self, fsm):
        """離散代理矩陣下的另一真空組合（0050 獨強、未創高）同樣穩定緩衝。"""
        bb = make_bb(macro_state="STRONG_TREND", days=99, pending_days=1)
        out = fsm.update(bb, breadth(lg_above60=True, lg_high60=False,
                                     lg_ma20=105.0, lg_ma60=100.0,
                                     mid_above20=False))
        # breadth_pct=0.5 不觸發真空前置攔截，但 rule4 需 mid_above20=True 亦不成立
        # ➜ 走到 rule5 边角真空 ➜ SIDEWAYS
        assert out == "SIDEWAYS"

    def test_vacuum_never_raises_on_any_input(self, fsm):
        """亂序/極端輸入組合遍歷：update 一律回傳字串、零例外（AC 邊界防護）。"""
        weird = [
            {}, {"valid": True}, breadth(breadth_pct=-1),
            breadth(lg_ma20=0.0, lg_ma60=0.0),
            None, "not-a-dict", {"valid": True, "large_ma60": float("nan")},
        ]
        for w in weird:
            bb = make_bb()
            res = fsm.update(bb, w)  # 任何輸入都不得拋例外
            assert isinstance(res, str) and res != ""


# ---------------------------------------------------------------------------
# 整合面：MacroFSM 寫入的字段可被 JSON 持久化（CI 無狀態 Runner 需求）
# ---------------------------------------------------------------------------
class TestPersistenceFields:
    def test_blackboard_macro_fields_roundtrip(self):
        import json
        bb = make_bb(macro_state="DIVERGENCE_BULL", days=2)
        fsm = MacroFSM(MacroFSMConfig())
        fsm.update(bb, breadth(lg_high60=True, mid_above20=False))  # no-op path
        snap = {
            "macro_state": bb.macro_state if isinstance(bb.macro_state, str)
            else bb.macro_state.name,
            "days_in_current_state": bb.days_in_current_state,
            "breadth_pct": bb.breadth_pct,
            "macro_max_exposure": bb.macro_max_exposure,
        }
        restored = json.loads(json.dumps(snap))
        assert restored["macro_state"] == "DIVERGENCE_BULL"
