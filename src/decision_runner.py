"""橋接層：讀取現有 JSON 數據 → 轉成 Blackboard → 呼叫 DecisionEngine → 輸出 data/decisions.json

🔌 資料源對應（與既有 daily_analysis.yml 產線一致）：
- 大盤狀態：data/value_candidates.json 的 etf_monitor 中「台股大盤」（0050 / 006208）
  之 price / ma200；MA60 優先由 yfinance 真實歷史收盤計算，失敗降級 ma50。
- 個股清單：data/deep_analysis.json all_results（code / current_price / ma20）
- 跨日狀態：data/engine_state.json —— 🛡️ GitHub Actions 是無狀態拋棄式 Runner，
  FSM 實例記憶體每日重置，故熊市連日計數、熔斷鎖、帳戶峰值、停利平倉旗標
  一律持久化至此檔，每次執行開始載入、結束寫回（Review 致命缺陷 #2/#4）。

持倉成本／是否持倉目前不在任何 data/*.json 中（存在於前端 localStorage），
故 cost_price=0、has_position=False —— 開市買進分支由 FSM 的大盤狀態把關，
止損／停利分支需等 telemetry／localStorage 打通後才會實際觸發（見下方 TODO）。

🛡️ Review 修正重點：
1. 致命#1：evaluate_environment() 每天只跑一次，迴圈內僅 evaluate_stock() 遍歷個股
2. 致命#2：engine_state.json 持久化 below_ma200_days / locked_until / peak_capital / tp_taken
3. 致命#3：移除 price*0.98 偽造季線——改由 yfinance 真實歷史算 MA60，降級 ma50（真實值）
4. 致命#4：tp_taken_symbols 持久化，移動停利不會隔天重複發出平倉訊號
"""
import json
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

try:  # 作為 package 執行（python -m src.decision_runner）
    from src.quant_engine import DecisionEngine, EngineConfig
    from src.quant_engine.blackboard import Blackboard, StockData, MarketState, PortfolioState
except ImportError:  # 在 src/ 目錄內直接執行
    from quant_engine import DecisionEngine, EngineConfig
    from quant_engine.blackboard import Blackboard, StockData, MarketState, PortfolioState

logger = logging.getLogger(__name__)
TZ = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATE_PATH = DATA_DIR / "engine_state.json"   # 🛡️ 跨日引擎狀態（提交回 repo 供隔日 CI 載入）

# 🛡️ 致命#3：已移除 price*0.98 偽造季線。yfinance 不可用時的最终降級採中性值
# （ma60=ma200 → 牛市 +1% 門檻幾乎不可能滿足 → 寧可保守震盪，也絕不假牛市）
FALLBACK_MA60_RATIO = None


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception as e:
        logger.warning(f"讀取 {path} 失敗: {e}")
        return default


def fetch_real_ma60(symbol: str) -> tuple:
    """🛡️ 致命#3：從真實歷史收盤序列計算 MA60 與「連續跌破年線交易日數」。

    Returns (ma60, trading_days_below_ma200)，取得失敗時回傳 (0.0, None)。
    """
    try:
        import pandas as pd  # noqa: F401  (value_screener 同產線已依賴)
        import yfinance as yf
        hist = yf.Ticker(f"{symbol}.TW").history(period="1y")
        closes = hist["Close"].dropna()
        if len(closes) < 60:
            raise ValueError(f"歷史資料不足 ({len(closes)} 天)")
        ma60 = float(closes.rolling(60).mean().iloc[-1])
        ma200_val = closes.rolling(200).mean().iloc[-1]
        # 少於 200 天時以全期均值近似年線（與 value_screener 退版邏輯一致）
        ma200_hist = float(ma200_val) if ma200_val == ma200_val else float(closes.mean())
        below = (closes < ma200_hist)  # pd.Series[bool]
        days_below = 0
        for flag in reversed(list(below)):
            if bool(flag):
                days_below += 1
            else:
                break
        return ma60, days_below
    except Exception as e:
        logger.warning(f"⚠️ 取得 {symbol} 真實 MA60/歷史失敗（降級處理）: {e}")
        return 0.0, None


