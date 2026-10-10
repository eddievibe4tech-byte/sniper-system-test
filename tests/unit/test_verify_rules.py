"""
🆕 Verification v2.1 規則引擎單元測試

覆蓋五項審查指正的回歸防護：
  1. 期望值符號（負負得正）＋買入/過濾器樣本隔離 → test_expectancy_sign_and_isolation
  2. 基準時窗錯位 → windowed_return 鎖定 [T, T+H] → test_windowed_return_locks_horizon
  3. 驗證價格隨執行時間漂移 → 以 entry_price 為錨、視窗未滿回傳 None（延後結算）
  4. 避開規則 or 虛增勝率 → 改為 net<=0 and alpha<=0 → test_avoid_strict
  5. 波動率縮放門檻 / 無基準降級
"""
import pytest

from src.main import (judge_correctness as j, windowed_return, compute_alpha_stats,
                      vol_hurdle_pct)


@pytest.mark.unit
class TestJudgeCorrectness:
    def test_buy(self):
        assert j("謹慎買入", 4.0, 1.0, 25)["was_correct"] is True      # net 3.5>2, alpha 3>0
        assert j("謹慎買入", 1.0, 0.5, 25)["was_correct"] is False     # v1 會判對 → v2 判錯
        assert j("謹慎買入", 5.0, 6.0, 25)["was_correct"] is False     # 漲但跑輸大盤＝無 alpha

    def test_avoid_strict(self):
        # 🔴 v2.1：必須「絕對虧損 且 相對跑輸」雙重條件才算正確避開
        assert j("避開", -4.0, 0.0, 25)["was_correct"] is True         # 虧且跑輸
        assert j("避開", 2.0, 5.0, 25)["was_correct"] is False         # 沒虧錢→不算正確避開
        assert j("避開", -1.5, -15.0, 25)["was_correct"] is False      # 相對贏家→避開是誤判
        assert j("避開", 5.0, 1.0, 25)["was_correct"] is False         # 錯過大漲

    def test_wait(self):
        assert j("觀望", 1.0, 1.5, 25)["was_correct"] is True          # 無邊際
        assert j("觀望", 9.0, 1.0, 25)["was_correct"] is False         # 錯過行情

    def test_vol_scaled_hurdle(self):
        # vol=55 → hurdle = 0.5*55*sqrt(5/252) ≈ 3.87（非 2% 下限）
        assert 3.5 < vol_hurdle_pct(55) < 4.0
        assert j("積極買入", 4.0, 0.0, 55)["was_correct"] is False     # net 3.5 < 門檻 3.87
        assert j("積極買入", 5.0, 0.0, 55)["was_correct"] is True      # net 4.5 > 門檻 3.87
        # vol=25 → scaled ≈1.77 < 2% → 取下限 2%
        assert vol_hurdle_pct(25) == 2.0

    def test_no_benchmark_fallback(self):
        assert j("謹慎買入", 3.0, None, 25)["was_correct"] is True     # net 2.5>2 降級規則
        assert j("謹慎買入", 2.4, None, 25)["was_correct"] is False    # net 1.9<2
        assert j("避開", 1.0, None, 25)["was_correct"] is False        # net 0.5>0
        assert j("避開", -1.0, None, 25)["was_correct"] is True        # net -1.5<=0

    def test_audit_fields_present(self):
        v = j("謹慎買入", 4.0, 1.0, 25)
        assert v["criteria_version"] == 2
        assert v["net_pct"] == 3.5
        assert v["alpha_pct"] == 3.0
        assert v["hurdle_pct"] == 2.0
        assert v["criteria"] == "buy:net>hurdle&alpha>0"


@pytest.mark.unit
class TestWindowedReturn:
    def test_windowed_return_locks_horizon(self):
        s = [{"date": f"2026-01-{d:02d}", "close": 100 + d} for d in range(1, 16)]
        # T=01-01 → i=0（序列第 0 筆），j=i+5 → 第 5 筆＝01-06 close 106；anchor=101（entry_price 為錨）
        assert windowed_return(s, "2026-01-01", 5, anchor_price=101.0) == round((106 / 101 - 1) * 100, 2)
        assert windowed_return(s[:13], "2026-01-10", 5) is None        # 視窗未滿 → 延後結算
        assert windowed_return([], "2026-01-01", 5) is None            # 無資料
        assert windowed_return(s, "2026-02-01", 5) is None             # T 晚於最後交易日

    def test_windowed_return_rejects_invalid_close(self):
        # 🔴 PR review #8 回歸防護：p1（結算收盤）為 0/負值 → 回傳 None（延後結算），不得硬算
        s = [{"date": f"2026-01-{d:02d}", "close": 100 + d} for d in range(1, 16)]
        bad = [dict(x) for x in s]
        bad[5]["close"] = 0.0                                           # T+H 收盤異常為 0
        assert windowed_return(bad, "2026-01-01", 5) is None
        bad[5]["close"] = -3.0                                          # 負值同樣拒絕
        assert windowed_return(bad, "2026-01-01", 5) is None

    def test_anchor_ignores_execution_time_drift(self):
        # 同一 T+H 視窗，無論何時執行 regrade，結果只取 T+H 當日收盤 → 統計同質性
        s = [{"date": f"2026-01-{d:02d}", "close": 100 + d} for d in range(1, 16)]
        r1 = windowed_return(s, "2026-01-01", 5, anchor_price=100.0)          # 取 01-06 close 106
        r2 = windowed_return(s[:12], "2026-01-01", 5, anchor_price=100.0)     # 資料截短仍同窗
        assert r1 == r2 == 6.0


@pytest.mark.unit
class TestComputeAlphaStats:
    def test_expectancy_sign_and_isolation(self):
        recs = [
            {"prediction": {"recommendation": "謹慎買入"},
             "actual_result": {"criteria_version": 2, "was_correct": True,
                               "net_pct": 2.0, "alpha_pct": 1.0}},
            {"prediction": {"recommendation": "謹慎買入"},
             "actual_result": {"criteria_version": 2, "was_correct": False,
                               "net_pct": -4.0, "alpha_pct": -5.0}},
            {"prediction": {"recommendation": "避開"},      # 過濾器：不得污染期望值
             "actual_result": {"criteria_version": 2, "was_correct": True,
                               "net_pct": -10.0, "alpha_pct": -8.0}},
        ]
        st = compute_alpha_stats(recs)
        assert st["trade_records"] == 2
        assert st["expectancy_pct"] == -1.0        # 0.5*2 - 0.5*4；舊公式會算出 +3.0
        assert st["graded_records"] == 3
        assert st["avg_loss_pct"] == 4.0           # 🔴 一律正值（abs）
        assert st["classification_accuracy_pct"] == round(2 / 3 * 100, 1)

    def test_empty_when_no_v2_records(self):
        recs = [{"prediction": {"recommendation": "觀望"},
                 "actual_result": {"criteria_version": 1, "was_correct": True}}]
        assert compute_alpha_stats(recs) == {}

    def test_all_wins_positive_expectancy(self):
        recs = [{"prediction": {"recommendation": "積極買入"},
                 "actual_result": {"criteria_version": 2, "was_correct": True,
                                   "net_pct": 3.0, "alpha_pct": 2.0}}]
        st = compute_alpha_stats(recs)
        assert st["expectancy_pct"] == 3.0
        assert st["avg_loss_pct"] == 0.0
