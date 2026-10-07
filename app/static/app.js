const $ = (s) => document.querySelector(s);
const fmt = (n, d = 0) => n == null ? "—" : Number(n).toLocaleString("id-ID", { maximumFractionDigits: d, minimumFractionDigits: d });
const rp = (n) => n == null ? "—" : "Rp" + fmt(n);
const cls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "");

const state = {
  symbol: localStorage.getItem("symbol") || "BBCA",
  watchlist: JSON.parse(localStorage.getItem("watchlist") || '["BBCA","BBRI","TLKM","ASII","GOTO","BMRI"]'),
  broker: "paper",
  brokers: [],
  side: "BUY",
  price: null,
  config: {},
};

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

// Indeks (tidak bisa diperdagangkan): kode aplikasi -> simbol TradingView. Sama dengan app/idx_rules.py.
const INDICES = { IHSG: "COMPOSITE", LQ45: "LQ45" };
const INDEX_ALIASES = { COMPOSITE: "IHSG", JKSE: "IHSG", JCI: "IHSG", JKLQ45: "LQ45" };
const isIndex = (s) => s in INDICES;
const tvSymbol = (s) => `IDX:${INDICES[s] ?? s}`;
const fmtPrice = (p, sym) => fmt(p, isIndex(sym) ? 2 : 0);

function cleanSymbol(s) {
  const c = s.trim().toUpperCase().replace(/^IDX:/, "").replace(/\.JK$/, "").replace(/[^A-Z0-9]/g, "");
  return INDEX_ALIASES[c] ?? c;
}

function tickSize(p) { return p < 200 ? 1 : p < 500 ? 2 : p < 2000 ? 5 : p < 5000 ? 10 : 25; }

// ---------- chart ----------
function renderChart(symbol) {
  $("#tvChart").innerHTML = "";
  if (!window.TradingView) {
    $("#tvChart").innerHTML = '<p style="padding:16px">Widget TradingView gagal dimuat (cek koneksi internet).</p>';
    return;
  }
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  new TradingView.widget({
    container_id: "tvChart", autosize: true, symbol: tvSymbol(symbol), interval: "D",
    timezone: "Asia/Jakarta", theme: dark ? "dark" : "light", style: "1", locale: "id",
    allow_symbol_change: false, studies: ["RSI@tv-basicstudies", "MACD@tv-basicstudies", "MAExp@tv-basicstudies"],
  });
}

// ---------- quote & analysis ----------
async function loadSymbol(symbol) {
  symbol = cleanSymbol(symbol);
  if (!symbol) return;
  state.symbol = symbol;
  localStorage.setItem("symbol", symbol);
  $("#qSymbol").textContent = symbol;
  $("#lnkStockbit").href = `https://stockbit.com/symbol/${symbol}`;
  $("#lnkTradingView").href = `https://www.tradingview.com/symbols/${tvSymbol(symbol).replace(":", "-")}/`;
  refreshOrderTicket();
  window.liveResubscribe?.();
  if (!$("#fundPane").classList.contains("hidden")) window.loadFundamentals?.();
  renderWatchlist();
  (window.renderPriceChart || renderChart)(symbol);
  $("#signal").textContent = "…"; $("#signal").className = "signal"; $("#reasons").innerHTML = "";
  state.quote = null;
  $("#qLimits").innerHTML = "";
  try {
    const q = await api(`/api/quote/${symbol}`);
    if (symbol !== state.symbol) return; // pengguna sudah pindah saham
    renderQuote(q);
    window.liveStatus?.(q);
  } catch (e) {
    state.price = null;
    $("#qPrice").textContent = "—"; $("#qChange").textContent = e.message; $("#qChange").className = "q-change down";
  }
  try {
    const a = await api(`/api/analysis/${symbol}`);
    $("#signal").textContent = { BUY: "BELI", SELL: "JUAL", HOLD: "TAHAN" }[a.action] + ` (skor ${a.score})`;
    $("#signal").className = "signal " + a.action;
    $("#reasons").innerHTML = a.reasons.map((r) => `<li>${r}</li>`).join("");
  } catch (e) {
    $("#signal").textContent = "—"; $("#reasons").innerHTML = `<li>${e.message}</li>`;
  }
}

