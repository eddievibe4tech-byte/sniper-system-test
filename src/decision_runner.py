"""橋接層：讀取現有 JSON 數據 → 轉成 Blackboard → 呼叫 DecisionEngine → 輸出 data/decisions.json

🔌 資料源對應（與既有 daily_analysis.yml 產線一致）：
- 大盤狀態：data/value_candidates.json 的 etf_monitor 中「台股大盤」（0050 / 006208）
  之 price / ma200；MA60 優先由 yfinance 真實歷史收盤計算，失敗降級 ma50。
- 個股清單：data/deep_analysis.json all_results（code / current_price / ma20）
- 跨日狀態：data/engine_state.json —— 🛡️ GitHub Actions 是無狀態拋棄式 Runner，
  FSM 實例記憶體每日重置，故熊市連日計數、熔斷鎖、帳戶峰值、停利平倉旗標
  一律持久化至此檔，每次執行開始載入、結束原子寫回（Review 致命缺陷 #2/#4）。

持倉成本／是否持倉目前不在任何 data/*.json 中（存在於前端 localStorage），
故 cost_price=0、has_position=False —— 開市買進分支由 FSM 的大盤狀態把關，
止損／停利分支需等 telemetry／localStorage 打通後才會實際觸發（見下方 TODO）。

🛡️ Review 修正重點：
1. 致命#1：evaluate_environment() 每天只跑一次，迴圈內僅 evaluate_stock() 遍歷個股
2. 致命#2：engine_state.json 持久化 below_ma200_days / locked_until / peak_capital / tp_taken
3. 致命#3：移除 price*0.98 偽造季線——改由 yfinance 真實歷史算 MA60，降級 ma50（真實值）
4. 致命#4：tp_taken_symbols 持久化，移動停利不會隔天重複發出平倉訊號

🛡️ 本輪 Code Review 微調（三致命＋兩優化）：
- 🔴 tp_taken 生命週期：state 結構改為 {symbol: {"cost_price": X, "tp_taken": true}}，
  還原時比對 cost_price，換手／加碼即重置旗標——絕不「終身免疫」停利保護
- 🟡 MA60 中性降級不再令 ma60==ma200（消滅 SIDEWAYS 狀態空間），改帶 +2% 緩衝帶
- 🟡 engine_state.json 改「暫存檔 + os.replace」原子寫入；載入端抵禦半寫入損壞 JSON
- 🟢 Pandas 判空改用 pd.isna（取代 val==val 黑魔法）
- 🟢 dataclass 序列化改用 dataclasses.asdict（取代 json.dumps(default=__dict__)）
- 🟡 reasons 分層：關鍵理由（驅動最終決策）前置、過程性攔截理由其後，前端 slice(0,2) 只顯關鍵
"""
import dataclasses
import json
import logging
import os
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

# 🛡️ Review 🟡2：中性降級給 MA60 人為加 2% 緩衝帶（ma60 := ma200 * 1.02）。
# 若直接 ma60 := ma200，則 idx > ma60 與 idx > ma200 同義反覆，SIDEWAYS 狀態空間
# 被壓縮——股價只要在年線之上就會被判牛市，失去震盪市緩衝保護。
NEUTRAL_MA60_BUFFER = 0.02


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception as e:
        logger.warning(f"讀取 {path} 失敗: {e}")
        return default


