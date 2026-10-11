"""特價獵人引擎單元測試

驗證：
- calculate_discount_zones 小/大特價區間計算（含緩衝）
- judge_proximity 四種狀態邊界條件
- run_discount_hunter 預算來源優先級（參數 > HUNT_BUDGET 環境變數 > 預設 10000）
"""
import json

import pytest

from src.discount_hunter import (
    calculate_discount_zones,
    judge_proximity,
    run_discount_hunter,
)


class TestDiscountZones:
    def test_small_discount_zone(self):
        zones = calculate_discount_zones(100.0, 0.03, 0.06)
        # 小特價 -3% ± 0.5% 緩衝 = [-3.5%, -2.5%]
        assert zones["small_zone"] == [96.5, 97.5]
        assert zones["small_discount_pct"] == 3.0

    def test_big_discount_zone(self):
        zones = calculate_discount_zones(100.0, 0.03, 0.08)
        # 大特價 -8% ± 1% 緩衝 = [-9%, -7%]
        assert zones["big_zone"] == [91.0, 93.0]
        assert zones["big_discount_pct"] == 8.0

    def test_zone_ordering(self):
        # 下限必須小於上限，且大特價區間整體低於小特價區間
        zones = calculate_discount_zones(500.0, 0.03, 0.06)
        assert zones["small_zone"][0] < zones["small_zone"][1]
        assert zones["big_zone"][0] < zones["big_zone"][1]
        assert zones["big_zone"][1] < zones["small_zone"][0]


class TestJudgeProximity:
    def test_big_discount_reached(self):
        zones = calculate_discount_zones(100.0, 0.03, 0.08)
        result = judge_proximity(92.0, zones)  # 在大特價區間內
        assert "已達大特價" in result["status"]
        assert result["proximity_pct"] == 0.0

    def test_small_discount_reached(self):
        zones = calculate_discount_zones(100.0, 0.03, 0.08)
        result = judge_proximity(97.0, zones)  # 在小特價區間內
        assert "已達小特價" in result["status"]
        assert result["proximity_pct"] == 0.0

    def test_near_discount(self):
        zones = calculate_discount_zones(100.0, 0.03, 0.08)
        result = judge_proximity(98.5, zones)  # 距離小特價 < 2%
        assert "接近特價" in result["status"]
        assert result["proximity_pct"] < 2.0

    def test_not_near_discount(self):
        zones = calculate_discount_zones(100.0, 0.03, 0.08)
        result = judge_proximity(100.0, zones)  # 現價
        assert "尚未特價" in result["status"]
        assert result["proximity_pct"] >= 2.0

    def test_boundary_at_small_high(self):
        # 恰好等於小特價上限 → 已達小特價
        zones = calculate_discount_zones(100.0, 0.03, 0.08)
        result = judge_proximity(zones["small_zone"][1], zones)
        assert "已達小特價" in result["status"]

    def test_deep_below_big_low_still_big_discount(self):
        # 跌破大特價下限（如財報暴雷）仍屬「已達大特價」分支
        zones = calculate_discount_zones(100.0, 0.03, 0.08)
        result = judge_proximity(85.0, zones)
        assert "已達大特價" in result["status"]


class TestBudgetConfig:
    """P1-3：預算可透過環境變數 / 函式參數配置"""

    def _run_and_capture(self, tmp_path, monkeypatch, patch_prices=True):
        if patch_prices:
            monkeypatch.setattr(
                "src.discount_hunter.fetch_current_prices",
                lambda symbols: {s: 100.0 for s in symbols},
            )
        import src.discount_hunter as dh
        monkeypatch.setattr(dh, "DATA_DIR", tmp_path)
        run_discount_hunter()
        return json.loads((tmp_path / "discount_hunter.json").read_text(encoding="utf-8"))

    def test_default_budget_10000(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HUNT_BUDGET", raising=False)
        data = self._run_and_capture(tmp_path, monkeypatch)
        alloc = data["targets"][0]["budget_allocation"]
        assert alloc["first_batch"] == 2500   # 25%
        assert alloc["hunt_reserve"] == 7500  # 75%

    def test_env_budget_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HUNT_BUDGET", "15000")
        data = self._run_and_capture(tmp_path, monkeypatch)
        alloc = data["targets"][0]["budget_allocation"]
        assert alloc["first_batch"] == 3750   # 15000 × 25%
        assert alloc["hunt_reserve"] == 11250

    def test_no_price_targets_skipped(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HUNT_BUDGET", raising=False)
        data = self._run_and_capture(tmp_path, monkeypatch, patch_prices=False)
        # 無網路環境下抓不到價格也不應崩潰，僅輸出空清單
        assert isinstance(data["targets"], list)
        assert "updated_at" in data
