// Tampilan Crypto (Binance). Memakai helper global dari app.js: $, api, fmt, cls, escapeHtml, blink, priceMove.
(() => {
  const LWC = window.LightweightCharts;
  const DEFAULT_WATCH = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"];
  const QUOTES = ["FDUSD", "USDT", "USDC", "TUSD", "BUSD", "BTC", "ETH", "BNB", "TRY", "EUR", "BRL", "IDR"];
  const SOURCE_LABEL = { auto: "🤖 bot", manual: "manual" };
  const pref = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };
  const keep = (k, v) => { try { localStorage.setItem(k, v); } catch { /* abaikan */ } };
  let watchPref = null;
  try { watchPref = JSON.parse(pref("cWatch", "null")); } catch { /* abaikan */ }

  const st = {
    symbol: pref("cSymbol", "BTCUSDT"), watch: Array.isArray(watchPref) ? watchPref : DEFAULT_WATCH,
    broker: pref("cBroker", "paper"), interval: pref("cInterval", "1h"), side: "BUY", unit: "quote",
    quote: null, config: null, started: false, botRunning: false, tab: "portfolio", positions: [],
  };
  const timers = [];
  let hovering = false;
  let chart = null, candles = null, volume = null, ema12 = null, ema26 = null, markerApi = null, chartData = null;

  // ---- format -------------------------------------------------------------
  const cleanPair = (s) => s.trim().toUpperCase().replace(/^BINANCE:/, "").replace(/[^A-Z0-9]/g, "");
  const split = (s) => { const q = QUOTES.find((x) => s.endsWith(x) && s.length > x.length + 1); return q ? [s.slice(0, -q.length), q] : [s, ""]; };
  const decimalsOf = (step) => { const s = String(step ?? ""); return s.includes(".") ? s.split(".")[1].replace(/0+$/, "").length : 0; };
  function price(p) {
    if (p == null) return "—";
    const a = Math.abs(p);
    const d = a >= 1000 ? 2 : a >= 1 ? 4 : a >= 0.01 ? 6 : 8;
    return Number(p).toLocaleString("id-ID", { maximumFractionDigits: d, minimumFractionDigits: a >= 1000 ? 2 : 0 });
  }
  const qty = (q) => q == null ? "—" : Number(q).toLocaleString("id-ID", { maximumFractionDigits: 8 });
  const usd = (n) => n == null ? "—" : fmt(n, 2);
  const pct = (n) => n == null ? "—" : `${n >= 0 ? "+" : ""}${fmt(n, 2)}%`;
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const hhmmss = (ms) => new Date(ms).toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Jakarta" }) + " WIB";
  const brokerInfo = () => st.config?.brokers?.[st.broker];

  // ---- pasar: IDX / Crypto ------------------------------------------------
  function setMarket(m) {
    document.body.dataset.market = m;
    keep("market", m);
    document.querySelectorAll(".idx-only").forEach((el) => el.classList.toggle("hidden", m !== "idx"));
    document.querySelectorAll(".crypto-only").forEach((el) => el.classList.toggle("hidden", m !== "crypto"));
    document.querySelectorAll("#marketSeg button").forEach((b) => b.classList.toggle("active", b.dataset.market === m));
    document.title = m === "crypto" ? "Crypto · IDX Trading View" : "IDX Trading View";
    window.liveResubscribe?.();
    if (m === "crypto") start(); else stop();
  }

  // ---- watchlist ----------------------------------------------------------
  function saveWatch() { keep("cWatch", JSON.stringify(st.watch)); }
  function renderWatch() {
    const ul = $("#cWatch");
    ul.innerHTML = "";
    for (const s of st.watch) {
      const li = document.createElement("li");
      li.className = s === st.symbol ? "active" : "";
      li.innerHTML = `<span class="wl-sym">${escapeHtml(s)}</span><span><span class="w-price" data-cs="${escapeHtml(s)}"></span><span class="rm" title="Hapus">✕</span></span>`;
      li.onclick = (ev) => {
        if (ev.target.classList.contains("rm")) { st.watch = st.watch.filter((x) => x !== s); saveWatch(); renderWatch(); }
        else loadPair(s);
      };
      ul.appendChild(li);
    }
    refreshWatch();
  }
  function renderWatchQuote(q) {
    const el = document.querySelector(`.w-price[data-cs="${q.symbol}"]`);
    if (!el) return;
    el.innerHTML = `${price(q.price)}<small>${pct(q.change_pct)}</small>`;
    el.className = "w-price " + cls(q.change_pct);
    blink(el.closest("li")?.querySelector(".wl-sym"), priceMove("cw:" + q.symbol, q.price));
  }
  async function refreshWatch() {
    await Promise.all(st.watch.map(async (s) => {
      try { renderWatchQuote(await api(`/api/crypto/quote/${s}`)); }
      catch { const el = document.querySelector(`.w-price[data-cs="${s}"]`); if (el) el.textContent = "n/a"; }
    }));
  }

  // ---- harga --------------------------------------------------------------
  function renderQuote(q) {
    st.quote = q;
    $("#cPrice").textContent = price(q.price);
    $("#cChange").textContent = `${q.change >= 0 ? "+" : ""}${price(q.change)} (${pct(q.change_pct)} 24 jam)`;
    $("#cChange").className = "q-change " + cls(q.change);
    const move = priceMove("cmain:" + q.symbol, q.price);
    if (move) {
      blink($("#cSymbol"), move);
      const el = $("#cPrice");
      el.classList.remove("flash-up", "flash-down"); void el.offsetWidth; el.classList.add(move > 0 ? "flash-up" : "flash-down");
    }
    const age = q.data_age_seconds;
    $("#cStatus").className = "live-status on" + (age != null && age > 30 ? " warn" : "");
    $("#cStatus").innerHTML = `<span class="dot-live"></span>Binance · ${hhmmss(Date.now())}` + (age != null && age > 30 ? ` · data ${age} dtk lalu` : " · real-time");
    const r = q.rules;
    if (r) {
      $("#cLimitPrice").step = r.tick_size;
      $("#cRulesHint").textContent = `Min. ${qty(r.min_qty)} ${q.base} (kelipatan ${r.step_size}) · nilai min. ${r.min_notional} ${q.quote_asset}`;
    }
    const unitSel = $("#cAmountUnit");
    if (unitSel.dataset.pair !== q.symbol) {
      unitSel.innerHTML = `<option value="quote">${escapeHtml(q.quote_asset)}</option><option value="base">${escapeHtml(q.base)}</option>`;
      unitSel.value = st.unit;
      unitSel.dataset.pair = q.symbol;
    }
    if (!$("#cLimitPrice").value || $("#cLimitPrice").dataset.pair !== q.symbol) {
      $("#cLimitPrice").value = r ? Number(q.price).toFixed(decimalsOf(r.tick_size)) : q.price;
      $("#cLimitPrice").dataset.pair = q.symbol;
    }
    renderWatchQuote(q);
    liveUpdateChart(q.price);
    updateEstimate();
  }
  async function refreshQuote() {
    try { renderQuote(await api(`/api/crypto/quote/${st.symbol}`)); }
    catch (e) {
      $("#cStatus").className = "live-status off";
      $("#cStatus").innerHTML = `<span class="dot-live"></span>${escapeHtml(e.message)}`;
    }
  }

  async function loadPair(sym) {
    sym = cleanPair(sym);
    if (!sym) return;
    st.symbol = sym;
    keep("cSymbol", sym);
    const [base, quote] = split(sym);
    $("#cSymbol").textContent = sym;
    $("#cLnkBinance").href = `https://www.binance.com/en/trade/${base}_${quote}?type=spot`;
    $("#cLnkTradingView").href = `https://www.tradingview.com/symbols/${sym}/?exchange=BINANCE`;
    $("#cPrice").textContent = "—"; $("#cChange").textContent = "";
    document.querySelectorAll("#cWatch li").forEach((li) => li.classList.toggle("active", li.querySelector(".wl-sym").textContent === sym));
    $("#cLimitPrice").value = "";
    await refreshQuote();
    loadChart();
  }

  // ---- grafik -------------------------------------------------------------
  function buildChart() {
    if (chart || !LWC) return;
    chart = LWC.createChart($("#cChart"), {
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
    markerApi = LWC.createSeriesMarkers(candles, []);
    chart.subscribeCrosshairMove((p) => { hovering = p?.time != null; legend(hovering ? p.seriesData.get(candles) : null); });
  }

  function legend(bar) {
    if (!chartData) return;
    const b = bar || chartData.bars[chartData.bars.length - 1];
    if (!b) { $("#cLegend").innerHTML = ""; return; }
    const at = (line) => line.find((x) => x.time === b.time)?.value;
    const e12 = at(chartData.ema12), e26 = at(chartData.ema26);
    $("#cLegend").innerHTML = `<span>${escapeHtml(chartData.symbol)} · ${escapeHtml(chartData.interval)}</span>` +
      `<span>O <b>${price(b.open)}</b> H <b>${price(b.high)}</b> L <b>${price(b.low)}</b> ` +
      `C <b class="${cls(b.close - b.open)}">${price(b.close)}</b></span>` +
      `<span class="lg"><i class="sw s1"></i>EMA12 ${e12 != null ? price(e12) : "—"}</span>` +
      `<span class="lg"><i class="sw s2"></i>EMA26 ${e26 != null ? price(e26) : "—"}</span>` +
      chartData.markers.filter((m) => m.time === b.time).map((m) => `<span class="chart-trades">${m.side === "BUY" ? "BELI" : "JUAL"} ` +
        `${qty(m.quantity)} @ ${price(m.avg_price)}${m.count > 1 ? ` (${m.count} order)` : ""} · ` +
        `${m.sources.map((s) => SOURCE_LABEL[s] || s).join(", ")}</span>`).join("");
  }

  function renderSignal(a) {
    if (!a) return;
    const label = { BUY: "BELI", SELL: "JUAL", HOLD: "TAHAN" }[a.action] || a.action;
    $("#cSignal").textContent = `${label} (skor ${a.score})`;
    $("#cSignal").className = "signal " + a.action;
    $("#cReasons").innerHTML = (a.reasons || []).map((r) => `<li>${escapeHtml(r)}</li>`).join("");
  }

  async function loadChart() {
    buildChart();
    if (!chart) { $("#cChartMsg").textContent = "Lightweight Charts gagal dimuat"; return; }
    const sym = st.symbol;
    try {
      const d = await api(`/api/crypto/chart/${sym}?interval=${st.interval}&limit=300&broker=${st.broker}`);
      if (sym !== st.symbol) return;
      chartData = d;
      $("#cChartMsg").textContent = d.bars.length ? "" : "Belum ada data";
      const tick = st.quote?.symbol === sym && st.quote.rules ? Number(st.quote.rules.tick_size) : 0.01;
      candles.applyOptions({ priceFormat: { type: "price", precision: Math.min(decimalsOf(st.quote?.rules?.tick_size ?? "0.01"), 8), minMove: tick || 0.01 } });
      candles.setData(d.bars);
      volume.setData(d.bars.map((b) => ({ time: b.time, value: b.volume,
        color: b.close >= b.open ? css("--up") + "55" : css("--down") + "55" })));
      ema12.setData(d.ema12); ema26.setData(d.ema26);
      const up = css("--up"), down = css("--down");
      markerApi.setMarkers(d.markers.map((m) => ({
        time: m.time, position: m.side === "BUY" ? "belowBar" : "aboveBar", shape: m.side === "BUY" ? "arrowUp" : "arrowDown",
        color: m.side === "BUY" ? up : down,
        text: `${m.side === "BUY" ? "B" : "S"}${m.sources.includes("auto") ? " 🤖" : ""}`,
      })));
      legend(null);
      renderSignal(d.analysis);
    } catch (e) {
      $("#cChartMsg").textContent = e.message;
    }
  }

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
    const sel = $("#cBroker");
    sel.innerHTML = Object.entries(st.config.brokers).map(([k, b]) =>
      `<option value="${k}">${escapeHtml(b.display_name)}${b.available ? "" : " — belum diatur"}</option>`).join("");
    if (!st.config.brokers[st.broker]) st.broker = "paper";
    sel.value = st.broker;
    onBrokerChange();
  }
  function onBrokerChange() {
    st.broker = $("#cBroker").value;
    keep("cBroker", st.broker);
    const b = brokerInfo();
    $("#cMarkerAcct").textContent = b?.display_name || st.broker;
    let note = "";
    if (b && !b.available) note = b.note;
    else if (b?.is_live) note = st.config.live_trading_enabled
      ? "⚠ Akun Binance ASLI — order memakai uang sungguhan dan perlu konfirmasi."
      : "Akun Binance asli terkunci: set ENABLE_LIVE_TRADING=true di .env untuk mengaktifkan.";
    else if (st.broker === "testnet") note = "Binance Spot Testnet: saldo & order palsu, alur sama dengan akun asli.";
    $("#cBrokerNote").textContent = note;
    $("#cBrokerNote").classList.toggle("hidden", !note);
    $("#cSubmit").disabled = !b?.available || (b.is_live && !st.config.live_trading_enabled);
    loadAccount();
    loadChart();
  }
  function setSide(side) {
    st.side = side;
    document.querySelectorAll("#cOrderForm .seg button").forEach((b) => b.classList.toggle("active", b.dataset.side === side));
    $("#cSubmit").textContent = side === "BUY" ? "Beli" : "Jual";
    $("#cSubmit").className = "primary " + (side === "BUY" ? "buy" : "sell");
    updateEstimate();
  }
  function orderNumbers() {
    const q = st.quote;
    if (!q) return null;
    const isLimit = $("#cOrderType").value === "LIMIT";
    const p = isLimit ? Number($("#cLimitPrice").value) : q.price;
    const amount = Number($("#cAmount").value) || 0;
    const quantity = st.unit === "base" ? amount : (p ? amount / p : 0);
    return { p, quantity, value: quantity * p, isLimit };
  }
  function updateEstimate() {
    $("#cLimitWrap").classList.toggle("hidden", $("#cOrderType").value !== "LIMIT");
    const n = orderNumbers(), q = st.quote;
    if (!n || !q) { $("#cEstimate").textContent = ""; return; }
    const fee = n.value * (st.config?.fee_pct ?? 0.1) / 100;
    $("#cEstimate").innerHTML = `± ${qty(n.quantity)} ${escapeHtml(q.base)} · nilai ${usd(n.value)} ${escapeHtml(q.quote_asset)}` +
      `<br>Fee ±${st.config?.fee_pct ?? 0.1}%: ${usd(fee)} ${escapeHtml(q.quote_asset)} · jumlah dibulatkan ke kelipatan Binance`;
  }
  async function submitOrder(ev) {
    ev.preventDefault();
    const msg = $("#cOrderMsg"), q = st.quote, n = orderNumbers(), b = brokerInfo();
    if (!q || !n) return;
    const body = { broker: st.broker, symbol: st.symbol, side: st.side, order_type: $("#cOrderType").value };
    if (n.isLimit) body.limit_price = n.p;
    if (st.unit === "base") body.quantity = Number($("#cAmount").value); else body.quote_amount = Number($("#cAmount").value);
    if (b?.is_live) {
      if (!confirm(`ORDER BINANCE ASLI (uang sungguhan)\n\n${st.side === "BUY" ? "BELI" : "JUAL"} ±${qty(n.quantity)} ${q.base} ` +
                   `(${usd(n.value)} ${q.quote_asset}) ${body.order_type}\n\nLanjutkan?`)) return;
      body.confirm_live = true;
    }
    $("#cSubmit").disabled = true;
    try {
      const o = await api("/api/crypto/orders", { method: "POST", body: JSON.stringify(body) });
      msg.className = "msg ok";
      msg.textContent = `${o.side} ${qty(o.quantity)} ${q.base} — ${o.status}` + (o.fill_price ? ` @ ${price(o.fill_price)}` : "");
      loadAccount(); loadChart();
    } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
    finally { $("#cSubmit").disabled = !b?.available; }
  }

  // ---- akun & riwayat -------------------------------------------------------
  async function loadAccount() {
    const sum = $("#cSummary"), table = $("#cPortfolio");
    try {
      const a = await api(`/api/crypto/account?broker=${st.broker}`);
      const quote = a.quote_asset;
      sum.innerHTML = [
        ["Ekuitas (perkiraan)", `${usd(a.equity)} ${quote}`], [`Saldo ${quote}`, usd(a.cash)], ["Bisa dipakai", usd(a.buying_power)],
        ["Nilai aset", usd(a.market_value)],
        ...(a.total_pl != null ? [["Total P/L", `<span class="${cls(a.total_pl)}">${usd(a.total_pl)} (${pct(a.total_pl_pct)})</span>`]] : []),
      ].map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
      const live = st.broker !== "paper";
      st.positions = a.positions;
      table.innerHTML = `<tr><th>Aset</th><th>Jumlah</th>${live ? "<th>Milik bot</th>" : ""}<th>Avg</th><th>Last</th><th>Nilai (${quote})</th><th>P/L</th></tr>` +
        (a.positions.length ? a.positions.map((p) => `<tr data-s="${escapeHtml(p.symbol)}"><td><a href="#">${escapeHtml(p.asset)}</a></td>
          <td>${qty(p.quantity)}</td>${live ? `<td>${p.bot_quantity ? qty(p.bot_quantity) : "—"}</td>` : ""}
          <td>${price(p.avg_price)}</td><td>${price(p.last_price)}</td><td>${usd(p.market_value)}</td>
          <td class="${cls(p.unrealized_pl)}">${p.unrealized_pl == null ? "—" : `${usd(p.unrealized_pl)} (${pct(p.unrealized_pl_pct)})`}</td></tr>`).join("")
          : `<tr><td colspan="7" style="text-align:left;color:var(--muted)">Belum ada aset.</td></tr>`);
      table.querySelectorAll("tr[data-s] a").forEach((el) => el.onclick = (e) => { e.preventDefault(); loadPair(el.closest("tr").dataset.s); });
      for (const p of a.positions) blink(table.querySelector(`tr[data-s="${p.symbol}"] a`), priceMove(`cpos:${st.broker}:${p.symbol}`, p.last_price));
    } catch (e) {
      sum.innerHTML = `<div class="muted">${escapeHtml(e.message)}</div>`;
      table.innerHTML = "";
    }
    loadOrders();
  }
  async function loadOrders() {
    try {
      const orders = await api(`/api/crypto/orders?broker=${st.broker}&limit=200`);
      $("#cOrders").innerHTML = `<tr><th>Waktu</th><th>Pasangan</th><th>Aksi</th><th>Tipe</th><th>Jumlah</th><th>Harga</th><th>Fee</th><th>Sumber</th><th>Status</th><th></th></tr>` +
        (orders.length ? orders.map((o) => `<tr><td>${new Date(o.created_at * 1000).toLocaleString("id-ID")}</td><td>${escapeHtml(o.symbol)}</td>
          <td class="${o.side === "BUY" ? "up" : "down"}">${o.side}</td><td>${o.order_type}</td><td>${qty(o.filled_qty || o.quantity)}</td>
          <td>${price(o.fill_price ?? o.limit_price)}</td><td>${o.fee ? `${qty(o.fee)} ${escapeHtml(o.fee_asset)}` : "—"}</td>
          <td>${SOURCE_LABEL[o.source] || o.source}</td><td title="${escapeHtml(o.message)}">${o.status}</td>
          <td>${o.status === "OPEN" ? `<button data-ccancel="${o.id}">Batal</button>` : ""}</td></tr>`).join("")
          : `<tr><td colspan="10" style="text-align:left;color:var(--muted)">Belum ada order.</td></tr>`);
      $("#cOrders").querySelectorAll("[data-ccancel]").forEach((b) => b.onclick = async () => {
        try { await api(`/api/crypto/orders/${b.dataset.ccancel}?broker=${st.broker}`, { method: "DELETE" }); loadAccount(); }
        catch (e) { alert(e.message); }
      });
    } catch { /* akun belum diatur */ }
  }

  // ---- bot ------------------------------------------------------------------
  function fillBotForm(cfg) {
    const f = $("#cBotForm");
    for (const [k, v] of Object.entries(cfg)) {
      const el = f.elements[k];
      if (el) el.value = Array.isArray(v) ? v.join(", ") : v;
    }
  }
  function renderBot(s, fill = false) {
    st.botRunning = s.running;
    st.bot = s;
    if (fill) fillBotForm(s.config);
    $("#cBotDot").classList.toggle("on", s.running);
    const btn = $("#cBotToggle");
    btn.textContent = s.running ? "Hentikan" : "Mulai";
    btn.className = "primary " + (s.running ? "sell" : "buy");
    const t = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString("id-ID") : "—";
    const target = s.targets[s.config.broker];
    $("#cBotStatus").textContent = `${s.running ? "● Berjalan" : "○ Berhenti"} · akun ${target?.display_name ?? s.config.broker}` +
      ` · candle ${s.config.candle_interval} · siklus terakhir ${t(s.last_run)}` + (s.next_run ? ` · berikutnya ±${t(s.next_run)}` : "");
    $("#cBotRunOnce").disabled = s.config.broker === "binance";
    const pos = Object.entries(s.bot_positions);
    $("#cBotPositions").innerHTML = `<tr><th>Pasangan</th><th>Jumlah</th><th>Harga rata-rata</th><th>Dibuka</th></tr>` +
      (pos.length ? pos.map(([sym, p]) => `<tr><td>${escapeHtml(sym)}</td><td>${qty(p.quantity)}</td><td>${price(p.avg_price)}</td>
        <td>${p.opened_at ? new Date(p.opened_at * 1000).toLocaleString("id-ID") : "—"}</td></tr>`).join("")
        : `<tr><td colspan="4" style="text-align:left;color:var(--muted)">Bot belum memegang posisi.</td></tr>`);
    $("#cBotLog").innerHTML = `<tr><th>Waktu</th><th>Jenis</th><th>Pasangan</th><th>Keterangan</th></tr>` +
      (s.log.length ? s.log.map((l) => `<tr><td>${new Date(l.time * 1000).toLocaleString("id-ID")}</td>
        <td class="lvl lvl-${l.level}">${l.level}</td><td>${escapeHtml(l.symbol)}</td><td class="wrap">${escapeHtml(l.message)}</td></tr>`).join("")
        : `<tr><td colspan="4" style="text-align:left;color:var(--muted)">Belum ada aktivitas.</td></tr>`);
  }
  const botMsg = (text, ok = true) => { $("#cBotMsg").className = "msg " + (ok ? "ok" : "err"); $("#cBotMsg").textContent = text; };
  async function loadBot(fill) { try { renderBot(await api("/api/crypto/autotrader"), fill); } catch { /* server belum siap */ } }
  async function saveBot(ev) {
    ev.preventDefault();
    const f = $("#cBotForm"), num = (k) => Number(f.elements[k].value);
    const body = {
      broker: f.elements.broker.value, candle_interval: f.elements.candle_interval.value,
      symbols: f.elements.symbols.value.split(",").map(cleanPair).filter(Boolean),
      interval_seconds: num("interval_seconds"), position_pct: num("position_pct"), max_order_usdt: num("max_order_usdt"),
      max_positions: num("max_positions"), min_buy_score: num("min_buy_score"), max_sell_score: num("max_sell_score"),
      stop_loss_pct: num("stop_loss_pct"), take_profit_pct: num("take_profit_pct"), cooldown_minutes: num("cooldown_minutes"),
    };
    try { renderBot(await api("/api/crypto/autotrader/config", { method: "PUT", body: JSON.stringify(body) }), true); botMsg("Tersimpan"); }
    catch (e) { botMsg(e.message, false); }
  }
  async function toggleBot() {
    const s = st.bot;
    if (!s) return;
    try {
      if (s.running) { renderBot(await api("/api/crypto/autotrader/stop", { method: "POST" })); return; }
      const live = s.config.broker === "binance";
      const text = live
        ? `MULAI BOT DI AKUN BINANCE ASLI?\n\nBot akan membeli/menjual dengan uang sungguhan, maks. ${s.config.max_order_usdt} USDT per order, ` +
          `untuk ${s.config.symbols.join(", ")}. Hanya posisi yang dibuka bot yang akan dijual.`
        : `Mulai auto-trading crypto di ${s.targets[s.config.broker].display_name}?`;
      if (!confirm(text)) return;
      renderBot(await api("/api/crypto/autotrader/start", { method: "POST", body: JSON.stringify({ confirm_live: live }) }));
    } catch (e) { botMsg(e.message, false); }
  }

  function setTab(tab) {
    st.tab = tab;
    document.querySelectorAll(".ctab[data-ctab]").forEach((t) => t.classList.toggle("active", t.dataset.ctab === tab));
    $("#cPortfolioPane").classList.toggle("hidden", tab !== "portfolio");
    $("#cOrdersPane").classList.toggle("hidden", tab !== "orders");
    $("#cBotPane").classList.toggle("hidden", tab !== "bot");
    if (tab === "bot") loadBot(true);
  }

  // ---- siklus hidup ---------------------------------------------------------
  let wired = false;
  function wire() {
    if (wired) return;
    wired = true;
    $("#cAddWatch").onsubmit = (e) => {
      e.preventDefault();
      const s = cleanPair($("#cAddWatchInput").value);
      if (s && !st.watch.includes(s)) { st.watch.push(s); saveWatch(); renderWatch(); }
      $("#cAddWatchInput").value = "";
      if (s) loadPair(s);
    };
    $("#cInterval").value = st.interval;
    $("#cInterval").onchange = () => { st.interval = $("#cInterval").value; keep("cInterval", st.interval); loadChart(); };
    $("#cBroker").onchange = onBrokerChange;
    document.querySelectorAll("#cOrderForm .seg button").forEach((b) => b.onclick = () => setSide(b.dataset.side));
    ["#cOrderType", "#cAmount", "#cLimitPrice"].forEach((s) => $(s).addEventListener("input", updateEstimate));
    $("#cAmountUnit").onchange = () => { st.unit = $("#cAmountUnit").value; updateEstimate(); };
    $("#cOrderForm").onsubmit = submitOrder;
    document.querySelectorAll(".ctab[data-ctab]").forEach((t) => t.onclick = () => setTab(t.dataset.ctab));
    // Pengaturan Telegram dipakai bersama saham & crypto; ada di tab Notifikasi tampilan Saham IDX.
    $("#cOpenNotif").onclick = () => {
      setMarket("idx");
      document.querySelector('.tab[data-tab="notif"]')?.click();
      $("#notifPane").scrollIntoView({ behavior: "smooth", block: "start" });
    };
    $("#cResetPaper").onclick = async () => {
      if (!confirm("Reset akun simulasi crypto ke saldo awal?")) return;
      await api("/api/crypto/paper/reset", { method: "POST" });
      loadAccount(); loadChart();
    };
    $("#cBotForm").onsubmit = saveBot;
    $("#cBotToggle").onclick = toggleBot;
    $("#cBotRunOnce").onclick = async () => {
      $("#cBotRunOnce").disabled = true;
      try { renderBot(await api("/api/crypto/autotrader/run-once", { method: "POST" })); loadAccount(); loadChart(); }
      catch (e) { botMsg(e.message, false); }
      finally { $("#cBotRunOnce").disabled = st.bot?.config.broker === "binance"; }
    };
  }

  async function start() {
    wire();
    if (!st.started) {
      st.started = true;
      try { st.config = await api("/api/crypto/config"); } catch { st.config = { brokers: { paper: { display_name: "Simulasi", available: true } } }; }
      renderBrokers();
      renderWatch();
      setSide("BUY");
      loadPair(st.symbol);
      loadBot(true);
    }
    const active = () => document.body.dataset.market === "crypto" && !document.hidden;
    const every = (ms, fn) => timers.push(setInterval(() => { if (active()) fn(); }, ms));
    stop();
    every(3_000, refreshQuote);
    every(10_000, refreshWatch);
    every(15_000, loadAccount);
    every(60_000, loadChart);
    every(10_000, () => { if (st.tab === "bot" || st.botRunning) loadBot(false); });
  }
  function stop() { while (timers.length) clearInterval(timers.pop()); }

  window.cryptoWatchlist = () => st.watch;
  window.cryptoCurrent = () => ({ symbol: st.symbol, price: st.quote?.symbol === st.symbol ? st.quote.price : null,
                                  quote: st.quote?.quote_asset || "" });
  document.querySelectorAll("#marketSeg button").forEach((b) => b.onclick = () => setMarket(b.dataset.market));
  setMarket(pref("market", "idx") === "crypto" ? "crypto" : "idx");
})();
