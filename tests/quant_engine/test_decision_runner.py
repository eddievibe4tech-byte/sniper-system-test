"""🛡️ Review 致命缺陷 #2/#4 回歸測試：decision_runner 跨日狀態持久化

模擬 GitHub Actions 無狀態 Runner：每次執行都是「新 process、新 FSM 實例」，
唯一延續記憶的是 data/engine_state.json。本測試驗證：
1. save/load engine_state 往復一致（熊市計數、熔斷鎖、peak、tp_taken_symbols）
2. tp_taken 旗標跨日還原 → 移動停利不會隔天重複觸發部分平倉
3. extract_market_data 不再產生 price*0.98 偽造季線（致命#3）
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import decision_runner as dr
from src.quant_engine.blackboard import Blackboard, StockData, MarketState, PortfolioState
from src.quant_engine.engine import DecisionEngine
from src.quant_engine.config import EngineConfig

TZ = timezone(timedelta(hours=8))


def _write(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def test_engine_state_roundtrip(tmp_path, monkeypatch):
    """save → load 往復：所有跨日狀態完整保留"""
    monkeypatch.setattr(dr, "STATE_PATH", tmp_path / "engine_state.json")
    bb = Blackboard(market_index=900, market_ma60=1000, market_ma200=950,
                    peak_capital=1_200_000, current_capital=1_000_000)
    bb.below_ma200_days = 2
    bb.locked_until = "2026-11-01T00:00:00+08:00"
    bb.portfolio_state = PortfolioState.CIRCUIT_BREAKER
    bb.market_state = MarketState.BEAR
    bb.last_market_date = "2026-10-18"

    dr.save_engine_state(bb, ["2330", "2454"])
    state = dr.load_engine_state()

    assert state["below_ma200_days"] == 2
    assert state["locked_until"] == "2026-11-01T00:00:00+08:00"
    assert state["peak_capital"] == 1_200_000
    assert state["market_state"] == "BEAR"
    assert state["portfolio_state"] == "CIRCUIT_BREAKER"
    assert state["last_market_date"] == "2026-10-18"
    assert set(state["tp_taken_symbols"]) == {"2330", "2454"}


def test_tp_taken_persists_across_days(tmp_path, monkeypatch):
    """🛡️ 致命#4：第一天觸發停利後寫回狀態；第二天全新引擎載入 → 不再重複觸發"""
    monkeypatch.setattr(dr, "STATE_PATH", tmp_path / "engine_state.json")
    cfg = EngineConfig()

    # Day 1：持股獲利回檔 → TAKE_PROFIT，tp_taken 置 True 並持久化
    stock1 = StockData(symbol="2330", current_price=105, cost_price=100,
                       peak_price=120, ma20=100, has_position=True, tp_taken=False)
    bb = Blackboard(market_index=1000, market_ma60=1000, market_ma200=950,
                    peak_capital=1_000_000, current_capital=1_000_000)
    engine1 = DecisionEngine(cfg)  # 全新實例＝全新 Runner
    rec1 = engine1.evaluate_stock(bb, stock1, datetime.now(TZ))
    assert rec1.action == "TAKE_PROFIT"
    dr.save_engine_state(bb, [s.symbol for s in [stock1] if s.tp_taken])

    # Day 2：隔天另一個全新 Runner，用同一份個股數據 + 還原的 tp_taken_symbols
    state = dr.load_engine_state()
    restored = StockData(symbol="2330", current_price=105, cost_price=100,
                         peak_price=120, ma20=100, has_position=True,
                         tp_taken=("2330" in state["tp_taken_symbols"]))
    engine2 = DecisionEngine(cfg)
    rec2 = engine2.evaluate_stock(bb, restored, datetime.now(TZ) + timedelta(days=1))
    assert rec2.action != "TAKE_PROFIT"  # 🛡️ 不會連續三天重複發 50% 平倉訊號


def test_extract_market_data_no_fabricated_ma(tmp_path, monkeypatch):
    """🛡️ 致命#3：yfinance 失敗降級時也不得出現 price*0.98 偽造季線；
    有 ma50 用真實 ma50，全缺則中性 ma60:=ma200（牛市門檻近乎不可達）"""
    vc = {"etf_monitor": [{"symbol": "0050", "market": "🇹🇼", "type": "台股大盤",
                           "price": 100.0, "ma200": 90.0, "ma50": 97.0,
                           "bias_ma200_pct": 11.1}]}
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write(data_dir / "value_candidates.json", vc)
    monkeypatch.setattr(dr, "DATA_DIR", data_dir)
    monkeypatch.setattr(dr, "fetch_real_ma60", lambda symbol: (0.0, None))  # 模擬網路失敗

    m = dr.extract_market_data({})
    assert m["ma60"] == 97.0                      # 降級用真實 ma50，非 100*0.98=98
    assert m["ma60_source"] == "json_ma50_approx"

    # 連 ma50 都沒有 → 中性降級 ma60 := ma200
    vc["etf_monitor"][0].pop("ma50")
    _write(data_dir / "value_candidates.json", vc)
    m2 = dr.extract_market_data({})
    assert m2["ma60"] == 90.0 and m2["ma60_source"] == "neutral_fallback"


def test_run_end_to_end_writes_state(tmp_path, monkeypatch):
    """端到端：run_decision_engine 產出 decisions.json + environment 區塊 + engine_state.json"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write(data_dir / "value_candidates.json",
           {"etf_monitor": [{"symbol": "0050", "market": "🇹🇼", "type": "台股大盤",
                             "price": 100.0, "ma200": 90.0, "ma50": 97.0,
                             "bias_ma200_pct": 11.1}]})
    _write(data_dir / "deep_analysis.json",
           {"all_results": [{"code": "2330", "current_price": 105, "ma20": 100},
                           {"code": "2454", "current_price": 90, "ma20": 100}]})
    monkeypatch.setattr(dr, "DATA_DIR", data_dir)
    monkeypatch.setattr(dr, "STATE_PATH", data_dir / "engine_state.json")
    monkeypatch.setattr(dr, "fetch_real_ma60", lambda symbol: (104.5, 0))  # 牛市情境：100 > 104.5*1.01? 否 → 見下方斷言

    out = dr.run_decision_engine()
    assert (data_dir / "decisions.json").exists()
    assert (data_dir / "engine_state.json").exists()
    assert len(out["decisions"]) == 2
    assert "environment" in out
    # 大盤 100 < MA60(104.5) → 非牛市 → 禁止開倉，全部 HOLD（且理由鏈乾淨、無污染）
    assert out["environment"]["market_state"] == "SIDEWAYS"
    actions = {d["symbol"]: d["action"] for d in out["decisions"]}
    assert actions == {"2330": "HOLD", "2454": "HOLD"}
    by_symbol = {d["symbol"]: d for d in out["decisions"]}
    # 🛡️ Review R3：每檔 HOLD 都必須有可辨識的理由（非空、且含「大盤非牛市」攔截留痕）
    assert any("大盤非牛市" in r for r in by_symbol["2330"]["reasons"])
    assert all(len(d["reasons"]) >= 1 for d in out["decisions"])
    # 冪等保護：同日第二次執行不該再推任何 FSM 狀態
    second = dr.run_decision_engine()
    assert second["environment"]["reasons"] == []
