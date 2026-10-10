#!/usr/bin/env python3
"""
🆕 Verification v2.1 一次性重評分腳本

將所有已驗證 telemetry 記錄以 v2.1 對沖基金規則重新判定，讓歷史與新規則口徑一致
（否則新舊混算，勝率曲線會斷裂）。

v2.1 重點修復（相對初版 v2 建議）：
  - 🔴 時窗鎖定：個股與基準一律取 [T, T+H] 交易日報酬（windowed_return），
    不再拿「5 天個股報酬」比「90 天大盤報酬」。
  - 視窗資料不足 → 延後結算（deferred），保留原 actual_result 不硬算。
  - 價格序列按 code 快取，regrade 不重複打 API。
  - 期望值/統計由 compute_alpha_stats 純函數負責（此處只重寫單筆判定）。

用法：python scripts/regrade_telemetry.py [data/telemetry.json]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.main import (judge_correctness, windowed_return, _fetch_benchmark_closes,
                      FinMindClient, BENCHMARK_CODE)


def _series_from_rows(rows) -> list:
    series = [{"date": str(x.get("date", ""))[:10], "close": float(x.get("close", 0))}
              for x in (rows or []) if x.get("close")]
    series.sort(key=lambda s: s["date"])
    return series


def main() -> int:
    path: Path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/telemetry.json")
    data: dict = json.loads(path.read_text(encoding="utf-8"))
    fm = FinMindClient()
    bench: list = _fetch_benchmark_closes(fm, days=400)
    if not bench:
        print("⚠️ 基準 0050 抓取失敗：alpha 條件將降級為絕對報酬規則")
    cache: dict = {}
    n: int = 0
    deferred: int = 0
    for r in data.get("records", []):
        ar = r.get("actual_result")
        if not ar or r.get("legacy") or r.get("entry_price") is None:
            continue
        code, pred_date = r["stock_code"], r["timestamp"][:10]
        if code not in cache:
            try:
                cache[code] = _series_from_rows(
                    fm._make_request("TaiwanStockPrice", code, days=400))
            except Exception:
                cache[code] = []
        # 🔴 锁定 [T, T+5] 交易日窗口，以 entry_price 为锚；未满 → 延后
        ret = windowed_return(cache[code], pred_date, 5, anchor_price=r["entry_price"])
        if ret is None:
            deferred += 1
            continue
        bench_ret = windowed_return(bench, pred_date, 5) if bench else None
        # 🔴 PR review #7：25.0% 保守預設，與 src/main.py auto_verify 口徑一致
        vol = (r.get("input") or {}).get("volatility") \
              or (r.get("prediction") or {}).get("volatility") or 25.0
        v = judge_correctness((r.get("prediction") or {}).get("recommendation", ""),
                              ret, bench_ret, vol, 5)
        ar.update(v)
        ar.update({"profit_pct": ret, "actual_return_pct": ret,
                   "benchmark": BENCHMARK_CODE,
                   "benchmark_ret_pct": bench_ret, "window": f"{pred_date}+5td"})
        r["accuracy"] = 1 if v["was_correct"] else 0
        n += 1
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 已重評分 {n} 筆（v2.1 規則）；{deferred} 筆因時窗資料不足延後")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
