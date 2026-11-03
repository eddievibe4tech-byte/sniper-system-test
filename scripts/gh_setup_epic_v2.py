#!/usr/bin/env python3
"""建立「機構級風控升級 v2」Epic Issue、4 個子 Issues、Labels 與對應 PR。

使用 GitHub REST API (curl) 於 eddievibe4tech-byte/sniper-system-test 遠端建立：
  1. Labels: epic, enhancement, screener, cache, radar, event-chain,
     sizing, capital-pool, state-machine, notification
  2. Epic Issue（含 4 個 sub-issues）
  3. Issue 1~4（依落地順序：Screener -> Radar -> Notify -> Sizing，皆掛在 Epic 下）
  4. Branch + commits + PR（PR body closes #N），並把 PR 也掛為 Epic 的 sub-issue
"""
import json
import subprocess
import sys
import time

import os

OWNER = "eddievibe4tech-byte"
REPO = "sniper-system-test"
# 安全：token 一律由環境變數注入，嚴禁硬編碼（GitHub Push Protection / Secret Scanning）
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
if not TOKEN:
    sys.exit("請先設定環境變數 GH_TOKEN=<your PAT>")
API = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"
BASE_BRANCH = "main"


def curl(method, url, payload=None):
    cmd = ["curl", "-s", "-X", method, url,
           "-H", f"Authorization: Bearer {TOKEN}",
           "-H", "Accept: application/vnd.github+json",
           "-H", "Content-Type: application/json"]
    if payload is not None:
        cmd += ["--data", json.dumps(payload)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return {"_raw": res.stdout, "_err": res.stderr}


def gh_headers():
    return ["-H", f"Authorization: Bearer {TOKEN}",
            "-H", "Accept: application/vnd.github+json"]


def get_node_id(number):
    """取得 issue/PR 的 node_id（sub-issue 掛載用）。"""
    out = subprocess.run(
        ["gh", "api", f"repos/{OWNER}/{REPO}/issues/{number}",
         "--jq", ".node_id"],
        capture_output=True, text=True)
    nid = out.stdout.strip()
    if not nid:
        raise RuntimeError(f"取得 #{number} node_id 失敗: {out.stderr}")
    return nid


def add_label(name, color, description):
    r = curl("POST", f"{API}/repos/{OWNER}/{REPO}/labels",
             {"name": name, "color": color, "description": description})
    if "already_exists" in str(r.get("errors", "")) or r.get("name") == name:
        print(f"  label `{name}` OK")
        return True
    print(f"  label `{name}` -> {r}")
    return False


def create_issue(title, body, labels):
    r = curl("POST", f"{API}/repos/{OWNER}/{REPO}/issues",
             {"title": title, "body": body, "labels": labels})
    num, url = r.get("number"), r.get("html_url")
    if not num:
        print(f"  建立失敗: {r}")
        sys.exit(1)
    print(f"  #{num} {url}")
    return num, url


def link_subissue(parent_num, child_num):
    parent_node = get_node_id(parent_num)
    child_node = get_node_id(child_num)
    q = ("mutation($parent:ID!,$child:ID!){addSubIssue(input:"
         "{issueId:$parent,subissueId:$child}){issue{number}"
         ",subissues(first:10){nodes{number}}}}")
    res = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={q}",
         "-F", f"parent={parent_node}", "-F", f"child={child_node}"],
        capture_output=True, text=True)
    ok = '"number"' in res.stdout and "errors" not in res.stdout.lower().replace('"errors": null', '')
    if "errors" in res.stdout:
        print(f"  ⚠️ sub-issue #{child_num} 掛載回應: {res.stdout[:300]} {res.stderr[:200]}")
        return False
    print(f"  ✅ Epic #{parent_num} <- sub-issue #{child_num}")
    return True


