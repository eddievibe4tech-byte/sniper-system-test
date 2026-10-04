// 前端進入點：先做 WebView 版本護欄（指南風險 #2），再掛載 Svelte App。
// 🔴 資料與展示分離：本檔案絕不出現任何 API Key；所有資料一律經 Rust command。
import { mount } from "svelte";
import { invoke } from "@tauri-apps/api/core";
import App from "./App.svelte";

async function bootstrap() {
  let webviewWarning = "";
  try {
    // UA 由前端讀取傳入，判定邏輯留在 Rust 側（合規：規則屬後端核心）
    await invoke("check_webview", { userAgent: navigator.userAgent });
  } catch (e) {
    webviewWarning = String(e);
  }

  const target = document.getElementById("app");
  if (!target) return;
  mount(App, { target, props: { webviewWarning } });
}

bootstrap();
