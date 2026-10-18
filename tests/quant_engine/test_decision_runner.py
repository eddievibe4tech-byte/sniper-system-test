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
    """save → load 往復：所有跨日狀態完整保留（🔴1：tp 旗標為 {symbol:{cost_price,tp_taken}} 結構）"""
    monkeypatch.setattr(dr, "STATE_PATH", tmp_path / "engine_state.json")
    bb = Blackboard(market_index=900, market_ma60=1000, market_ma200=950,
                    peak_capital=1_200_000, current_capital=1_000_000)
    bb.below_ma200_days = 2
    bb.locked_until = "2026-11-01T00:00:00+08:00"
    bb.portfolio_state = PortfolioState.CIRCUIT_BREAKER
    bb.market_state = MarketState.BEAR
    bb.last_market_date = "2026-10-18"

    dr.save_engine_state(bb, {"2330": {"cost_price": 100.0, "tp_taken": True},
                              "2454": {"cost_price": 50.0, "tp_taken": True}})
    state = dr.load_engine_state()

    assert state["below_ma200_days"] == 2
    assert state["locked_until"] == "2026-11-01T00:00:00+08:00"
    assert state["peak_capital"] == 1_200_000
    assert state["market_state"] == "BEAR"
    assert state["portfolio_state"] == "CIRCUIT_BREAKER"
    assert state["last_market_date"] == "2026-10-18"
    assert set(state["tp_taken_symbols"].keys()) == {"2330", "2454"}
    # 🛡️ 🔴1：成本必須一併持久化，供隔天比對
    assert state["tp_taken_symbols"]["2330"]["cost_price"] == 100.0


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
    # 🛡️ 🔴1：旗標綁定觸發時的 cost_price（由 ActionTakeProfit 寫入 stock.tp_taken_cost_price）
    assert stock1.tp_taken_cost_price == 100
    dr.save_engine_state(bb, {s.symbol: {"cost_price": s.tp_taken_cost_price,
                                         "tp_taken": True}
                              for s in [stock1] if s.tp_taken})

    # Day 2：隔天另一個全新 Runner，用同一份個股數據 + 還原的 tp_taken_symbols
    state = dr.load_engine_state()
    restored = StockData(symbol="2330", current_price=105, cost_price=100,
                         peak_price=120, ma20=100, has_position=True,
                         tp_taken=dr._tp_flag_for("2330", 100.0, state["tp_taken_symbols"]))
    engine2 = DecisionEngine(cfg)
    rec2 = engine2.evaluate_stock(bb, restored, datetime.now(TZ) + timedelta(days=1))
    assert rec2.action != "TAKE_PROFIT"  # 🛡️ 不會連續三天重複發 50% 平倉訊號


def test_tp_taken_reset_on_reposition(tmp_path, monkeypatch):
    """🛡️ Review 🔴1 驗收情境「換手測試」：停利觸發後賣出再買回（cost_price 改變）
    → 隔天 tp_taken 必須重置為 False，停利保護恢復——絕不「終身免疫」"""
    monkeypatch.setattr(dr, "STATE_PATH", tmp_path / "engine_state.json")
    cfg = EngineConfig()

    # Day 1：2330 以成本 100 觸發移動停利 → 持久化 {cost_price:100, tp_taken:true}
    stock1 = StockData(symbol="2330", current_price=105, cost_price=100,
                       peak_price=120, ma20=100, has_position=True)
    bb = Blackboard(market_index=1000, market_ma60=1000, market_ma200=950,
                    peak_capital=1_000_000, current_capital=1_000_000)
    rec1 = DecisionEngine(cfg).evaluate_stock(bb, stock1, datetime.now(TZ))
    assert rec1.action == "TAKE_PROFIT" and stock1.tp_taken
    dr.save_engine_state(bb, {"2330": {"cost_price": stock1.tp_taken_cost_price,
                                       "tp_taken": True}})

    state = dr.load_engine_state()
    # Day 2a：同一倉位（cost 仍 100）→ 旗標維持 True，不重複平倉
    assert dr._tp_flag_for("2330", 100.0, state["tp_taken_symbols"]) is True

    # Day 2b：使用者清仓後於 80 元重新買入（換手）→ 旗標必須作廢
    assert dr._tp_flag_for("2330", 80.0, state["tp_taken_symbols"]) is False

    # Day 2c：加碼摊平使 cost 變 90 → 同樣視為新倉位，重置
    assert dr._tp_flag_for("2330", 90.0, state["tp_taken_symbols"]) is False

    # 端到端驗證：換手後的股票若再次符合停利條件，應能再次觸發
    reentry = StockData(symbol="2330", current_price=84, cost_price=80,
                        peak_price=100, ma20=80, has_position=True,
                        tp_taken=dr._tp_flag_for("2330", 80.0, state["tp_taken_symbols"]))
    rec3 = DecisionEngine(cfg).evaluate_stock(bb, reentry, datetime.now(TZ) + timedelta(days=1))
    assert rec3.action == "TAKE_PROFIT"  # 🛡️ 停利保護未被「終身免疫」


