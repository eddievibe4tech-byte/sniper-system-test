//! WebView 版本檢測（指南「風險控管 #2」實作）。
//!
//! 痛點：Tauri Android 底層依賴內建 Android System WebView；OPPO ColorOS 若
//! WebView 過舊，Svelte 的現代 CSS/JS 語法可能崩潰。
//! 策略：啟動時以 `#[tauri::command]` 讓前端傳入 UA 字串中的 major 版本，
//! Rust 側做閾值判定 — 過舊時回傳需要顯示給使用者的更新提示。

use crate::error::DataError;

/// 最低可接受的 WebView major 版本（對應 Chrome 100+ 的現代 CSS/JS 支援基線）
pub const MIN_WEBVIEW_MAJOR: u32 = 100;

/// 從 User-Agent 解析 "wv) ... Chrome/xxx" 或 "Version/x.y" 取得 WebView major 版本。
/// 解析失敗回傳 None（不誤殺：無法判定時不阻擋使用）。
pub fn parse_webview_major(user_agent: &str) -> Option<u32> {
    // Android System WebView UA 範例：
    //   Mozilla/5.0 ... AppleWebKit/537.36 ... Chrome/118.0.0.0 Mobile Safari/537.36
    let chrome_idx = user_agent.find("Chrome/")?;
    let rest = &user_agent[chrome_idx + "Chrome/".len()..];
    let major_str = rest.split('.').next()?;
    major_str.parse::<u32>().ok()
}

/// 檢查 WebView 版本；過舊 → Err(DataError::WebViewTooOld)，前端顯示 Play 商店更新指引。
pub fn check_webview_version(user_agent: &str) -> Result<(), DataError> {
    match parse_webview_major(user_agent) {
        Some(major) if major < MIN_WEBVIEW_MAJOR => {
            Err(DataError::WebViewTooOld(major, MIN_WEBVIEW_MAJOR))
        }
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_modern_webview_major() {
        let ua = "Mozilla/5.0 (Linux; Android 13; OPPO A5 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/118.0.0.0 Mobile Safari/537.36";
        assert_eq!(parse_webview_major(ua), Some(118));
        assert!(check_webview_version(ua).is_ok());
    }

    #[test]
    fn rejects_ancient_webview() {
        let ua = "... Chrome/83.0.4103.106 Mobile Safari/537.36";
        assert_eq!(parse_webview_major(ua), Some(83));
        let err = check_webview_version(ua).unwrap_err();
        assert!(matches!(err, DataError::WebViewTooOld(83, 100)));
        // 使用者摘要必須包含更新指引且不含內部細節
        assert!(err.into_user_facing().contains("Android System WebView"));
    }

    #[test]
    fn unknown_ua_is_not_blocked() {
        // 解析不到 → None → 不誤殺（桌面 dev 模式即屬此類）
        assert_eq!(parse_webview_major("Mozilla/5.0 (Macintosh)"), None);
        assert!(check_webview_version("Mozilla/5.0 (Macintosh)").is_ok());
    }
}