// ---------- watchlist ----------
function saveWatchlist() {
  localStorage.setItem("watchlist", JSON.stringify(state.watchlist));
  window.liveResubscribe?.();
}

function renderWatchlist() {
  const ul = $("#watchlist");
  ul.innerHTML = "";
  for (const s of state.watchlist) {
    const li = document.createElement("li");
    li.className = s === state.symbol ? "active" : "";
    li.innerHTML = `<span>${s}</span><span><span class="wl-price" data-s="${s}"></span><span class="rm" title="Hapus">✕</span></span>`;
    li.onclick = (ev) => {
      if (ev.target.classList.contains("rm")) {
        state.watchlist = state.watchlist.filter((x) => x !== s); saveWatchlist(); renderWatchlist();
      } else loadSymbol(s);
    };
    ul.appendChild(li);
  }
  refreshWatchPrices();
}

async function refreshWatchPrices() {
  await Promise.all(state.watchlist.map(async (s) => {
    const el = document.querySelector(`.wl-price[data-s="${s}"]`);
    if (!el) return;
    try {
      renderWatchQuote(s, await api(`/api/quote/${s}`));
    } catch { el.textContent = "n/a"; }
  }));
}

async function refreshIhsgTicker() {
  const el = $("#ihsgTicker");
  try {
    renderIhsg(await api("/api/quote/IHSG"));
  } catch { el.innerHTML = "IHSG <b>n/a</b>"; }
}

// ---------- order ticket ----------
function setSide(side) {
  state.side = side;
  document.querySelectorAll(".seg button").forEach((b) => b.classList.toggle("active", b.dataset.side === side));
  const btn = $("#submitOrder");
  btn.textContent = side === "BUY" ? "Beli" : "Jual";
  btn.className = "primary " + (side === "BUY" ? "buy" : "sell");
  updateEstimate();
}

// Bar harga saham yang sedang dibuka (dipakai saat memuat & setiap pembaruan live).
function renderQuote(q, live = false) {
  const prev = state.quote && state.quote.symbol === q.symbol ? state.quote.price : null;
  state.price = q.price;
  state.quote = q;
  renderLimits(q);
  window.refreshLimitLines?.();
  $("#qPrice").textContent = fmtPrice(q.price, q.symbol);
  $("#qChange").textContent = `${q.change >= 0 ? "+" : ""}${fmtPrice(q.change, q.symbol)} (${fmt(q.change_pct, 2)}%)`;
  $("#qChange").className = "q-change " + cls(q.change);
  if (live && prev != null && prev !== q.price) { // kedipkan harga saat berubah
    const el = $("#qPrice");
    el.classList.remove("flash-up", "flash-down");
    void el.offsetWidth;
    el.classList.add(q.price > prev ? "flash-up" : "flash-down");
  }
  if (!$("#limitPrice").value || $("#limitPrice").dataset.symbol !== q.symbol) {
    $("#limitPrice").value = q.price; $("#limitPrice").dataset.symbol = q.symbol;
  }
  updateEstimate();
}

function renderWatchQuote(s, q) {
  const el = document.querySelector(`.wl-price[data-s="${s}"]`);
  if (!el) return;
  el.innerHTML = `${limitBadge(q.limit_status)}${fmtPrice(q.price, s)} ${q.change_pct >= 0 ? "+" : ""}${fmt(q.change_pct, 1)}%`;
  el.className = "wl-price " + cls(q.change_pct);
}