def extract_market_data(state: Dict) -> Dict:
    """從 value_candidates.json 的 ETF 雷達提取大盤位置（優先 0050，其次 006208）

    🛡️ 致命#3：MA60 不再使用 price*0.98 偽造——優先 yfinance 真實 60 日均線，
    次優先 JSON 欄位（ma60→ma50，皆為真實計算值），最後才中性降級（ma60=ma200）。
    """
    vc = load_json(DATA_DIR / "value_candidates.json", {})
    tw_etfs = [e for e in (vc.get("etf_monitor") or [])
               if e.get("market") == "🇹🇼" and e.get("type") == "台股大盤"]
    by_symbol = {e.get("symbol"): e for e in tw_etfs}
    anchor = by_symbol.get("0050") or by_symbol.get("006208") or (tw_etfs[0] if tw_etfs else {})

    price = float(anchor.get("price") or 0)
    ma200 = float(anchor.get("ma200") or price)

    real_ma60, days_below = fetch_real_ma60(anchor.get("symbol", "0050"))
    if real_ma60 > 0:
        ma60, ma60_src = real_ma60, "yfinance_60d"
    elif anchor.get("ma60"):
        ma60, ma60_src = float(anchor["ma60"]), "json_ma60"
    elif anchor.get("ma50"):
        # ma50 是 value_screener 用真實價格序列算出的均線（非偽造），作近似降級
        ma60, ma60_src = float(anchor["ma50"]), "json_ma50_approx"
    else:
        # 中性降級：ma60 := ma200，使牛市 +1% 門檻近乎不可達，FSM 傾向維持震盪
        ma60, ma60_src = (ma200 if ma200 > 0 else 0), "neutral_fallback"

    return {"index": price, "ma60": ma60, "ma200": ma200, "ma60_source": ma60_src,
            "bias_pct": float(anchor.get("bias_ma200_pct") or 0),
            "anchor_symbol": anchor.get("symbol", ""),
            "trading_days_below": days_below}  # None → FSM 改用持久化計數器


def extract_stocks(state: Dict) -> List[StockData]:
    """從 deep_analysis.json 提取個股技術面數據（無持倉資訊 → 保守預設）

    🛡️ 致命#4：tp_taken 由 engine_state.json 的 tp_taken_symbols 還原，
    停利平倉旗標跨日保留，不會隔天重複觸發部分平倉。
    """
    deep = load_json(DATA_DIR / "deep_analysis.json", {})
    tp_taken_symbols = set(state.get("tp_taken_symbols") or [])
    stocks = []
    for r in (deep.get("all_results") or []):
        price = float(r.get("current_price") or 0)
        if price <= 0:
            continue
        symbol = str(r.get("code", ""))
        stocks.append(StockData(
            symbol=symbol,
            current_price=price,
            cost_price=0.0,      # TODO: 從 telemetry / localStorage 取得持倉成本
            peak_price=price,
            ma20=float(r.get("ma20") or 0),
            has_position=False,  # TODO: 從 telemetry 判斷是否持倉
            tp_taken=symbol in tp_taken_symbols,  # 🛡️ 跨日持久化旗標
        ))
    return stocks


def load_engine_state() -> Dict:
    """🛡️ 致命#2/#4：載入跨日引擎狀態（CI 無狀態環境下唯一的記憶來源）"""
    state = load_json(STATE_PATH, {}) or {}
    return {
        "peak_capital": float(state.get("peak_capital", 1_000_000)),
        "current_capital": float(state.get("current_capital", 1_000_000)),
        "consecutive_losses": int(state.get("consecutive_losses", 0)),
        "locked_until": state.get("locked_until"),
        "below_ma200_days": int(state.get("below_ma200_days", 0)),
        "market_state": state.get("market_state"),
        "portfolio_state": state.get("portfolio_state"),
        "last_market_date": state.get("last_market_date"),
        "tp_taken_symbols": list(state.get("tp_taken_symbols") or []),
    }


def save_engine_state(bb: Blackboard, tp_taken_symbols: List[str]):
    """🛡️ 致命#2：執行結束前必須將更新後的狀態寫回磁碟（提交回 repo 供隔日使用）"""
    snapshot = {
        "updated_at": datetime.now(TZ).isoformat(),
        "peak_capital": bb.peak_capital,
        "current_capital": bb.current_capital,
        "consecutive_losses": bb.consecutive_losses,
        "locked_until": bb.locked_until,
        "below_ma200_days": bb.below_ma200_days,
        "market_state": bb.market_state.name,
        "portfolio_state": bb.portfolio_state.name,
        "last_market_date": bb.last_market_date,
        "tp_taken_symbols": sorted(tp_taken_symbols),
    }
    STATE_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"💾 引擎狀態已寫回 {STATE_PATH}（熊市計數={bb.below_ma200_days}、"
                f"熔斷鎖={bb.locked_until}、peak={bb.peak_capital}）")