# ---------------------------------------------------------------- Labels
LABELS = [
    ("epic", "5319E7", "Epic：跨多 Issue 的大型工作彙總"),
    ("enhancement", "a2eeef", "新功能 / 優化"),
    ("screener", "d4c5f9", "選股模組"),
    ("cache", "fbc4c4", "快取 / 效能"),
    ("radar", "c5def5", "雷達訊號模組"),
    ("event-chain", "fef2c0", "事件鏈 / 時序邏輯"),
    ("sizing", "94ccff", "資金調度 / 部位管理"),
    ("capital-pool", "ededed", "資金池模式"),
    ("state-machine", "e4e669", "狀態機 / 遲滯"),
    ("notification", "1d76db", "推播通知"),
]

# ---------------------------------------------------------------- Epic
EPIC_BODY = """## 🎯 Epic: 機構級風控升級 v2（雙層事件鏈 / 極端熊市網格 / 價值防雷 / 遲滯狀態機）

> 本 Epic 源自對 v1 狙擊手系統的 Code Review，識別出量化系統從「實驗室」走向「實盤真金白銀」
> 最容易翻車的 **5+1 個盲點**，將其轉化為 4 個可獨立驗收、具嚴格測試覆蓋率的 Issues。

### 原始 Code Review 識別的盲點

| # | 盲點 | 風險類別 | 對應 Issue |
|---|------|----------|-----------|
| 1 | **金融股誤殺** — Hard Filter 的 FCF/負債指標對金融業失真，會 100% 誤殺 JPM / BRK.B | 會計準則 | #Issue 1 |
| 2 | **API 超時 / 429 Rate Limit** — 每日排程對 1500+ 檔股票抓取 3 年財報 | 數據源穩定性 | #Issue 1 |
| 3 | **RSI 時態衝突** — RSI < 30（極度超賣）與「連續 2 天未破低」（止跌反彈）難以同一天共存 | 時序邏輯 | #Issue 2 |
| 4 | **除息陷阱** — 台股 ETF（0050）除息跳空造成假破線 / 假超賣訊號 | 數據品質 | #Issue 2 |
| 5 | **子彈過早打光** — MDD -20% 就耗盡預備金，無法防禦 2008/2020 級別海嘯 | 人性弱點 / 资金管理 | #Issue 3 |
| 6 | **邊界震盪洗盤** — 價格在 MDD 10% 邊界震盪導致 State 頻繁跳轉、通知疲勞 | 狀態管理 | #Issue 4 |

四大難題總結：**會計準則（金融股）、數據品質（除息/快取）、時序邏輯（事件鏈）、人性弱點（子彈管理/通知疲勞）**。

### 子 Issues（依建議落地順序）

1. `feat(screener)` 個股價值防雷、金融股旁路與基本面快取 —— 基礎建設，確保數據源穩定且不誤殺好股
2. `feat(radar)` 雙層濾網事件鏈、除息還原與波動度適配 —— 修正最容易產生「假訊號」的技術面邏輯
3. `feat(notify)` 遲滯狀態機與持久化推播 —— 讓系統具備記憶力，杜絕通知疲勞
4. `feat(sizing)` 極端熊市階梯網格與資金池定義 —— 最後掛上最複雜的資金調度與部位管理邏輯

### 驗收總則

- 每個 Issue 的 AC checkbox 需全部勾選，且對應測試（pytest / behave BDD）全數通過。
- 每個 Issue 各有對應 PR，PR merge 後自動 close 該 Issue；四者全數完成即關閉本 Epic。
"""

