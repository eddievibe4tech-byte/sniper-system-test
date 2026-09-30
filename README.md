

# 🎯 狙擊手系統（Sniper System）

> **Regime 自適應正期望值投資決策系統**
> 結合盤後數據分析、AI 輔助判斷、風險評估、Prompt 自動優化的一站式投資輔助平台。

---

## 📖 目錄

- [系統概述](#系統概述)
- [核心設計理念](#核心設計理念)
- [技術架構](#技術架構)
- [技術棧](#技術棧)
- [目錄結構](#目錄結構)
- [安裝與設定](#安裝與設定)
- [模組詳細說明](#模組詳細說明)
- [Prompt 模板](#prompt-模板)
- [Telemetry 遙測系統](#telemetry-遙測系統)
- [Prompt 自動優化器](#prompt-自動優化器)
- [視覺化與互動儀表板](#視覺化與互動儀表板)
- [GitHub Actions 排程](#github-actions-排程)
- [使用流程](#使用流程)
- [風險管理機制](#風險管理機制)
- [常見問題](#常見問題)
- [後續擴展方向](#後續擴展方向)

---

## 系統概述

### 系統定位

本系統是一套**輔助決策工具**，而非自動交易機器人。它負責：

- ✅ 收集與整理盤後數據（營收、籌碼、技術面）
- ✅ 根據市場體制（Regime）動態調整評分權重
- ✅ 使用 AI 進行多維度分析與風險提示
- ✅ 追蹤預測準確率並自動優化分析策略
- ✅ 生成視覺化報告供人工決策參考

### 系統不做的事

- ❌ 自動下單或自動交易
- ❌ 預測具體價格或漲跌幅度
- ❌ 替代人工的最終投資決策
- ❌ 保證任何投資報酬

### 適用對象

- 資金規模：5 萬元至 50 萬元的小資投資者
- 投資風格：盤後分析、右側交易、短線波段
- 使用工具：權證、零股、ETF
- 時間投入：每日 15 分鐘查看報告，每週 30 分鐘復盤

---

## 核心設計理念

### 1. 正期望值（Expected Value）

> 參考馬克羊（Mark Yang）的交易哲學：散戶賠錢不是心態問題，而是策略沒有正期望值。

系統的核心目標是建立一個**長期期望值為正**的交易策略，並透過數據追蹤持續驗證：

```
期望值 (EV) = (勝率 × 平均獲利) - (敗率 × 平均虧損)
```

只有當 `EV > 0` 且交易次數足夠多時，策略才具備統計意義。

### 2. Regime 自適應

> 市場環境會改變，再有效的策略也會失效。

系統不依賴固定的參數，而是根據當前市場體制動態調整：

| 市場體制 | 特徵 | 策略調整 |
|---|---|---|
| **多頭** | 大盤在60日均線之上，成交量放大 | 放寬動能追價門檻，積極右側追價 |
| **震盪** | 大盤在20-60日均線間糾結 | 加重籌碼權重，箱型下緣狙擊 |
| **空頭** | 大盤跌破60日均線 | 極度保守，現金為王 |

### 3. 低相關性資產配置

> 分散風險不是買很多不同的股票，而是買「連動性低」的資產。

建議的資產配置：

| 層級 | 佔比 | 標的 | 功能 |
|---|---|---|---|
| 核心資產 | 50% | 009816 凱基 TOP 50 | 長期複利，賺取市場 Beta |
| 避險資產 | 30% | 現金 / 短天期美債 | 低相關性防守 |
| 衛星狙擊 | 20% | 權證 / 零股 | 創造超額 Alpha |

### 4. 手割分析（決策方法論）

> 去除相同的，只比較不同的。

在選擇交易工具時，使用手割分析：

| 情境 | 選擇 | 理由 |
|---|---|---|
| 預期 3-5 天內完成漲幅 | 權證 | 槓桿大，資金效率高 |
| 預期 1 個月以上的波段 | 零股 | 無時間價值流失 |
| 接近除息日（<7天） | 觀望 | 避免除息扣價風險 |
| 波動率 > 30% | 零股 | 權證時間價值流失過快 |

---

## 技術架構

### 架構圖

```
┌─────────────────────────────────────────────────────────────┐
│                        GitHub Repository                      │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                  GitHub Actions                        │   │
│  │  ┌─────────────────┐    ┌─────────────────────────┐   │   │
│  │  │ daily-analysis   │    │ weekly-optimization      │   │   │
│  │  │ 每日 17:30 UTC+8 │    │ 每週日 20:00 UTC+8       │   │   │
│  │  └────────┬────────┘    └──────────┬──────────────┘   │   │
│  └───────────┼────────────────────────┼──────────────────┘   │
│              │                        │                        │
│              ▼                        ▼                        │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                   Python 分析引擎                      │   │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌─────────┐ │   │
│  │  │ main.py  │ │telemetry │ │prompt_   │ │ risk_   │ │   │
│  │  │ 主分析   │ │ .py      │ │optimizer │ │calculator│ │   │
│  │  └────┬─────┘ └────┬─────┘ │ .py      │ │ .py     │ │   │
│  │       │             │       └────┬─────┘ └────┬────┘ │   │
│  └───────┼─────────────┼────────────┼────────────┼──────┘   │
│          │             │            │            │            │
│          ▼             ▼            ▼            ▼            │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                    外部 API                            │   │
│  │  ┌──────────────┐  ┌──────────────┐                  │   │
│  │  │ FinMind API  │  │ Groq API     │                  │   │
│  │  │ 台股數據     │  │ Llama 3.3 70B│                  │   │
│  │  └──────────────┘  └──────────────┘                  │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                   數據存儲層 (JSON)                    │   │
│  │  regime.json │ stock_pool.json │ telemetry.json       │   │
│  │  deep_analysis.json │ risk_report.json                │   │
│  │  prompt_history.json │ performance_metrics.json       │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              靜態展示層 (GitHub Pages)                 │   │
│  │  index.html │ dashboard.html                          │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 數據流向

```
FinMind API ──→ Python 分析引擎 ──→ Groq AI 分析 ──→ JSON 數據文件
                                                          │
                                                          ▼
                                              GitHub Pages / Cloudflare Pages
                                                          │
                                                          ▼
                                                   使用者查看報告
                                                          │
                                                          ▼
                                              手動決策 → 記錄結果
                                                          │
                                                          ▼
                                              Telemetry 收集 → 每週優化
```

---

## 技術棧

### 核心技術

| 技術 | 版本 | 用途 |
|---|---|---|
| **Python** | 3.11+ | 主分析引擎 |
| **GitHub Actions** | - | 排程與自動化 |
| **GitHub Pages** | - | 靜態頁面託管 |
| **JSON** | - | 數據存儲格式 |

### API 服務

| 服務 | 用途 | 費用 |
|---|---|---|
| **Groq API** | AI 模型推理（Llama 3.3 70B） | 免費 |
| **FinMind API** | 台股盤後數據 | 免費（需註冊 Token） |

### AI 模型

| 模型 | 平台 | 用途 | 選擇理由 |
|---|---|---|---|
| **Llama 3.3 70B** | Groq | 主力分析、深度推理、Prompt 優化 | 免費、速度快、邏輯強、免綁卡 |
| **Qwen 2.5 72B**（備選） | 阿里雲百煉 / OpenRouter | 深度推理（中文更強） | 需額外申請，可後續切換 |
| **Mixtral 8x7B**（不建議） | Groq | - | 中文金融推理能力不足 |

#### Groq 模型選擇建議（針對 Prompt 優化）

由於本系統目前僅使用 Groq API 進行 Prompt 自動優化，以下是針對不同任務的模型選擇與參數設定建議：

**首選模型：`llama-3.3-70b-versatile`**
- **為什麼？** Prompt 優化需要高階推理 (Meta-Reasoning) 與指令遵循 (Instruction Following) 能力。Llama 3.3 70B 具備極強的邏輯分析與長文本指令遵循能力，是目前開源模型中最適合做 Meta-Prompting 的選擇。
- **備選 (若 Context 極大)**：`mixtral-8x7b-32768`。如果需要丟入大量失敗預測 Log (可能超過 10k tokens)，Mixtral 的 32k Context Window 會有優勢，但 70B 的優化品質通常更好。

**推薦 API 參數設定：**
```python
response = client.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[
        {"role": "system", "content": OPTIMIZER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt_with_error_logs}
    ],
    temperature=0.6,        # 0.5~0.7 最適合重寫 Prompt
    top_p=0.9,
    max_tokens=4096,        # 確保有足夠空間輸出完整的 Prompt 模板
    response_format={"type": "json_object"}  # 強制輸出 JSON，方便程式解析
)
```

**Meta-Prompt 設計要點：**
- 讓 AI 扮演「Prompt Engineer」角色
- 分析舊 Prompt 導致預測錯誤的原因
- 在【台股邏輯範例】中補充防呆規則
- 保持核心結構與 JSON 輸出格式不變
- 強制輸出 JSON 格式以便程式解析

### 前端技術

| 技術 | 用途 |
|---|---|
| **HTML5 / CSS3 (Tailwind)** | 靜態頁面結構與響應式 RWD 樣式 |
| **HTML5 Canvas** | 🆕 繪製價格地圖（進場區間、現價、停損停利線） |
| **原生 JavaScript (ES6+)** | 🆕 Client-side 邏輯運算（JS 鏡像 Python 邏輯，確保前後端一致性） |
| **Web Storage (localStorage)** | 🆕 本機儲存使用者持倉成本（隱私保護，不上傳伺服器） |
| **Chart.js 4.4+** | 儀表板圖表視覺化 |

### 可選擴展

| 技術 | 用途 | 何時使用 |
|---|---|---|
| **Cloudflare Pages** | CDN 加速、全球部署 | 需要更快的訪問速度時 |
| **Grafana Cloud** | 專業監控與警報 | 系統規模擴大後 |
| **OpenRouter** | 多模型 API 聚合 | 想切換使用 Qwen 時 |

---

## 目錄結構

```
sniper-system/
│
├── .github/
│   └── workflows/
│       ├── daily-analysis.yml          # 每日分析排程
│       └── weekly-optimization.yml     # 每週 Prompt 優化排程
│
├── data/                               # 數據存儲目錄
│   ├── regime.json                     # 市場體制與風險參數
│   ├── stock_pool.json                 # 股票監控池
│   ├── deep_analysis.json              # AI 深度分析結果
│   ├── risk_report.json                # 風險評估報告
│   ├── telemetry.json                  # 遙測記錄
│   ├── prompt_history.json             # Prompt 優化歷史
│   └── performance_metrics.json        # 績效指標
│
├── prompts/                            # Prompt 模板目錄
│   ├── main_analysis.txt               # 主力分析 Prompt
│   ├── deep_analysis.txt               # 深度推理 Prompt
│   ├── regime_judgment.txt             # Regime 判斷 Prompt
│   ├── main_analysis_v1.txt            # 歷史版本備份
│   └── main_analysis_v2.txt            # 歷史版本備份
│
├── src/                                # 原始碼目錄
│   ├── main.py                         # 主程式入口
│   ├── groq_client.py                  # Groq API 客戶端
│   ├── finmind_client.py               # FinMind API 客戶端
│   ├── trading_plan.py                 # 🆕 交易計畫生成（進出場策略、移動停利）
│   ├── position_manager.py             # 🆕 持倉管理與進場觸發條件生成
│   ├── risk_calculator.py              # 風險計算模組
│   ├── telemetry.py                    # 遙測記錄模組
│   └── prompt_optimizer.py             # Prompt 優化器
│
├── index.html                          # 靜態展示頁面
├── dashboard.html                      # 監控儀表板
├── requirements.txt                    # Python 依賴
├── .gitignore                          # Git 忽略規則
└── README.md                           # 本文件
```

---

## 安裝與設定

### 前置需求

- GitHub 帳號
- Groq API Key（免費）
- FinMind Token（免費）
- 基本的 Git 操作知識

### 步驟 1：取得 API Key

#### Groq API Key
1. 前往 [Groq Console](https://console.groq.com/)
2. 使用 Google 帳號登入（**不需要綁信用卡**）
3. 進入 `API Keys` 頁面
4. 點擊 `Create API Key`，複製生成的 Key（格式：`gsk_...`）

#### FinMind Token
1. 前往 [FinMind 官網](https://finmind.github.io/)
2. 註冊帳號
3. 登入後，在個人頁面取得 `API Token`

### 步驟 2：建立 GitHub Repository

```bash
# 建立專案資料夾
mkdir sniper-system
cd sniper-system
git init

# 建立目錄結構
mkdir -p .github/workflows data prompts src

# 初始化 git
echo "# Sniper System" > README.md
echo "*.pyc" > .gitignore
echo "__pycache__/" >> .gitignore
echo ".env" >> .gitignore

git add .
git commit -m "Initial commit"
```

### 步驟 3：設定 GitHub Secrets

進入 GitHub Repo → **Settings** → **Secrets and variables** → **Actions**：

| Secret Name | Value | 說明 |
|---|---|---|
| `GROQ_API_KEY` | `gsk_...` | Groq API 金鑰 |
| `FINMIND_TOKEN` | `你的 Token` | FinMind API 金鑰 |

### 步驟 4：啟用 GitHub Pages

進入 GitHub Repo → **Settings** → **Pages**：

- **Source**: `Deploy from a branch`
- **Branch**: `main` / `root`
- 點擊 **Save**

幾分鐘後，靜態頁面將在 `https://你的帳號.github.io/sniper-system/` 上線。

### 步驟 5：（可選）設定 Cloudflare Pages

1. 前往 [Cloudflare Pages](https://pages.cloudflare.com/)
2. 點擊 `Create a project` → `Connect to Git`
3. 選擇你的 GitHub Repo
4. 設定：
   - **Build command**: 留空（純靜態，不需要建置）
   - **Build output directory**: `/`（根目錄）
5. 點擊 `Save and Deploy`

---

## 模組詳細說明

### 1. `src/main.py` — 主程式

**功能**：每日分析的主入口，協調所有模組。

**執行流程**：
```
1. 讀取 regime.json（市場體制設定）
2. 讀取 stock_pool.json（監控池）
3. 遍歷每檔股票：
   a. 呼叫 FinMind API 抓取數據
   b. 計算波動率與風險等級
   c. 計算除息天數與權證條件
   d. 呼叫 Groq AI 進行分析
   e. 記錄到 Telemetry
4. 執行整體風險評估
5. 儲存結果到 JSON
6. Commit + Push 回 GitHub
```

### 2. `src/groq_client.py` — Groq API 客戶端

**功能**：封裝 Groq API 的呼叫邏輯。

**支援模型**：
```python
MODEL_MAIN = "llama-3.3-70b-versatile"  # 主力分析
```

**關鍵參數**：
```python
temperature = 0.3  # 低溫度 = 更穩定一致的輸出
max_tokens = 1000  # 限制輸出長度
```

### 3. `src/finmind_client.py` — FinMind API 客戶端

**功能**：抓取台股盤後數據。

**抓取項目**：

| 數據 | FinMind Dataset | 說明 |
|---|---|---|
| 股價與成交量 | `TaiwanStockPrice` | 計算漲跌幅、波動率 |
| 月營收 | `TaiwanStockMonthRevenue` | 計算營收年增率 |
| 三大法人 | `TaiwanStockInstitutionalInvestorsBuySell` | 計算投信連買天數 |
| 融資券 | `TaiwanStockMarginPurchaseShortSale` | 計算融資增減 |

### 4. `src/risk_calculator.py` — 風險計算

**功能**：計算波動率、評估風險等級、產業集中度。

**波動率計算**：
```python
年化波動率 = 每日報酬率標準差 × √252 × 100%
```

**風險等級判定**：

| 波動率範圍 | 風險等級 |
|---|---|
| > 警戒線 × 1.5 | 🔴 極高風險 |
| > 警戒線 | 🟠 高風險 |
| > 警戒線 × 0.5 | 🟡 中風險 |
| ≤ 警戒線 × 0.5 | 🟢 低風險 |

### 5. `src/telemetry.py` — 遙測記錄

**功能**：記錄每次分析的完整輸入/輸出/結果。

**記錄內容**：
```json
{
  "id": "20260911_173000_2330",
  "timestamp": "2026-09-11T17:30:00",
  "stock_code": "2330",
  "stock_name": "台積電",
  "prompt_version": 1,
  "model": "llama-3.3-70b-versatile",
  "regime": "震盪",
  "input": { ... },
  "prediction": { ... },
  "actual_result": null,
  "accuracy": null
}
```

### 6. `src/prompt_optimizer.py` — Prompt 優化器

**功能**：每週分析預測表現，自動生成改進版 Prompt。

**優化流程**：
```
1. 讀取過去 14 天的遥測數據
2. 計算準確率與常見錯誤
3. 讓 AI 分析錯誤原因
4. 生成改進版 Prompt
5. 備份舊版本，保存新版本
6. 記錄優化歷史
```

---

## Prompt 模板

### 主力分析 Prompt（`prompts/main_analysis.txt`）

```
你是一位擁有20年經驗的台股分析師，專精於籌碼面與基本面分析。

【台股邏輯範例】
- 融資大減 + 股價不跌 = 散戶洗盤，籌碼乾淨 (利多)
- 投信連買 + 營收YoY > 20% = 法人看好，基本面佳 (利多)
- 波動率 > 30% = 權證時間價值流失快，風險高 (利空)
- 接近除息日 (<7天) = 避免觀望期，除息後再戰 (中性)

【任務】
評估以下標的的狙擊價值。

【輸入數據】
- 股票代號：{code}
- 股票名稱：{name}
- 產業：{industry}
- 市場體制：{regime}
- 月營收年增率：{revenue_yoy}%
- 投信連續買超：{inst_buy_days}天
- 融資增減：{margin_change}張
- 近5日漲跌幅：{change_5d}%
- 20日波動率：{volatility}%
- 距除息天數：{ex_div_days}天

【輸出要求】
請僅輸出以下JSON格式，不要輸出其他內容：
{
  "ev_score": "0-100",
  "recommendation": "積極買入/觀望/避開",
  "reason": "50字內"
}
```

### 深度推理 Prompt（`prompts/deep_analysis.txt`）

```
你是一位擁有25年經驗的台股衍生性商品交易總監，專精於權證與零股的風險評估。
你的分析風格極為嚴謹，絕不輕易給出「買入」建議。

【任務】
使用者準備對以下標的進行右側狙擊，請進行「購買前的深度評估」。

【評估原則】
1. 必須分析「最佳、基準、最差」三種情境
2. 必須計算加權後的期望值
3. 必須明確指出「為什麼這筆交易可能會失敗」
4. 只有期望值明顯為正且風險可控時，才給出買入建議
5. 接近除息日或波動率過高時，應傾向保守

【市場體制背景】
目前市場體制：{regime}
市場體制說明：{regime_description}

【標的完整數據】
{stock_data}

【交易參數】
{trade_params}

【輸出要求】
請僅輸出以下JSON格式：
{
  "conclusion": "強烈買入/謹慎買入/觀望/避開",
  "confidence": "高/中/低",
  "scenarios": {
    "best": {"probability": "", "return": "", "description": ""},
    "base": {"probability": "", "return": "", "description": ""},
    "worst": {"probability": "", "return": "", "description": ""}
  },
  "expected_value": "",
  "risk_reward_ratio": "",
  "entry_point": "",
  "stop_loss": "",
  "take_profit": "",
  "holding_days": "",
  "position_size": "",
  "main_risks": [],
  "failure_reasons": [],
  "final_advice": ""
}
```

### Regime 判斷 Prompt（`prompts/regime_judgment.txt`）

```
你是一位擁有20年經驗的台股總經分析師，專精於判斷市場體制（Market Regime）。

【市場體制定義】
- 多頭：大盤在60日均線之上，成交量放大，外資連續買超
- 震盪：大盤在20-60日均線間糾結，成交量萎縮，多空拉鋸
- 空頭：大盤跌破60日均線，融資大增但股價下跌，外資連續賣超

【輸入數據】
{market_data}

【輸出要求】
請僅輸出以下JSON格式：
{
  "regime": "多頭/震盪/空頭",
  "confidence": "高/中/低",
  "reason": "判斷理由（50字內）",
  "operation_suggestion": "操作建議（30字內）",
  "risk_level": "1-10分"
}
```

---

## Telemetry 遙測系統

### 記錄什麼？

每次 AI 分析都會記錄完整的「輸入 → 輸出 → 實際結果」：

```
預測記錄 ──→ 實際交易 ──→ 回填結果 ──→ 計算準確度 ──→ 每週優化
```

### 回填實際結果

交易結束後，手動更新 `telemetry.json` 中的 `actual_result`：

```json
{
  "actual_result": {
    "profit_pct": 5.2,
    "was_correct": true,
    "updated_at": "2026-09-15T18:00:00"
  },
  "accuracy": 85
}
```

### 準確度計算規則

| 條件 | 得分 |
|---|---|
| 預測「買入」且實際獲利 | +50 |
| 預測「觀望」且實際波動 < 3% | +50 |
| 預測「避開」且實際虧損 | +50 |
| 評分 > 75 且實際獲利 > 5% | +30 |
| 評分 < 50 且實際虧損 | +30 |
| 理由字數 > 20（非敷衍） | +20 |

---

## Prompt 自動優化器

### 觸發條件

- **時間**：每週日 20:00（UTC+8）
- **條件**：過去 14 天至少有 5 筆有實際結果的預測

### 優化流程

```
1. 收集過去 14 天的遥測數據
2. 計算整體準確率
3. 找出常見錯誤案例（最多 5 個）
4. 將錯誤案例 + 當前 Prompt 送給 AI
5. AI 分析錯誤原因，提出改進建議
6. 生成優化後的 Prompt
7. 備份舊版本（main_analysis_v{N}.txt）
8. 保存新版本（main_analysis.txt）
9. 更新 prompt_history.json
```

### 優化歷史記錄

```json
{
  "current_version": 3,
  "optimizations": [
    {
      "version": 2,
      "date": "2026-09-15T20:00:00",
      "prompt_name": "main_analysis",
      "analysis": "系統常將融資大減誤判為利空，需加入籌碼邏輯說明",
      "improvements": [
        "加入融資大減=籌碼乾淨的邏輯",
        "加入除息日臨近時的保守建議"
      ],
      "expected_improvement": "預期減少 15% 的誤判"
    }
  ]
}
```

---

## 視覺化與互動儀表板

本系統包含兩個主要的靜態頁面，分別負責不同的決策輔助功能：

### 1. 決策與持倉儀表板 (`index.html`) 🌟 主要入口

負責將後端的分析數據轉化為「可執行的行動指南」，並結合使用者的本機數據提供個人化建議。

| 核心功能 | 說明 |
| --- | --- |
| **📊 價格地圖 (Canvas)** | 視覺化呈現進場區間、現價、停損與階段性停利點，一目了然。 |
| **🎯 進場觸發清單** | 將 AI 的建議拆解為「確認信號、K線形態、避免情況」，避免盲目進場。 |
| **💼 持倉管理策略** | 結合 `localStorage` 中的持有成本，自動計算損益百分比，給出「分批獲利、移動停利、嚴格停損」的具體建議。 |
| **📈 買入準備度分數** | 綜合 RSI、成交量、乖離率等指標，給出 0-100 分的進場準備度評分。 |
| **情境化操作指引** | 依據當前市場狀態，提供 A/B/C 三種情境的買入決策樹。 |

> **隱私設計**：使用者的「持有成本」僅保存在瀏覽器的 `localStorage` 中，不上傳至任何後端，確保財務隱私。

### 2. 績效監控儀表板 (`dashboard.html`)

負責追蹤系統的長期表現與 AI 模型的優化歷程。

#### `dashboard.html` 功能

| 區塊 | 內容 |
|---|---|
| 關鍵指標卡片 | 總預測次數、準確率、當前 Prompt 版本、最後更新時間 |
| 準確率趨勢圖 | 按週顯示準確率變化 |
| 版本對比圖 | 不同 Prompt 版本的準確率對比 |
| 優化歷史 | 每次優化的分析、改進、預期效果 |

### 技術實作

- **圖表庫**：Chart.js 4.4+（CDN 載入）
- **數據來源**：直接讀取 `data/*.json`
- **部署**：GitHub Pages（靜態託管）

### 關於 Grafana

| 方案 | 適合時機 |
|---|---|
| `dashboard.html` + Chart.js | **目前推薦**，免費、簡單、與架構整合 |
| Grafana Cloud | 未來擴展，需要專業監控與警報時 |
| Grafana + Prometheus | 機構級需求，需要後端伺服器 |

---

## GitHub Actions 排程

### 每日分析（`daily-analysis.yml`）

```yaml
name: Daily Sniper Analysis

on:
  schedule:
    - cron: '30 9 * * 1-5'  # UTC 09:30 = 台北 17:30（週一到週五）
  workflow_dispatch:         # 允許手動觸發

jobs:
  analyze:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install requests
      - run: cd src && python main.py
        env:
          GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}
          FINMIND_TOKEN: ${{ secrets.FINMIND_TOKEN }}
      - run: |
          git config --local user.email "action@github.com"
          git config --local user.name "GitHub Action"
          git add data/*.json
          git commit -m "🎯 Daily analysis: $(date +'%Y-%m-%d')" || echo "No changes"
          git push
```

### 每週優化（可選擴展）

若需要實作每週 Prompt 自動優化功能，可參考以下範例：

```yaml
name: Weekly Prompt Optimization

on:
  schedule:
    - cron: '0 12 * * 0'  # UTC 12:00 = 台北 20:00（週日）
  workflow_dispatch:

permissions:
  contents: write

jobs:
  optimize:
    runs-on: ubuntu-latest
    
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: true
      
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      
      - run: pip install -r requirements.txt
      
      - run: python src/prompt_optimizer.py
        env:
          GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}
      
      - uses: stefanzweifel/git-auto-commit-action@v5
        with:
          commit_message: "chore: prompt optimization update [skip ci]"
          branch: main
          file_pattern: 'data/*.json prompts/*.txt'
```

---

## 使用流程

### 每日流程

| 時間 | 動作 | 執行者 |
|---|---|---|
| 17:30 | GitHub Actions 自動執行分析 | 系統 |
| 17:35 | 數據更新，JSON 文件更新 | 系統 |
| 17:40 | GitHub Pages 自動部署 | 系統 |
| 晚上 | 查看 `index.html` 分析報告 | **你** |
| 晚上 | 查看 `dashboard.html` 監控儀表板 | **你** |
| 隔天盤中 | 手動決定是否下單 | **你** |
| 下單後 | 回填實際結果到 `telemetry.json` | **你** |

### 每週流程

| 時間 | 動作 | 執行者 |
|---|---|---|
| 週日 20:00 | GitHub Actions 執行 Prompt 優化 | 系統 |
| 週一早上 | 查看優化結果與新版 Prompt | **你** |
| 週末 | 復盤本週交易，更新 `telemetry.json` | **你** |
| 週末 | 檢視 `stock_pool.json`，汰換標的 | **你** |

### 每月流程

| 時間 | 動作 |
|---|---|
| 月初 | 檢視 `performance_metrics.json`，確認整體策略是否有效 |
| 月初 | 根據市場變化，手動調整 `regime.json` 的參數 |
| 月初 | 檢視 `prompt_history.json`，確認 Prompt 優化方向是否正確 |

---

## 風險管理機制

### 風險評估維度

| 維度 | 計算方式 | 警戒線 | 應對措施 |
|---|---|---|---|
| 波動率風險 | 20日年化標準差 | >30% | 降低倉位或避開 |
| 產業集中度 | 單一產業佔比 | >40% | 分散至其他產業 |
| 權證槓桿 | 實質槓桿倍數 | >5倍 | 改用零股或避開 |
| 時間價值 | 距除息天數 | <7天 | 除息後再戰 |
| 持倉數量 | 同時持有檔數 | >3檔 | 減碼至3檔以內 |
| 單筆風險 | 單筆最大虧損 | >2% | 強制停損 |

### 風險分數計算

```
風險分數 = 產業集中度違規 × 20 + 高風險標的數量 × 15

0-30 分：✅ 安全（可正常操作）
31-60 分：🟡 注意（建議降低倉位）
61+ 分：🔴 危險（建議減碼或觀望）
```

---

## 常見問題

### Q1：Groq API 需要綁信用卡嗎？
**A**：不需要。Groq 只需 Google 帳號登入即可使用，目前完全免費。

### Q2：為什麼不用 Qwen 而用 Llama 3.3？
**A**：
- Qwen 在 Groq 上不可用，需要切換到阿里雲或 OpenRouter（需綁卡）
- Llama 3.3 70B 的邏輯推理能力已足夠處理台股分析
- 透過優化 Prompt（加入台股邏輯範例），可以彌補中文語感的差距
- 未來如需切換，只需更改 `MODEL_DEEP` 變數

### Q3：GitHub Actions 的免費額度夠用嗎？
**A**：夠用。
- 每日分析：每天 1 次，每月約 22 次
- 每週優化：每週 1 次，每月約 4 次
- 每次執行約 1-2 分鐘
- GitHub 免費帳戶每月有 2000 分鐘的 Actions 額度

### Q4：FinMind API 的免費額度夠用嗎？
**A**：夠用。
- 每日分析 6 檔股票，每檔抓取 4 種數據 = 24 次呼叫
- 每月約 22 天 × 24 次 = 528 次呼叫
- FinMind 免費額度遠高於此

### Q5：如果 AI 預測不準怎麼辦？
**A**：
- 系統會自動追蹤準確率（Telemetry）
- 每週 Prompt 優化器會分析錯誤原因並自動修正
- 如果準確率持續低於 50%，建議暫停使用，重新檢視策略
- **AI 只是輔助工具，最終決策權在你手上**

### Q6：如何手動觸發分析？
**A**：進入 GitHub Repo → **Actions** → 選擇 workflow → 點擊 **Run workflow**

### Q7：如何更換監控的股票？
**A**：直接編輯 `data/stock_pool.json`，新增或刪除股票即可。系統會在下次執行時自動載入。

---

## 後續擴展方向

### 短期（1-3 個月）

- [x] 視覺化價格地圖與進出場策略卡片（已完成）
- [x] 前端持倉管理與買入準備度評分（已完成）
- [ ] 串接 Line Notify / Telegram Bot，當觸發「進場清單」或「停損條件」時主動推播提醒。
- [ ] 加入選擇權策略分析（買賣權、價差策略）
- [ ] 加入 Qwen 2.5 72B 作為深度推理模型（透過阿里雲百煉）
- [ ] 加入美股與港股的跨市場分析

### 中期（3-6 個月）

- [ ] 建立個人化的選股模型（基於歷史交易數據）
- [ ] 將 `localStorage` 的持倉數據匯出/匯入功能，防止清除瀏覽器快取導致資料遺失。
- [ ] 建立回測系統，驗證策略的歷史表現
- [ ] 接入更多數據源（籌碼K、主力分點）

### 長期（6 個月以上）

- [ ] 考慮使用 Grafana Cloud 進行專業監控
- [ ] 建立多策略比較框架（不同 Regime 下的策略表現）
- [ ] 加入即時數據串接（盤中監控）
- [ ] 考慮使用 Cloudflare Workers 建立動態 API 端點

---

## 免責聲明

本系統僅供學習與研究使用，不構成任何投資建議。股市投資具有風險，過去績效不代表未來表現。使用者應自行評估風險，並對自己的投資決策負責。

---

## 授權條款

本專案採用 [MIT License](https://opensource.org/licenses/MIT) 授權。

---

## 致謝

- 感謝 [FinMind](https://finmind.github.io/) 提供免費的台股數據 API
- 感謝 [Groq](https://groq.com/) 提供高速的 AI 推理服務
- 感謝馬克羊（Mark Yang）在《理財資優生》訪談中分享的投資哲學
- 感謝所有在學習過程中提出問題與建議的使用者

---

> **記住：這套系統是你的「研究助理」，不是你的「老闆」。扣板機的永遠是你。**
