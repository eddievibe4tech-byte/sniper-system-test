"""端到端場景測試：DecisionEngine 完整管線（FSM → FSM → BT → DecisionRecord）

對應用藍圖「驗收清單」中三個模擬場景：牛市突破開倉、止損清倉、熔斷禁交易。
"""
from datetime import datetime, timedelta, timezone

from src.quant_engine import DecisionEngine, EngineConfig, DecisionRecord
from src.quant_engine.blackboard import Blackboard, StockData, MarketState, PortfolioState

TZ = timezone(timedelta(hours=8))


def make_bb(stock=None, idx=1000, ma60=1000, ma200=950,
            peak=1_000_000, current=1_000_000):
    return Blackboard(market_index=idx, market_ma60=ma60, market_ma200=ma200,
                      peak_capital=peak, current_capital=current, stock=stock)


def test_scenario_bull_breakout_buy():
    """場景一：大盤進入牛市，個股突破 20MA → BUY 100% 標準部位"""
    engine = DecisionEngine(EngineConfig())
    now = datetime.now(TZ)
    bb = make_bb(stock=StockData(symbol="2330", current_price=105, cost_price=0,
                                 peak_price=0, ma20=100, has_position=False),
                 idx=1020)  # 站上季線+1% 與年線 → 牛市
    rec = engine.evaluate(bb, now)

    assert isinstance(rec, DecisionRecord)
    assert rec.action == "BUY"
    assert rec.position_size_pct == 1.0
    assert "牛市" in rec.market_state
    assert rec.config_version == "1.0.0"
    assert any("突破 20MA" in r for r in rec.reasons)


def test_scenario_stop_loss_full_clear():
    """場景二：持股跌破成本 8% → STOP_LOSS 全數清倉（優先於其他一切）"""
    engine = DecisionEngine(EngineConfig())
    now = datetime.now(TZ)
    bb = make_bb(stock=StockData(symbol="2317", current_price=90, cost_price=100,
                                 peak_price=100, ma20=95, has_position=True))
    rec = engine.evaluate(bb, now)

    assert rec.action == "STOP_LOSS"
    assert "止損" in rec.decision_text
    assert rec.position_size_pct == 0.0


def test_scenario_circuit_breaker_blocks_trading():
    """場景三：回撤 12% 觸發熔斷 → 之後即使牛市突破也只給 HOLD"""
    cfg = EngineConfig()
    engine = DecisionEngine(cfg)
    now = datetime.now(TZ)

    # 第一天：回撤 12% → 熔斷
    bb = make_bb(stock=StockData(symbol="2330", current_price=105, cost_price=0,
                                 peak_price=0, ma20=100, has_position=False),
                 idx=1020, peak=1_000_000, current=880_000)
    rec = engine.evaluate(bb, now)
    assert rec.portfolio_state == PortfolioState.CIRCUIT_BREAKER.value
    assert rec.action == "HOLD"
    assert any("熔斷" in r for r in rec.reasons)

    # 鎖定期內再評估：仍被禁止開倉
    bb2 = make_bb(stock=StockData(symbol="2330", current_price=105, cost_price=0,
                                  peak_price=0, ma20=100, has_position=False),
                  idx=1020, peak=1_000_000, current=880_000)
    bb2.locked_until = bb.locked_until
    bb2.portfolio_state = PortfolioState.CIRCUIT_BREAKER
    rec2 = engine.evaluate(bb2, now + timedelta(days=3))
    assert rec2.action == "HOLD"
    assert any("熔斷" in r for r in rec2.reasons)


def test_record_is_immutable_and_auditable():
    """DecisionRecord frozen：寫入後不可變異，且帶完整審計欄位"""
    engine = DecisionEngine(EngineConfig())
    bb = make_bb(stock=StockData(symbol="0050", current_price=100, ma20=99))
    rec = engine.evaluate(bb, datetime.now(TZ))

    try:
        rec.action = "HACK"
        raise AssertionError("DecisionRecord 應為 immutable")
    except Exception as e:
        if isinstance(e, AssertionError):
            raise
    assert rec.timestamp  # ISO 時間戳
    assert rec.symbol == "0050"
    assert isinstance(rec.reasons, tuple)