def test_legacy_list_format_tp_symbols_compat(tmp_path, monkeypatch):
    """向後相容：舊版 List[str] 格式 state 檔仍可載入（正規化為 dict、cost=0）"""
    monkeypatch.setattr(dr, "STATE_PATH", tmp_path / "engine_state.json")
    _write(tmp_path / "engine_state.json",
           {"peak_capital": 1e6, "current_capital": 1e6, "tp_taken_symbols": ["2330"]})
    state = dr.load_engine_state()
    assert state["tp_taken_symbols"] == {"2330": {"cost_price": 0.0, "tp_taken": True}}
    # cost_price=0 的舊記錄遇 cost=0 的股票 → 保守視為同批倉位仍阻擋重複平倉
    assert dr._tp_flag_for("2330", 0.0, state["tp_taken_symbols"]) is True
    # 但一旦有真實成本且不同 → 重置
    assert dr._tp_flag_for("2330", 100.0, state["tp_taken_symbols"]) is False


def test_corrupted_state_file_recovers(tmp_path, monkeypatch):
    """🛡️ Review 🟡3 驗收情境「損壞恢復測試」：半寫入的損壞 JSON →
    不崩潰（無 Traceback）、log warning、以初始狀態繼續、執行後檔案恢復正常格式"""
    bad = tmp_path / "engine_state.json"
    bad.write_text('{ "peak_capital": 100,', encoding="utf-8")  # 模擬中途中斷的半成品
    monkeypatch.setattr(dr, "STATE_PATH", bad)

    import logging
    records = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r.getMessage())
    dr.logger.addHandler(handler)
    try:
        state = dr.load_engine_state()  # 不應拋出例外
    finally:
        dr.logger.removeHandler(handler)

    assert state["peak_capital"] == 1_000_000          # 重置為初始預設
    assert state["below_ma200_days"] == 0
    assert any("損壞" in m or "重置" in m for m in records)  # 有 warning 留痕

    # 後續 save 讓檔案恢復合法 JSON（且經原子寫入，不留 tmp 垃圾）
    bb = Blackboard(market_index=1000, market_ma60=1000, market_ma200=950,
                    peak_capital=1_000_000, current_capital=1_000_000)
    dr.save_engine_state(bb, {})
    reparsed = json.loads(bad.read_text(encoding="utf-8"))
    assert reparsed["peak_capital"] == 1_000_000
    assert not (tmp_path / "engine_state.json.tmp").exists()


def test_atomic_write_no_partial_file(tmp_path):
    """🛡️ 直接驗證 atomic_write_json：正常情況只出現完整檔、暫存檔被 rename 吃掉"""
    p = tmp_path / "x.json"
    dr.atomic_write_json(p, {"a": 1, "中文": "值得"})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "中文": "值得"}
    assert not (tmp_path / "x.json.tmp").exists()


def test_extract_market_data_no_fabricated_ma(tmp_path, monkeypatch):
    """🛡️ 致命#3：yfinance 失敗降級時也不得出現 price*0.98 偽造季線；
    有 ma50 用真實 ma50，全缺則中性降級（帶 +2% 緩衝，ma60 ≠ ma200）"""
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

    # 🛡️ Review 🟡2 驗收情境「降級鏈測試」：連 ma50 都沒有 → 中性降級必須保留
    # 緩衝帶（ma60 := ma200*1.02），不可 ma60==ma200 令牛市條件與年線同義反覆
    vc["etf_monitor"][0].pop("ma50")
    _write(data_dir / "value_candidates.json", vc)
    m2 = dr.extract_market_data({})
    assert m2["ma60_source"] == "neutral_fallback"
    assert abs(m2["ma60"] - 90.0 * 1.02) < 1e-9
    assert m2["ma60"] > m2["ma200"]               # SIDEWAYS 狀態空間未被消滅

    # 在緩衝帶下：價格 merely 高於年線(+1%) 不足以進牛（需 > ma200*1.02*1.01）
    from src.quant_engine.blackboard import Blackboard, MarketState
    from src.quant_engine.fsm.market import MarketFSM
    from src.quant_engine.config import MarketFSMConfig
    fsm = MarketFSM(MarketFSMConfig())
    bb = Blackboard(market_index=90.9, market_ma60=m2["ma60"], market_ma200=90.0,
                    peak_capital=1e6, current_capital=1e6)   # 年線上 +1%，但 < ma60*1.01
    fsm.update(bb, trading_days_below=0)
    assert bb.market_state == MarketState.SIDEWAYS            # 🛡️ 震盪緩衝仍在


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