# ---------------------------------------------------------------- Issue 1
I1_TITLE = "feat(screener): 個股價值防雷、金融股旁路與基本面快取"
I1_BODY = """### 🏷️ Labels: `enhancement` `screener` `cache`

### 背景與動機
原版 Hard Filter 會 **100% 誤殺金融股**（FCF/負債指標對金融業失真），且 yfinance 每日排程
頻繁抓取 1500+ 檔股票的 3 年財報，導致 **429 Rate Limit 與超時崩潰**。

### 實作重點
1. **金融股旁路 (Sector Bypass)**：若 `sector == 'Financial Services'`，跳過 FCF 與負債比檢查，
   改用 `P/B < 歷史 5 年中位數` 且 `ROE > 10%`。
2. **基本面快取 (Fundamentals Cache)**：引入本地 JSON/SQLite 快取機制，設定 TTL 為 30 天（季報頻率），
   避免每日排程發出 1500+ 次 API 請求。（複用既有 `src/financial_cache.py` 擴充）

### ✅ 驗收條件 (AC)
- [ ] **金融股倖存測試**：匯入 JPM (摩根大通) 或 BRK.B (波克夏) 數據，驗證其不被 FCF < 0 誤殺，且能通過金融股專屬的 P/B + ROE 門檻。
- [ ] **API 降載測試**：連續執行兩次 Screener，第二次執行時 yfinance 財報 API 呼叫次數必須為 0（100% 命中本地快取）。
- [ ] **循環股攔截測試**：模擬一檔單年 ROE=40% 但 3Y Avg ROE=8% 的航運股，驗證其被成功剔除。

Part of Epic #EPIC_NUM
"""

# ---------------------------------------------------------------- Issue 2
I2_TITLE = "feat(radar): 雙層濾網事件鏈、除息還原與波動度適配"
I2_BODY = """### 🏷️ Labels: `enhancement` `radar` `event-chain`

### 背景與動機
RSI < 30（極度超賣）與「連續 2 天未破低」（止跌反彈）**難以在同一天共存**（時態衝突）；
且台股 ETF（如 0050）**除息跳空**會造成假破線陷阱。

### 實作重點
1. **兩階段事件鏈 (Event Chain)**：
   - *條件 A (Over-sold Memory)*：近 5 個交易日內，曾出現 `RSI(14) < 30`。
   - *條件 B (Confirmation)*：在條件 A 有效期內，觸發「今日收盤 > 昨日最高」或「連續 2 天 Higher Low」。
2. **強制除息還原**：所有價格與均線計算強制使用 `Adj Close`。
3. **波動度適配（可選/後續迭代）**：將固定 MDD 10% 改為與標的 200 日歷史波動率 (HV) 聯動
   （如 QQQ 門檻放寬，VOO 門檻收緊）。

### ✅ 驗收條件 (AC)
- [ ] **時態解耦測試**：輸入一檔 Day 1 RSI=28，Day 2~4 盤整，Day 5 收盤突破昨日高點的數據 ➜ 系統必須在 Day 5 成功觸發進場確認。
- [ ] **除息防呆測試**：輸入 0050 除息日數據（Close 大跌但 Adj Close 平穩） ➜ 系統不得觸發假跌破或假超賣訊號。
- [ ] **靜音守門測試**：年線乖離率 > 0% 且 MDD < 10% 時，即使 RSI < 30，系統仍強制輸出 `State 0 (靜音)`。

Part of Epic #EPIC_NUM
"""

# ---------------------------------------------------------------- Issue 3
I3_TITLE = "feat(sizing): 極端熊市階梯網格與資金池定義"
I3_BODY = """### 🏷️ Labels: `enhancement` `sizing` `capital-pool`

### 背景與動機
原版 MDD -20% 子彈就打光，**無法防禦 2008/2020 級別海嘯**；且「戰術部位結算」存在
**資金回補悖論**需要釐清。

### 實作重點
1. **拉開 MDD 級距（四階梯網格）**：
   - Stage 1 (MDD 10%~15%)：釋放 20% 預備金。
   - Stage 2 (MDD 15%~25%)：釋放 30% 預備金。
   - Stage 3 (MDD 25%~35%)：釋放 30% 預備金。
   - Stage 4 (MDD ≥ 35%)：釋放剩餘 20%（末日彈藥）。
2. **資金池模式定義**（Config 明確區分兩種模式）：
   - *純長線累積模式（預設）*：抄底部位直接併入核心庫存，不賣出；Reserve Capital 由外部現金流（如月薪）定期補回。
   - *波段回補模式*：定義明確出場規則（如：從谷底反彈 +15% 或突破季線站穩年線），獲利了結後資金回滾至現金池。

### ✅ 驗收條件 (AC)
- [ ] **極端熊市測試**：模擬 MDD 達 -32% ➜ 驗證 Stage 1~3 已釋放 80% 資金，且 Stage 4 的 20% 末日彈藥仍被保留。
- [ ] **資金池切換測試**：設定為「純長線累積模式」時，驗證狀態重置回 Stage 0 時，**不會**產生賣出/結算指令。
- [ ] **邊界歸屬測試**：MDD 恰好為 15.0% 時，驗證其正確歸屬至 Stage 2 的觸發區間。

Part of Epic #EPIC_NUM
"""