def atomic_write_json(path: Path, obj) -> None:
    """🛡️ Review 🟡3：先寫暫存檔再 os.replace 原子置換。

    GitHub Actions Runner 可能在寫入途中被中斷（workflow 超時／手動取消），
    直接 write_text 會留下半寫入的損壞 JSON，讓隔天整個決策引擎崩潰。
    os.replace 在同磁碟區是原子操作：要嘛舊檔完整、要嘛新檔完整。
    """
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def fetch_real_ma60(symbol: str) -> tuple:
    """🛡️ 致命#3：從真實歷史收盤序列計算 MA60 與「連續跌破年線交易日數」。

    Returns (ma60, trading_days_below)，取得失敗時回傳 (0.0, None)。
    """
    try:
        import pandas as pd
        import yfinance as yf
        hist = yf.Ticker(f"{symbol}.TW").history(period="1y")
        closes = hist["Close"].dropna()
        if len(closes) < 60:
            raise ValueError(f"歷史資料不足 ({len(closes)} 天)")
        ma60 = float(closes.rolling(60).mean().iloc[-1])
        ma200_val = closes.rolling(200).mean().iloc[-1]
        # 🛡️ Review 🟢1：pd.isna 取代 val==val NaN 黑魔法，可讀性更高
        # 少於 200 天時以全期均值近似年線（與 value_screener 退版邏輯一致）
        ma200_hist = float(ma200_val) if not pd.isna(ma200_val) else float(closes.mean())
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
    次優先 JSON 欄位（ma60→ma50，皆為真實計算值），最後才中性降級。
    🛡️ Review 🟡2：中性降級 ma60 := ma200*(1+2%) 保留緩衝帶，避免 ma60==ma200
    使牛市條件與年線條件同義反覆、消滅 SIDEWAYS 狀態空間。
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
        # 🛡️ Review 🟡2：中性降級帶 +2% 緩衝——牛市需 idx > ma200*1.02*1.01，
        # 遠離季線的極端漲勢才可能進牛，一般情況 FSM 傾向維持震盪緩衝
        ma60, ma60_src = (ma200 * (1 + NEUTRAL_MA60_BUFFER) if ma200 > 0 else 0), "neutral_fallback"

    return {"index": price, "ma60": ma60, "ma200": ma200, "ma60_source": ma60_src,
            "bias_pct": float(anchor.get("bias_ma200_pct") or 0),
            "anchor_symbol": anchor.get("symbol", ""),
            "trading_days_below": days_below}  # None → FSM 改用持久化計數器


def _tp_flag_for(symbol: str, cost_price: float, tp_map: Dict) -> bool:
    """🛡️ Review 🔴1：tp_taken 必須與當前持倉的 cost_price 綁定。

    engine_state.json 結構：{"2330": {"cost_price": 100.0, "tp_taken": true}}
    - symbol 不在表中 → False
    - cost_price 與觸發時不一致（賣出換手／加碼摊平）→ 視為新倉位，重置 False
      （否則該股票將「終身免疫」移動停利保護）
    """
    entry = tp_map.get(symbol)
    if not isinstance(entry, dict) or not entry.get("tp_taken"):
        return False
    saved_cost = float(entry.get("cost_price", 0.0))
    if abs(saved_cost - cost_price) > 1e-9:
        logger.info(f"🔄 {symbol} 持倉成本變動（{saved_cost} → {cost_price}，換手/加碼）"
                    f"→ tp_taken 旗標重置，停利保護恢復")
        return False
    return True


def extract_stocks(state: Dict) -> List[StockData]:
    """從 deep_analysis.json 提取個股技術面數據（無持倉資訊 → 保守預設）

    🛡️ 致命#4：tp_taken 由 engine_state.json 的 tp_taken_symbols 還原，
    停利平倉旗標跨日保留，不會隔天重複觸發部分平倉。
    🛡️ Review 🔴1：還原時比對 cost_price，換手即重置旗標（見 _tp_flag_for）。
    """
    deep = load_json(DATA_DIR / "deep_analysis.json", {})
    tp_map = _normalize_tp_map(state.get("tp_taken_symbols"))
    stocks = []
    for r in (deep.get("all_results") or []):
        price = float(r.get("current_price") or 0)
        if price <= 0:
            continue
        symbol = str(r.get("code", ""))
        cost_price = float(r.get("cost_price") or 0.0)  # TODO: telemetry/localStorage 打通後填實值
        stocks.append(StockData(
            symbol=symbol,
            current_price=price,
            cost_price=cost_price,
            peak_price=price,
            ma20=float(r.get("ma20") or 0),
            has_position=False,  # TODO: 從 telemetry 判斷是否持倉
            tp_taken=_tp_flag_for(symbol, cost_price, tp_map),  # 🛡️ 跨日持久化＋成本綁定
        ))
    return stocks


def _normalize_tp_map(raw) -> Dict[str, Dict]:
    """抵禦舊格式（List[str]）與髒資料，一律正規化為 {symbol: {cost_price, tp_taken}}"""
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    if isinstance(raw, list):  # 上一版的 ["2330", ...] 舊格式向後相容
        return {str(s): {"cost_price": 0.0, "tp_taken": True} for s in raw}
    return {}


