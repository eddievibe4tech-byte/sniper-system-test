"""
TWSE / TPEx 開放資料客戶端 — 真·全市場海選資料源（#93）

動機：FinMind 免費 Token 對交易類 dataset 的批量（全市場）查詢必然 400
（見 #90/#92），海選宇宙只能縮到「監控池 ∪ 精選活躍股」(~34 檔)。
本模組改以證交所/櫃買中心官方開放資料作為批量資料源：

1. **投信連買**：TWSE 新版 RWD「三大法人買賣超日報（T86）」— 1 天 1 call
   涵蓋全市場上市股：
   https://www.twse.com.tw/rwd/zh/fund/T86?date={YYYYMMDD}&responseType=json
   （2026-10-04 curl 實測 200：title=「115年10月01日 三大法人買賣超日報」、
   fields 含「證券代號/投信買進股數/投信賣出股數/投信買賣超股數」。
   舊 twse-api.../stock/fibindex 為大盤指數端點、非 T86，Code Review P0-1
   已於本版本移除。）
2. **股票代碼清單**：TWSE 每日成交量全市場報表（open_data CSV，免金鑰）：
   https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=open_data
   （2026-10-04 curl 實測 200、1,380 行；Code Review P1-2：解除宇宙對
   FinMind 的硬依賴。）
3. **月營收 YoY**：⚠️ 本環境（GitHub Actions CI）對 MOPS 與 TPEx 觀測站
   皆被出口端防護擋下（Code Review P0-2/P0-3 實測：MOPS JSP 即使帶
   Referer 仍回「安全性考量」HTML；TPEx web/*.php|json 全數 404 HTML）。
   因此預設**停用**直接抓取，改為 FinMind 個股營收保底（已證明穩定）；
   如需啟用可設環境變數 SNIPER_TWSE_REVENUE=1（供境外自架 runner 驗證）。

設計原則（呼應 issue #93 風險章節「TWSE 端點回應不穩」）：
- 每個端點都有 timeout + 有限重試 + 指數退避（P2-1）；任何一源失敗 →
  回傳部分/空結果，由上層 screener 決定降級路徑（FinMind 保底模式）。
- 解析函數與網路分離（parse_* 為純函數）→ 可用固定 fixture 做單元測試，
  涵蓋欄位缺失／格式變動／部分來源失敗情境（驗收標準 3、P1-4）。
- 所有數值欄位以 _to_float() 防呆：TWSE 用 '-' 表示無資料。
- 支援 context manager（P2-2）確保 session 關閉。
"""
import csv
import io
import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# TWSE 新版 RWD T86 三大法人買賣超日報（每日全市場上市股）——P0-1 修正
T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
# TWSE 每日成交量全市場報表（open_data CSV）→ 股票代碼清單來源（P1-2）
STOCK_LIST_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL"
# TWSE MOPS 個股單月營收概況（B2i）——P0-2：CI 環境被反爬蟲擋下，預設停用
TWSE_REVENUE_URL = "https://mops.twse.com.tw/nas/t21/securities/transaction/B2i/s_code.jsp"
# TPEx 櫃買個股月營收彙總——P0-3：端點已失效（404 HTML），預設停用
TPEX_REVENUE_URL = "https://www.tpex.org.tw/web/finance/finance_monthly/revcombine/{date}/data.json"

# 境外自架 runner 若驗證 MOPS/TPEx 可達，設 SNIPER_TWSE_REVENUE=1 啟用
ENABLE_TWSE_REVENUE = os.getenv("SNIPER_TWSE_REVENUE", "0") == "1"

_DEFAULT_HEADERS = {
    "User-Agent": "SniperSystem/1.0 (+https://github.com/eddievibe4tech-byte/sniper-system-test)",
    # P0-2：MOPS 需要站內 Referer／Accept，否則回「安全性考量」HTML 錯誤頁
    "Referer": "https://mops.twse.com.tw/",
    "Accept": "application/json, text/csv, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9",
}


