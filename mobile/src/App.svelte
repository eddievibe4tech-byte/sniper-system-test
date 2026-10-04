<!--
  狙擊手投資儀表板（Svelte 5 runes）
  🔴 安全契約（沿用 PR#121 review 結論）：reason 屬外部文字，一律以 {text} 插值
     （Svelte 自動 escape），絕不使用 {@html}。敏感資料永不經前端 — 全部 invoke()。
-->
<script lang="ts">
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";

  type Props = { webviewWarning?: string };
  let { webviewWarning = "" }: Props = $props();

  interface RiskReward {
    sampleCount: number;
    avgRr: number | null;
    expectedValuePct: number | null;
    winRatePct: number | null;
  }
  interface Drawdown {
    maxDrawdownPct: number | null;
    equityPoints: number;
  }
  interface Metrics {
    totalPredictions: number;
    verifiedCount: number;
    correctCount: number;
    accuracyRate: number | null;
    riskReward: RiskReward | null;
    drawdown: Drawdown | null;
  }
  interface Signal {
    code: string;
    name: string;
    recommendation: string | null;
    evScore: number | null;
    reason: string | null;
    stopLoss: number | null;
    takeProfit1: number | null;
    riskReward: number | null;
  }
  interface Dashboard {
    metrics: Metrics | null;
    signals: Signal[];
    fetchedAt: string;
    errors: string[];
  }

  let data = $state<Dashboard | null>(null);
  let loading = $state(true);
  let cryptoSymbol = $state("sol");
  let cryptoPrice = $state<string>("");

  async function load() {
    loading = true;
    try {
      data = await invoke<Dashboard>("fetch_dashboard");
    } finally {
      loading = false;
    }
  }

  async function lookupCrypto() {
    cryptoPrice = "查詢中…";
    try {
      const r = await invoke<{ symbol: string; priceTwd: number }>("crypto_price", {
        symbol: cryptoSymbol,
      });
      cryptoPrice = `${r.symbol.toUpperCase()}: NT$ ${r.priceTwd.toLocaleString("en-US")}`;
    } catch (e) {
      cryptoPrice = String(e);
    }
  }

  onMount(load);

  const fmtPct = (v: number | null) => (v == null ? "資料不足" : `${v.toFixed(1)}%`);
  const fmtNum = (v: number | null, d = 2) => (v == null ? "—" : v.toFixed(d));
</script>

<main>
  <h1>🎯 狙擊手投資儀表板</h1>

  {#if webviewWarning}
    <div class="warn">⚠️ {webviewWarning}</div>
  {/if}

  {#if loading}
    <p class="muted">載入中…</p>
  {:else if data}
    {#each data.errors as err}
      <div class="err">{err}</div>
    {/each}

    {#if data.metrics}
      {@const m = data.metrics}
      <section class="cards">
        <div class="card"><span>{m.totalPredictions}</span><label>預測總數</label></div>
        <div class="card">
          <span>{m.accuracyRate == null ? "資料不足" : fmtPct(m.accuracyRate)}</span>
          <label>準確率</label>
        </div>
        <!-- Issue #140：機構級績效指標 -->
        <div class="card">
          <span>{m.riskReward?.avgRr != null ? `${fmtNum(m.riskReward.avgRr)} : 1` : "資料不足"}</span>
          <label>盈虧比 R/R</label>
        </div>
        <div class="card">
          <span>{m.drawdown?.maxDrawdownPct != null ? fmtPct(-Math.abs(m.drawdown.maxDrawdownPct)) : "資料不足"}</span>
          <label>最大回撤 MDD</label>
        </div>
      </section>
    {/if}

    <section>
      <h2>今日訊號</h2>
      {#if data.signals.length === 0}
        <p class="muted">暫無資料</p>
      {:else}
        <table>
          <thead>
            <tr><th>代碼</th><th>名稱</th><th>建議</th><th>EV</th><th>R/R</th></tr>
          </thead>
          <tbody>
            {#each data.signals as s}
              <tr>
                <td>{s.code}</td>
                <td>{s.name}</td>
                <td>{s.recommendation ?? "—"}</td>
                <td>{fmtNum(s.evScore)}</td>
                <td>{s.riskReward != null ? `${fmtNum(s.riskReward)}:1` : "—"}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </section>
  {:else}
    <p class="err">無法載入儀表板資料</p>
  {/if}

  <section>
    <h2>加密價格（CoinGecko → TWD）</h2>
    <div class="row">
      <input bind:value={cryptoSymbol} placeholder="SOL" aria-label="crypto symbol" />
      <button onclick={lookupCrypto}>查詢</button>
      <button onclick={load}>刷新儀表板</button>
    </div>
    {#if cryptoPrice}<p>{cryptoPrice}</p>{/if}
  </section>

  <footer class="muted">Rust Core + Tauri IPC · API Key 永不進入 WebView</footer>
</main>

<style>
  main { font-family: system-ui, sans-serif; padding: 16px; max-width: 480px; margin: auto; }
  h1 { font-size: 1.3rem; }
  .cards { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .card { background: #111827; color: #f9fafb; border-radius: 12px; padding: 12px; text-align: center; }
  .card span { display: block; font-size: 1.25rem; font-weight: 700; }
  .card label { font-size: 0.75rem; opacity: 0.7; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { padding: 6px 4px; border-bottom: 1px solid #e5e7eb; text-align: left; }
  .warn, .err { background: #fef3c7; padding: 8px; border-radius: 8px; font-size: 0.85rem; }
  .err { background: #fee2e2; }
  .muted { color: #6b7280; }
  .row { display: flex; gap: 8px; }
  input { flex: 1; padding: 8px; border: 1px solid #d1d5db; border-radius: 8px; }
  button { padding: 8px 12px; border: none; border-radius: 8px; background: #2563eb; color: white; }
  footer { margin-top: 24px; font-size: 0.7rem; text-align: center; }
</style>
