"""
美股數據客戶端
數據源：yfinance（免費）
提供：價格歷史、技術指標（MA/RSI/52週高點/波動率）、VIX、財報日
"""
import logging
import time
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger("USClient")

# 監控池：Mag 7 + 半導體 + 指數
US_WATCHLIST = ["META", "NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "TSLA", "AVGO", "AMD"]
US_INDEX = {"spy": "SPY", "qqq": "QQQ", "vix": "^VIX"}


class USClient:
    def __init__(self, sleep_between: float = 1.0):
        self.sleep_between = sleep_between  # 避免 yfinance 限流

    def get_history(self, symbol: str, period: str = "1y", interval: str = "1d") -> Optional[pd.DataFrame]:
        try:
            df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
            if df is None or df.empty or len(df) < 60:
                logger.warning("%s 歷史資料不足", symbol)
                return None
            return df
        except Exception as e:
            logger.error("%s 歷史資料失敗：%s", symbol, e)
            return None

    @staticmethod
    def _rsi(closes: pd.Series, period: int = 14) -> float:
        """Wilder's RSI（與 crypto_client 一致）"""
        delta = closes.diff().dropna()
        if len(delta) < period + 1:
            return 50.0
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.iloc[:period].mean()
        avg_loss = loss.iloc[:period].mean()
        for g, l in zip(gain.iloc[period:], loss.iloc[period:]):
            avg_gain = (avg_gain * (period - 1) + g) / period
            avg_loss = (avg_loss * (period - 1) + l) / period
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return round(100 - 100 / (1 + rs), 2)

    def get_technical_indicators(self, symbol: str) -> Optional[Dict]:
        df = self.get_history(symbol)
        if df is None:
            return None
        closes = df["Close"]
        price = float(closes.iloc[-1])
        ma20 = float(closes.iloc[-20:].mean())
        ma50 = float(closes.iloc[-50:].mean()) if len(closes) >= 50 else ma20
        high52 = float(df["High"].iloc[-252:].max()) if len(df) >= 252 else float(df["High"].max())
        rets = closes.pct_change().dropna().iloc[-20:]
        vol_ann = float(rets.std() * (252 ** 0.5) * 100) if len(rets) >= 10 else 0.0
        chg = lambda n: round((price / float(closes.iloc[-1 - n]) - 1) * 100, 2) if len(closes) > n else 0.0
        return {
            "price": round(price, 2),
            "ma20": round(ma20, 2),
            "ma50": round(ma50, 2),
            "rsi": self._rsi(closes),
            "high_52w": round(high52, 2),
            "dist_to_52w_high_pct": round((high52 / price - 1) * 100, 2),
            "price_above_ma20": price > ma20,
            "price_above_ma50": price > ma50,
            "change_5d": chg(5),
            "change_20d": chg(20),
            "volatility_ann_pct": round(vol_ann, 1),
        }

    def get_vix(self) -> Optional[float]:
        df = self.get_history(US_INDEX["vix"], period="1mo")
        return round(float(df["Close"].iloc[-1]), 2) if df is not None else None

    def get_index_rsi(self, key: str = "spy") -> Optional[float]:
        df = self.get_history(US_INDEX.get(key, "SPY"))
        return self._rsi(df["Close"]) if df is not None else None

    def get_days_to_earnings(self, symbol: str) -> Optional[int]:
        """距下次財報天數；失敗回 None（不阻斷主流程）"""
        try:
            cal = yf.Ticker(symbol).calendar
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if not dates:
                return None
            d = dates[0] if isinstance(dates, list) else dates
            return int((pd.Timestamp(d).normalize() - pd.Timestamp.now().normalize()).days)
        except Exception as e:
            logger.warning("%s 財報日取得失敗：%s", symbol, e)
            return None

    def scan(self, symbols: Optional[List[str]] = None) -> Dict[str, Dict]:
        out = {}
        for sym in (symbols or US_WATCHLIST):
            tech = self.get_technical_indicators(sym)
            time.sleep(self.sleep_between)
            if tech is None:
                continue
            tech["days_to_earnings"] = self.get_days_to_earnings(sym)
            time.sleep(self.sleep_between)
            out[sym] = tech
        return out
