import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// Tauri expects a fixed port in dev; clearScreen false so Rust logs stay visible.
export default defineConfig({
  plugins: [svelte()],
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    target: ["es2021", "chrome100"], // 對應 webview_guard 的最低基線
    outDir: "dist",
  },
});
