"""
台股海選引擎 (Screener) - 方案 C 混合版
動態條件調整 + 空結果容錯

🔴 修正（#92）：FinMind 免費 Token 對交易類 dataset 的批量（全市場）查詢
必然回 400（#91 修掉空 data_id 後仍拒答）。因此：
- 批量查詢改為「快路」（可用時用，不可用時靜默降級）；
- 個股查詢為「保底」（daily analysis 證明穩定）；
- 海選宇宙縮小為「監控池 ∪ 精選活躍股」（~34 檔 × 0.3s ≈ 10 秒可行），
  3,629 檔個股呼叫不現實。真·全市場海選見 follow-up issue #93。

🟢 改進（#95）：批量能力快取——每次 run 都打批量端點探測必然吃 400，
久了會麻痺「真正的錯誤」。首次確認批量不可用後記錄至
data/finmind_capabilities.json，之後直接跳過探測、走個股模式。
未來升級付費 Token 只要刪除該檔即恢復探測。

🟠 P1-1（#93 PR #139 review）：能力快取拆為兩個獨立旗標（故障隔離）——
投信買賣超（TaiwanStockInstitutionalInvestorsBuySell）與月營收
（TaiwanStockMonthRevenue）是兩個獨立 dataset，各自記檔互不波及：
- bulk_supported(dataset) → 只看對應旗標；
- bulk_supported()（無參數）→ 「任一」通道可用即 True（全域守門語意）；
- mark_bulk_unsupported(dataset) → 只標記對應旗標；無參數 → 同時標記兩者；
- 舊格式快取檔（僅 "bulk" 鍵）→ 兩旗標同步沿用其值（相容升級）。
"""
import json
import os
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from finmind_client import FinMindClient

# 🆕 #93：真·全市場海選——TWSE T86 / TWSE-TPEx 月營收開放資料（快速通道）
try:
    from twse_open_data import TwseOpenDataClient
except ImportError:  # 相容於 src. 包路徑
    try:
        from src.twse_open_data import TwseOpenDataClient
    except ImportError:
        TwseOpenDataClient = None  # type: ignore

# 🆕 #93：環境開關——TWSE 端點回應不穩（issue 風險章節），先以開關控制上線，
# 預設关闭（維持 #92 宇宙行為不變）；設 SCREENER_TWSE_OPEN_DATA=1 啟用全市場快速通道。
ENABLE_TWSE_FULL_MARKET = os.getenv("SCREENER_TWSE_OPEN_DATA", "0") == "1"

# 嚴格條件（多頭市場）
STRICT_RULES = {
    "revenue_yoy_min": 15.0,
    "inst_buy_days_min": 3,
    "price_above_ma20": True,
    "max_price_checks": 60,
    "sleep_seconds": 0.5,
}

# 放寬條件（震盪/空頭市場）
RELAXED_RULES = {
    "revenue_yoy_min": 10.0,
    "inst_buy_days_min": 2,
    "price_above_ma20": False,
    "max_price_checks": 60,
    "sleep_seconds": 0.5,
}

# 精選活躍股（#92）：批量不可用時的個股保底宇宙要夠小才能跑完
CURATED_UNIVERSE = [
    "2330", "2317", "2382", "2308", "2454", "2881", "2882", "3711", "3017", "1504",
    "1519", "2303", "2412", "2002", "2884", "2885", "2886", "2890", "2891", "2892",
    "2409", "3443", "4919", "6669", "2301", "2356", "2324", "3008", "4938", "6116",
]


def _latest_rev_month(today):
    """月營收約於次月 10 日公佈 → 取最近已公佈月份"""
    y, m = today.year, today.month - (1 if today.day >= 10 else 2)
    while m <= 0:
        m += 12
        y -= 1
    return y, m


# ---------------------------------------------------------------------------
# 🟢 批量能力快取（#95）
# 每次 run 都打批量端點探測必然吃 400，log 噪音久了會麻痺「真正的錯誤」。
# 首次確認批量不可用後記到 data/finmind_capabilities.json，之後直接跳過探測。
# 未來升級付費 Token：刪除該檔即恢復探測。
# ---------------------------------------------------------------------------
CAP_FILE = os.path.join(os.path.dirname(__file__), "..", "data",
                        "finmind_capabilities.json")

# P1-1：批量能力拆為兩個獨立旗標（故障隔離）——投信買賣超與月營收是
# 兩個獨立 dataset，任一端點暫時故障不应永久禁用另一條批量探測。
BULK_FLAG_INST = "bulk_institutional"
BULK_FLAG_REV = "bulk_revenue"


