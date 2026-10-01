"""
Alpha Vantage 財報客戶端（FinMind 422 的第三層 fallback）

注意：
- AV 用 HTTP 200 回傳錯誤（Note / Information / Error Message），必須檢查內容而非狀態碼
- 欄位值為字串，缺失時為 "None" 字串，需特別處理
"""
import logging
import os
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)


class AlphaVantageClient:
    BASE_URL = "https://www.alphavantage.co/query"

    def __init__(self, api_key: Optional[str] = None, timeout: int = 15):
        self.api_key = api_key or os.getenv("ALPHA_VANTAGE_API_KEY", "")
        self.timeout = timeout
        self.session = requests.Session()

    @property
    def enabled(self) -> bool:
        """未設定 API key 時整個 AV 層自動退場（零成本、零例外）"""
        return bool(self.api_key)

    def get_quarterly_financials(self, symbol: str) -> Optional[Dict]:
        """取最近一季：營收／毛利率／淨利率／EPS"""
        if not self.enabled:
            return None
        try:
            resp = self.session.get(self.BASE_URL, params={
                "function": "INCOME_STATEMENT",
                "symbol": symbol,
                "apikey": self.api_key,
            }, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            logger.warning("Alpha Vantage %s 請求失敗：%s", symbol, e)
            return None

        # AV 的限流/錯誤是 HTTP 200 + 訊息欄
        if any(k in payload for k in ("Note", "Information", "Error Message")):
            logger.warning("Alpha Vantage %s 回傳非資料：%s", symbol,
                           payload.get("Note") or payload.get("Information") or payload.get("Error Message"))
            return None

        reports = payload.get("quarterlyReports") or []
        if not reports:
            return None
        latest = reports[0]

        def num(key: str) -> Optional[float]:
            raw = latest.get(key)
            if raw in (None, "", "None"):
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        revenue = num("totalRevenue")
        gross_profit = num("grossProfit")
        net_income = num("netIncome")
        eps = num("eps")
        if eps is None:
            eps = num("epsDiluted")

        if revenue is None and eps is None:
            return None

        gross_margin = (gross_profit / revenue * 100) if (revenue and gross_profit is not None) else None
        net_margin = (net_income / revenue * 100) if (revenue and net_income is not None) else None

        return {
            "fiscal_quarter": latest.get("fiscalDateEnding"),
            "revenue": revenue,
            "gross_margin": round(gross_margin, 2) if gross_margin is not None else None,
            "net_margin": round(net_margin, 2) if net_margin is not None else None,
            "eps": round(eps, 2) if eps is not None else None,
            "source": "alphavantage",
        }
