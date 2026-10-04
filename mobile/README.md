# 狙擊手投資儀表板 Mobile

Rust (Tauri 2.0) + Svelte 5 (runes) 行動端儀表板。
- **資料與展示分離**：Rust 端呼叫 GitHub Contents API / CoinGecko，API Key 永不進入 WebView。
- **錯誤隔離**：`thiserror` 領域錯誤 → `into_user_facing()` 淨化，前端只看到安全中文摘要。
- **WebView 護欄**：啟動時檢查 Android System WebView 版本，過舊提示 Play 商店更新。

## 目錄
- [環境建置](#環境建置)
- [本地開發](#本地開發)
- [測試](#測試)
- [打包 APK](#打包-apk)
- [CI/CD](#cicd)
- [故障排除](#故障排除)

## 環境建置

### 系統工具
| 工具 | 版本 | 安裝方式 |
|:---|:---|:---|
| Rust | stable | https://rustup.rs |
| Node.js | 20 LTS | https://nodejs.org |
| JDK | 17 (Temurin) | https://adoptium.net |
| Android Studio | latest | https://developer.android.com/studio |

```bash
# 1) Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
# 2) Node.js 20 LTS / 3) JDK 17（Temurin）→ 見上表官網安裝
# 4) Tauri CLI
cargo install tauri-cli --version "^2"
```

### Android SDK（透過 Android Studio SDK Manager）
- Android SDK Platform **34**（`SDK Platforms`）
- Android SDK Build-Tools **34.0.0**（`SDK Tools`）
- Android SDK Command-line Tools (latest)（`SDK Tools`）
- **NDK 26.2.11394342** (r26b) — `SDK Tools → Show Package Details`；必選 r26b，其他版本編譯易失敗
- Android Emulator (latest)

### 環境變數（加到 `~/.zshrc` 或 `~/.bashrc`）
```bash
export JAVA_HOME="/path/to/jdk-17"
export ANDROID_HOME="$HOME/Library/Android/sdk"        # macOS
# export ANDROID_HOME="$HOME/Android/Sdk"              # Linux
# export ANDROID_HOME="$HOME/AppData/Local/Android/Sdk" # Windows
export NDK_HOME="$ANDROID_HOME/ndk/26.2.11394342"
export PATH="$PATH:$ANDROID_HOME/platform-tools:$ANDROID_HOME/cmdline-tools/latest/bin"
```

### 初始化（首次 clone 後執行）
```bash
cd mobile
npm install
cargo tauri android init    # 產生 src-tauri/gen/android（不入庫）
```

## 本地開發

### 前端隔離開發（不需 Rust）
```bash
cd mobile
npm run dev
# 開 http://localhost:5173（invoke() 會失敗，僅調 UI 時使用）
```

### 完整 Android 開發（含 Rust + WebView + HMR）
連接 Android 裝置（開啟 USB 偵錯）或啟動模擬器後：
```bash
cd mobile
cargo tauri android dev
# 會自動：npm build → Rust 編譯 → 部署至裝置 → 啟動 app
```

### 模擬器設定（OPPO A5 Pro 等中低階機型模擬）
```bash
avdmanager create avd -n pixel_4 -k "system-images;android-34;google_apis;x86_64"
emulator -avd pixel_4 -no-snapshot -gpu auto
```

## 測試

```bash
# Rust 單元測試（webview_guard 等，應 3 passed）
cd mobile/src-tauri
cargo test --lib

# Clippy lint（CI 強制零警告）
cargo clippy -- -D warnings

# Svelte 型別與編譯期檢查
cd mobile
npm ci
npm run check
```

## 打包 APK

### 開發版（自動 debug 簽名，可側載安裝）
```bash
cd mobile
cargo tauri android build --apk
```
產出路徑：
```
src-tauri/gen/android/app/build/outputs/apk/universal/release/app-universal-release-unsigned.apk
```
傳到 Android 裝置後直接點擊安裝（需開啟「允許安裝未知來源應用」）。

### 正式版（Release，需 keystore）
1. 產生 keystore：
   ```bash
   keytool -genkey -v -keystore sniper-release.jks -keyalg RSA \
     -keysize 2048 -validity 10000 -alias sniper
   ```
2. 設定環境變數：
   ```bash
   export KEYSTORE_PATH="/path/to/sniper-release.jks"
   export KEYSTORE_PASSWORD="your_password"
   export KEY_ALIAS="sniper"
   export KEY_PASSWORD="your_password"
   ```
3. 編譯：
   ```bash
   cargo tauri android build --apk
   ```
4. CI 正式簽名：將 `.jks` base64 編碼後存入 Repo Secrets `KEYSTORE_BASE64`，並設定 `KEYSTORE_PASSWORD`；推 tag 即自動簽名。
   ⚠️ Keystore 一經發布不可遺失——遺失後該簽名的 App 永遠無法更新，請離線備份 `.jks` 與密碼。

## CI/CD

| 事件 | Job | 產出 |
|:---|:---|:---|
| Pull Request | `static-check` | Svelte typecheck + Rust check/clippy/test（無 NDK） |
| Push Tag `v*` | `build-android` | 可安裝 APK/AAB（artifact 上傳，保留 30 天） |
| 手動觸發 | `build-android` | 同上 |

詳情見 `.github/workflows/build-android.yml`。

- **APK 產出流程**：推 tag（`git tag v0.1.0 && git push --tags`）或到 Actions 頁面手動 `Run workflow`，下載 artifact 中的 APK 傳到手機安裝測試。
- **`gen/android` 不入庫**：刻意設計，CI 每次從頭 `cargo tauri android init` 以驗證建置可重現性（代價 +2~3 分鐘，由 rust-cache 緩解）。
- **冷編譯時間**：首次 CI（無 cache）預期 20-30 分鐘；cache 生效後壓到 5-8 分鐘。
- ⚠️ `-unsigned.apk` 在部分裝置（如 OPPO ColorOS）會被拒絕安裝，需 debug 簽名版或完成 release 簽名。

## IPC 命令契約
| command | 輸入 | 輸出 |
|---|---|---|
| `fetch_dashboard` | – | `DashboardPayload{metrics,signals,fetchedAt,errors}` |
| `crypto_price` | `symbol: String` | `CryptoPrice{symbol,priceTwd,updatedAt}` |
| `check_webview` | `userAgent: String` | `"ok"` 或中文更新提示（Err） |
| `sign_payload_placeholder` | `message: String` | 佔位摘要（⚠️ 非 RFC 2104 HMAC，上線前替換） |

## 背景推播（風險控管）
不在 Rust 端寫無限輪詢迴圈（Android 會掛起背景程序）。價格突破通知改由
既有 GitHub Actions 監控觸發 → Firebase Cloud Messaging 推送；手機端僅接收顯示（後續 Issue）。

## 故障排除

### `No Svelte configuration found in vite config`
`npm ci` 後確認 `mobile/svelte.config.js` 存在（PR #142 P0-1 修正，內容為 `vitePreprocess()` + `compilerOptions.runes = true`），重跑 `npm run check`。

### `cargo build` 在 Android 目標報 linker 找不到
確認 `NDK_HOME` 環境變數指向 r26b（`26.2.11394342`），且 PATH 包含 `$NDK_HOME/toolchains/llvm/prebuilt/<host>/bin`。

### `cargo tauri android dev` 裝置清單為空
- Android 裝置需開啟「USB 偵錯」並信任電腦
- 執行 `adb devices` 確認裝置狀態為 `device`
- macOS 首次連接需 `brew install android-platform-tools`

### OPPO ColorOS WebView 過舊
App 啟動時會顯示「系統 WebView 版本過舊」，至 Play 商店搜尋「Android System WebView」更新後重啟 App。