# ---------------------------------------------------------------- Issue 4
I4_TITLE = "feat(notify): 遲滯狀態機與持久化推播"
I4_BODY = """### 🏷️ Labels: `enhancement` `state-machine` `notification`

### 背景與動機
價格在 MDD 10% 邊界震盪導致 State 頻繁跳轉（**洗盤**），造成通知疲勞；
且伺服器重啟可能**遺失狀態**。

### 實作重點
1. **遲滯緩衝帶 (Hysteresis)**：
   - 進入 State 1：MDD ≥ 10.0%。
   - 退出 State 1：MDD 必須收窄至 ≤ 7.0%（中間 7%~10% 為灰色緩衝區，維持原狀態）。
2. **狀態持久化**：實作 `data/market_state.json`，記錄 `prev_state`、`current_mdd` 與 `last_transition_time`。

### ✅ 驗收條件 (AC)
- [ ] **防洗盤測試**：Day 1 MDD=10.1% (進入 State 1) ➜ Day 2 MDD=9.9% (維持 State 1) ➜ Day 3 MDD=10.2% (維持 State 1，不重複推播) ➜ Day 4 MDD=6.5% (退出 State 1)。
- [ ] **持久化重啟測試**：觸發 State 1 後，手動刪除記憶體中的狀態變數（模擬 Docker 重啟），讀取 `market_state.json` 後，系統能正確接續狀態，不發送重複通知。
- [ ] **冷卻防護測試**：若因極端行情導致狀態在 24 小時內發生 `State 0 ➔ 1 ➔ 0` 的完整循環，第二次進入 State 1 必須被 24 小時冷卻機制攔截。

Part of Epic #EPIC_NUM
"""


def main():
    print("== Step 1: Labels ==")
    for name, color, desc in LABELS:
        add_label(name, color, desc)

    print("== Step 2: Epic Issue ==")
    epic_num, epic_url = create_issue(
        "Epic: 機構級風控升級 v2（雙層事件鏈 / 極端熊市網格 / 價值防雷 / 遲滯狀態機）",
        EPIC_BODY, ["epic", "enhancement"])

    print("== Step 3: Sub Issues（依落地順序）==")
    issues = []
    for title, body, labels in [
        (I1_TITLE, I1_BODY, ["enhancement", "screener", "cache"]),
        (I2_TITLE, I2_BODY, ["enhancement", "radar", "event-chain"]),
        (I4_TITLE, I4_BODY, ["enhancement", "state-machine", "notification"]),
        (I3_TITLE, I3_BODY, ["enhancement", "sizing", "capital-pool"]),
    ]:
        num, url = create_issue(title, body.replace("#EPIC_NUM", f"#{epic_num}"), labels)
        issues.append((num, url, title))
        time.sleep(1)

    print("== Step 4: Link sub-issues to Epic ==")
    for num, _, _ in issues:
        link_subissue(epic_num, num)

    result = {"epic": {"number": epic_num, "url": epic_url},
              "issues": [{"number": n, "url": u, "title": t} for n, u, t in issues]}
    with open("/tmp/gh_created.json", "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