function renderIhsg(q) {
  $("#ihsgTicker").innerHTML = `IHSG <b>${fmtPrice(q.price, "IHSG")}</b> <span class="${cls(q.change_pct)}">${q.change_pct >= 0 ? "+" : ""}${fmt(q.change_pct, 2)}%</span>`;
}

// Batas Auto Rejection hari ini (dihitung server dari harga penutupan sebelumnya).
function limitBadge(status) {
  return status ? `<span class="limit-badge ${status === "ARA" ? "ara" : "arb"}">${status}</span>` : "";
}

function renderLimits(q) {
  if (q.ara == null) { $("#qLimits").innerHTML = ""; return; }
  $("#qLimits").innerHTML = `${limitBadge(q.limit_status)}<span class="muted" title="Batas Auto Rejection hari ini dari harga acuan ${fmt(q.reference_price)}">
    ARB <b>${fmt(q.arb)}</b> · ARA <b>${fmt(q.ara)}</b></span>`;
}

function limitWarning(price, isLimit) {
  const q = state.quote;
  if (!q || q.ara == null || !price) return "";
  if (isLimit && price > q.ara) return `⚠ Di atas batas ARA (${fmt(q.ara)}) — akan ditolak bursa`;
  if (isLimit && price < q.arb) return `⚠ Di bawah batas ARB (${fmt(q.arb)}) — akan ditolak bursa`;
  if (!isLimit && state.side === "BUY" && q.limit_status === "ARA") return "⚠ Saham sedang ARA: tidak ada penjual, order beli market akan ditolak";
  if (!isLimit && state.side === "SELL" && q.limit_status === "ARB") return "⚠ Saham sedang ARB: tidak ada pembeli, order jual market akan ditolak";
  return "";
}

function updateEstimate() {
  const isLimit = $("#orderType").value === "LIMIT";
  $("#limitWrap").classList.toggle("hidden", !isLimit);
  const price = isLimit ? Number($("#limitPrice").value) : state.price;
  const lots = Number($("#lots").value) || 0;
  if (isLimit && price) {
    const t = tickSize(price);
    $("#tickHint").textContent = price % t === 0 ? `Fraksi harga: ${t}` : `⚠ Harus kelipatan ${t} (mis. ${Math.round(price / t) * t})`;
  }
  if (!price || !lots || isIndex(state.symbol)) { $("#estimate").textContent = ""; return; }
  const warn = limitWarning(price, isLimit);
  const value = price * lots * (state.config.lot_size || 100);
  const feePct = state.side === "BUY" ? state.config.buy_fee_pct : state.config.sell_fee_pct;
  const fee = value * (feePct || 0) / 100;
  $("#estimate").innerHTML = `Nilai: <b>${rp(value)}</b><br>Fee ±${fmt(feePct, 2)}%: ${rp(fee)}<br>` +
    (state.side === "BUY" ? `Total bayar: <b>${rp(value + fee)}</b>` : `Diterima: <b>${rp(value - fee)}</b>`) +
    (warn ? `<div class="limit-warn">${warn}</div>` : "");
}