def _cap_flags() -> Dict[str, bool]:
    """讀取能力快取；無檔案 → 兩旗標預設 True（維持原探測行為）。

    相容舊格式：僅有 "bulk" 鍵的舊快取檔 → 兩旗標同步沿用其值。
    """
    try:
        with open(CAP_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {BULK_FLAG_INST: True, BULK_FLAG_REV: True}
    legacy = data.get("bulk", True)
    return {
        BULK_FLAG_INST: bool(data.get(BULK_FLAG_INST, legacy)),
        BULK_FLAG_REV: bool(data.get(BULK_FLAG_REV, legacy)),
    }


def bulk_supported(dataset: Optional[str] = None) -> bool:
    """批量快路是否已知可用（無快取檔 → 預設 True，維持原探測行為）。

    P1-1：dataset 指定 FinMind dataset 名稱時只看對應旗標；不指定 →
    任一批量通道仍可用即回 True（供 check_finmind_bulk 等全域守門）。
    """
    flags = _cap_flags()
    if dataset == "TaiwanStockInstitutionalInvestorsBuySell":
        return flags[BULK_FLAG_INST]
    if dataset == "TaiwanStockMonthRevenue":
        return flags[BULK_FLAG_REV]
    return flags[BULK_FLAG_INST] or flags[BULK_FLAG_REV]


def mark_bulk_unsupported(dataset: Optional[str] = None):
    """記錄「批量不可用」；P1-1：依 dataset 只標記對應旗標（故障隔離）。

    dataset 未指定 → 同時標記兩者（維持 #95 全批次探測腳本行為）。
    寫檔失敗不影響主流程（下次 run 再探一次）。
    """
    flags = _cap_flags()
    if dataset == "TaiwanStockInstitutionalInvestorsBuySell":
        flags[BULK_FLAG_INST] = False
    elif dataset == "TaiwanStockMonthRevenue":
        flags[BULK_FLAG_REV] = False
    else:
        flags[BULK_FLAG_INST] = flags[BULK_FLAG_REV] = False
    try:
        os.makedirs(os.path.dirname(CAP_FILE), exist_ok=True)
        with open(CAP_FILE, "w", encoding="utf-8") as f:
            json.dump({**flags,
                       "detected_at": datetime.now().isoformat()}, f)
    except Exception as e:
        print(f"⚠️ 能力快取寫入失敗（下次 run 會重新探測）：{e}")



def load_pool_codes() -> List[str]:
    """載入監控池股票代碼（data/stock_pool.json），失敗回傳空清單"""
    p = os.path.join(os.path.dirname(__file__), "..", "data", "stock_pool.json")
    try:
        with open(p, encoding="utf-8") as f:
            return [s["code"] for s in json.load(f).get("stocks", [])]
    except Exception:
        return []


def fetch_month_revenue(finmind, y, m):
    """取得指定月份的營收數據（批量快路）

    🔴 修正（#90）：抓取失敗回傳 None（不再用 `or []` 吞掉失敗），
    讓上層能區分「FinMind 拒答」與「真的沒有資料」。
    🔴 修正（#92）：免費 Token 批量必然 400 → None 屬預期，
    上層 fetch_revenue_yoy_map 會降級為個股模式。
    """
    data = finmind._make_request('TaiwanStockMonthRevenue', '',
                                 start_date=f"{y}-{m:02d}-01",
                                 end_date=f"{y}-{m:02d}-31")
    if data is None:
        return None
    return {r['stock_id']: r['revenue'] for r in data if r.get('revenue')}


def _per_stock_yoy(finmind, code) -> Optional[float]:
    """個股保底：計算單檔「真·年增率」（最新月 vs 13 個月前）

    注意：finmind.get_revenue() 的 yoy_growth 其實是月對月，
    海選門檻寫的是 YoY，保底路徑必須自己算正確的 YoY 口徑。
    """
    data = finmind._make_request("TaiwanStockMonthRevenue", code, days=400) or []
    if len(data) < 13:
        return None
    cur = float(data[-1].get("revenue", 0))
    past = float(data[-13].get("revenue", 0))
    if past <= 0:
        return None
    return round((cur - past) / past * 100, 2)


def fetch_revenue_yoy_map(finmind, codes: List[str]) -> Dict[str, float]:
    """關卡 1 營收 YoY：批量為快路、個股為保底（#92）

    快路：批量抓最近兩個同名月份比較（支援批量的 Token 可用）；
    保底：逐檔個股查詢（daily analysis 證明穩定），雙路都失敗才 raise。
    🟢 #95：能力快取命中「批量不可用」時直接跳過探測，消除每次 run 的 400 噪音。
    """
    bulk_ok = True
    if not bulk_supported("TaiwanStockMonthRevenue"):  # P1-1：獨立旗標
        # 已知批量不可用（免費 Token）→ 不再打端點產生 400 log 噪音
        print("ℹ️ 批量營收已知不可用（免費 Token），直接個股模式")
        bulk_ok = False
    else:
        # 快路：批量（免費版可能 400 → None）
        y, m = _latest_rev_month(datetime.now())
        rev_now = fetch_month_revenue(finmind, y, m)
        rev_past = fetch_month_revenue(finmind, y - 1, m)
        if rev_now and rev_past:
            return {c: (rev_now[c] - rev_past[c]) / rev_past[c] * 100
                    for c in rev_now if rev_past.get(c)}
        # P1-1：只標記「營收」旗標——投信批量不受波及（故障隔離）
        mark_bulk_unsupported("TaiwanStockMonthRevenue")
    # 保底：個股（daily analysis 證明穩定）
    if bulk_ok:
        print("⚠️ 批量營收不可用（FinMind 400），降級為個股模式...")
    yoy = {}
    for c in codes:
        v = _per_stock_yoy(finmind, c)
        if v is not None:
            yoy[c] = v
        time.sleep(0.3)
    if not yoy:
        raise RuntimeError("營收批量與個股抓取皆失敗")
    return yoy


def fetch_inst_streak(finmind, codes, lookback) -> Optional[Dict[str, int]]:
    """批量逐日抓全市場投信買賣超，回傳各檔連買天數

    🔴 修正（#92）：任一日批量回傳 None（免費版必然 400）→ 整個回傳 None
    觸發上層個股保底，不再把「FinMind 拒答」誤算成「全市場連買 0 天」。
    🟢 #95：能力快取命中時完全跳過探測（一次端點都不打），直接回 None。
    """
    if not bulk_supported("TaiwanStockInstitutionalInvestorsBuySell"):  # P1-1
        return None
    days, d = [], datetime.now()
    while len(days) < lookback:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))

    streak, stopped, ok = {c: 0 for c in codes}, set(), True
    for day in days:
        data = finmind._make_request('TaiwanStockInstitutionalInvestorsBuySell', '',
                                     start_date=day, end_date=day)
        if data is None:
            ok = False
            # P1-1：只標記「投信」旗標——營收批量探測不受波及（故障隔離）
            mark_bulk_unsupported("TaiwanStockInstitutionalInvestorsBuySell")
            break
        net = {r["stock_id"]: r.get("Investment_Trust_net", 0) for r in data}
        for c in codes:
            if c in stopped:
                continue
            if net.get(c, 0) > 0:
                streak[c] += 1
            else:
                stopped.add(c)
        time.sleep(0.3)
    return streak if ok else None


