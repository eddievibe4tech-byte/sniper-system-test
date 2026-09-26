"""批量能力快取單元測試（#95）

驗證：
1. 無快取檔 → bulk_supported() 預設 True（維持原探測行為）；
2. mark_bulk_unsupported() 記檔後 → bulk_supported() False；
3. 快取命中時 fetch_revenue_yoy_map 完全不打批量端點（零 400 噪音），直接個股模式；
4. 快取命中時 fetch_inst_streak 一次端點都不打，直接回 None；
5. 首次探測失敗（批量 None）→ 自動 mark_bulk_unsupported() 記檔。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import screener  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_cap_file(tmp_path, monkeypatch):
    """每個測試用 tmp 目錄下的獨立快取檔，避免污染 repo / 互相干擾"""
    cap = tmp_path / "finmind_capabilities.json"
    monkeypatch.setattr(screener, "CAP_FILE", str(cap))
    return cap


class FakeFinMind:
    """記錄所有 _make_request 呼叫；批量（stock_id 空）依 spec 回傳"""

    def __init__(self, bulk_response=None, per_stock_ok=True):
        self.calls = []
        self.bulk_response = bulk_response
        self.per_stock_ok = per_stock_ok

    def _make_request(self, dataset, stock_id, days=60,
                      start_date=None, end_date=None):
        self.calls.append((dataset, stock_id))
        if not stock_id:  # 批量
            return self.bulk_response
        if not self.per_stock_ok:
            return None
        if dataset == "TaiwanStockMonthRevenue":
            # 13+ 筆月營收，最新月 vs 13 個月前 → YoY +20%
            return [{"revenue": 100} for _ in range(12)] + [{"revenue": 120}]
        return []

    def get_institutional_buy(self, code, days=10):
        self.calls.append(("inst_per_stock", code))
        return 5


def test_default_true_when_no_cache(isolated_cap_file):
    assert screener.bulk_supported() is True


def test_mark_then_unsupported(isolated_cap_file):
    screener.mark_bulk_unsupported()
    assert screener.bulk_supported() is False
    with open(isolated_cap_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["bulk"] is False
    assert "detected_at" in data


def test_cache_hit_skips_bulk_probe_revenue(isolated_cap_file):
    screener.mark_bulk_unsupported()
    fm = FakeFinMind(bulk_response=None)
    yoy = screener.fetch_revenue_yoy_map(fm, ["2330"])
    # 零批量端點呼叫（无 400 噪音）
    assert all(stock_id for _, stock_id in fm.calls), \
        "快取命中時不應打任何批量請求"
    assert yoy == {"2330": pytest.approx(20.0)}


def test_cache_hit_skips_bulk_probe_inst(isolated_cap_file):
    screener.mark_bulk_unsupported()
    fm = FakeFinMind(bulk_response=None)
    streak = screener.fetch_inst_streak_map(fm, ["2330"])
    # fetch_inst_streak 應完全跳過逐日批量探測
    assert ("TaiwanStockInstitutionalInvestorsBuySell", "") not in fm.calls
    assert streak == {"2330": 5}


def test_first_failure_records_capability(isolated_cap_file):
    """無快取 → 探測批量 → 失敗（None）→ 自動記檔"""
    assert screener.bulk_supported() is True
    fm = FakeFinMind(bulk_response=None)
    screener.fetch_revenue_yoy_map(fm, ["2330"])
    assert screener.bulk_supported() is False  # 已記檔
    # 之後再抓一次：批量探測不重複發生
    fm2 = FakeFinMind(bulk_response=None)
    screener.fetch_revenue_yoy_map(fm2, ["2330"])
    assert all(stock_id for _, stock_id in fm2.calls)


def test_inst_bulk_failure_records_capability(isolated_cap_file):
    fm = FakeFinMind(bulk_response=None)
    assert screener.fetch_inst_streak(fm, ["2330"], 2) is None
    assert screener.bulk_supported() is False
