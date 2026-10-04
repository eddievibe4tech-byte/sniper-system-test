import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

/** @type {import('@sveltejs/vite-plugin-svelte').Config} */
// PR #142 Code Review P0-1 方案 A：
// svelte-check（新版 ConfigLoader）需要明確的設定源，否則會走 vite.config 讀取路徑
// 並踩中「No Svelte configuration found in vite config」的上游回歸。
// 同時 vitePreprocess 讓 IDE 對 <script lang="ts"> 的診斷更準確。
export default {
  preprocess: vitePreprocess(),
  compilerOptions: { runes: true },
};
