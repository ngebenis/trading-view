// Price feed TradingView (bagian tab Webhook). Memakai helper global dari app.js: $, api, cleanSymbol, escapeHtml, state, fmtPrice.
(() => {
  let current = null, initialized = false;

  const msg = (text, ok = true) => { $("#feedMsg").className = "msg " + (ok ? "ok" : "err"); $("#feedMsg").textContent = text; };
  const wib = (ts) => new Date(ts * 1000).toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Jakarta" });
  const ago = (s) => s == null ? "—" : s < 90 ? `${s} dtk lalu` : s < 5400 ? `${Math.round(s / 60)} mnt lalu` : `${Math.round(s / 3600)} jam lalu`;

  function stateCell(r) {
    if (r.received_at == null) return `<span class="feed-state none">belum ada data</span>`;
    return r.active ? `<span class="feed-state ok">● dipakai</span>`
      : `<span class="feed-state stale" title="Tidak ada bar baru; harga kembali dari sumber data biasa">basi</span>`;
  }

  function render(st, fillForm = false) {
    current = st;
    const c = st.config, s = st.stats;
    const btn = $("#feedToggle");
    btn.textContent = c.enabled ? "Nonaktifkan" : "Aktifkan";
    btn.className = "primary " + (c.enabled ? "sell" : "buy");
    const active = st.symbols.filter((r) => r.active).length;
    $("#feedStatus").textContent = !c.enabled ? "○ Nonaktif — harga dari sumber data biasa"
      : `● Aktif · ${active}/${st.symbols.length} saham memakai harga TradingView` +
        (s.last_message_at ? ` · pesan terakhir ${ago(Math.round(Date.now() / 1000 - s.last_message_at))}` : " · belum ada pesan masuk");
    $("#feedMax").textContent = st.max_symbols;
    if (fillForm) {
      const f = $("#feedForm");
      f.elements.symbols.value = c.symbols.join(", ");
      f.elements.stale_seconds.value = c.stale_seconds;
      $("#feedGuide").open = !c.enabled;
    }
    $("#feedTable").innerHTML = `<tr><th>Kode</th><th>Simbol TradingView</th><th>Harga</th><th>Bar terakhir</th>
      <th>Diterima</th><th>Bar hari ini</th><th>Status</th></tr>` +
      st.symbols.map((r) => `<tr><td>${escapeHtml(r.symbol)}</td><td>${escapeHtml(r.tradingview_symbol)}</td>
        <td>${r.price != null ? fmtPrice(r.price, r.symbol) : "—"}</td>
        <td>${r.bar_time != null ? `${wib(r.bar_time)} WIB (${r.bar_seconds / 60} mnt)` : "—"}</td>
        <td>${ago(r.age_seconds)}</td><td>${r.bars_today}</td><td>${stateCell(r)}</td></tr>`).join("") +
      (s.last_error ? `<tr><td colspan="7" class="limit-warn">Pesan terakhir ditolak: ${escapeHtml(s.last_error.message)}</td></tr>` : "");
  }

  async function loadPine() {
    try {
      const res = await fetch("/api/feed/pine");
      $("#feedPine").value = await res.text();
    } catch { /* server belum siap */ }
  }

  async function load(fill) {
    try { render(await api("/api/feed"), fill); } catch { /* server belum siap */ }
  }

  async function put(body, okText) {
    try {
      render(await api("/api/feed/config", { method: "PUT", body: JSON.stringify(body) }), "symbols" in body);
      msg(okText);
      loadPine();
    } catch (e) { msg(e.message, false); }
  }

  function save(ev) {
    ev.preventDefault();
    const f = $("#feedForm");
    put({ symbols: f.elements.symbols.value.split(",").map(cleanSymbol).filter(Boolean),
          stale_seconds: Number(f.elements.stale_seconds.value) },
        "Tersimpan — salin ulang skrip ke TradingView bila daftar saham berubah");
  }

  async function copy() {
    const el = $("#feedPine");
    try { await navigator.clipboard.writeText(el.value); }
    catch { el.select(); document.execCommand("copy"); }
    msg("Skrip disalin");
  }

  function init() {
    if (initialized) { load(true); loadPine(); return; }
    initialized = true;
    $("#feedForm").onsubmit = save;
    $("#feedToggle").onclick = () => put({ enabled: !current.config.enabled },
      current.config.enabled ? "Price feed dinonaktifkan" : "Price feed aktif");
    $("#feedCopy").onclick = copy;
    $("#feedUseWatchlist").onclick = () => {
      $("#feedForm").elements.symbols.value = [...new Set([...state.watchlist, "IHSG"])].join(", ");
    };
    load(true);
    loadPine();
    setInterval(() => { if (!$("#hookPane").classList.contains("hidden")) load(false); }, 10_000);
  }

  window.loadFeed = init;
})();