def load_engine_state() -> Dict:
    """🛡️ 致命#2/#4：載入跨日引擎狀態（CI 無狀態環境下唯一的記憶來源）

    🛡️ Review 🟡3：若檔案被中途打斷成半寫入損壞 JSON，load_json 回傳 None →
    此處自動退回初始狀態並 log warning，絕不讓程式崩潰（配合 atomic_write_json
    雙重防護：正常流程根本不會產生半寫入檔）。
    """
    raw = load_json(STATE_PATH, None)
    if raw is None:
        if STATE_PATH.exists():
            logger.warning(f"⚠️ {STATE_PATH} 缺失或損壞（疑似上次執行被中斷）→ 重置為初始狀態繼續運行")
        raw = {}
    state = {
        "peak_capital": float(raw.get("peak_capital", 1_000_000)),
        "current_capital": float(raw.get("current_capital", 1_000_000)),
        "consecutive_losses": int(raw.get("consecutive_losses", 0)),
        "locked_until": raw.get("locked_until"),
        "below_ma200_days": int(raw.get("below_ma200_days", 0)),
        "market_state": raw.get("market_state"),
        "portfolio_state": raw.get("portfolio_state"),
        "last_market_date": raw.get("last_market_date"),
        "tp_taken_symbols": _normalize_tp_map(raw.get("tp_taken_symbols")),
    }
    return state


def save_engine_state(bb: Blackboard, tp_flags: Dict[str, Dict]):
    """🛡️ 致命#2：執行結束前必須將更新後的狀態寫回磁碟（提交回 repo 供隔日使用）

    🛡️ Review 🟡3：經 atomic_write_json 落盘——Runner 中途被殺也不會留下損壞狀態檔。
    🛡️ Review 🔴1：tp_taken_symbols 為 {symbol: {"cost_price", "tp_taken"}} 結構，
    旗標與觸發時的持倉成本綁定，換手後自動失效。
    """
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
        "tp_taken_symbols": {k: tp_flags[k] for k in sorted(tp_flags)},
    }
    atomic_write_json(STATE_PATH, snapshot)
    logger.info(f"💾 引擎狀態已原子寫回 {STATE_PATH}（熊市計數={bb.below_ma200_days}、"
                f"熔斷鎖={bb.locked_until}、peak={bb.peak_capital}）")


# ── 🛡️ Review 🟡4：reasons 分層——「關鍵理由」vs「過程性攔截理由」────────────
# 行為樹遍歷時 Condition* 節點會留下攔截留痕（如「大盤非牛市，禁止開倉」），
# 而真正驅動最終決策的是 Action*/Check* 節點的理由。混在一起會讓 50 檔股票的
# 報表冗長且淹沒重點。分層後前端預設只顯示關鍵理由（slice(0,2)），其餘收起。
_CRITICAL_MARKERS = ("硬性止損", "移動停利", "開倉買進", "確認熊市", "熔斷", "觸發")
_INTERCEPTION_MARKERS = ("禁止開倉", "未突破", "未達開倉條件")


def classify_reasons(reasons) -> tuple:
    """回傳 (critical, process)：驅動決策的關鍵理由前置，過程性攔截理由其後。"""
    critical, process = [], []
    for r in reasons:
        if any(m in r for m in _INTERCEPTION_MARKERS):
            process.append(r)
        elif any(m in r for m in _CRITICAL_MARKERS) or r.startswith(("跌破成本", "自高點回檔", "突破 20MA")):
            critical.append(r)
        else:
            process.append(r)
    return critical, process


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
        # 🛡️ Review 🟡4：分層——關鍵理由前置、過程性攔截理由其後（前端只顯關鍵）
        critical, process = classify_reasons(record.reasons)
        decisions.append({
            "timestamp": record.timestamp,
            "symbol": record.symbol,
            "market_state": record.market_state,
            "portfolio_state": record.portfolio_state,
            "action": record.action,
            "decision_text": record.decision_text,
            "position_size_pct": record.position_size_pct,
            "config_version": record.config_version,
            "reasons": critical + process,
            "reasons_critical": critical,
            "reasons_process": process,
        })

    # 🛡️ 致命#4＋🔴1：把本輪所有 tp_taken=True 的符號连同觸發時的 cost_price 持久化
    tp_flags = {s.symbol: {"cost_price": s.cost_price, "tp_taken": True}
                for s in stocks if s.tp_taken}

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
            # 🛡️ Review 🟢2：dataclass 標準序列化，乾淨、高效、類型安全
            "market": dataclasses.asdict(cfg.market),
            "portfolio": dataclasses.asdict(cfg.portfolio),
            "execution": dataclasses.asdict(cfg.execution),
            "version": cfg.version,
        },
    }

    out_path = DATA_DIR / "decisions.json"
    atomic_write_json(out_path, output)
    logger.info(f"✅ 決策引擎完成：{len(decisions)} 筆決策已寫入 {out_path}")

    # 🛡️ 致命#2：狀態寫回（在 decisions.json 之後——若中途崩潰，舊狀態仍自洽）
    save_engine_state(bb, tp_flags)
    return output


if __name__ == "__main__":
    run_decision_engine()