async function submitOrder(ev) {
  ev.preventDefault();
  const broker = state.brokers.find((b) => b.name === state.broker);
  const msg = $("#orderMsg");
  msg.className = "msg"; msg.textContent = "";
  const body = {
    broker: state.broker, symbol: state.symbol, side: state.side,
    lots: Number($("#lots").value), order_type: $("#orderType").value,
    limit_price: $("#orderType").value === "LIMIT" ? Number($("#limitPrice").value) : null,
  };
  if (broker?.is_live) {
    if (!confirm(`ORDER UANG SUNGGUHAN di ${broker.display_name}:\n${body.side} ${body.symbol} ${body.lots} lot. Lanjutkan?`)) return;
    body.confirm_live = true;
  }
  try {
    const o = await api("/api/orders", { method: "POST", body: JSON.stringify(body) });
    msg.className = "msg ok";
    msg.textContent = o.status === "FILLED"
      ? `Tereksekusi: ${o.side} ${o.symbol} ${o.lots} lot @ ${fmt(o.fill_price)}`
      : `Order ${o.order_type} ${o.side} ${o.symbol} terpasang (${o.status})`;
    loadAccount();
    window.refreshPriceChart?.();
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
}

// ---------- brokers ----------
async function loadBrokers() {
  state.brokers = await api("/api/brokers");
  $("#brokerSelect").innerHTML = state.brokers
    .map((b) => `<option value="${b.name}">${b.display_name}${b.available ? "" : " (manual)"}</option>`).join("");
  $("#brokerSelect").value = state.broker;
}

// Form order nonaktif untuk indeks dan broker yang belum tersedia.
function refreshOrderTicket() {
  const b = state.brokers.find((x) => x.name === state.broker);
  const note = $("#brokerNote");
  if (isIndex(state.symbol)) {
    note.innerHTML = `<b>${state.symbol}</b> adalah indeks pasar — bisa dipantau & dianalisis, tapi tidak bisa dibeli/dijual.`;
    note.classList.remove("hidden");
  } else if (b && !b.available) {
    const url = (b.app_url || "").replace("{symbol}", state.symbol);
    note.innerHTML = `${b.note}<br><a href="${url}" target="_blank" rel="noopener">Buka ${b.display_name} ↗</a>`;
    note.classList.remove("hidden");
  } else note.classList.add("hidden");
  const disabled = isIndex(state.symbol) || Boolean(b && !b.available);
  $("#orderForm").querySelectorAll("button, input, select").forEach((el) => { el.disabled = disabled; });
  updateEstimate();
}

function onBrokerChange() {
  state.broker = $("#brokerSelect").value;
  const b = state.brokers.find((x) => x.name === state.broker);
  refreshOrderTicket();
  $("#resetPaper").classList.toggle("hidden", state.broker !== "paper");
  loadAccount();
}

// ---------- account ----------
async function loadAccount() {
  const sum = $("#summary"), pt = $("#portfolioTable"), ot = $("#ordersTable");
  try {
    const a = await api(`/api/account?broker=${state.broker}`);
    sum.innerHTML = [
      ["Ekuitas", rp(a.equity)], ["Kas", rp(a.cash)], ["Buying power", rp(a.buying_power)],
      ["Nilai saham", rp(a.market_value)],
      ["Total P/L", `<span class="${cls(a.total_pl)}">${rp(a.total_pl)} (${fmt(a.total_pl_pct, 2)}%)</span>`],
    ].map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
    pt.innerHTML = `<tr><th>Kode</th><th>Lot</th><th>Avg</th><th>Last</th><th>Nilai</th><th>P/L</th></tr>` +
      (a.positions.length ? a.positions.map((p) => `<tr data-s="${p.symbol}"><td><a href="#">${p.symbol}</a></td><td>${fmt(p.lots)}</td>
        <td>${fmt(p.avg_price, 2)}</td><td>${fmt(p.last_price)}</td><td>${rp(p.market_value)}</td>
        <td class="${cls(p.unrealized_pl)}">${rp(p.unrealized_pl)} (${fmt(p.unrealized_pl_pct, 2)}%)</td></tr>`).join("")
        : `<tr><td colspan="6" style="text-align:left;color:var(--muted)">Belum ada posisi.</td></tr>`);
    pt.querySelectorAll("tr[data-s] a").forEach((a) => a.onclick = (e) => { e.preventDefault(); loadSymbol(a.closest("tr").dataset.s); });
    const orders = await api(`/api/orders?broker=${state.broker}`);
    ot.innerHTML = `<tr><th>Waktu</th><th>Kode</th><th>Aksi</th><th>Tipe</th><th>Lot</th><th>Harga</th><th>Fee</th><th>Sumber</th><th>Status</th><th></th></tr>` +
      orders.map((o) => `<tr><td>${new Date(o.created_at * 1000).toLocaleString("id-ID")}</td><td>${o.symbol}</td>
        <td class="${o.side === "BUY" ? "up" : "down"}">${o.side}</td><td>${o.order_type}</td><td>${o.lots}</td>
        <td>${fmt(o.fill_price ?? o.limit_price)}</td><td>${fmt(o.fee)}</td><td>${{ auto: "🤖 auto", webhook: "📡 webhook" }[o.source] || "manual"}</td><td title="${o.message}">${o.status}</td>
        <td>${o.status === "OPEN" ? `<button data-cancel="${o.id}">Batal</button>` : ""}</td></tr>`).join("");
    ot.querySelectorAll("[data-cancel]").forEach((b) => b.onclick = async () => {
      await api(`/api/orders/${b.dataset.cancel}?broker=${state.broker}`, { method: "DELETE" }); loadAccount();
    });
  } catch (e) {
    sum.innerHTML = `<div class="note">${e.message}</div>`; pt.innerHTML = ""; ot.innerHTML = "";
  }
}

// ---------- auto-trading ----------
const escapeHtml = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function fillAutoForm(cfg) {
  const f = $("#autoForm");
  for (const [k, v] of Object.entries(cfg)) {
    const el = f.elements[k];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = v;
    else el.value = Array.isArray(v) ? v.join(", ") : v;
  }
}

function renderAuto(st, fillForm = false) {
  const prevRunning = state.autoRunning;
  state.autoRunning = st.running;
  if (fillForm) fillAutoForm(st.config);
  $("#autoDot").classList.toggle("on", st.running);
  $("#autoToggle").dataset.running = st.running ? "1" : "0";
  $("#autoToggle").textContent = st.running ? "Hentikan" : "Mulai";
  $("#autoToggle").className = "primary " + (st.running ? "sell" : "buy");
  const t = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString("id-ID") : "—";
  $("#autoStatus").textContent = (st.running ? "● Berjalan" : "○ Berhenti") +
    ` · siklus terakhir ${t(st.last_run)}` + (st.next_run ? ` · berikutnya ±${t(st.next_run)}` : "") +
    ` · ${st.config.symbols.length} simbol`;
  $("#autoLog").innerHTML = `<tr><th>Waktu</th><th>Jenis</th><th>Kode</th><th>Keterangan</th></tr>` +
    (st.log.length ? st.log.map((l) => `<tr><td>${new Date(l.time * 1000).toLocaleString("id-ID")}</td>
      <td class="lvl lvl-${l.level}">${l.level}</td><td>${escapeHtml(l.symbol)}</td><td>${escapeHtml(l.message)}</td></tr>`).join("")
      : `<tr><td colspan="4" style="text-align:left;color:var(--muted)">Belum ada aktivitas.</td></tr>`);
  if (prevRunning && st.log.some((l) => l.level === "TRADE")) loadAccount();
}

async function loadAuto(fillForm) {
  try { renderAuto(await api("/api/autotrader"), fillForm); } catch { /* server belum siap */ }
}

async function saveAutoConfig(ev) {
  ev.preventDefault();
  const f = $("#autoForm"), msg = $("#autoMsg");
  const num = (k) => Number(f.elements[k].value);
  const body = {
    symbols: f.elements.symbols.value.split(",").map(cleanSymbol).filter(Boolean),
    interval_seconds: num("interval_seconds"), position_pct: num("position_pct"), max_positions: num("max_positions"),
    min_buy_score: num("min_buy_score"), max_sell_score: num("max_sell_score"), stop_loss_pct: num("stop_loss_pct"),
    take_profit_pct: num("take_profit_pct"), cooldown_minutes: num("cooldown_minutes"),
    market_hours_only: f.elements.market_hours_only.checked,
  };
  try {
    renderAuto(await api("/api/autotrader/config", { method: "PUT", body: JSON.stringify(body) }), true);
    msg.className = "msg ok"; msg.textContent = "Tersimpan";
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
}

// ---------- init ----------
async function init() {
  state.config = await api("/api/config");
  $("#dataSource").textContent = `Data: ${state.config.market_data_provider}` + (state.config.live_trading_enabled ? " · LIVE ON" : "");
  await loadBrokers();
  $("#search").onsubmit = (e) => { e.preventDefault(); loadSymbol($("#symbolInput").value); $("#symbolInput").value = ""; };
  $("#addWatch").onsubmit = (e) => {
    e.preventDefault();
    const s = cleanSymbol($("#addWatchInput").value);
    if (s && !state.watchlist.includes(s)) { state.watchlist.push(s); saveWatchlist(); renderWatchlist(); }
    $("#addWatchInput").value = "";
  };
  document.querySelectorAll(".seg button").forEach((b) => b.onclick = () => setSide(b.dataset.side));
  ["#orderType", "#lots", "#limitPrice"].forEach((s) => $(s).addEventListener("input", updateEstimate));
  $("#orderForm").onsubmit = submitOrder;
  $("#brokerSelect").onchange = onBrokerChange;
  $("#resetPaper").onclick = async () => { if (confirm("Reset akun simulasi ke saldo awal?")) { await api("/api/paper/reset", { method: "POST" }); loadAccount(); } };
  document.querySelectorAll(".tab").forEach((t) => t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
    const tab = t.dataset.tab;
    $("#portfolioTable").classList.toggle("hidden", tab !== "portfolio");
    $("#ordersTable").classList.toggle("hidden", tab !== "orders");
    $("#autoPane").classList.toggle("hidden", tab !== "auto");
    $("#backtestPane").classList.toggle("hidden", tab !== "backtest");
    $("#notifPane").classList.toggle("hidden", tab !== "notif");
    $("#fundPane").classList.toggle("hidden", tab !== "fund");
    $("#hookPane").classList.toggle("hidden", tab !== "hook");
    $("#summary").classList.toggle("hidden", ["auto", "backtest", "notif", "fund", "hook"].includes(tab));
    if (tab === "hook") window.loadWebhook?.();
    if (tab === "notif") window.initNotifications?.();
    if (tab === "fund") window.loadFundamentals?.();
    if (tab === "backtest") window.initBacktest?.();
    if (t.dataset.tab === "auto") loadAuto(true);
  });
  $("#autoForm").onsubmit = saveAutoConfig;
  $("#autoToggle").onclick = async () => {
    const running = $("#autoToggle").dataset.running === "1";
    if (!running && !confirm("Mulai auto-trading di akun simulasi?")) return;
    renderAuto(await api(`/api/autotrader/${running ? "stop" : "start"}`, { method: "POST" }));
    loadAccount();
  };
  $("#autoRunOnce").onclick = async () => {
    $("#autoRunOnce").disabled = true;
    try { renderAuto(await api("/api/autotrader/run-once", { method: "POST" })); loadAccount(); }
    catch (e) { $("#autoMsg").className = "msg err"; $("#autoMsg").textContent = e.message; }
    finally { $("#autoRunOnce").disabled = false; }
  };
  loadAuto(true);
  onBrokerChange();
  loadSymbol(state.symbol);
  $("#ihsgTicker").onclick = () => loadSymbol("IHSG");
  refreshIhsgTicker();
  // Harga diperbarui lewat mode live (live.js); polling ini hanya cadangan bila koneksi live putus.
  setInterval(() => {
    if (!window.liveConnected?.()) { refreshWatchPrices(); refreshIhsgTicker(); }
    loadAccount(); window.refreshPriceChart?.();
  }, 60_000);
  setInterval(() => { if (!$("#autoPane").classList.contains("hidden") || state.autoRunning) loadAuto(false); }, 15_000);
}

init();