def fetch_inst_streak_map(finmind, codes) -> Dict[str, int]:
    """關卡 2 投信連買：批量為快路、個股為保底（#92）

    🟢 #95：快取命中時 fetch_inst_streak 直接回 None（零端點探測），
    此處只印一行資訊性提示，不再重複降級警告。
    """
    bulk = fetch_inst_streak(finmind, codes, 10)
    if bulk is not None:
        return bulk
    if not bulk_supported("TaiwanStockInstitutionalInvestorsBuySell"):  # P1-1
        print("ℹ️ 批量投信已知不可用（免費 Token），直接個股模式")
    else:
        print("⚠️ 批量投信不可用，降級為個股模式...")
    out = {}
    for c in codes:
        out[c] = finmind.get_institutional_buy(c, days=10) or 0
        time.sleep(0.3)
    return out


def get_all_taiwan_stocks(finmind: FinMindClient) -> List[Dict]:
    """
    取得全市場股票清單（過濾掉 ETF 與權證）

    保留作為參考表／除錯用途；海選正式宇宙請用 get_universe()（#92）。
    """
    try:
        data = finmind.get_stock_list() or []
    except Exception as e:
        print(f"⚠️ 獲取股票清單失敗：{e}")
        data = []

    stocks = []
    for item in data:
        stock_id = item.get('stock_id', '')
        stock_name = item.get('stock_name', '')
        industry = item.get('industry_category', '未知')

        if len(stock_id) == 4 and stock_id.isdigit():
            stocks.append({
                'stock_id': stock_id,
                'stock_name': stock_name,
                'industry': industry
            })

    # P1-1 防禦：若股票數量異常少，發出警告並使用預設清單
    if len(stocks) < 500:
        print(f"⚠️ 警告：股票宇宙只有 {len(stocks)} 檔（正常應 >1700），使用預設活躍股清單...")
        fallback_codes = ["2330", "2317", "2382", "2308", "2454", "2881", "2882", "3711", "3017", "1504",
                          "1519", "2303", "2412", "2002", "2884", "2885", "2886", "2890", "2891", "2892"]
        stocks = [{'stock_id': code, 'stock_name': code, 'industry': '預設'} for code in fallback_codes]

    print(f"✅ 載入 {len(stocks)} 檔台股")
    return stocks


