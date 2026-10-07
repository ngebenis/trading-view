// Grafik harga Lightweight Charts (TradingView, Apache-2.0) dengan penanda transaksi.
// Memakai helper global dari app.js: $, api, fmt, fmtPrice, isIndex, escapeHtml, state, renderChart.
(() => {
  const LWC = window.LightweightCharts;
  const SOURCE_LABEL = { auto: "🤖 bot", webhook: "📡 webhook", manual: "manual" };
  const pref = (k, d) => { try { return localStorage.getItem(k) || d; } catch { return d; } };
  const save = (k, v) => { try { localStorage.setItem(k, v); } catch { /* abaikan */ } };

  const view = { mode: pref("chartView", "lw"), range: pref("chartRange", "1y"), markers: "paper" };
  let chart = null, candles = null, volume = null, ema12 = null, ema26 = null, markerApi = null, avgLine = null;
  let data = null, markerByDay = new Map(), loadedFor = null;

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function destroy() {
    if (chart) { chart.remove(); chart = null; }
    markerApi = avgLine = null;
  }

  function build() {
    destroy();
    const el = $("#lwChart");
    chart = LWC.createChart(el, {
      autoSize: true,
      layout: { background: { color: css("--surface") }, textColor: css("--muted"), fontSize: 11 },
      grid: { vertLines: { color: css("--grid") }, horzLines: { color: css("--grid") } },
      rightPriceScale: { borderColor: css("--border") },
      timeScale: { borderColor: css("--border"), rightOffset: 4 },
      crosshair: { mode: LWC.CrosshairMode.Normal },
      localization: { locale: "id-ID" },
    });
    const up = css("--up"), down = css("--down");
    candles = chart.addSeries(LWC.CandlestickSeries, {
      upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down,
    });
    volume = chart.addSeries(LWC.HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol",
      lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    candles.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.22 } });
    const lineOpts = { lineWidth: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false };
    ema12 = chart.addSeries(LWC.LineSeries, { ...lineOpts, color: css("--series-1") });
    ema26 = chart.addSeries(LWC.LineSeries, { ...lineOpts, color: css("--series-2") });
    markerApi = LWC.createSeriesMarkers(candles, []);
    chart.subscribeCrosshairMove(onCrosshair);
  }

  // ---- penanda ----------------------------------------------------------
  function snap(day, days) { // tanggal transaksi -> candle terakhir <= tanggal itu
    let lo = 0, hi = days.length - 1, ans = null;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (days[mid] <= day) { ans = days[mid]; lo = mid + 1; } else hi = mid - 1; }
    return ans;
  }

  function paperMarkers() {
    return data.markers.map((m) => ({
      time: m.time, side: m.side,
      text: `${m.side === "BUY" ? "B" : "S"} ${fmt(m.lots)}`,
      detail: `${m.side === "BUY" ? "BELI" : "JUAL"} ${fmt(m.lots)} lot @ ${fmtPrice(m.avg_price, data.symbol)}` +
        (m.count > 1 ? ` (${m.count} order)` : "") + ` · ${m.sources.map((s) => SOURCE_LABEL[s] || s).join(", ")}`,
    }));
  }

  function backtestMarkers() {
    const trades = window.getBacktestTrades?.(data.symbol) || [];
    const days = data.bars.map((b) => b.time);
    const out = [];
    for (const t of trades) {
      const entry = snap(t.entry_date, days);
      if (entry) out.push({ time: entry, side: "BUY", text: `B ${fmt(t.lots)}`,
        detail: `Backtest BELI ${fmt(t.lots)} lot @ ${fmt(t.entry_price, 2)}` });
      if (t.closed) {
        const exit = snap(t.exit_date, days);
        const pl = `${t.pl_pct > 0 ? "+" : ""}${fmt(t.pl_pct, 1)}%`;
        if (exit) out.push({ time: exit, side: "SELL", text: `S ${pl}`,
          detail: `Backtest JUAL @ ${fmt(t.exit_price)} · hasil ${pl}` });
      }
    }
    return out;
  }

  function applyMarkers() {
    if (!data || !markerApi) return;
    const list = view.markers === "backtest" ? backtestMarkers() : paperMarkers();
    markerByDay = new Map();
    for (const m of list) markerByDay.set(m.time, [...(markerByDay.get(m.time) || []), m.detail]);
    const up = css("--up"), down = css("--down");
    markerApi.setMarkers(list.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0)).map((m) => ({
      time: m.time, position: m.side === "BUY" ? "belowBar" : "aboveBar",
      shape: m.side === "BUY" ? "arrowUp" : "arrowDown", color: m.side === "BUY" ? up : down, text: m.text,
    })));
    if (avgLine) { candles.removePriceLine(avgLine); avgLine = null; }
    if (view.markers === "paper" && data.position) {
      avgLine = candles.createPriceLine({ price: data.position.avg_price, color: css("--accent"), lineWidth: 1,
        lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true, title: `Avg ${fmt(data.position.shares / 100)} lot` });
    }
    const count = list.length;
    $("#chartMarkerInfo").textContent = view.markers === "backtest"
      ? (count ? `${count} penanda dari backtest terakhir` : "Tidak ada transaksi backtest untuk saham ini")
      : (count ? `${count} penanda transaksi akun simulasi` : "Belum ada transaksi akun simulasi untuk saham ini");
  }

  // ---- keterangan (crosshair) -------------------------------------------
  function readout(bar, extra) {
    if (!bar) { $("#chartLegend").innerHTML = ""; return; }
    const p = (v) => fmtPrice(v, data.symbol);
    const chg = bar.close - bar.open;
    $("#chartLegend").innerHTML =
      `<span class="muted">${new Date(bar.time + "T00:00:00").toLocaleDateString("id-ID", { day: "numeric", month: "short", year: "numeric" })}</span>
       O <b>${p(bar.open)}</b> H <b>${p(bar.high)}</b> L <b>${p(bar.low)}</b> C <b class="${chg >= 0 ? "up" : "down"}">${p(bar.close)}</b>
       <span class="muted">Vol ${fmt(bar.volume / 100)} lot</span>
       <span class="lg"><i class="sw s1"></i>EMA12 ${extra.e12 != null ? p(extra.e12) : "—"}</span>
       <span class="lg"><i class="sw s2"></i>EMA26 ${extra.e26 != null ? p(extra.e26) : "—"}</span>` +
      (extra.trades ? `<div class="chart-trades">${extra.trades.map(escapeHtml).join("<br>")}</div>` : "");
  }

  function onCrosshair(param) {
    if (!data) return;
    let bar, e12, e26;
    if (param.time && param.seriesData.size) {
      bar = param.seriesData.get(candles);
      e12 = param.seriesData.get(ema12)?.value;
      e26 = param.seriesData.get(ema26)?.value;
      const v = param.seriesData.get(volume);
      if (bar) bar = { ...bar, time: param.time, volume: v ? v.value : 0 };
    }
    if (!bar) { // kursor di luar grafik -> tampilkan candle terakhir
      bar = data.bars[data.bars.length - 1];
      e12 = data.ema12.at(-1)?.value; e26 = data.ema26.at(-1)?.value;
    }
    const time = typeof bar.time === "string" ? bar.time
      : `${bar.time.year}-${String(bar.time.month).padStart(2, "0")}-${String(bar.time.day).padStart(2, "0")}`;
    readout({ ...bar, time }, { e12, e26, trades: markerByDay.get(time) });
  }

  // ---- muat data --------------------------------------------------------
  async function draw(symbol, keepView = false) {
    const msg = $("#chartMsg");
    if (!LWC) { msg.textContent = "Pustaka grafik gagal dimuat."; return; }
    try {
      data = await api(`/api/chart/${symbol}?range=${view.range}`);
    } catch (e) {
      destroy(); msg.textContent = e.message; $("#chartLegend").innerHTML = ""; return;
    }
    if (symbol !== state.symbol) return; // pengguna sudah pindah saham
    msg.textContent = "";
    if (!chart) build();
    const precision = isIndex(symbol) ? 2 : 0;
    candles.applyOptions({ priceFormat: { type: "price", precision, minMove: precision ? 0.01 : 1 } });
    candles.setData(data.bars.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
    const upA = css("--up"), downA = css("--down");
    volume.setData(data.bars.map((b) => ({ time: b.time, value: b.volume,
      color: (b.close >= b.open ? upA : downA) + "55" })));
    ema12.setData(data.ema12);
    ema26.setData(data.ema26);
    applyMarkers();
    if (!keepView || loadedFor !== symbol) chart.timeScale().fitContent();
    loadedFor = symbol;
    onCrosshair({});
    $("#chartMarkerSel").querySelector('option[value="backtest"]').disabled = !(window.getBacktestTrades?.(symbol) || []).length;
  }

  function showMode() {
    const lw = view.mode === "lw";
    $("#lwWrap").classList.toggle("hidden", !lw);
    $("#tvChart").classList.toggle("hidden", lw);
    document.querySelectorAll("#chartViewSeg button").forEach((b) => b.classList.toggle("active", b.dataset.view === view.mode));
    $("#chartLwControls").classList.toggle("hidden", !lw);
  }

  // Dipanggil app.js saat saham berganti.
  function render(symbol) {
    showMode();
    if (view.mode === "lw") { draw(symbol); } else { destroy(); loadedFor = null; renderChart(symbol); }
  }

  // Perbarui data & penanda tanpa mengubah zoom (mis. setelah order baru).
  function refresh() {
    if (view.mode === "lw" && state.symbol) draw(state.symbol, true);
  }

  // Dari tab Backtest: tampilkan transaksi backtest untuk saham tertentu.
  function showBacktest(symbol) {
    view.markers = "backtest";
    $("#chartMarkerSel").value = "backtest";
    if (view.mode !== "lw") { view.mode = "lw"; save("chartView", "lw"); }
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (symbol === state.symbol) render(symbol); else loadSymbol(symbol);
  }

  function init() {
    document.querySelectorAll("#chartViewSeg button").forEach((b) => b.onclick = () => {
      view.mode = b.dataset.view; save("chartView", view.mode); render(state.symbol);
    });
    const rangeSel = $("#chartRangeSel");
    rangeSel.value = view.range;
    rangeSel.onchange = () => { view.range = rangeSel.value; save("chartRange", view.range); loadedFor = null; draw(state.symbol); };
    $("#chartMarkerSel").onchange = (e) => { view.markers = e.target.value; applyMarkers(); onCrosshair({}); };
    // Warna grafik mengikuti tema terang/gelap.
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (view.mode === "lw" && state.symbol) { destroy(); loadedFor = null; draw(state.symbol); }
    });
  }

  init();
  window.renderPriceChart = render;
  window.refreshPriceChart = refresh;
  window.showBacktestOnChart = showBacktest;
})();