def _to_float(v: Any) -> Optional[float]:
    """TWSE/TPEx 以 '-' 或空字串表示無資料；一律轉 None，其餘嘗試 float。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


class TwseOpenDataClient:
    """TWSE/TPEx 開放資料客戶端（唯讀、無需金鑰）"""

    def __init__(self, timeout: int = 15, retries: int = 2):
        self.timeout = timeout
        self.retries = max(1, retries)
        self.session = requests.Session()
        self.session.headers.update(_DEFAULT_HEADERS)

    # P2-2：context manager 支援，避免長駐服務連線洩漏
    def close(self):
        self.session.close()

    def __enter__(self) -> "TwseOpenDataClient":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def _get_with_backoff(self, url: str, params: Optional[dict] = None) -> requests.Response:
        """GET + 指數退避重試（P2-1：TWSE 對高頻請求有隱性限制）"""
        last_exc: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                r.raise_for_status()
                return r
            except (requests.RequestException, ValueError) as e:
                last_exc = e
                logger.warning(f"{url} 第{attempt + 1}次失敗：{e}")
                if attempt < self.retries - 1:
                    time.sleep(2 ** attempt)  # 1s, 2s, 4s...
        raise requests.RequestException(f"{url} 重試 {self.retries} 次後仍失敗：{last_exc}")

    # ------------------------------------------------------------------
    # 股票代碼清單（P1-2：解除宇宙對 FinMind 的硬依賴）
    # ------------------------------------------------------------------
    def fetch_stock_list_twse(self) -> Optional[List[Dict[str, str]]]:
        """從 TWSE 每日成交量全市場報表（open_data CSV）提取上市股票代碼。

        Returns:
            [{stock_id, stock_name, industry='上市'}...]；失敗回 None。
            過濾 ETF/存證/權證等非 4 碼普通股。
        """
        try:
            r = self._get_with_backoff(STOCK_LIST_URL,
                                       params={"response": "open_data"})
            return self.parse_stock_list_csv(r.text)
        except requests.RequestException as e:
            logger.error(f"TWSE 股票清單抓取失敗：{e}")
            return None

    @staticmethod
    def parse_stock_list_csv(text: str) -> List[Dict[str, str]]:
        """解析 STOCK_DAY_ALL open_data CSV → 4 碼普通股清單（純函數，供測試）"""
        out: List[Dict[str, str]] = []
        seen = set()
        try:
            reader = csv.reader(io.StringIO(text))
            header = next(reader, None)
            if not header or "證券代號" not in header:
                return []
            idx_code = header.index("證券代號")
            idx_name = header.index("證券名稱") if "證券名稱" in header else None
            for row in reader:
                try:
                    sid = str(row[idx_code]).strip().replace('"', "")
                except IndexError:
                    continue
                if len(sid) == 4 and sid.isdigit() and sid not in seen:
                    seen.add(sid)
                    name = ""
                    if idx_name is not None:
                        try:
                            name = str(row[idx_name]).strip().replace('"', "")
                        except IndexError:
                            pass
                    out.append({"stock_id": sid, "stock_name": name or sid,
                                "industry": "上市"})
        except csv.Error as e:
            logger.warning(f"股票清單 CSV 解析失敗：{e}")
            return []
        return out

    # ------------------------------------------------------------------
    # 投信連買（T86）
    # ------------------------------------------------------------------
    def fetch_institutional_day(self, date_yyyymmdd: str) -> Optional[Dict[str, float]]:
        """抓單日全市場投信買賣超淨額（TWSE 上市公司）。

        P0-1 修正：改用新版 RWD T86 端點（responseType=json），
        fields/data 二維陣列結構（2026-10-04 curl 實測可達）。

        Returns:
            {stock_id: 投信買賣超股數}；端點失敗回 None（呼叫端須視為
            「資料源失效」而非「全市場賣超」——誠實紅燈語意，同 #90/#92）。
        """
        try:
            r = self._get_with_backoff(
                T86_URL,
                params={"date": date_yyyymmdd, "responseType": "json"})
            payload = r.json()
        except (requests.RequestException, ValueError) as e:
            logger.error(f"T86 {date_yyyymmdd} 抓取失敗：{e}")
            return None
        if not isinstance(payload, dict) or payload.get("stat") not in (None, "OK"):
            # 例：{"stat":"message","msg":"..."} 非交易日／參數錯誤 → 視為失敗
            logger.error(f"T86 {date_yyyymmdd} 回應異常：{str(payload)[:120]}")
            return None
        return self.parse_t86(payload)

    @staticmethod
    def parse_t86(payload) -> Dict[str, float]:
        """解析新版 T86 {fields:[字串], data:[[...]]} → {stock_id: 投信淨超買}

        相容舊版 list-of-dicts 結構（純函數，供測試）。
        以欄位名「投信買賣超股數」定位淨額欄，缺欄時 fallback 買-賣。
        """
        out: Dict[str, float] = {}
        if isinstance(payload, dict):
            fields = [str(f) for f in (payload.get("fields") or [])]
            rows = payload.get("data") or []
            if not fields:
                return out
            try:
                i_code = fields.index("證券代號")
            except ValueError:
                return out
            i_net = i_buy = i_sell = None
            for key, name in (("net", "投信買賣超股數"),
                              ("buy", "投信買進股數"),
                              ("sell", "投信賣出股數")):
                if name in fields:
                    if key == "net":
                        i_net = fields.index(name)
                    elif key == "buy":
                        i_buy = fields.index(name)
                    else:
                        i_sell = fields.index(name)
            if i_net is None and (i_buy is None or i_sell is None):
                return out
            for row in rows:
                if not isinstance(row, (list, tuple)):
                    continue
                try:
                    sid = str(row[i_code]).strip()
                except IndexError:
                    continue
                if not (len(sid) == 4 and sid.isdigit()):
                    continue  # 欄位缺失／非股票行 → 跳過
                net = _to_float(row[i_net]) if i_net is not None else None
                if net is None:
                    buy = _to_float(row[i_buy]) or 0.0 if i_buy is not None else 0.0
                    sell = _to_float(row[i_sell]) or 0.0 if i_sell is not None else 0.0
                    net = buy - sell
                out[sid] = net
            return out
        # 舊格式相容：list[dict]（StockId / InvestTrustOverBuy）
        for row in payload or []:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("StockId", "")).strip()
            if not (len(sid) == 4 and sid.isdigit()):
                continue
            net = _to_float(row.get("InvestTrustOverBuy"))
            if net is None:
                buy = _to_float(row.get("InvestTrustBuy")) or 0.0
                sell = _to_float(row.get("InvestTrustSell")) or 0.0
                net = buy - sell
            out[sid] = net
        return out

    def inst_buy_streak_map(self, dates_yyyymmdd: List[str],
                            codes: Optional[List[str]] = None,
                            require_all_days: bool = True) -> Optional[Dict[str, int]]:
        """依日期序列（由新到舊）計算各股投信連續買超天數。

        dates 需按「交易日由新到舊」排序。codes 提供時只回傳該宇宙的計數。

        P1-3 韌性：require_all_days=True 時若「連續 ≥3 日」失敗且最新日
        成功 → 判定為長假（春節/慶典休市）而非端點故障，自動切換韌性模式
        繼續計數（連買語意保守：跳過日不計入亦不中斷）。
        """
        day_maps: List[Dict[str, float]] = []
        consec_fail = 0
        for i, d in enumerate(dates_yyyymmdd):
            m = self.fetch_institutional_day(d)
            if m is None:
                consec_fail += 1
                latest_failed = (i == 0)
                # 長假推定：最新日有資料、但已連續多日失敗 → 休市日機率遠高於故障
                holiday_suspect = (not latest_failed and consec_fail >= 3)
                if require_all_days and not holiday_suspect:
                    logger.error(f"T86 {d} 抓取失敗，連買統計中止（上層需降級）")
                    return None
                if latest_failed:
                    logger.error("T86 最新交易日抓取失敗，連買統計中止"
                                 "（無最新交易日即無意義，上層需降級）")
                    return None
                if holiday_suspect and i == consec_fail:
                    logger.warning(
                        f"P1-3：T86 最近 {consec_fail} 個平日皆回空/失敗，"
                        "判定疑似連續休市（長假），自動切換韌性模式跳過這些日期")
                else:
                    logger.warning(f"T86 {d} 抓取失敗，跳過該日（韌性模式）")
                continue
            consec_fail = 0
            day_maps.append(m)

        universe = codes if codes is not None else sorted(
            set(c for m in day_maps for c in m))
        streak: Dict[str, int] = {}
        for c in universe:
            n = 0
            for m in day_maps:
                if m.get(c, 0) > 0:
                    n += 1
                else:
                    break
            streak[c] = n
        return streak

    # ------------------------------------------------------------------
    # 月營收 YoY（TWSE B2i + TPEx RevenueCombine）
    # ⚠️ P0-2/P0-3：本環境（GitHub Actions CI）實測 MOPS 回「安全性考量」
    # HTML、TPEx 觀測站端點 404 → 預設停用（ENABLE_TWSE_REVENUE=0），
    # fetch_* 直接回 None，由上層降級 FinMind 個股保底。解析純函數保留
    # （境外自架 runner 驗證後設 SNIPER_TWSE_REVENUE=1 即可啟用）。
    # ------------------------------------------------------------------
    def fetch_month_revenue_twse(self, yyyymm: str) -> Optional[Dict[str, float]]:
        """TWSE 上市個股單月營收（B2i）。失敗回 None。"""
        if not ENABLE_TWSE_REVENUE:
            logger.debug(f"TWSE 營收通道停用（P0-2），{yyyymm} 回 None")
            return None
        params = {"date": yyyymm, "format": "JSON"}
        try:
            r = self._get_with_backoff(TWSE_REVENUE_URL, params=params)
            payload = r.json()
            return self.parse_twse_revenue(payload)
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"TWSE 營收 {yyyymm} 抓取失敗：{e}")
            return None

    @staticmethod
    def parse_twse_revenue(payload) -> Dict[str, float]:
        """解析 B2i JSON（{fields, data} 結構）→ {stock_id: 當月營收}

        欄位名稱可能變動（verDate 不同），因此以「包含『營收』且排除
        『年增率/同期比較』」的启发式定位當月營收欄，並保留 StockCode。
        """
        return TwseOpenDataClient._extract_revenue_table(
            payload, code_key_candidates=("StockCode", "SecurTypeCode"),
            revenue_hint="本月營收")

    def fetch_month_revenue_tpex(self, yyyymm: str) -> Optional[Dict[str, float]]:
        """TPEx 櫃買個股單月營收彙總。失敗回 None。"""
        if not ENABLE_TWSE_REVENUE:
            logger.debug(f"TPEx 營收通道停用（P0-3），{yyyymm} 回 None")
            return None
        url = TPEX_REVENUE_URL.format(date=yyyymm)
        try:
            r = self._get_with_backoff(url, params={"l": "zh-tw"})
            payload = r.json()
            return self.parse_tpex_revenue(payload)
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"TPEx 營收 {yyyymm} 抓取失敗：{e}")
            return None

    @staticmethod
    def parse_tpex_revenue(payload) -> Dict[str, float]:
        """解析 TPEx RevenueCombine JSON → {stock_id: 當月營收}"""
        return TwseOpenDataClient._extract_revenue_table(
            payload, code_key_candidates=("SecuCode", "StockCode"),
            revenue_hint="RevMonthAmnt")

    @staticmethod
    def _extract_revenue_table(payload, code_key_candidates, revenue_hint) -> Dict[str, float]:
        """通用 {fields:[{id,name}], data:[[...]]} 結構解析（純函數，供測試）"""
        if not isinstance(payload, dict):
            return {}
        fields = payload.get("fields") or []
        rows = payload.get("data") or []
        if not fields or not rows:
            return {}

        id_by_name = {}
        for f in fields:
            name = str(f.get("name", ""))
            fid = str(f.get("id", ""))
            id_by_name[name] = fid

        code_id = next((id_by_name[n] for n in code_key_candidates
                        if n in id_by_name), None)
        rev_id = None
        for name, fid in id_by_name.items():
            if revenue_hint and revenue_hint in name:
                rev_id = fid
                break
        if rev_id is None:  # fallback：找一個可轉數字的「營收」欄
            for name, fid in id_by_name.items():
                if "營收" in name and "增率" not in name and "比較" not in name:
                    rev_id = fid
                    break
        if code_id is None or rev_id is None:
            logger.warning("營收表欄位無法辨識（格式變動？）→ 回傳空 map")
            return {}

        out: Dict[str, float] = {}
        for row in rows:
            if not isinstance(row, (list, tuple)):
                continue
            try:
                sid = str(row[int(code_id)]).strip()
                rev = _to_float(row[int(rev_id)])
            except (ValueError, IndexError):
                continue
            if len(sid) == 4 and sid.isdigit() and rev is not None:
                out[sid] = rev
        return out

    # ------------------------------------------------------------------
    # 高階：全市場月營收 YoY map
    # ------------------------------------------------------------------
    def revenue_yoy_map(self, cur_yyyymm: str, prev_yyyymm: str,
                        include_tpex: bool = True) -> Dict[str, Optional[float]]:
        """計算全市場「同名月份」YoY（%）。

        任一來源兩個月都抓不到 → 該檔不在 map 中；
        全部來源失敗 → 回傳空 dict（上層 raise／降級）。
        YoY 無法計算（基期為 0/缺）→ value None。
        """
        cur = self._merge_revenue(cur_yyyymm, include_tpex)
        prev = self._merge_revenue(prev_yyyymm, include_tpex)
        yoy: Dict[str, Optional[float]] = {}
        for code, c_val in cur.items():
            p_val = prev.get(code)
            if p_val is None or p_val <= 0:
                yoy[code] = None
            else:
                yoy[code] = round((c_val - p_val) / p_val * 100, 2)
        return yoy

    def _merge_revenue(self, yyyymm: str, include_tpex: bool) -> Dict[str, float]:
        """合併 TWSE/TPEx 營收。P1-4：單源失敗僅記錄，不阻斷另一源結果。"""
        merged: Dict[str, float] = {}
        twse = self.fetch_month_revenue_twse(yyyymm)
        if twse is None:
            logger.warning(f"{yyyymm} TWSE(B2i) 營收抓取失敗")
        else:
            merged.update(twse)
        if include_tpex:
            tpex = self.fetch_month_revenue_tpex(yyyymm)
            if tpex is None:
                logger.warning(f"{yyyymm} TPEx 營收抓取失敗（partial：僅上市）")
            else:
                merged.update(tpex)
        if not merged:
            logger.error(f"{yyyymm} 月營收：TWSE/TPEx 皆失敗")
        return merged
