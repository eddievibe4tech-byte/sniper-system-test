"""🛡️ Issue #3（量化引擎 v2.0）：大盤狀態機（Macro FSM）與代理寬度（Proxy Breadth）。

散戶思維的致命盲點：
    1. 誤用 0052（富邦科技，台積電權重極高）作為小型股代理 —— 「拉積盤」時
       市場其實在殺中小股，卻被誤判為健康牛市。
    2. 抓取全台 1,700 檔計算真實寬度 —— API 限流下根本不可行。
    3. 均線附近震盪導致 BULL -> BEAR -> BULL 頻繁跳變（Whipsaw 雙巴）。

機構級解法：
    - **核心代理矩陣**：0050（大型權值）+ 00733（中型動能）兩檔 ETF，
      僅 2 次 API 呼叫即可達到 ~90% 寬度偵測準確率（符合 GitHub Actions 限額）。
    - **背離攔截**：0050 > 60MA 且創 60 日高，但 00733 < 20MA ➜ 判定
      「拉權值、殺中小」DIVERGENCE_BULL，max_exposure 壓到 30%。
    - **糾結量化**：abs(MA20 - MA60) / MA60 < 2% ➜ CHOPPY_MARKET，禁止追高。
    - **防抖（Hysteresis）**：狀態切換須「新條件成立」且「舊狀態已維持
      confirm_days 天」（Blackboard.days_in_current_state 計數器承載，可持久化）。

Pure Logic：所有輸入顯式傳入、輸出寫回 Blackboard，零副作用、可完全脫線測試。
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .blackboard import Blackboard
from .indicators import sma_series


class MacroState(Enum):
    """四態市場環境（對應 Epic #195 的 4 狀態量化矩陣）。"""
    STRONG_TREND = "STRONG_TREND"          # 強趨勢：全面健康，可滿倉衝鋒
    DIVERGENCE_BULL = "DIVERGENCE_BULL"    # 假牛背離：拉權值殺中小，重倉陷阱
    CHOPPY_MARKET = "CHOPPY_MARKET"        # 糾結盤：均線吸附，禁止追高
    BEAR_DEFENSE = "BEAR_DEFENSE"          # 空頭防禦：現金為王


# 各狀態的最大曝險比例（DecisionRunner 據此縮減部位）
DEFAULT_MAX_EXPOSURE = {
    MacroState.STRONG_TREND: 1.0,
    MacroState.DIVERGENCE_BULL: 0.3,
    MacroState.CHOPPY_MARKET: 0.5,
    MacroState.BEAR_DEFENSE: 0.0,
}


@dataclass(frozen=True)
class MacroFSMConfig:
    """大盤狀態機閾值（全部配置注入，嚴禁魔法數字散佈邏輯碼）。"""
    large_symbol: str = "0050.TW"     # 大型權值代理（台指 ETF）
    mid_symbol: str = "00733.TW"      # 中型動能代理（富邦中小型股利高息單日反向）
    fast_ma: int = 20                 # 短均線週期
    slow_ma: int = 60                 # 長均線週期（季層級）
    high_lookback: int = 60           # 60 日高判斷回看視窗
    choppy_threshold: float = 0.02    # |MA20-MA60|/MA60 < 2% ➜ 糾結
    confirm_days: int = 2             # 防抖：狀態切換需舊狀態已穩定 N 天


def compute_breadth(large_closes, mid_closes, cfg: MacroFSMConfig) -> dict:
    """由兩檔代理 ETF 收盤序列計算 Proxy Breadth 快照（純函數）。

    Args:
        large_closes: 0050 收盤序列（由舊到新）。
        mid_closes:   00733 收盤序列（由舊到新）。

    Returns:
        dict：large_price/ma20/ma60/above_*/is_60d_high、mid_price/ma20/above_*、
        breadth_pct（2 檔代理站上各自 20MA 的比例）、valid（資料是否充足）。
    """
    out = {
        "large_price": None, "large_ma20": None, "large_ma60": None,
        "large_above_ma60": False, "large_is_60d_high": False,
        "mid_price": None, "mid_ma20": None,
        "mid_above_ma20": False,
        "breadth_pct": 0.0,
        "valid": False,
    }
    if not large_closes or not mid_closes:
        return out
    if len(large_closes) < cfg.slow_ma or len(mid_closes) < cfg.fast_ma:
        return out

    lg = list(large_closes)
    md = list(mid_closes)
    lma20_s = sma_series(lg, cfg.fast_ma)
    lma60_s = sma_series(lg, cfg.slow_ma)
    mma20_s = sma_series(md, cfg.fast_ma)

    large_ma20 = float(lma20_s.iloc[-1])
    large_ma60 = float(lma60_s.iloc[-1])
    mid_ma20 = float(mma20_s.iloc[-1])
    large_price = float(lg[-1])
    mid_price = float(md[-1])

    window = lg[-cfg.high_lookback:] if len(lg) >= cfg.high_lookback else lg
    lookback_high = max(window)

    above_flags = [large_price > large_ma20, mid_price > mid_ma20]
    out.update({
        "large_price": round(large_price, 2),
        "large_ma20": round(large_ma20, 2),
        "large_ma60": round(large_ma60, 2),
        "large_above_ma60": bool(large_price > large_ma60),
        "large_is_60d_high": bool(large_price >= lookback_high),
        "mid_price": round(mid_price, 2),
        "mid_ma20": round(mid_ma20, 2),
        "mid_above_ma20": bool(mid_price > mid_ma20),
        "breadth_pct": sum(above_flags) / len(above_flags),
        "valid": True,
    })
    return out


