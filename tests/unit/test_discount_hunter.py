"""特價獵人引擎單元測試（重構版）

驗證：
- DiscountHunterStrategy.evaluate：以「錨定基準價」推算特價區間與四種狀態邊界
  （迴歸保護：徹底解決舊版「現價 vs 現價×(1-pct)」自我參照死鎖、永遠無法觸發特價的 Bug）
- AnchorType.RECENT_HIGH：波段拉回模式以 20 日高點為基準
- HUNT_TARGETS：dataclass 配置、幣別標明、00940 → 006208（富邦台50）
- run_discount_hunter：預算來源優先級（參數 > HUNT_BUDGET 環境變數 > 預設 10000）
"""
import json

import pytest

from src.discount_hunter import (
    AnchorType,
    DiscountHunterStrategy,
    HUNT_TARGETS,
    HunterResult,
    MarketData,
    TargetConfig,
    run_discount_hunter,
)


def make_target(small=0.03, big=0.08, anchor_type=AnchorType.PREV_CLOSE):
    return TargetConfig("TEST", "測試標的", "USD", small, big, anchor_type)


class TestDiscountZones:
    def test_zones_anchored_to_prev_close(self):
        """特價區間必須由「基準價」推算，而非現價"""
        target = make_target(0.03, 0.08)
        data = MarketData(current_price=100.0, anchor_price=100.0, change_pct=0.0)
        r = DiscountHunterStrategy.evaluate(target, data, 10000)
        # 小特價目標 = 100*(1-0.03)=97，±0.5% → [96.52, 97.48]
        assert r.small_zone == [96.52, 97.48]
        # 大特價目標 = 100*(1-0.08)=92，±1% → [91.08, 92.92]
        assert r.big_zone == [91.08, 92.92]

    def test_zone_ordering(self):
        target = make_target(0.03, 0.08)
        data = MarketData(current_price=500.0, anchor_price=500.0, change_pct=0.0)
        r = DiscountHunterStrategy.evaluate(target, data, 10000)
        assert r.big_zone[0] < r.big_zone[1] < r.small_zone[0] < r.small_zone[1]


class TestStatusJudgement:
    """核心迴歸測試：回檔後必須能真實觸發各級特價（舊版死鎖 Bug 修復驗證）"""

    def _eval(self, curr, anchor=100.0, small=0.03, big=0.08):
        target = make_target(small, big)
        change_pct = round((curr - anchor) / anchor * 100, 2)
        data = MarketData(current_price=curr, anchor_price=anchor, change_pct=change_pct)
        return DiscountHunterStrategy.evaluate(target, data, 10000)

    def test_big_discount_reachable(self):
        # 單日跌 8% → 舊版永遠顯示「尚未特價」，新版必須觸發「已達大特價」
        r = self._eval(92.0)
        assert "已達大特價" in r.status
        assert "70-80%" in r.action

    def test_small_discount_reachable(self):
        r = self._eval(97.0)  # 跌 3%，落入小特價區間
        assert "已達小特價" in r.status

    def test_near_discount(self):
        r = self._eval(97.9)  # 距離小特價上限 < 1%
        assert "接近特價" in r.status

    def test_not_discounted(self):
        r = self._eval(100.0)  # 現價 = 基準價
        assert "尚未特價" in r.status

    def test_deep_crash_still_big_discount(self):
        r = self._eval(85.0)  # 跌破大特價下限（財報暴雷）
        assert "已達大特價" in r.status

    def test_recent_high_anchor_mode(self):
        """波段拉回模式：相比 20 日高點折價，即使未跌也非自我參照"""
        target = make_target(0.03, 0.08, AnchorType.RECENT_HIGH)
        data = MarketData(current_price=92.0, anchor_price=100.0, change_pct=-8.0)
        r = DiscountHunterStrategy.evaluate(target, data, 10000)
        assert "已達大特價" in r.status


class TestBudgetAllocation:
    def test_28_split(self):
        target = make_target()
        data = MarketData(current_price=100.0, anchor_price=100.0, change_pct=0.0)
        r = DiscountHunterStrategy.evaluate(target, data, 10000)
        assert r.first_batch_budget == 2500.0   # 25% 底倉
        assert r.hunt_reserve_budget == 7500.0  # 75% 狩獵金