def get_universe(finmind: FinMindClient) -> List[Dict]:
    """海選宇宙＝監控池 ∪ 精選活躍股（#92）

    FinMind 免費版批量不可用 → 個股保底模式下 3,629 檔不現實；
    ~34 檔 × 0.3s ≈ 10 秒，完全可行。名稱/產業由 TaiwanStockInfo
    （靜態參考表，批量可用）補全，查不到的以代碼佔位。

    🆕 #93：ENABLE_TWSE_FULL_MARKET=True 時改用全市場宇宙，
    關卡 1/2 資料改由 TWSE/TPEx 開放資料批量提供（見 _screen_with_rules），
    不再受「個股保底只能跑小宇宙」限制。
    P1-2：宇宙來源改為「FinMind 股票清單 ∪ TWSE STOCK_DAY_ALL 每日成交
    全市場名單」——即使 FinMind Token 失效，仍能從證交所端點獨立建構
    全市場宇宙（完全脫離 FinMind 的 fallback）。
    """
    all_stocks = get_all_taiwan_stocks(finmind)
    if ENABLE_TWSE_FULL_MARKET:
        # P1-2：FinMind 清單不足或失敗 → TWSE 開放資料股票清單補強
        if len(all_stocks) < 500 and TwseOpenDataClient is not None:
            with TwseOpenDataClient() as twse:
                twse_list = twse.fetch_stock_list_twse() or []
            known = {s["stock_id"] for s in all_stocks}
            merged = all_stocks + [s for s in twse_list
                                   if s["stock_id"] not in known]
            print(f"🌐 P1-2：FinMind 清單 {len(all_stocks)} 檔 → "
                  f"TWSE 開放資料補強後 {len(merged)} 檔")
            all_stocks = merged
        # P2-4：降級必須明確 log，避免使用者誤以為全市場已啟用
        if len(all_stocks) >= 500:
            print(f"🌐 #93 真·全市場海選啟用：宇宙 {len(all_stocks)} 檔"
                  f"（TWSE/TPEx 開放資料快速通道）")
            return all_stocks
        print(f"⚠️ 股票清單僅 {len(all_stocks)} 檔（<500，FinMind 與 TWSE "
              f"開放資料皆未取得全市場名單），降級為小宇宙模式")
    info = {s["stock_id"]: s for s in all_stocks}
    codes = list(dict.fromkeys(load_pool_codes() + CURATED_UNIVERSE))
    universe = [info.get(c, {"stock_id": c, "stock_name": c, "industry": "未知"})
                for c in codes]
    print(f"✅ 海選宇宙（監控池 ∪ 精選活躍股）：{len(universe)} 檔")
    return universe


# ---------------------------------------------------------------------------
# 🆕 #93：TWSE/TPEx 開放資料快速通道（批量、全市場）
# ---------------------------------------------------------------------------
def recent_trading_dates(count: int, until: Optional[datetime] = None) -> List[str]:
    """取最近 count 個「平日」（由新到舊，YYYYMMDD）。

    注意：不處理農曆春节等休市日——遇休市日 T86 回空資料，該日 net=0
    會自然中斷連買計數（保守語意，不會虛報連買）。
    """
    d = until or datetime.now()
    days = []
    while len(days) < count:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d.strftime("%Y%m%d"))
    return days


def _twse_yyyymm(today, back_months: int) -> str:
    """today 往前 back_months 個月 → 'YYYYMM'"""
    y, m = today.year, today.month - back_months
    while m <= 0:
        m += 12
        y -= 1
    return f"{y}{m:02d}"