def test_reasons_accumulate_across_layers():
    """reasons 可追蹤整條管線：大盤進入熊市 + 個股止損都留痕。

    🛡️ Review 修訂版：改用「跨日」方式推進時間（now + n 天），並用
    evaluate_environment(force=True) 明確表達每日環境評估——同一天內
    重複呼叫會被冪等保護跳過，不再像舊測試那樣靠同日迴圈刷計數器。
    """
    engine = DecisionEngine(EngineConfig())
    now = datetime.now(TZ)
    # 先讓引擎在牛市運行一次
    bull_bb = make_bb(stock=StockData(symbol="2330", current_price=90, cost_price=100,
                                      peak_price=100, ma20=95, has_position=True),
                      idx=1020)
    engine.evaluate(bull_bb, now)
    assert bull_bb.market_state == MarketState.BULL

    # 大盤崩落：連續 3 個「交易日」跌破年線 → 第 3 天確認熊市
    bear_bb = make_bb(stock=StockData(symbol="2330", current_price=90, cost_price=100,
                                      peak_price=100, ma20=95, has_position=True),
                      idx=900, ma200=950)
    for day in range(1, 4):
        engine.evaluate_environment(bear_bb, now + timedelta(days=day), force=True)
    assert bear_bb.market_state == MarketState.BEAR
    assert any("熊市" in r for r in bear_bb.reasons)
    # 止損分支優先執行（不依賴大盤狀態）
    rec = engine.evaluate_stock(bear_bb, bear_bb.stock, now + timedelta(days=3))
    assert rec.action == "STOP_LOSS"


def test_environment_idempotent_same_day():
    """🛡️ 致命#1 回歸：同一天對同一黑板重複 evaluate_environment 只生效一次；
    且批次場景下環境評估一次 + 迴圈遍歷多檔，FSM 不被重複呼叫。"""
    engine = DecisionEngine(EngineConfig())
    now = datetime.now(TZ)
    bb = make_bb(idx=1015)  # 高於季線 +1% → 應入牛市

    first = engine.evaluate_environment(bb, now)
    assert first is True and bb.market_state == MarketState.BULL

    second = engine.evaluate_environment(bb, now)  # 同日第二次呼叫
    assert second is False                          # 冪等跳過
    assert bb.last_market_date == now.date().isoformat()

    # 模擬一日批次：1 次環境 + N 檔股票，行為樹結果互不污染
    stocks = [StockData(symbol=f"6{i:03d}", current_price=105, ma20=100) for i in range(5)]
    recs = [engine.evaluate_stock(bb, s, now) for s in stocks]
    assert all(r.action == "BUY" for r in recs)     # 全部突破 20MA
    assert all(len(r.reasons) >= 1 for r in recs)   # 每檔都有自己乾淨的理由鏈

def test_no_fabricated_ma_guardrail():
    """🛡️ 致命#3 精神驗證：若 ma60 被偽造成 price*0.98（恆定 +2.04%），
    任何跌勢都會被判牛市。此測試確保正常資料流下，跌破季線能正確退出牛市——
    （decision_runner 已移除偽造公式，改 yfinance 真實 MA60／ma50 降級鏈）"""
    engine = DecisionEngine(EngineConfig())
    now = datetime.now(TZ)
    bb = make_bb(idx=1015, ma60=1000, ma200=950)
    engine.evaluate_environment(bb, now, force=True)
    assert bb.market_state == MarketState.BULL
    # 真實市場下跌：指數掉到季線下方 1%（ma60 不動——真實均線不會跟著價格瞬移）
    bb.market_index = 990
    engine.evaluate_environment(bb, now + timedelta(days=1), force=True)
    assert bb.market_state == MarketState.SIDEWAYS  # 能退出 = 均線非偽造跟隨值
