"""Issue #205: 個股價值防雷、金融股旁路與基本面快取。

實作 Code Review 盲點「金融股誤殺」與「API 超時/429」的解法：
- check_fundamentals(): 金融股走 Sector Bypass（P/B + ROE），一般股走 FCF/負債 Hard Filter，
  並附循環股（Value Trap）攔截。
- CachedFundamentalsProvider: 本地 JSON 快取（TTL 30 天），命中時完全不碰 yfinance。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

FINANCIAL_SECTOR = "Financial Services"
FUNDAMENTALS_TTL_DAYS = 30  # 季報頻率
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "fundamentals_cache.json"


@dataclass
class Fundamentals:
    ticker: str
    sector: str = ""
    fcf: float = 0.0                    # Free Cash Flow（億）
    debt_ratio: float = 0.0             # 負債比
    pb: float = 0.0                     # 現行 P/B
    pb_5y_median: float = 0.0           # 歷史 5 年 P/B 中位數
    roe_latest: float = 0.0             # 最近一年 ROE (%)
    roe_3y_avg: float = 0.0             # 3 年平均 ROE (%)


@dataclass
class ScreenResult:
    ticker: str
    passed: bool
    reason: str = ""
    bypassed: bool = False              # True = 走金融股旁路


def check_fundamentals(f: Fundamentals) -> ScreenResult:
    """Hard Filter + 金融股旁路 + 循環股攔截。"""
    # --- 循環股攔截（兩種模式都適用）：單年暴衝但 3Y 平均不合格 → Value Trap
    if f.roe_latest >= 30 and f.roe_3y_avg < 10:
        return ScreenResult(f.ticker, False,
                            f"循環股雷：單年 ROE={f.roe_latest}% 但 3Y Avg={f.roe_3y_avg}% < 10%")

    # --- 金融股旁路：FCF/負債比對金融業失真，改用 P/B + ROE
    if f.sector == FINANCIAL_SECTOR:
        if f.pb < f.pb_5y_median and f.roe_3y_avg > 10:
            return ScreenResult(f.ticker, True,
                                "金融股旁路通過：P/B < 5Y 中位數且 ROE(3Y) > 10%", bypassed=True)
        return ScreenResult(f.ticker, False,
                            "金融股門檻未過：需 P/B < 5Y 中位數且 ROE(3Y) > 10%")

    # --- 一般股 Hard Filter
    if f.fcf < 0:
        return ScreenResult(f.ticker, False, f"FCF < 0 ({f.fcf})")
    if f.debt_ratio > 1.0:
        return ScreenResult(f.ticker, False, f"負債比過高 ({f.debt_ratio})")
    if f.roe_3y_avg < 10:
        return ScreenResult(f.ticker, False, f"ROE 3Y Avg < 10% ({f.roe_3y_avg})")
    return ScreenResult(f.ticker, True, "通過 Hard Filter")


class YFinanceBackend:
    """yfinance 財報資料源封裝（可注入 fake 以測試 API 呼叫計數）。"""

    def __init__(self, fetcher=None):
        self._fetcher = fetcher or self._default_fetcher
        self.api_calls = 0

    @staticmethod
    def _default_fetcher(ticker: str) -> Dict:
        import yfinance as yf  # lazy import
        t = yf.Ticker(ticker)
        info = t.info or {}
        return {
            "sector": info.get("sector", ""),
            "fcf": info.get("freeCashflow", 0) or 0,
            "debt_ratio": (info.get("debtToEquity") or 0) / 100.0,
            "pb": info.get("priceToBook", 0) or 0,
            "pb_5y_median": info.get("priceToBook", 0) or 0,
            "roe_latest": (info.get("returnOnEquity") or 0) * 100,
            "roe_3y_avg": (info.get("returnOnEquity") or 0) * 100,
        }

    def get(self, ticker: str) -> Dict:
        self.api_calls += 1
        return self._fetcher(ticker)


class CachedFundamentalsProvider:
    """基本面快取：TTL 30 天；命中快取時 backend API 呼叫次數必須為 0。"""

    def __init__(self, backend: YFinanceBackend, cache_path: Path = DEFAULT_CACHE_PATH,
                 ttl_days: int = FUNDAMENTALS_TTL_DAYS, now=None):
        self.backend = backend
        self.cache_path = Path(cache_path)
        self.ttl_seconds = ttl_days * 86400
        self._now = now or time.time

    def _load(self) -> Dict:
        if self.cache_path.exists():
            try:
                return json.loads(self.cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save(self, data: Dict) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                   encoding="utf-8")

    def get(self, ticker: str) -> Fundamentals:
        data = self._load()
        entry = data.get(ticker)
        if entry and (self._now() - entry["cached_at"]) < self.ttl_seconds:
            return Fundamentals(ticker=ticker, **entry["payload"])
        raw = self.backend.get(ticker)
        self._cache_put(data, ticker, raw)
        return Fundamentals(ticker=ticker, **raw)

    def _cache_put(self, data: Dict, ticker: str, payload: Dict) -> None:
        data[ticker] = {"cached_at": self._now(), "payload": payload}
        self._save(data)
