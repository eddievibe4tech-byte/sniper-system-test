//! Rust Core 資料層：GitHub Contents API / CoinGecko 抓取與解析。
//!
//! 🏛 合規原則：
//! - 所有 HTTP 與解析都在 Rust 側完成；JS 只收到淨化後的 struct。
//! - GitHub Token（若需要較高 rate limit）僅從環境變數讀取，絕不經 IPC 暴露。
//! - 錯誤一律轉為 `DataError` → `into_user_facing()` 安全摘要。

use base64::Engine as _;
use serde_json::Value;

use crate::error::DataError;
use crate::models::{MetricsSummary, SignalRow};

const REPO_OWNER: &str = "eddievibe4tech-byte";
const REPO_NAME: &str = "sniper-system-test";
const BRANCH: &str = "main";

/// 取得單一檔案。主路徑走 raw.githubusercontent.com（P2-1）：
/// - 純 JSON 輸出，免去 Contents API 的 base64 解碼；
/// - 不佔用 GitHub REST API 匿名配額（60 req/hr），對儀表板高頻刷新更友善。
///
/// raw 回非成功狀態時 fallback 到 GitHub Contents API（Token 存在則帶上拉高配額）。
async fn fetch_file(client: &reqwest::Client, path: &str) -> Result<Value, DataError> {
    let raw_url = format!(
        "https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/{path}"
    );
    let resp = client
        .get(&raw_url)
        .header("User-Agent", "sniper-mobile-tauri")
        .send()
        .await?;
    if resp.status().is_success() {
        return resp.json::<Value>().await.map_err(Into::into);
    }
    // raw CDN 異常（5xx / 限流）→ Contents API fallback
    fetch_file_via_contents_api(client, path).await
}

/// Fallback：GitHub Contents API（base64 content → JSON）。
async fn fetch_file_via_contents_api(
    client: &reqwest::Client,
    path: &str,
) -> Result<Value, DataError> {
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

    // P2-2：risk_reward / drawdown 由 serde `#[serde(default)]` + Option 直接處理，
    //       不再手動二次解析；僅保留 accuracy_rate 的寬容處理（數字或「資料不足」字串），
    //       從 Value 樹移除該欄位後一次 from_value（比字串替換更穩健）。
    let accuracy_rate = raw.get("accuracy_rate").and_then(Value::as_f64);
    let mut obj = raw;
    if let Some(map) = obj.as_object_mut() {
        map.remove("accuracy_rate");
    }

    let mut metrics: MetricsSummary =
        serde_json::from_value(obj).map_err(|_| DataError::Decode)?;
    metrics.accuracy_rate = accuracy_rate;
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
    // P1-2：嚴格 key 匹配 — 若回傳 map 的 key 與請求 id 不一致（別名/大小寫邊界），
    //       絕不 fallback 到 values().next()，避免把「另一個資產的價格」掛在用戶
    //       請求的 symbol 下（金融儀表板中錯誤價格比無價更危險）→ 直接 Decode 錯誤。
    let price = body
        .as_object()
        .and_then(|map| map.get(&symbol.to_lowercase()))
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
