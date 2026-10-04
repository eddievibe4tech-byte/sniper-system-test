//! Tauri IPC 資料契約（FFI 邊界）。
//!
//! 🔴 指南重點：AI 協作最常在 Serialization 出錯 —— 所有跨 JS/Rust 邊界的
//! struct 必須 `derive(Serialize, Deserialize)`，欄位命名統一 `#[serde(rename_all = "camelCase")]`
//! 以貼近 JavaScript 慣例，並讓 tsc/svelte-check 能對齊 TS 型別定義。

use serde::{Deserialize, Serialize};

/// 頂部 KPI 卡片（對應 data/performance_metrics.json）
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
#[derive(Default)]
pub struct MetricsSummary {
    pub total_predictions: u32,
    pub verified_count: u32,
    pub correct_count: u32,
    pub incorrect_count: u32,
    pub pending_count: u32,
    /// 準確率（%）；樣本不足時後端發佈字串「資料不足」→ 此處以 f64? 呈現
    pub accuracy_rate: Option<f64>,
    pub current_version: u32,
    pub last_updated: Option<String>,
    /// 🆕 Issue #140：機構級績效指標（盈虧比/期望值/最大回撤），Rust 側聚合展示
    pub risk_reward: Option<RiskRewardStats>,
    pub drawdown: Option<DrawdownStats>,
}

/// 盈虧比與期望值統計
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
#[derive(Default)]
pub struct RiskRewardStats {
    pub sample_count: u32,
    pub avg_rr: Option<f64>,
    pub expected_value_pct: Option<f64>,
    pub win_rate_pct: Option<f64>,
}

/// 模擬資金曲線的最大回撤
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
#[derive(Default)]
pub struct DrawdownStats {
    pub max_drawdown_pct: Option<f64>,
    pub equity_points: u32,
}

/// 單一分析結果（deep_analysis.json → all_results 的精簡投影，只帶顯示所需欄位）
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
#[derive(Default)]
pub struct SignalRow {
    pub code: String,
    pub name: String,
    pub industry: Option<String>,
    pub recommendation: Option<String>,
    pub ev_score: Option<f64>,
    pub reason: Option<String>,
    pub close_price: Option<f64>,
    pub rsi: Option<f64>,
    pub volatility: Option<f64>,
    /// 交易計畫（進場區間/停損/停利/R:R）— 僅數字與硬編碼字串，無外部文字
    pub entry_zone: Option<[f64; 2]>,
    pub stop_loss: Option<f64>,
    pub take_profit_1: Option<f64>,
    pub risk_reward: Option<f64>,
}

/// CoinGecko 價格回應（symbol → TWD）
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CryptoPrice {
    pub symbol: String,
    pub price_twd: f64,
    pub updated_at: String,
}

/// `fetch_dashboard` 的完整回傳（一次 IPC 拿齊儀表板資料，減少橋接往返）
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DashboardPayload {
    pub metrics: Option<MetricsSummary>,
    pub signals: Vec<SignalRow>,
    pub fetched_at: String,
    pub errors: Vec<String>, // 已淨化的安全摘要字串
}
