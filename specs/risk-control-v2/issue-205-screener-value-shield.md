# Spec: Issue #205 — feat(screener): 個股價值防雷、金融股旁路與基本面快取

## 設計摘要
1. **Sector Bypass**：`sector == 'Financial Services'` → 跳過 FCF/負債比，改查 `P/B < 5Y 中位數` 且 `ROE > 10%`。
2. **Fundamentals Cache**：本地 JSON 快取（TTL = 30 天，季報頻率），命中時 yfinance API 呼叫次數為 0。
3. **循環股攔截**：單年 ROE 高但 3Y Avg ROE < 10% → 剔除（Value Trap Filter）。

## AC 對應測試
| AC | 測試 |
|----|------|
| 金融股倖存（JPM/BRK.B） | tests/unit/test_screener_value_shield.py::test_financial_sector_bypass_survives |
| API 降載（第二次 0 呼叫） | ::test_second_run_zero_api_calls |
| 循環股攔截 | ::test_cyclical_stock_rejected |
