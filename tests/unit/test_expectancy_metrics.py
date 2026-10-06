# -*- coding: utf-8 -*-
"""
🆕 Sprint 3 (#160)：期望值 (Expectancy) 追蹤單測

驗證 update_performance_metrics() 的 expectancy / avg_win_pct / avg_loss_pct
與 auto_verify_predictions() 寫入的 actual_return_pct 欄位。
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    """將 main 模組的 DATA_DIR 指向 tmp，避免污染真實 data/"""
    import src.main as m
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    monkeypatch.setattr(m, 'DATA_DIR', data_dir)
    return data_dir


def _rec(code, accuracy, ret_pct, has_return_field=True):
    ar = {"was_correct": accuracy == 1, "horizon_days": 5, "auto": True}
    if has_return_field:
        ar["actual_return_pct"] = ret_pct
    else:
        ar["profit_pct"] = ret_pct  # 舊格式：只有 profit_pct
    return {
        "stock_code": code,
        "timestamp": "2026-09-01T10:00:00+08:00",
        "entry_price": 100,
        "prediction": {"recommendation": "積極買入"},
        "actual_result": ar,
        "accuracy": accuracy,
    }


class TestExpectancyMetrics:
    def test_expectancy_formula(self, isolated_data_dir):
        """expectancy = win_rate × avg_win + loss_rate × avg_loss"""
        import src.main as m
        telemetry = {"records": [
            _rec("2330", 1, 10.0),   # win
            _rec("2454", 1, 6.0),    # win
            _rec("3017", 0, -4.0),   # loss
        ], "metadata": {}}
        m.update_performance_metrics(telemetry)
        metrics = json.loads((isolated_data_dir / 'performance_metrics.json').read_text(encoding='utf-8'))
        # win_rate=2/3, avg_win=8.0, loss_rate=1/3, avg_loss=-4.0
        assert metrics['avg_win_pct'] == 8.0
        assert metrics['avg_loss_pct'] == -4.0
        assert metrics['expectancy'] == round((2/3) * 8.0 + (1/3) * (-4.0), 2)  # 4.0
        assert metrics['expectancy_sample_count'] == 3

    def test_legacy_profit_pct_fallback(self, isolated_data_dir):
        """舊記錄無 actual_return_pct → 退回 profit_pct 仍納入計算"""
        import src.main as m
        telemetry = {"records": [
            _rec("2330", 1, 5.0, has_return_field=False),
            _rec("2454", 0, -2.0, has_return_field=False),
        ], "metadata": {}}
        m.update_performance_metrics(telemetry)
        metrics = json.loads((isolated_data_dir / 'performance_metrics.json').read_text(encoding='utf-8'))
        assert metrics['expectancy_sample_count'] == 2
        assert metrics['expectancy'] == 1.5  # 0.5*5 + 0.5*(-2)

    def test_no_verified_records_returns_none(self, isolated_data_dir):
        """無已驗證記錄 → expectancy=None（前端顯示資料不足），不炸"""
        import src.main as m
        telemetry = {"records": [
            {"stock_code": "2330", "timestamp": "2026-09-01T10:00:00+08:00",
             "entry_price": 100, "prediction": {}, "actual_result": None, "accuracy": None},
        ], "metadata": {}}
        m.update_performance_metrics(telemetry)
        metrics = json.loads((isolated_data_dir / 'performance_metrics.json').read_text(encoding='utf-8'))
        assert metrics['expectancy'] is None
        assert metrics['avg_win_pct'] == 0.0
        assert metrics['avg_loss_pct'] == 0.0

    def test_all_wins_expectancy_equals_avg_win(self, isolated_data_dir):
        import src.main as m
        telemetry = {"records": [_rec("2330", 1, 4.0), _rec("2454", 1, 8.0)], "metadata": {}}
        m.update_performance_metrics(telemetry)
        metrics = json.loads((isolated_data_dir / 'performance_metrics.json').read_text(encoding='utf-8'))
        assert metrics['expectancy'] == 6.0
        assert metrics['avg_loss_pct'] == 0.0

    def test_actual_return_pct_written_by_auto_verify(self):
        """auto_verify_predictions 必須在 actual_result 寫入 actual_return_pct（與 profit_pct 同值）"""
        import src.main as m
        from datetime import datetime, timedelta, timezone
        tz = timezone(timedelta(hours=8))
        old_ts = (datetime.now(tz) - timedelta(days=20)).isoformat()
        telemetry = {"records": [{
            "stock_code": "2330",
            "timestamp": old_ts,
            "entry_price": 100.0,
            "prediction": {"recommendation": "積極買入"},
        }], "metadata": {}}
        finmind = MagicMock()
        finmind._get_raw_prices.return_value = [110.0]
        n = m.auto_verify_predictions(telemetry, finmind, horizon=5)
        assert n == 1
        ar = telemetry["records"][0]["actual_result"]
        assert ar["actual_return_pct"] == 10.0
        assert ar["profit_pct"] == 10.0  # 相容性：保留既有欄位
        assert ar["was_correct"] is True
