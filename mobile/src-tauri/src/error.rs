//! 領域錯誤型別 — 依指南要求使用 `thiserror` 做嚴格錯誤處理。
//!
//! 設計原則（機構級合規）：
//! - 每個可預期的失敗模式都對應一個明確變體，前端只拿到「安全摘要字串」，
//!   绝不洩漏內部 URL / API Key / HTTP body 細節。
//! - `#[from] reqwest::Error` 等外部錯誤在邊界處由 `anyhow` 彙整，
//!   再經 `into_user_facing()` 淨化為使用者可讀的中文提示。

use thiserror::Error;

/// 儀表板資料層錯誤
#[derive(Debug, Error)]
pub enum DataError {
    /// HTTP 請求失敗（網路層）
    #[error("http request failed: {0}")]
    Http(#[from] reqwest::Error),

    /// GitHub Contents API 回傳非 200
    #[error("remote returned status {0} for {1}")]
    RemoteStatus(u16, String),

    /// Base64 解碼 content 欄位失敗
    #[error("failed to decode file content")]
    Decode,

    /// JSON 反序列化失敗（schema 不符）
    #[error("json schema mismatch: {0}")]
    Schema(#[from] serde_json::Error),

    /// WebView 版本過舊（OPPO ColorOS 痛點防護）
    #[error("android system webview version {0} is below required {1}")]
    WebViewTooOld(u32, u32),
}

impl DataError {
    /// 淨化為「可顯示給使用者」的安全摘要；原始錯誤僅留在 Rust 日誌側。
    pub fn into_user_facing(&self) -> String {
        match self {
            DataError::Http(_) => "網路連線失敗，請檢查手機網路後重試".to_string(),
            DataError::RemoteStatus(_, _) => "遠端資料 temporarily 不可用，稍後再試".to_string(),
            DataError::Decode => "資料解碼失敗，檔案可能已損毀".to_string(),
            DataError::Schema(_) => "資料格式與 App 版本不匹配，請更新 App".to_string(),
            DataError::WebViewTooOld(cur, min) => format!(
                "系統 WebView 版本過舊（{cur} < {min}），請至 Play 商店更新 Android System WebView"
            ),
        }
    }
}
