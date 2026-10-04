//! Rust Core 資料層：GitHub Contents API / CoinGecko 抓取與解析。
//!
//! 🏛 合規原則：
//! - 所有 HTTP 與解析都在 Rust 側完成；JS 只收到淨化後的 struct。
//! - GitHub Token（若需要較高 rate limit）僅從環境變數讀取，絕不經 IPC 暴露。
//! - 錯誤一律轉為 `DataError` → `into_user_facing()` 安全摘要。

use base64::Engine as _;
use serde_json::Value;

use crate::error::DataError;
use crate::models::{DrawdownStats, MetricsSummary, RiskRewardStats, SignalRow};

const REPO_OWNER: &str = "eddievibe4tech-byte";
const REPO_NAME: &str = "sniper-system-test";
const BRANCH: &str = "main";

/// 取得單一檔案（GitHub Contents API，base64 content）。
/// Token 存在時帶上以拉高 rate limit；不存在則匿名（60 req/hr 對儀表板足夠）。
async fn fetch_file(client: &reqwest::Client, path: &str) -> Result<Value, DataError> {
    let url = format!(
        "https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}?ref={BRANCH}"
    );
    let mut req = client
        .get(&url)
        .header("Accept", "application/vnd.github+json")
        .header("User-Agent", "sniper-mobile-tauri");
    if let Ok(token) = std::env::var("GITHUB_TOKEN") {
        if !token.is_empty() {
            req = req.header("Authorization", format!("Bearer {token}"));
        }
    }
    let resp = req.send().await?;
    let status = resp.status();
    if !status.is_success() {
        return Err(DataError::RemoteStatus(status.as_u16(), path.to_string()));
    }
    let body: Value = resp.json().await?;
    let content = body
        .get("content")
        .and_then(Value::as_str)
        .ok_or(DataError::Decode)?;
    // Contents API 的 content 含換行，需先剔除
    let cleaned: String = content.chars().filter(|c| !c.is_whitespace()).collect();
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(cleaned)
        .map_err(|_| DataError::Decode)?;
    let json: Value = serde_json::from_slice(&bytes)?;
    Ok(json)
}

/// performance_metrics.json → MetricsSummary（含 Issue #140 的 R/R 與 MDD 區塊）
pub async fn get_metrics(client: &reqwest::Client) -> Result<MetricsSummary, DataError> {
    let raw = fetch_file(client, "data/performance_metrics.json").await?;

    // accuracy_rate 欄位可能是數字或「資料不足」字串 → 先手動寬容取出，
    // 再從 Value 樹移除該欄位避免 serde 型別錯誤（比字串替換更穩健）。
    let accuracy_rate = raw.get("accuracy_rate").and_then(Value::as_f64);
    let mut obj = raw.clone();
    if let Some(map) = obj.as_object_mut() {
        map.remove("accuracy_rate");
    }

    let mut metrics: MetricsSummary =
        serde_json::from_value(obj.clone()).map_err(|_| DataError::Decode)?;
    metrics.accuracy_rate = accuracy_rate;
    metrics.risk_reward = obj
        .get("risk_reward")
        .cloned()
        .and_then(|v| serde_json::from_value::<RiskRewardStats>(v).ok());
    metrics.drawdown = obj
        .get("drawdown")
        .cloned()
        .and_then(|v| serde_json::from_value::<DrawdownStats>(v).ok());
    Ok(metrics)
}

/// deep_analysis.json → Vec<SignalRow>（精簡投影；reason 屬外部文字，前端仍需 escapeHTML）
pub async fn get_signals(client: &reqwest::Client) -> Result<Vec<SignalRow>, DataError> {
    let raw = fetch_file(client, "data/deep_analysis.json").await?;
    let arr = raw
        .get("all_results")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    let rows = arr
        .iter()
        .map(|item| {
            let tp = item.get("tradingPlan").or(item.get("trading_plan"));
            let entry_zone = tp
                .and_then(|t| t.get("entryZone").or(t.get("entry_zone")))
                .and_then(Value::as_array)
                .and_then(|a| {
                    if a.len() == 2 {
                        Some([a[0].as_f64()?, a[1].as_f64()?])
                    } else {
                        None
                    }
                });
            SignalRow {
                code: item
                    .get("code")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string(),
                name: item
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string(),
                industry: item
                    .get("industry")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                recommendation: item
                    .get("recommendation")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                ev_score: item.get("evScore").or(item.get("ev_score")).and_then(Value::as_f64),
                reason: item.get("reason").and_then(Value::as_str).map(str::to_string),
                close_price: item
                    .get("closePrice")
                    .or(item.get("current_price"))
                    .and_then(Value::as_f64),
                rsi: item.get("rsi").and_then(Value::as_f64),
                volatility: item.get("volatility").and_then(Value::as_f64),
                entry_zone,
                stop_loss: tp
                    .and_then(|t| t.get("stopLoss").or(t.get("stop_loss")))
                    .and_then(Value::as_f64),
                take_profit_1: tp
                    .and_then(|t| t.get("takeProfit1").or(t.get("take_profit_1")))
                    .and_then(Value::as_f64),
                risk_reward: tp
                    .and_then(|t| t.get("riskReward").or(t.get("risk_reward")))
                    .and_then(Value::as_f64),
            }
        })
        .collect();
    Ok(rows)
}

/// CoinGecko：symbol（例："sol"）→ TWD 現價。
/// 🔴 指南 FFI 邊界範例：回傳 derive(Serialize) 的 struct，JS 端直接得到 camelCase JSON。
pub async fn get_crypto_price_tw(
    client: &reqwest::Client,
    symbol: &str,
) -> Result<crate::models::CryptoPrice, DataError> {
    let url = format!(
        "https://api.coingecko.com/api/v3/simple/price?ids={}&vs_currencies=twd",
        symbol.to_lowercase()
    );
    let resp = client.get(&url).send().await?;
    let status = resp.status();
    if !status.is_success() {
        return Err(DataError::RemoteStatus(status.as_u16(), "coingecko".into()));
    }
    let body: Value = resp.json().await?;
    let price = body
        .as_object()
        .and_then(|map| map.get(&symbol.to_lowercase()).or_else(|| map.values().next()))
        .and_then(|v| v.get("twd"))
        .and_then(Value::as_f64)
        .ok_or(DataError::Decode)?;
    Ok(crate::models::CryptoPrice {
        symbol: symbol.to_lowercase(),
        price_twd: price,
        updated_at: chrono_now_iso(),
    })
}

pub fn chrono_now_iso() -> String {
    // 避免多拉 chrono 依賴：用 std 時間戳做粗略 ISO（儀表板只需顯示刷新時間）
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!("{secs}")
}