def run_decision_engine():
    logging.basicConfig(level=logging.INFO)
    logger.info("🎯 啟動決策引擎...")

    cfg = EngineConfig()
    engine = DecisionEngine(cfg)
    now = datetime.now(TZ)

    state = load_engine_state()
    market = extract_market_data(state)
    stocks = extract_stocks(state)

    if market["index"] <= 0:
        logger.warning("⚠️ 大盤數據缺失（value_candidates.json 無台股大盤 ETF），FSM 將維持震盪預設")

    # ── 組黑板：還原跨日持久化狀態 ────────────────────────────────
    bb = Blackboard(
        market_index=market["index"],
        market_ma60=market["ma60"],
        market_ma200=market["ma200"],
        peak_capital=state["peak_capital"],
        current_capital=state["current_capital"],
        consecutive_losses=state["consecutive_losses"],
        locked_until=state.get("locked_until"),
        below_ma200_days=state["below_ma200_days"],
        last_market_date=state.get("last_market_date"),
    )
    if state.get("market_state") in MarketState.__members__:
        bb.market_state = MarketState[state["market_state"]]
    if state.get("portfolio_state") in PortfolioState.__members__:
        bb.portfolio_state = PortfolioState[state["portfolio_state"]]

    # 🛡️ 致命#1：宏觀環境態勢——一天只需要且只能評估一次（冪等保護在 engine 內部）
    env_reasons_before = len(bb.reasons)
    evaluated = engine.evaluate_environment(bb, now,
                                            trading_days_below=market.get("trading_days_below"))
    env_reasons = list(bb.reasons[env_reasons_before:]) if evaluated else []
    bb.reasons = []  # 環境層理由不外洵到個股記錄
    logger.info(f"🌍 環境態勢：{bb.market_state.value} / {bb.portfolio_state.value}"
                f"（本次{'已' if evaluated else '未（今日已評估過，冪等跳過）'}更新）")

    # ── 迴圈遍歷個股：純行為樹評估，不觸碰宏觀 FSM ─────────────────
    decisions: List[dict] = []
    for stock in stocks:
        record = engine.evaluate_stock(bb, stock, now)
        decisions.append({
            "timestamp": record.timestamp,
            "symbol": record.symbol,
            "market_state": record.market_state,
            "portfolio_state": record.portfolio_state,
            "action": record.action,
            "decision_text": record.decision_text,
            "position_size_pct": record.position_size_pct,
            "config_version": record.config_version,
            "reasons": list(record.reasons),
        })

    # 🛡️ 致命#4：把本輪所有 tp_taken=True 的符號收集起來持久化
    tp_taken_symbols = [s.symbol for s in stocks if s.tp_taken]

    output = {
        "updated_at": now.isoformat(),
        "environment": {
            "market_state": bb.market_state.name,
            "market_state_label": bb.market_state.value,
            "portfolio_state": bb.portfolio_state.name,
            "portfolio_state_label": bb.portfolio_state.value,
            "below_ma200_days": bb.below_ma200_days,
            "locked_until": bb.locked_until,
            "peak_capital": bb.peak_capital,
            "drawdown_pct": round((bb.peak_capital - bb.current_capital) / bb.peak_capital * 100, 2)
            if bb.peak_capital > 0 else 0.0,
            "reasons": env_reasons,
        },
        "market_snapshot": market,
        "decisions": decisions,
        "config": {
            "market": json.loads(json.dumps(cfg.market, default=lambda o: o.__dict__)),
            "portfolio": json.loads(json.dumps(cfg.portfolio, default=lambda o: o.__dict__)),
            "execution": json.loads(json.dumps(cfg.execution, default=lambda o: o.__dict__)),
            "version": cfg.version,
        },
    }

    out_path = DATA_DIR / "decisions.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"✅ 決策引擎完成：{len(decisions)} 筆決策已寫入 {out_path}")

    # 🛡️ 致命#2：狀態寫回（在 decisions.json 之後——若中途崩潰，舊狀態仍自洽）
    save_engine_state(bb, tp_taken_symbols)
    return output


if __name__ == "__main__":
    run_decision_engine()