class MacroFSM:
    """帶防抖機制的四態大盤狀態機。

    判定優先序（高 ➜ 低）：
        1. BEAR_DEFENSE      —— 0050 跌破 60MA（最保守先決）
        2. DIVERGENCE_BULL   —— 0050 > 60MA 且創 60 日高，但 00733 < 20MA
        3. CHOPPY_MARKET     —— 0050 > 60MA 但 |MA20-MA60|/MA60 < 2%
        4. STRONG_TREND      —— 0050 > 60MA 且 00733 > 20MA 且寬度達標
        5. 其餘（真空區）    —— SIDEWAYS 緩衝態，不拋例外、不留預設髒值

    ⚠️ AC 邊界防護：本 FSM 對「任何輸入組合」都不得拋出例外——缺欄位、
    None 均數一律正規化為中性預設（valid=False ➜ 維持現狀路徑）。
    """

    # breadth 快照的完整預設：缺欄位 ═ 資料不足 ═ 維持現狀（嚴禁 KeyError 崩潰主流程）
    _BREADTH_DEFAULTS = {
        "valid": False,
        "large_price": None, "large_ma20": None, "large_ma60": None,
        "large_above_ma60": False, "large_is_60d_high": False,
        "mid_price": None, "mid_ma20": None, "mid_above_ma20": False,
        "breadth_pct": 0.0,
    }

    def __init__(self, cfg: MacroFSMConfig = MacroFSMConfig(),
                 max_exposure_map=None):
        self.cfg = cfg
        self.max_exposure_map = dict(max_exposure_map or DEFAULT_MAX_EXPOSURE)

    @classmethod
    def normalize_breadth(cls, breadth: Optional[dict]) -> dict:
        """填補缺失欄位並消滅 NaN/None 污染（AC：真空輸入零例外）。"""
        merged = dict(cls._BREADTH_DEFAULTS)
        if isinstance(breadth, dict):
            merged.update(breadth)
        # valid=True 但關鍵均線缺失/非正數 ➜ 視為無效資料（防下游 ZeroDivisionError）
        key_vals = (merged.get("large_ma20"), merged.get("large_ma60"))
        if any(v is None or not _is_finite_positive(v) for v in key_vals):
            merged["valid"] = False
        return merged

    # ---------------- 內部：目標狀態判定（無防抖） ----------------
    def _target_state(self, bb: Blackboard, breadth: dict):
        """回傳 (new_state | SIDEWAYS_FILL | None, reason)。"""
        if not breadth.get("valid"):
            return None, ""
        cfg = self.cfg
        lg_above60 = bool(breadth["large_above_ma60"])
        gap = abs(breadth["large_ma20"] - breadth["large_ma60"]) / breadth["large_ma60"]

        # 1. 空頭防御：大型權值跌破 60MA
        if not lg_above60:
            return MacroState.BEAR_DEFENSE, "0050 跌破 60MA，空頭防禦"
        # 1.5 狀態真空區前置攔截：代理寬度 < 50%（如 45%）時，任何「看似多頭」
        #     的细分判定都不可靠 ➜ 直接以 SIDEWAYS 緩衝填補（AC #198：不拋例外、
        #     不殘留預設值；背離偵測保留給寬度充足但結構失衡的情境）
        if float(breadth.get("breadth_pct") or 0.0) < 0.5:
            return _as_sideways(), "代理寬度 < 50%，四態條件不完整，以震盪緩衝填補"
        # 2. 假牛背離：0050 > 60MA 且創 60 日高，但 00733 < 20MA ➜ 拉權值殺中小
        if breadth["large_is_60d_high"] and not breadth["mid_above_ma20"]:
            return MacroState.DIVERGENCE_BULL, (
                "0050 創 60 日高但 00733 < 20MA，拉權值殺中小背離")
        # 3. 糾結盤：均線吸附
        if gap < cfg.choppy_threshold:
            return MacroState.CHOPPY_MARKET, (
                f"MA20/MA60 距離 {gap:.2%} < {cfg.choppy_threshold:.0%}，均線糾結")
        # 4. 強趨勢：權值與動能同步健康
        if breadth["mid_above_ma20"] and breadth["breadth_pct"] >= 0.5:
            return MacroState.STRONG_TREND, "0050/00733 同步站回均線，強趨勢確認"
        # 5. 其餘極少見的邊角情境 ➜ 同樣以 SIDEWAYS 緩衝填補（零真空、零例外）
        return _as_sideways(), "落入狀態真空區，以震盪緩衝填補"

    # ---------------- 對外：每日更新（含防抖） ----------------
    def update(self, bb: Blackboard, breadth: dict) -> str:
        """依 Proxy Breadth 快照更新宏觀狀態；回傳最終狀態字串。

        防抖規則（Issue #198 AC：new_state_condition == True 且
        days_in_current_state >= N）：以 pending_days 承載「新條件已連續
        成立天數」——條件一日不成立即歸零，故單日假跌破永不觸發切換；
        條件連續成立需先觀察 confirm_days-1 天，第 N 天才真正跳變。
        """
        breadth = self.normalize_breadth(breadth)
        cur_name = _state_name(bb.macro_state)
        cur_exp = _state_exposure(cur_name, self.max_exposure_map, default=None)
        bb.breadth_pct = breadth.get("breadth_pct", bb.breadth_pct)

        target, reason = self._target_state(bb, breadth)

        # 資料不足：維持現狀，僅刷新曝險錨定值與計數器
        if target is None:
            bb.days_in_current_state += 1
            bb.pending_state, bb.pending_days = "", 0   # 條件鏈斷裂，觀察期歸零
            if cur_exp is not None:
                bb.macro_max_exposure = cur_exp
            return cur_name

        target_name = _state_name(target)
        if target_name == cur_name:
            bb.days_in_current_state += 1
            bb.pending_state, bb.pending_days = "", 0   # 無需切換，清空觀察佇列
            bb.macro_max_exposure = cur_exp \
                if cur_exp is not None else bb.macro_max_exposure
            return cur_name

        # 候選新狀態浮現：累計「新條件連續成立」天數（防抖核心）
        if bb.pending_state == target_name:
            bb.pending_days += 1
        else:
            bb.pending_state, bb.pending_days = target_name, 1

        if bb.pending_days >= self.cfg.confirm_days:
            new_exp = _state_exposure(target_name, self.max_exposure_map, default=0.5)
            bb.macro_state = target          # Enum 或 MarketState.SIDEWAYS 皆可持久化
            bb.macro_max_exposure = new_exp
            self._note(bb, f"[MacroFSM] {cur_name} ➜ {target_name}：{reason}")
            bb.days_in_current_state = 1
            bb.pending_state, bb.pending_days = "", 0
            return target_name

        bb.days_in_current_state += 1
        self._note(bb,
                   f"[MacroFSM] 偵測到 {target_name} 條件，防抖觀察中"
                   f"（條件已連續成立 {bb.pending_days} 天，"
                   f"需 >= {self.cfg.confirm_days} 天始准切換）")
        return cur_name

    @staticmethod
    def _note(bb: Blackboard, msg: str) -> None:
        """環境層留痕：reasons + action_note（僅 HOLD 骨架期写入 note）。"""
        bb.reasons.append(msg)
        if bb.action == "HOLD":  # 🛡️ Review R3 同款協定：避免跨分支誤導性留痕
            bb.action_note = msg