def fetch_revenue_yoy_map_opendata(twse: TwseOpenDataClient,
                                   today: Optional[datetime] = None) -> Dict[str, float]:
    """關卡 1（#93 快速通道）：TWSE B2i + TPEx 月營收同名月份 YoY

    月營收約次月 10 日出齊 → cur=最新已公布月、prev=同月去年。
    抓不到任何資料 → raise RuntimeError（上層降級至 FinMind 個股保底）。
    """
    today = today or datetime.now()
    offset = 1 if today.day >= 10 else 2
    cur = _twse_yyyymm(today, offset)
    prev = _twse_yyyymm(today, offset + 12)
    raw = twse.revenue_yoy_map(cur, prev)
    yoy = {c: v for c, v in raw.items() if v is not None}
    if not yoy:
        raise RuntimeError(f"TWSE/TPEx 月營收 {cur}/{prev} 抓取失敗或為空")
    print(f"  📊 開放資料營收 YoY：{cur} vs {prev} → 取得 {len(yoy)} 檔")
    return yoy


def fetch_inst_streak_map_opendata(twse: TwseOpenDataClient,
                                   lookback: int = 10) -> Optional[Dict[str, int]]:
    """關卡 2（#93 快速通道）：TWSE T86 每日全市場投信買賣超 → 連買天數

    任一日抓取失敗 → None（上層降級；不把「端點掛」誤算成「連買 0 天」，
    語意同 #90/#92）。回傳 map 涵蓋全市場上市股（櫃買股不在 T86，
    由上層 .get(c, 0) 處理→ naturally 不通過關卡 2，屬已知限制）。
    """
    dates = recent_trading_dates(lookback)
    streak = twse.inst_buy_streak_map(dates)
    if streak is None:
        return None
    positive = {c: n for c, n in streak.items() if n > 0}
    print(f"  📊 T86 投信連買（{dates[-1]}~{dates[0]}）：連買>0 共 {len(positive)} 檔")
    return streak


def _screen_with_rules(finmind: FinMindClient, rules: Dict, info: Dict) -> List[Dict]:
    """使用指定規則進行海選

    🔴 修正（#92）：關卡 1/2 改用雙路 map（批量快路 → 個股保底），
    僅在批量與個股皆失敗時才 raise RuntimeError（誠實紅燈語意不變）。
    🆕 #93：ENABLE_TWSE_FULL_MARKET=True 時，關卡 1/2 優先走 TWSE/TPEx
    開放資料快速通道（全市場、批量）；任一通道失敗 → 降級回既有
    FinMind 批量→個股保底路徑（驗收標準 2）。關卡 3 MA20 仍為個股查詢，
    以 max_price_checks 上限控制成本。
    """
    print(f"🔍 啟動海選引擎（營收>{rules['revenue_yoy_min']}%, 投信>{rules['inst_buy_days_min']}天，MA20={rules['price_above_ma20']}）...")

    codes = [s["stock_id"] for s in info.values()]

    twse_client = None
    if ENABLE_TWSE_FULL_MARKET and TwseOpenDataClient is not None:
        twse_client = TwseOpenDataClient()

    # 關卡 1：營收 YoY
    #   #93 快速通道（TWSE B2i + TPEx）→ 失敗降級 #92 雙路（FinMind 批量→個股）
    yoy: Optional[Dict[str, float]] = None
    if twse_client is not None:
        try:
            yoy = fetch_revenue_yoy_map_opendata(twse_client)
        except Exception as e:
            print(f"⚠️ #93 開放資料營收通道失敗（{e}），降級 FinMind 模式...")
            yoy = None
    if yoy is None:
        yoy = fetch_revenue_yoy_map(finmind, codes)
    pool = [c for c in codes if yoy.get(c, -999) >= rules["revenue_yoy_min"]]
    print(f"  關卡 1 營收 YoY>{rules['revenue_yoy_min']}%：剩 {len(pool)} 檔")

    # 關卡 2：投信連買
    #   #93 快速通道（T86）→ 失敗降級 #92 雙路（FinMind 批量→個股）
    streak: Optional[Dict[str, int]] = None
    if twse_client is not None:
        streak = fetch_inst_streak_map_opendata(twse_client)
        if streak is None:
            print("⚠️ #93 T86 連買通道失敗，降級 FinMind 模式...")
    if streak is None:
        streak = fetch_inst_streak_map(finmind, pool)
    pool = [c for c in pool if streak.get(c, 0) >= rules["inst_buy_days_min"]]
    print(f"  關卡 2 投信連買>={rules['inst_buy_days_min']}天：剩 {len(pool)} 檔")

    # 關卡 3：股價 vs MA20（可選）— P0-2 修正：使用 _get_raw_prices 直接取價格陣列
    pool.sort(key=lambda c: (yoy.get(c, 0), streak.get(c, 0)), reverse=True)
    candidates = []
    for code in pool[:rules["max_price_checks"]]:
        try:
            prices = finmind._get_raw_prices(code, days=60)
            if len(prices) < 20:
                continue

            ma20 = sum(prices[-20:]) / 20
            current = prices[-1]

            if rules["price_above_ma20"] and current < ma20:
                continue

            s = info.get(code, {})
            candidates.append({
                "code": code,
                "name": s.get('stock_name', code),
                "industry": s.get('industry', '未知'),
                "revenue_yoy": round(yoy[code], 2),
                "inst_buy_days": streak.get(code, 0),
                "current_price": round(current, 2),
                "ma20": round(ma20, 2),
                "screened_at": datetime.now().strftime('%Y-%m-%d'),
            })
        except Exception as e:
            print(f"  ⚠️ {code} 股價檢查失敗：{e}")
        finally:
            time.sleep(rules["sleep_seconds"])

    candidates.sort(key=lambda x: (x['revenue_yoy'], x['inst_buy_days']), reverse=True)
    return candidates