class TestHuntTargetsConfig:
    def test_targets_are_dataclasses(self):
        assert all(isinstance(t, TargetConfig) for t in HUNT_TARGETS)

    def test_symbols_and_currency(self):
        syms = {t.symbol for t in HUNT_TARGETS}
        assert syms == {"VOO", "JPM", "EOG", "0050.TW", "006208.TW"}
        # 🆕 00940 已替換為 006208（富邦台50，長期總報酬優先）
        assert "00940.TW" not in syms
        tw_symbols = {t.symbol for t in HUNT_TARGETS if t.currency == "TWD"}
        assert tw_symbols == {"0050.TW", "006208.TW"}

    def test_taiwan_etf_discounts(self):
        tw = {t.symbol: t for t in HUNT_TARGETS if t.currency == "TWD"}
        for t in tw.values():
            assert t.small_discount == 0.015
            assert t.big_discount == 0.035


class TestRunHunter:
    """run_discount_hunter I/O 層：預算優先級與輸出結構"""

    def _fake_fetch(self, monkeypatch, price_map=None):
        price_map = price_map or {
            t.symbol: MarketData(current_price=100.0, anchor_price=100.0, change_pct=0.0)
            for t in HUNT_TARGETS
        }
        monkeypatch.setattr(
            "src.discount_hunter.MarketDataFetcher.fetch_market_data",
            staticmethod(lambda targets: price_map),
        )

    def _run_and_capture(self, tmp_path, monkeypatch):
        import src.discount_hunter as dh
        monkeypatch.setattr(dh, "DATA_DIR", tmp_path)
        run_discount_hunter()
        return json.loads((tmp_path / "discount_hunter.json").read_text(encoding="utf-8"))

    def test_default_budget_10000(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HUNT_BUDGET", raising=False)
        self._fake_fetch(monkeypatch)
        data = self._run_and_capture(tmp_path, monkeypatch)
        t0 = data["targets"][0]
        assert t0["first_batch_budget"] == 2500.0
        assert t0["hunt_reserve_budget"] == 7500.0

    def test_env_budget_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUNT_BUDGET", "15000")
        self._fake_fetch(monkeypatch)
        data = self._run_and_capture(tmp_path, monkeypatch)
        t0 = data["targets"][0]
        assert t0["first_batch_budget"] == 3750.0   # 15000 × 25%
        assert t0["hunt_reserve_budget"] == 11250.0

    def test_discount_triggered_end_to_end(self, tmp_path, monkeypatch):
        """端到端迴歸：006208 單日跌 3.5% 必須在輸出中顯示「已達大特價」"""
        monkeypatch.delenv("HUNT_BUDGET", raising=False)
        price_map = {
            t.symbol: MarketData(current_price=100.0, anchor_price=100.0, change_pct=0.0)
            for t in HUNT_TARGETS
        }
        price_map["006208.TW"] = MarketData(
            current_price=96.5, anchor_price=100.0, change_pct=-3.5)
        self._fake_fetch(monkeypatch, price_map)
        import src.discount_hunter as dh
        monkeypatch.setattr(dh, "DATA_DIR", tmp_path)
        run_discount_hunter()
        data = json.loads((tmp_path / "discount_hunter.json").read_text(encoding="utf-8"))
        # 已達特價者排最前面
        assert data["targets"][0]["symbol"] == "006208"
        assert "已達大特價" in data["targets"][0]["status"]
        assert data["targets"][0]["currency"] == "TWD"

    def test_no_data_targets_skipped(self, tmp_path, monkeypatch):
        """無網路／抓不到資料時不崩潰，僅輸出空清單"""
        monkeypatch.delenv("HUNT_BUDGET", raising=False)
        self._fake_fetch(monkeypatch, price_map={})
        data = self._run_and_capture(tmp_path, monkeypatch)
        assert isinstance(data["targets"], list)
        assert "updated_at" in data