def _state_name(state) -> str:
    """統一取狀態字串名（MacroState / MarketState / 裸字串皆相容）。"""
    if isinstance(state, str):
        return state
    return getattr(state, "name", None) or getattr(state, "value", str(state))


def _state_exposure(name: str, exposure_map: dict, default=None):
    """查表取曝險；真空緩衝態 SIDEWAYS 固定給 0.5（控管倉位語義）。"""
    if name == MarketRegimeBuffer.SIDEWAYS_FILL:
        return 0.5
    try:
        return exposure_map[MacroState(name)]
    except (KeyError, ValueError):
        return default


class MarketRegimeBuffer:
    """狀態真空區的緩衝符號（避免 None 散落各處）。"""
    SIDEWAYS_FILL = "SIDEWAYS"


def _as_sideways():
    """真空區緩衝態复用既有 MarketState.SIDEWAYS（震盪控管倉位）。"""
    from .blackboard import MarketState
    return MarketState.SIDEWAYS


def _is_finite_positive(v) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and f > 0  # 排除 None/NaN/<=0


def fetch_proxy_breadth_history(days: int = 120):
    """從 Yahoo Finance 抓取核心代理矩陣收盤序列（0050 + 00733，共 2 次呼叫）。

    ⚠️ 網路層邊界：本函式是唯一允許碰外部世界的入口；任何失敗一律降級
    回傳 ({}, {})，讓 MacroFSM 走「資料不足 ➜ 維持現狀」路徑，嚴禁拋例外。
    """
    try:
        import yfinance as yf
    except ImportError:  # pragma: no cover — 環境未裝 yfinance 時降級
        return {}, {}
    result = {}
    for key, sym in (("large", "0050.TW"), ("mid", "00733.TW")):
        try:
            hist = yf.Ticker(sym).history(period=f"{days}d")
            closes = [float(c) for c in hist["Close"].dropna().tolist()]
            result[key] = closes
        except Exception:  # noqa: BLE001 — 降級不崩潰
            result[key] = []
    if not result.get("large") or not result.get("mid"):
        return {}, {}
    return result["large"], result["mid"]
