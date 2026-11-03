"""Issue #205 AC 測試：金融股旁路、基本面快取降載、循環股攔截。"""
import json
from pathlib import Path

from src.screener_value_shield import (
    CachedFundamentalsProvider, Fundamentals, YFinanceBackend, check_fundamentals)


# ---- AC1: 金融股倖存測試（JPM FCF<0 不被誤殺，走 P/B + ROE 門檻）----
def test_financial_sector_bypass_survives():
    jpm = Fundamentals(ticker="JPM", sector="Financial Services",
                       fcf=-999.0, debt_ratio=9.1,          # 一般規則必死
                       pb=1.2, pb_5y_median=1.6,            # P/B < 5Y 中位數
                       roe_latest=13.0, roe_3y_avg=12.5)    # ROE(3Y) > 10%
    r = check_fundamentals(jpm)
    assert r.passed and r.bypassed

    brk = Fundamentals(ticker="BRK.B", sector="Financial Services",
                       fcf=-500.0, debt_ratio=6.0,
                       pb=1.05, pb_5y_median=1.2, roe_latest=11.0, roe_3y_avg=10.5)
    assert check_fundamentals(brk).passed

    # 金融股但門檻不過 → 仍應剔除（防雷不放水）
    bad = Fundamentals(ticker="GL", sector="Financial Services",
                       fcf=-1.0, debt_ratio=3.0,
                       pb=2.5, pb_5y_median=1.4, roe_latest=8.0, roe_3y_avg=7.0)
    assert not check_fundamentals(bad).passed


# ---- AC2: API 降載測試（第二次執行 yfinance 呼叫次數 = 0）----
def test_second_run_zero_api_calls(tmp_path):
    cache_file = tmp_path / "fundamentals_cache.json"
    fake_payload = {"sector": "Technology", "fcf": 10.0, "debt_ratio": 0.4,
                    "pb": 3.0, "pb_5y_median": 4.0, "roe_latest": 18.0, "roe_3y_avg": 16.0}
    backend = YFinanceBackend(fetcher=lambda t: dict(fake_payload))
    provider = CachedFundamentalsProvider(backend, cache_path=cache_file, now=lambda: 1_700_000_000.0)

    # Run 1：快取冷 → 各打一次 API
    for tk in ("AAPL", "MSFT", "TSM"):
        f = provider.get(tk)
        assert f.ticker == tk and check_fundamentals(f).passed
    assert backend.api_calls == 3

    # Run 2：同一排程再跑一次 → 100% 命中快取，API 呼叫必須為 0
    calls_before = backend.api_calls
    for tk in ("AAPL", "MSFT", "TSM"):
        assert check_fundamentals(provider.get(tk)).passed
    assert backend.api_calls == calls_before  # 增量 = 0


def test_cache_ttl_expiry_forces_refetch(tmp_path):
    cache_file = tmp_path / "c.json"
    payload = {"sector": "x", "fcf": 1.0, "debt_ratio": 0.1, "pb": 1, "pb_5y_median": 2,
               "roe_latest": 20, "roe_3y_avg": 15}
    backend = YFinanceBackend(fetcher=lambda t: dict(payload))
    clock = {"t": 0.0}
    provider = CachedFundamentalsProvider(backend, cache_path=cache_file,
                                          now=lambda: clock["t"])
    provider.get("TWTR")
    assert backend.api_calls == 1
    clock["t"] += 29 * 86400           # TTL 內 → 命中快取
    provider.get("TWTR")
    assert backend.api_calls == 1
    clock["t"] += 2 * 86400            # 超過 30 天 TTL → 重新抓取
    provider.get("TWTR")
    assert backend.api_calls == 2


# ---- AC3: 循環股攔截測試（單年 ROE=40% 但 3Y Avg=8% 的航運股）----
def test_cyclical_stock_rejected():
    shipping = Fundamentals(ticker="2618.TW", sector="Industrials",
                            fcf=25.0, debt_ratio=0.6,       # 其他指標全過
                            pb=1.1, pb_5y_median=1.5,
                            roe_latest=40.0, roe_3y_avg=8.0)  # 單年暴衝、三年平均不合格
    r = check_fundamentals(shipping)
    assert not r.passed and "循環股" in r.reason
