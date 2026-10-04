# Sniper Mobile — Rust + Tauri 2.0 行動投資儀表板

依「機構級 Rust 行動端開發與 CI/CD 指南」實作：Rust Core（資料/HMAC/錯誤處理）
＋ Svelte 前端（無虛擬 DOM，OPPO A5 Pro WebView 渲染高效）＋ Tauri IPC 橋樑。

## 目錄結構
```
mobile/
├── index.html / src/            # Svelte 5 (runes) 前端
├── package.json / vite.config.ts / tsconfig.json
└── src-tauri/                   # Rust Core
    ├── Cargo.toml               # thiserror + anyhow + reqwest(rustls)
    ├── taururi.conf.json        # CSP 白名單、minSdk 24、APK bundle
    └── src/
        ├── lib.rs               # #[tauri::command] × 4（IPC 邊界）
        ├── api.rs               # GitHub Contents API / CoinGecko 解析
        ├── models.rs            # FFI serde 契約（camelCase）
        ├── error.rs             # DataError + into_user_facing() 淨化
        └── webview_guard.rs     # WebView 版本護欄（風險 #2）
```

## 本地開發（開發期簽名 = Debug keystore）
```bash
cd mobile && npm install
npm run check                      # svelte-check
cargo test -p sniper-mobile --manifest-path src-tauri/Cargo.toml   # Rust 單元測試
npx tauri android dev              # 自動 debug 簽名並安裝到 USB 連接的手機
```

## CI/CD（GitHub Actions 雲端交叉編譯）
- `.github/workflows/build-android.yml`
  - PR → 僅 static-check（svelte-check + cargo check/clippy，免 NDK）
  - Tag `v*` / workflow_dispatch → NDK r26b + 四目標 linker 注入 → APK
- 正式簽名 Secrets（未設定時自動跳過簽名步驟）：
  `ANDROID_KEYSTORE_BASE64`、`ANDROID_KEYSTORE_PASSWORD`、`ANDROID_KEY_ALIAS`、`ANDROID_KEY_PASSWORD`
- ⚠️ 未簽名 APK 會被 ColorOS 拒絕安裝；發布前務必設定上述 Secrets。

## Android 權限
`INTERNET` 由 `tauri android init` 生成的 manifest 預設包含；
首次於 CI 或本地執行 `npx tauri android init` 會產生 `src-tauri/gen/android/`（已 gitignore）。

## 背景推播（風險 #3）
不在 Rust 端寫無限輪詢迴圈（Android 會掛起背景程序）。價格突破通知改由
既有 GitHub Actions 監控觸發 → Firebase Cloud Messaging 推送；手機端僅接收顯示（後續 Issue）。

## IPC 命令契約
| command | 輸入 | 輸出 |
|---|---|---|
| `fetch_dashboard` | – | `DashboardPayload{metrics,signals,fetchedAt,errors}` |
| `crypto_price` | `symbol: String` | `CryptoPrice{symbol,priceTwd,updatedAt}` |
| `check_webview` | `userAgent: String` | `"ok"` 或中文更新提示（Err） |
| `sign_payload_placeholder` | `message: String` | 佔位摘要（⚠️ 非 RFC 2104 HMAC，上線前替換） |
