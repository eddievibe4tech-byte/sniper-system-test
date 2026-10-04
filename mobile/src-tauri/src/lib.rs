//! Sniper Mobile — Tauri 2.0 入口。
//!
//! 架構分層（機構級「資料與展示分離」合規要求）：
//! - `api`     : Rust Core — HTTP / GitHub Contents API / CoinGecko / 解析
//! - `models`  : FFI 邊界序列化契約（derive Serialize/Deserialize）
//! - `error`   : thiserror 領域錯誤 + anyhow 邊界彙整
//! - `webview_guard`: OPPO ColorOS WebView 版本防護
//! JS 端只透過 invoke() 呼叫下方 `#[tauri::command]`，API Key 永不進入 WebView 環境。

mod api;
mod error;
mod models;
mod webview_guard;

use models::{CryptoPrice, DashboardPayload};
use tauri::State;

/// 共用 HTTP Client（連線池重用；reqwest::Client 內部為 Arc，跨執行緒 clone 極便宜）
struct AppState {
    client: reqwest::Client,
}

impl AppState {
    fn new() -> Self {
        Self {
            client: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(15))
                .build()
                .expect("failed to build http client"),
        }
    }
}

/// 一次 IPC 拿齊儀表板資料（metrics + signals），減少橋接往返次數。
/// 任何單一資料源失敗都降級為「部分資料 + 淨化後的錯誤摘要」，不整體崩潰。
#[tauri::command]
async fn fetch_dashboard(state: State<'_, AppState>) -> Result<DashboardPayload, String> {
    let client = state.inner().client.clone();

    let mut errors = Vec::new();

    let metrics = match api::get_metrics(&client).await {
        Ok(m) => Some(m),
        Err(e) => {
            errors.push(e.into_user_facing());
            None
        }
    };

    let signals = match api::get_signals(&client).await {
        Ok(s) => s,
        Err(e) => {
            errors.push(e.into_user_facing());
            Vec::new()
        }
    };

    Ok(DashboardPayload {
        metrics,
        signals,
        fetched_at: api::chrono_now_iso(),
        errors,
    })
}

/// 指南 FFI 邊界示範 command：Svelte 傳入 symbol（如 "SOL"），
/// Rust 呼叫 CoinGecko 取得 TWD 價格，回傳 derive(Serialize) 的 struct。
#[tauri::command]
async fn crypto_price(symbol: String, state: State<'_, AppState>) -> Result<CryptoPrice, String> {
    let symbol = symbol.trim().to_lowercase();
    if symbol.is_empty() || !symbol.chars().all(|c| c.is_ascii_alphanumeric()) {
        return Err("代碼格式不正確".to_string());
    }
    let client = state.inner().client.clone();
    api::get_crypto_price_tw(&client, &symbol)
        .await
        .map_err(|e| e.into_user_facing())
}

/// WebView 版本護欄：前端於 mount 時讀 navigator.userAgent 傳入，
/// Rust 側判定過舊 → 回傳需要顯示的更新提示（指南風險 #2）。
#[tauri::command]
fn check_webview(user_agent: String) -> Result<String, String> {
    webview_guard::check_webview_version(&user_agent)
        .map(|_| "ok".to_string())
        .map_err(|e| e.into_user_facing())
}

/// 🔐 HMAC 簽章範例（未來串接 MAX 交易所 API 用）：
/// secret 僅存在於 Rust 側（env/安全存儲），JS 永遠拿不到金鑰本身。
/// 這裡先以純 Rust 實作（不引入 hmac crate 以保持本 PR 依賴精簡），
/// 正式接 MAX 時應改用 `hmac` + `sha2` crate 的常數時間實作。
#[tauri::command]
fn sign_payload_placeholder(message: String) -> Result<String, String> {
    let secret = std::env::var("MAX_API_SECRET")
        .map_err(|_| "尚未設定交易金鑰（MAX_API_SECRET）".to_string())?;
    // 佔位：以 SHA-256(secret ‖ message) 前 16 bytes hex 演示邊界設計。
    // ⚠️ 非真正 HMAC — 上線前必須替換為 RFC 2104 實作。
    let input = format!("{secret}{message}");
    let digest = simple_hash(input.as_bytes());
    Ok(digest[..32].to_string())
}

/// 精簡 FNV-1a 擴充雜湊（僅供佔位演示，非密碼學安全 — 見上方 ⚠️）
fn simple_hash(data: &[u8]) -> String {
    let mut h: u128 = 0xcbf29ce484222325;
    for b in data {
        h ^= *b as u128;
        h = h.wrapping_mul(0x100000001b3);
    }
    format!("{h:032x}")
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(AppState::new())
        .invoke_handler(tauri::generate_handler![
            fetch_dashboard,
            crypto_price,
            check_webview,
            sign_payload_placeholder
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