def run_weekly_screener(finmind: FinMindClient) -> List[Dict]:
    """方案 C 混合版：動態條件 + 空結果容錯

    🔴 修正（#90）：抓取失敗（RuntimeError）時記錄 error 欄位並非零退出，
    讓 workflow 紅燈、Actions 通知；不再把「FinMind 拒答」偽裝成「市場太弱」。
    rules_applied 改為誠實標籤：STRICT / RELAXED (auto) / NONE (genuine zero)。
    🔴 修正（#92）：宇宙改為 get_universe()（監控池 ∪ 精選活躍股），
    配合批量→個股雙路，保證免費 Token 也能跑完海選。
    """
    info = {s['stock_id']: s for s in get_universe(finmind)}

    try:
        # 第一輪：嚴格條件
        candidates = _screen_with_rules(finmind, STRICT_RULES, info)
        rules_used = "STRICT"

        # 如果 0 檔，自動放寬條件重跑
        if not candidates:
            print("⚠️ 嚴格條件無候選股，自動放寬條件重跑...")
            candidates = _screen_with_rules(finmind, RELAXED_RULES, info)
            rules_used = "RELAXED (auto)"

            if candidates:
                print(f"✅ 放寬條件後找到 {len(candidates)} 檔候選股")
    except RuntimeError as e:
        # 抓取失敗 → 誠實記錄錯誤並讓 workflow 紅燈
        print(f"❌ 海選資料抓取失敗：{e}")
        save_screener_results([], rules_used="ERROR", error=str(e))
        raise SystemExit(1)

    # 真的 0 檔（數據正常但無符合者）→ 接受空結果（不中斷 workflow）
    if not candidates:
        print("⚠️ 本週真的無符合條件的候選股（數據正常，genuine zero）")
        save_screener_results([], rules_used="NONE (genuine zero)")
        return []

    top = candidates[:15]
    save_screener_results(top, rules_used=rules_used)
    print(f"✅ 海選完成：{len(top)} 檔候選")
    return top


def save_screener_results(candidates: List[Dict], rules_used: str = "UNKNOWN",
                          error: Optional[str] = None) -> None:
    data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
    os.makedirs(data_dir, exist_ok=True)
    output_path = os.path.join(data_dir, 'screener_candidates.json')

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump({
            "candidates": candidates,
            "updated_at": datetime.now().isoformat(),
            "rules_applied": rules_used,
            "error": error,
        }, f, ensure_ascii=False, indent=2)

    print(f"📁 結果已儲存至：{output_path}")


if __name__ == "__main__":
    token = os.getenv('FINMIND_TOKEN', '')
    if not token:
        print("⚠️ 警告：未設定 FINMIND_TOKEN")
        exit(1)

    client = FinMindClient(token=token)
    candidates = run_weekly_screener(client)

    print("\n🎯 本週 Alpha 候選股 Top 5:")
    for i, stock in enumerate(candidates[:5], 1):
        print(f"  {i}. {stock['code']} {stock['name']} - 營收 {stock['revenue_yoy']}%, 投信 {stock['inst_buy_days']} 天")
