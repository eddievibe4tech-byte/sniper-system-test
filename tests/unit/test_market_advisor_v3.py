"""Market Advisor v3 單元測試

驗證三件事（對應「配置層漏接 Alpha 信號」的設計缺口修補）：
1. collect_opportunities 接上 alpha（screener_candidates.json）：
   - Alpha 候選以 prio 2 進入配置、deep 買入級降為 prio 3；
   - MA20 乖離 >3% → detail 註明「不追價、等回踩」；乖離溫和 → 可限價；
   - alpha=None / alpha.error 時完全不產生 Alpha 機會（向後相容）。
2. attach_warrant_options：只為既有股票機會附上 warrant_option，
   絕不新增部位；warrants=None / error / 無對應代號時自動隱藏。
3. main() 於 us 缺失或 vix=None 時誠實給出 VIX 缺失警告（不再無聲用保守預設值）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import market_advisor as ma  # noqa: E402


ALPHA = {
    "candidates": [
        {"code": "3017", "name": "奇鋐", "revenue_yoy": 54.34, "inst_buy_days": 3,
         "current_price": 3555.0, "ma20": 3366.75},          # 乖離 +5.6% → 不追價
        {"code": "4938", "name": "和碩", "revenue_yoy": 34.11, "inst_buy_days": 3,
         "current_price": 91.3, "ma20": 90.91},              # 乖離 +0.4% → 可限價
        {"code": "2884", "name": "玉山金", "revenue_yoy": 19.29, "inst_buy_days": 3,
         "current_price": 45.6, "ma20": None},               # MA20 缺失 → 人工確認
    ],
    "error": None,
}

DEEP = {
    "all_results": [
        {"code": "2881", "name": "富邦金", "recommendation": "謹慎買入",
         "ev_score": 64, "ma20": 148.28},
        {"code": "9999", "name": "弱訊號", "recommendation": "觀望",
         "ev_score": 40, "ma20": 10},
    ]
}

WARRANTS = {
    "candidates": [
        {"stock_code": "3017", "warrant_code": "奇鋐合庫66開A", "effective_leverage": 4.2,
         "days_to_expiry": 45, "bid_ask_spread_pct": 3.1},
        {"stock_code": "2603", "warrant_code": "不相干權證", "effective_leverage": 3.5,
         "days_to_expiry": 60, "bid_ask_spread_pct": 2.0},
    ]
}


class TestAlphaIntegration:
    def test_alpha_candidates_enter_plan_at_prio2(self):
        opps = ma.collect_opportunities(None, DEEP, None, None, alpha=ALPHA)
        alpha_opps = [o for o in opps if o["label"].startswith("Alpha 候選")]
        assert len(alpha_opps) == 3
        assert all(o["prio"] == 2 and o["market"] == "台股" for o in alpha_opps)
        assert {o["code"] for o in alpha_opps} == {"3017", "4938", "2884"}

    def test_deep_buy_downgraded_to_prio3_after_alpha(self):
        opps = ma.collect_opportunities(None, DEEP, None, None, alpha=ALPHA)
        deep = [o for o in opps if o["label"].startswith("買入級")]
        assert [(o["code"], o["prio"]) for o in deep] == [("2881", 3)]
        # 排序結果：Alpha(prio2) 必須排在 deep 買入級(prio3) 之前
        assert opps.index(deep[0]) > max(opps.index(o) for o in opps if o["prio"] == 2)

    def test_gap_over_3pct_says_no_chase(self):
        opps = ma.collect_opportunities(None, None, None, None, alpha=ALPHA)
        qh = next(o for o in opps if o["code"] == "3017")
        assert "不追價" in qh["detail"] and "回踩" in qh["detail"] and "3366.75" in qh["detail"]

    def test_mild_gap_allows_limit_order(self):
        opps = ma.collect_opportunities(None, None, None, None, alpha=ALPHA)
        hi = next(o for o in opps if o["code"] == "4938")
        assert "溫和" in hi["detail"] and "限價" in hi["detail"]

    def test_missing_ma20_honest_fallback(self):
        opps = ma.collect_opportunities(None, None, None, None, alpha=ALPHA)
        es = next(o for o in opps if o["code"] == "2884")
        assert "資料缺失" in es["detail"]

    def test_max_three_alpha_candidates(self):
        big = {"candidates": ALPHA["candidates"] * 4, "error": None}
        opps = ma.collect_opportunities(None, None, None, None, alpha=big)
        assert len([o for o in opps if o["label"].startswith("Alpha")]) == 3

    def test_backward_compatible_without_alpha(self):
        # 舊簽名呼叫（位置參數）不受影響
        opps = ma.collect_opportunities(None, DEEP, None, None)
        assert all(not o["label"].startswith("Alpha") for o in opps)
        # alpha.error 時不產生 Alpha 機會
        opps2 = ma.collect_opportunities(None, DEEP, None, None,
                                         alpha={"candidates": [{"code": "1"}], "error": "API down"})
        assert all(not o["label"].startswith("Alpha") for o in opps2)


class TestWarrantVehicle:
    def test_warrant_option_attached_to_matching_stock(self):
        opps = ma.collect_opportunities(None, DEEP, None, None, alpha=ALPHA, warrants=WARRANTS)
        qh = next(o for o in opps if o["code"] == "3017")
        assert "warrant_option" in qh
        assert "奇鋐合庫66開A" in qh["warrant_option"]
        assert "4.2x" in qh["warrant_option"] and "-30%" in qh["warrant_option"]

    def test_warrant_never_creates_new_position(self):
        before = ma.collect_opportunities(None, DEEP, None, None, alpha=ALPHA)
        after = ma.collect_opportunities(None, DEEP, None, None, alpha=ALPHA, warrants=WARRANTS)
        assert [o["code"] for o in before] == [o["code"] for o in after]
        assert all("warrant_option" not in o for o in after if o.get("code") == "2603")

    def test_absent_or_error_warrants_hidden(self):
        for w in (None, {"error": "模組未合併"}, {"candidates": []}):
            opps = ma.collect_opportunities(None, DEEP, None, None, alpha=ALPHA, warrants=w)
            assert all("warrant_option" not in o for o in opps)


class TestVixWarning:
    def _run_main(self, monkeypatch, tmp_path, us_data):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "us_candidates.json").write_text(
            json.dumps(us_data), encoding="utf-8") if us_data is not ... else None
        monkeypatch.setattr(ma, "BASE", str(data_dir))
        monkeypatch.setattr(sys, "argv", ["market_advisor"])
        captured = []
        monkeypatch.setattr(ma, "load", lambda name: (
            None if name != "us_candidates.json" or us_data is ...
            else (us_data if name == "us_candidates.json" and (data_dir / name).exists() else None)))
        return captured

    def _warnings_for(self, monkeypatch, tmp_path, us_data):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr(ma, "BASE", str(data_dir))

        def fake_load(name):
            if name == "us_candidates.json":
                return us_data
            return None
        monkeypatch.setattr(ma, "load", fake_load)
        out = {}
        real_dump = json.dump

        def spy_dump(obj, f, **kw):
            out.update(obj)
            return real_dump(obj, f, **kw)
        monkeypatch.setattr(json, "dump", spy_dump)
        ma.main()
        return out["warnings"]

    def test_vix_none_triggers_manual_check_warning(self, monkeypatch, tmp_path):
        warns = self._warnings_for(monkeypatch, tmp_path,
                                   {"vix": None, "candidates": [{"symbol": "AAPL", "scenario": "動量突破",
                                                                 "recommendation": "謹慎買入", "rsi": 60}]})
        assert any("VIX 缺失" in w and "VIX<20" in w for w in warns)

    def test_us_file_missing_also_warns(self, monkeypatch, tmp_path):
        warns = self._warnings_for(monkeypatch, tmp_path, None)
        assert any("VIX 缺失" in w for w in warns)

    def test_vix_present_no_warning(self, monkeypatch, tmp_path):
        warns = self._warnings_for(monkeypatch, tmp_path, {"vix": 15.2, "candidates": []})
        assert not any("VIX 缺失" in w for w in warns)
