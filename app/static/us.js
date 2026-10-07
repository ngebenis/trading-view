// Tampilan Saham Amerika (Alpaca Markets). Memakai helper global dari app.js: $, api, fmt, cls, escapeHtml, blink, priceMove.
(() => {
  const LWC = window.LightweightCharts;
  const DEFAULT_WATCH = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN"];
  const pref = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };
  const keep = (k, v) => { try { localStorage.setItem(k, v); } catch { /* abaikan */ } };
  let watchPref = null;
  try { watchPref = JSON.parse(pref("uWatch", "null")); } catch { /* abaikan */ }

  const st = {
    symbol: pref("uSymbol", "AAPL"), watch: Array.isArray(watchPref) ? watchPref : DEFAULT_WATCH,
    broker: pref("uBroker", ""), interval: pref("uInterval", "5m"), side: "BUY", unit: "usd",
    quote: null, config: null, started: false, tab: "portfolio",
  };
  const timers = [];
  let hovering = false, chart = null, candles = null, volume = null, ema12 = null, ema26 = null, chartData = null;

  const cleanSym = (s) => s.trim().toUpperCase().replace(/^[A-Z]+:/, "").replace(/-/g, ".").replace(/[^A-Z.]/g, "");
  const usd = (n) => n == null ? "—" : "$" + Number(n).toLocaleString("id-ID", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const price = (p) => p == null ? "—" : Number(p).toLocaleString("id-ID", { minimumFractionDigits: 2, maximumFractionDigits: p < 1 ? 4 : 2 });
  const qty = (q) => q == null ? "—" : Number(q).toLocaleString("id-ID", { maximumFractionDigits: 6 });
  const pct = (n) => n == null ? "—" : `${n >= 0 ? "+" : ""}${fmt(n, 2)}%`;
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const clock = (ms) => new Date(ms).toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "America/New_York" }) + " ET";
  const brokerInfo = () => st.config?.brokers?.[st.broker];

  // ---- watchlist ----------------------------------------------------------
  const saveWatch = () => keep("uWatch", JSON.stringify(st.watch));
  function renderWatch() {
    const ul = $("#uWatch");
    ul.innerHTML = "";
    for (const s of st.watch) {
      const li = document.createElement("li");
      li.className = s === st.symbol ? "active" : "";
      li.innerHTML = `<span class="wl-sym">${escapeHtml(s)}</span><span><span class="w-price" data-us="${escapeHtml(s)}"></span><span class="rm" title="Hapus">✕</span></span>`;
      li.onclick = (ev) => {
        if (ev.target.classList.contains("rm")) { st.watch = st.watch.filter((x) => x !== s); saveWatch(); renderWatch(); }
        else loadSymbol(s);
      };
      ul.appendChild(li);
    }
    refreshWatch();
  }
  function renderWatchQuote(q) {
    const el = document.querySelector(`.w-price[data-us="${q.symbol}"]`);
    if (!el) return;
    el.innerHTML = `${price(q.price)}<small>${pct(q.change_pct)}</small>`;
    el.className = "w-price " + cls(q.change_pct);
    blink(el.closest("li")?.querySelector(".wl-sym"), priceMove("uw:" + q.symbol, q.price));
  }
  async function refreshWatch() {
    await Promise.all(st.watch.map(async (s) => {
      try { renderWatchQuote(await api(`/api/us/quote/${s}`)); }
      catch { const el = document.querySelector(`.w-price[data-us="${s}"]`); if (el) el.textContent = "n/a"; }
    }));
  }

  // ---- harga --------------------------------------------------------------
  function renderQuote(q) {
    st.quote = q;
    $("#uPrice").textContent = usd(q.price);
    $("#uChange").textContent = `${q.change >= 0 ? "+" : ""}${price(q.change)} (${pct(q.change_pct)})`;
    $("#uChange").className = "q-change " + cls(q.change);
    const move = priceMove("umain:" + q.symbol, q.price);
    if (move) {
      blink($("#uSymbol"), move);
      const el = $("#uPrice");
      el.classList.remove("flash-up", "flash-down"); void el.offsetWidth; el.classList.add(move > 0 ? "flash-up" : "flash-down");
    }
    const age = q.data_age_seconds;
    const open = st.config?.market_open !== false;
    const stale = open && age != null && age > 60;
    $("#uStatus").className = "live-status on" + (stale ? " warn" : "");
    $("#uStatus").innerHTML = `<span class="dot-live"></span>Alpaca · ${clock(Date.now())}` +
      (!open ? " · bursa tutup (harga penutupan)" : stale ? ` · transaksi terakhir ${age} dtk lalu` : "");
    if (!$("#uLimitPrice").value || $("#uLimitPrice").dataset.sym !== q.symbol) {
      $("#uLimitPrice").value = Number(q.price).toFixed(2);
      $("#uLimitPrice").dataset.sym = q.symbol;
    }
    renderWatchQuote(q);
    liveUpdateChart(q.price);
    updateEstimate();
  }
  async function refreshQuote() {
    try { renderQuote(await api(`/api/us/quote/${st.symbol}`)); }
    catch (e) {
      $("#uStatus").className = "live-status off";
      $("#uStatus").innerHTML = `<span class="dot-live"></span>${escapeHtml(e.message)}`;
    }
  }
  async function loadSymbol(sym) {
    sym = cleanSym(sym);
    if (!sym) return;
    st.symbol = sym;
    keep("uSymbol", sym);
    $("#uSymbol").textContent = sym;
    $("#uLnkTradingView").href = `https://www.tradingview.com/symbols/${sym.replace(".", "-")}/`;
    $("#uPrice").textContent = "—"; $("#uChange").textContent = "";
    document.querySelectorAll("#uWatch li").forEach((li) => li.classList.toggle("active", li.querySelector(".wl-sym").textContent === sym));
    $("#uLimitPrice").value = "";
    await refreshQuote();
    loadChart();
  }

  // ---- grafik -------------------------------------------------------------
  function buildChart() {
    if (chart || !LWC) return;
    chart = LWC.createChart($("#uChart"), {
      autoSize: true,
      layout: { background: { color: css("--surface") }, textColor: css("--muted"), fontSize: 11 },
      grid: { vertLines: { color: css("--grid") }, horzLines: { color: css("--grid") } },
      rightPriceScale: { borderColor: css("--border") },
      timeScale: { borderColor: css("--border"), rightOffset: 4, timeVisible: true },
      crosshair: { mode: LWC.CrosshairMode.Normal }, localization: { locale: "id-ID" },
    });
    const up = css("--up"), down = css("--down");
    candles = chart.addSeries(LWC.CandlestickSeries, { upColor: up, downColor: down, borderUpColor: up,
      borderDownColor: down, wickUpColor: up, wickDownColor: down });
    volume = chart.addSeries(LWC.HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "vol",
      lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    candles.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.22 } });
    const line = { lineWidth: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false };
    ema12 = chart.addSeries(LWC.LineSeries, { ...line, color: css("--series-1") });
    ema26 = chart.addSeries(LWC.LineSeries, { ...line, color: css("--series-2") });
    chart.subscribeCrosshairMove((p) => { hovering = p?.time != null; legend(hovering ? p.seriesData.get(candles) : null); });
  }
  function legend(bar) {
    if (!chartData) return;
    const b = bar || chartData.bars[chartData.bars.length - 1];
    if (!b) { $("#uLegend").innerHTML = ""; return; }
    const at = (line) => line.find((x) => x.time === b.time)?.value;
    const e12 = at(chartData.ema12), e26 = at(chartData.ema26);
    $("#uLegend").innerHTML = `<span>${escapeHtml(chartData.symbol)} · ${escapeHtml(chartData.interval)}</span>` +
      `<span>O <b>${price(b.open)}</b> H <b>${price(b.high)}</b> L <b>${price(b.low)}</b> C <b class="${cls(b.close - b.open)}">${price(b.close)}</b></span>` +
      `<span class="lg"><i class="sw s1"></i>EMA12 ${e12 != null ? price(e12) : "—"}</span>` +
      `<span class="lg"><i class="sw s2"></i>EMA26 ${e26 != null ? price(e26) : "—"}</span>`;
  }
  function renderSignal(a) {
    if (!a) return;
    const label = { BUY: "BELI", SELL: "JUAL", HOLD: "TAHAN" }[a.action] || a.action;
    $("#uSignal").textContent = `${label} (skor ${a.score})`;
    $("#uSignal").className = "signal " + a.action;
    $("#uReasons").innerHTML = (a.reasons || []).map((r) => `<li>${escapeHtml(r)}</li>`).join("");
  }
  async function loadChart() {
    buildChart();
    if (!chart) { $("#uChartMsg").textContent = "Lightweight Charts gagal dimuat"; return; }
    const sym = st.symbol;
    try {
      const d = await api(`/api/us/chart/${sym}?interval=${st.interval}&limit=300`);
      if (sym !== st.symbol) return;
      chartData = d;
      $("#uChartMsg").textContent = d.bars.length ? "" : "Belum ada data";
      candles.applyOptions({ priceFormat: { type: "price", precision: 2, minMove: 0.01 } });
      candles.setData(d.bars);
      applyAlertLines();
      volume.setData(d.bars.map((b) => ({ time: b.time, value: b.volume,
        color: b.close >= b.open ? css("--up") + "55" : css("--down") + "55" })));
      ema12.setData(d.ema12); ema26.setData(d.ema26);
      legend(null);
      renderSignal(d.analysis);
    } catch (e) {
      $("#uChartMsg").textContent = e.message;
    }
  }
  // Garis target alert harga Telegram (alerts.js) untuk saham yang sedang dibuka.
  let alertLines = [];
  function applyAlertLines() {
    if (!candles || !chartData) return;
    alertLines.forEach((l) => candles.removePriceLine(l));
    const bars = chartData.bars;
    alertLines = window.alertPriceLines?.(candles, chartData.symbol, bars.length ? bars[bars.length - 1].close : null,
                                         css("--accent"), LWC, "us") || [];
  }
  window.addEventListener("alerts-changed", applyAlertLines);
  window.usCurrent = () => ({ symbol: st.symbol, price: st.quote?.symbol === st.symbol ? st.quote.price : null });

  function liveUpdateChart(p) {
    if (!chartData || !candles || !chartData.bars.length || st.quote?.symbol !== chartData.symbol) return;
    const last = chartData.bars[chartData.bars.length - 1];
    const bar = { ...last, high: Math.max(last.high, p), low: Math.min(last.low, p), close: p };
    chartData.bars[chartData.bars.length - 1] = bar;
    candles.update(bar);
    if (!hovering) legend(null);
  }

  // ---- order --------------------------------------------------------------
  function renderBrokers() {
    const sel = $("#uBroker");
    sel.innerHTML = Object.entries(st.config.brokers).map(([k, b]) =>
      `<option value="${k}">${escapeHtml(b.display_name)}${b.available ? "" : " — belum diatur"}</option>`).join("");
    if (!st.config.brokers[st.broker]) st.broker = st.config.default_broker;
    sel.value = st.broker;
    $("#uSourceNote").textContent = st.config.data_source === "finnhub"
      ? "Harga dari Finnhub (real-time untuk saham AS; paket gratis dibatasi 60 permintaan/menit). Candle memakai Finnhub bila paket Anda mendukung, selain itu Yahoo Finance."
      : `Harga dari Alpaca Markets (feed ${st.config.data_feed}). Feed IEX gratis dan real-time, tetapi hanya memuat transaksi bursa IEX; feed SIP (berbayar) mencakup seluruh bursa AS.`;
    if (!st.config.data_configured) $("#uSourceNote").textContent += " ⚠ API key data belum diatur — lihat .env.";
    onBrokerChange();
  }
  function renderMarketState() {
    const c = st.config;
    if (!c) return;
    $("#uMarketState").textContent = c.market_open ? "Bursa AS buka" : "Bursa AS tutup — order market menunggu pembukaan";
    $("#uMarketState").title = `Sumber jam bursa: ${c.market_clock_source}`;
  }
  function onBrokerChange() {
    st.broker = $("#uBroker").value;
    keep("uBroker", st.broker);
    const b = brokerInfo();
    let note = "";
    if (b && !b.available) note = b.note;
    else if (b?.is_live) note = st.config.live_trading_enabled
      ? `⚠ Akun Alpaca LIVE — uang sungguhan, maks. $${st.config.max_live_order_usd} per order, perlu konfirmasi.`
      : "Akun live terkunci: set ENABLE_LIVE_TRADING=true di .env untuk mengaktifkan.";
    else if (st.broker === "sim") note = "Simulasi lokal: saldo & order hanya di aplikasi ini (tanpa broker). Order market terisi di harga terakhir.";
    else note = "Alpaca Paper Trading: saldo & order simulasi di server Alpaca.";
    $("#uBrokerNote").textContent = note;
    $("#uBrokerNote").classList.toggle("hidden", !note);
    $("#uSubmit").disabled = !b?.available || (b.is_live && !st.config.live_trading_enabled);
    loadAccount();
  }
  function setSide(side) {
    st.side = side;
    document.querySelectorAll("#uOrderForm .seg button").forEach((b) => b.classList.toggle("active", b.dataset.side === side));
    $("#uSubmit").textContent = side === "BUY" ? "Beli" : "Jual";
    $("#uSubmit").className = "primary " + (side === "BUY" ? "buy" : "sell");
    updateEstimate();
  }
  function orderNumbers() {
    const q = st.quote;
    if (!q) return null;
    const isLimit = $("#uOrderType").value === "LIMIT";
    const p = isLimit ? Number($("#uLimitPrice").value) : q.price;
    const amount = Number($("#uAmount").value) || 0;
    const quantity = st.unit === "shares" ? amount : (p ? amount / p : 0);
    return { p, quantity, value: quantity * p, isLimit };
  }
  function updateEstimate() {
    $("#uLimitWrap").classList.toggle("hidden", $("#uOrderType").value !== "LIMIT");
    const n = orderNumbers();
    $("#uEstimate").innerHTML = n ? `± ${qty(n.quantity)} saham · nilai ${usd(n.value)}<br>Tanpa komisi.` : "";
  }
  async function submitOrder(ev) {
    ev.preventDefault();
    const msg = $("#uOrderMsg"), n = orderNumbers(), b = brokerInfo();
    if (!st.quote || !n) return;
    const body = { broker: st.broker, symbol: st.symbol, side: st.side, order_type: $("#uOrderType").value };
    if (n.isLimit) body.limit_price = n.p;
    if (st.unit === "shares") body.quantity = Number($("#uAmount").value); else body.notional = Number($("#uAmount").value);
    if (b?.is_live) {
      if (!confirm(`ORDER ALPACA LIVE (uang sungguhan)\n\n${st.side === "BUY" ? "BELI" : "JUAL"} ±${qty(n.quantity)} ${st.symbol} ` +
                   `(${usd(n.value)}) ${body.order_type}\n\nLanjutkan?`)) return;
      body.confirm_live = true;
    }
    $("#uSubmit").disabled = true;
    try {
      const o = await api("/api/us/orders", { method: "POST", body: JSON.stringify(body) });
      msg.className = "msg ok";
      msg.textContent = `${o.side} ${qty(o.filled_qty || o.quantity)} ${o.symbol} — ${o.status}` + (o.fill_price ? ` @ ${usd(o.fill_price)}` : "");
      loadAccount();
    } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
    finally { $("#uSubmit").disabled = !b?.available; }
  }

  // ---- akun & riwayat -------------------------------------------------------
  async function loadAccount() {
    const sum = $("#uSummary"), table = $("#uPortfolio");
    try {
      const a = await api(`/api/us/account?broker=${st.broker}`);
      sum.innerHTML = [
        ["Ekuitas", usd(a.equity)], ["Kas", usd(a.cash)], ["Daya beli", usd(a.buying_power)], ["Nilai saham", usd(a.market_value)],
        ["P/L hari ini", `<span class="${cls(a.day_pl)}">${usd(a.day_pl)} (${pct(a.day_pl_pct)})</span>`],
      ].map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
      table.innerHTML = `<tr><th>Saham</th><th>Jumlah</th><th>Avg</th><th>Last</th><th>Nilai</th><th>P/L</th></tr>` +
        (a.positions.length ? a.positions.map((p) => `<tr data-s="${escapeHtml(p.symbol)}"><td><a href="#">${escapeHtml(p.symbol)}</a></td>
          <td>${qty(p.quantity)}</td><td>${price(p.avg_price)}</td><td>${price(p.last_price)}</td><td>${usd(p.market_value)}</td>
          <td class="${cls(p.unrealized_pl)}">${usd(p.unrealized_pl)} (${pct(p.unrealized_pl_pct)})</td></tr>`).join("")
          : `<tr><td colspan="6" style="text-align:left;color:var(--muted)">Belum ada posisi.</td></tr>`);
      table.querySelectorAll("tr[data-s] a").forEach((el) => el.onclick = (e) => { e.preventDefault(); loadSymbol(el.closest("tr").dataset.s); });
    } catch (e) {
      sum.innerHTML = `<div class="muted">${escapeHtml(e.message)}</div>`;
      table.innerHTML = "";
    }
    if (st.tab === "orders") loadOrders();
  }
  async function loadOrders() {
    const t = $("#uOrders");
    try {
      const rows = await api(`/api/us/orders?broker=${st.broker}`);
      t.innerHTML = `<tr><th>Waktu (WIB)</th><th>Saham</th><th>Sisi</th><th>Tipe</th><th>Jumlah</th><th>Harga</th><th>Status</th><th></th></tr>` +
        (rows.length ? rows.map((o) => `<tr><td>${new Date(o.created_at * 1000).toLocaleString("id-ID", { timeZone: "Asia/Jakarta" })}</td>
          <td>${escapeHtml(o.symbol)}</td><td class="${o.side === "BUY" ? "up" : "down"}">${o.side === "BUY" ? "Beli" : "Jual"}</td>
          <td>${o.order_type === "LIMIT" ? "Limit " + price(o.limit_price) : "Market"}</td>
          <td>${o.quantity != null ? qty(o.filled_qty || o.quantity) : usd(o.notional)}</td>
          <td>${o.fill_price ? usd(o.fill_price) : "—"}</td><td>${escapeHtml(o.alpaca_status || o.status)}</td>
          <td>${o.status === "OPEN" ? `<button class="link" data-cancel="${escapeHtml(o.id)}">Batalkan</button>` : ""}</td></tr>`).join("")
          : `<tr><td colspan="8" style="text-align:left;color:var(--muted)">Belum ada order.</td></tr>`);
      t.querySelectorAll("[data-cancel]").forEach((el) => el.onclick = async () => {
        try { await api(`/api/us/orders/${el.dataset.cancel}?broker=${st.broker}`, { method: "DELETE" }); } catch (e) { alert(e.message); }
        loadOrders(); loadAccount();
      });
    } catch (e) { t.innerHTML = `<tr><td style="text-align:left;color:var(--muted)">${escapeHtml(e.message)}</td></tr>`; }
  }
  function setTab(tab) {
    st.tab = tab;
    document.querySelectorAll(".utab[data-utab]").forEach((t) => t.classList.toggle("active", t.dataset.utab === tab));
    $("#uPortfolioPane").classList.toggle("hidden", tab !== "portfolio");
    $("#uOrdersPane").classList.toggle("hidden", tab !== "orders");
    if (tab === "orders") loadOrders();
  }

  // ---- siklus hidup ---------------------------------------------------------
  let wired = false;
  function wire() {
    if (wired) return;
    wired = true;
    $("#uAddWatch").onsubmit = (e) => {
      e.preventDefault();
      const s = cleanSym($("#uAddWatchInput").value);
      if (s && !st.watch.includes(s)) { st.watch.push(s); saveWatch(); renderWatch(); }
      $("#uAddWatchInput").value = "";
      if (s) loadSymbol(s);
    };
    $("#uInterval").value = st.interval;
    $("#uInterval").onchange = () => { st.interval = $("#uInterval").value; keep("uInterval", st.interval); loadChart(); };
    $("#uBroker").onchange = onBrokerChange;
    document.querySelectorAll("#uOrderForm .seg button").forEach((b) => b.onclick = () => setSide(b.dataset.side));
    ["#uOrderType", "#uAmount", "#uLimitPrice"].forEach((s) => $(s).addEventListener("input", updateEstimate));
    $("#uAmountUnit").onchange = () => { st.unit = $("#uAmountUnit").value; updateEstimate(); };
    $("#uOrderForm").onsubmit = submitOrder;
    document.querySelectorAll(".utab[data-utab]").forEach((t) => t.onclick = () => setTab(t.dataset.utab));
    $("#uResetSim").onclick = async () => {
      if (!confirm("Reset akun simulasi saham AS ke saldo awal?")) return;
      await api("/api/us/paper/reset", { method: "POST" });
      loadAccount();
    };
    $("#uOpenNotif").onclick = () => {  // Telegram dipakai bersama; pengaturannya ada di tab Notifikasi Saham IDX
      window.setMarket("idx");
      document.querySelector('.tab[data-tab="notif"]')?.click();
      $("#notifPane").scrollIntoView({ behavior: "smooth", block: "start" });
    };
  }
  async function start() {
    wire();
    if (!st.started) {
      st.started = true;
      try { st.config = await api("/api/us/config"); } catch (e) { st.started = false; $("#uStatus").textContent = e.message; return; }
      renderBrokers();
      renderMarketState();
      renderWatch();
      setSide("BUY");
      loadSymbol(st.symbol);
    }
    const active = () => document.body.dataset.market === "us" && !document.hidden;
    const every = (ms, fn) => timers.push(setInterval(() => { if (active()) fn(); }, ms));
    stop();
    every(5_000, refreshQuote);   // hemat kuota Finnhub gratis (60 permintaan/menit)
    every(15_000, refreshWatch);
    every(15_000, loadAccount);
    every(60_000, loadChart);
    every(60_000, async () => { try { st.config = await api("/api/us/config"); renderMarketState(); } catch { /* abaikan */ } });
  }
  function stop() { while (timers.length) clearInterval(timers.pop()); }

  window.addEventListener("market-changed", (e) => { if (e.detail === "us") start(); else stop(); });
  if (document.body.dataset.market === "us") start();
})();
