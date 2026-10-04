//! Sniper Mobile — Tauri 2.0 入口。
//!
//! 架構分層（機構級「資料與展示分離」合規要求）：
//! - `api`     : Rust Core — HTTP / GitHub Contents API / CoinGecko / 解析
//! - `models`  : FFI 邊界序列化契約（derive Serialize/Deserialize）
//! - `error`   : thiserror 領域錯誤 + into_user_facing() 淨化摘要
//! - `webview_guard`: OPPO ColorOS WebView 版本防護
//!
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

    // P2-3：兩個資料源以 join! 平行請求，行動網路下延遲約減半；
    //       各自 map_err 保留降級語意（單源失敗 → 部分資料 + 淨化摘要）。
    let (metrics_res, signals_res) =
        tokio::join!(api::get_metrics(&client), api::get_signals(&client));

    let mut errors = Vec::new();

    let metrics = match metrics_res {
        Ok(m) => Some(m),
        Err(e) => {
            errors.push(e.into_user_facing());
            None
        }
    };

    let signals = match signals_res {
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

/// 🔐 HMAC 簽章佔位（未來串接 MAX 交易所 API 用）：
/// secret 僅存在於 Rust 側（env/安全存儲），JS 永遠拿不到金鑰本身。
///
/// ⚠️ PR #142 Code Review P1-1（簽章預言機風險）：
///    本函數以非密碼學雜湊 + secret 前綴模擬簽章，**不得**註冊於 invoke_handler —
///    一旦 MAX_API_SECRET 真實設定，任何能在 WebView 內執行的程式碼（含潛在 XSS）
///    皆可呼叫此 command 取得「簽章」。待引入 `hmac` + `sha2` crate 的
///    RFC 2104 常數時間實作並通過安全審查後，才可重新註冊。
#[allow(dead_code)]
fn sign_payload_placeholder(message: String) -> Result<String, String> {
    let secret = std::env::var("MAX_API_SECRET")
        .map_err(|_| "尚未設定交易金鑰（MAX_API_SECRET）".to_string())?;
    // 佔位：以 SHA-256(secret ‖ message) 前 16 bytes hex 演示邊界設計。
    // ⚠️ 非真正 HMAC — 上線前必須替換為 RFC 2104 實作。
    let input = format!("{secret}{message}");
    let digest = simple_hash(input.as_bytes());
    Ok(digest[..32].to_string())
}

/// 精簡 FNV-1a 擴充雜湊（僅供 sign_payload_placeholder 佔位演示，
/// 非密碼學安全 — 見上方 P1-1 說明；本函數未註冊至 IPC）
#[allow(dead_code)]
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
        // P1-1：sign_payload_placeholder 已從 IPC 註冊移除（簽章預言機風險），
        // 待 hmac+sha2 的 RFC 2104 實作通過安全審查後再重新註冊。
        .invoke_handler(tauri::generate_handler![
            fetch_dashboard,
            crypto_price,
            check_webview
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
