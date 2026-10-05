//! Tauri IPC 資料契約（FFI 邊界）。
//!
//! 🔴 指南重點：AI 協作最常在 Serialization 出錯 —— 所有跨 JS/Rust 邊界的
//! struct 必須 `derive(Serialize, Deserialize)`，欄位命名統一 `#[serde(rename_all = "camelCase")]`
//! 以貼近 JavaScript 慣例，並讓 tsc/svelte-check 能對齊 TS 型別定義。

use serde::{Deserialize, Serialize};

/// 頂部 KPI 卡片（來源 data/performance_metrics.json 為 snake_case）
///
/// ⚠️ Issue #156：來源 JSON 由 Python 報表管線產出，使用 snake_case key；
/// `rename_all = "camelCase"` 只影響序列化輸出（IPC → Svelte 契約），
/// 反序列化需靠各欄位的 `alias` 容錯 snake_case，否則 `default` 會靜默歸零。
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase", default)]
pub struct MetricsSummary {
    #[serde(alias = "total_predictions")]
    pub total_predictions: u32,
    #[serde(alias = "verified_count")]
    pub verified_count: u32,
    #[serde(alias = "correct_count")]
    pub correct_count: u32,
    #[serde(alias = "incorrect_count")]
    pub incorrect_count: u32,
    #[serde(alias = "pending_count")]
    pub pending_count: u32,
    /// 準確率（%）；樣本不足時後端發佈字串「資料不足」→ 此處以 f64? 呈現
    #[serde(alias = "accuracy_rate")]
    pub accuracy_rate: Option<f64>,
    #[serde(alias = "current_version")]
    pub current_version: u32,
    #[serde(alias = "last_updated")]
    pub last_updated: Option<String>,
    /// 🆕 Issue #140：機構級績效指標（盈虧比/期望值/最大回撤），Rust 側聚合展示
    #[serde(alias = "risk_reward")]
    pub risk_reward: Option<RiskRewardStats>,
    #[serde(alias = "drawdown")]
    pub drawdown: Option<DrawdownStats>,
}

/// 盈虧比與期望值統計
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase", default)]
pub struct RiskRewardStats {
    #[serde(alias = "sample_count")]
    pub sample_count: u32,
    #[serde(alias = "avg_rr")]
    pub avg_rr: Option<f64>,
    #[serde(alias = "expected_value_pct")]
    pub expected_value_pct: Option<f64>,
    #[serde(alias = "win_rate_pct")]
    pub win_rate_pct: Option<f64>,
}

/// 模擬資金曲線的最大回撤
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase", default)]
pub struct DrawdownStats {
    #[serde(alias = "max_drawdown_pct")]
    pub max_drawdown_pct: Option<f64>,
    #[serde(alias = "equity_points")]
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn metrics_deserializes_from_snake_case_source() {
        let json = serde_json::json!({
            "total_predictions": 416,
            "verified_count": 182,
            "correct_count": 112,
            "incorrect_count": 70,
            "pending_count": 234,
            "current_version": 1,
            "last_updated": "2026-10-04",
            "risk_reward": { "sample_count": 10, "avg_rr": 1.67 },
            "drawdown": { "max_drawdown_pct": -12.5, "equity_points": 40 }
        });
        let m: MetricsSummary = serde_json::from_value(json).unwrap();
        assert_eq!(m.total_predictions, 416); // 不再歸零
        assert_eq!(m.risk_reward.as_ref().unwrap().avg_rr, Some(1.67));
        assert_eq!(m.drawdown.as_ref().unwrap().max_drawdown_pct, Some(-12.5));

        // FFI 輸出維持 camelCase（前端契約）
        let out = serde_json::to_value(&m).unwrap();
        assert_eq!(out["totalPredictions"], 416);
    }
}
