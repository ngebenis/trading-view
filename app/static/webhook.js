// Tab Webhook TradingView. Memakai helper global dari app.js: $, api, cleanSymbol, escapeHtml, state, loadAccount.
(() => {
  const PATH = "/api/webhooks/tradingview";
  let initialized = false;
  let current = null;

  const msg = (text, ok = true) => { $("#hookMsg").className = "msg " + (ok ? "ok" : "err"); $("#hookMsg").textContent = text; };
  const LABELS = { RECEIVED: "MASUK", ORDER: "ORDER", NOTIFY: "TELEGRAM", SKIP: "LEWATI", ERROR: "GAGAL",
                   AUTH: "DITOLAK", SECURITY: "KEAMANAN" };

  function render(st, fillForm = false) {
    current = st;
    const c = st.config;
    $("#hookDot").classList.toggle("on", c.enabled);
    const btn = $("#hookToggle");
    btn.textContent = c.enabled ? "Nonaktifkan" : "Aktifkan";
    btn.className = "primary " + (c.enabled ? "sell" : "buy");
    const modeText = { log: "catat saja", notify: "kirim ke Telegram", order: "Telegram + order simulasi" }[c.mode];
    $("#hookStatus").textContent = (c.enabled ? "● Aktif" : "○ Nonaktif — alert ditolak") + ` · aksi: ${modeText}` +
      (c.allowed_symbols.length ? ` · simbol: ${c.allowed_symbols.join(", ")}` : " · semua simbol");
    $("#hookUrl").value = location.origin + PATH;
    $("#hookTplStrategy").value = st.template_strategy;
    $("#hookTplIndicator").value = st.template_indicator;
    $("#hookSecret").value = c.secret;
    if (fillForm) {
      const f = $("#hookForm");
      f.elements.mode.value = c.mode;
      f.elements.position_pct.value = c.position_pct;
      f.elements.max_lots.value = c.max_lots;
      f.elements.allowed_symbols.value = c.allowed_symbols.join(", ");
      f.elements.notify_security.checked = c.notify_security;
      $("#hookGuide").open = !c.enabled;
    }
    $("#hookLog").innerHTML = `<tr><th>Waktu</th><th>Jenis</th><th>Kode</th><th>Keterangan</th></tr>` +
      (st.log.length ? st.log.map((l) => `<tr><td>${new Date(l.time * 1000).toLocaleString("id-ID")}</td>
        <td class="kind kind-${l.kind}">${LABELS[l.kind] || l.kind}</td><td>${escapeHtml(l.symbol)}</td>
        <td>${escapeHtml(l.message)}</td></tr>`).join("")
        : `<tr><td colspan="4" style="color:var(--muted)">Belum ada alert masuk.</td></tr>`);
  }

  async function load(fill) {
    try { render(await api("/api/webhooks"), fill); } catch { /* server belum siap */ }
  }

  async function save(ev) {
    ev?.preventDefault();
    const f = $("#hookForm");
    const body = {
      mode: f.elements.mode.value,
      position_pct: Number(f.elements.position_pct.value),
      max_lots: Number(f.elements.max_lots.value),
      allowed_symbols: f.elements.allowed_symbols.value.split(",").map(cleanSymbol).filter(Boolean),
      notify_security: f.elements.notify_security.checked,
    };
    try { render(await api("/api/webhooks/config", { method: "PUT", body: JSON.stringify(body) }), true); msg("Tersimpan"); }
    catch (e) { msg(e.message, false); }
  }

  async function toggle() {
    try {
      render(await api("/api/webhooks/config", { method: "PUT", body: JSON.stringify({ enabled: !current.config.enabled }) }));
      msg(current.config.enabled ? "Webhook aktif" : "Webhook dinonaktifkan");
    } catch (e) { msg(e.message, false); }
  }

  async function regenerate() {
    if (!confirm("Ganti kode rahasia? Alert TradingView yang memakai kode lama akan ditolak sampai pesannya diperbarui.")) return;
    try {
      render(await api("/api/webhooks/regenerate-secret", { method: "POST" }));
      msg("Kode rahasia diganti — perbarui pesan alert & skrip price feed di TradingView");
      window.loadFeed?.(); // skrip Pine memuat kode rahasia
    }
    catch (e) { msg(e.message, false); }
  }

  // Simulasikan alert dari TradingView. Mode "order" diturunkan ke "notify" agar uji tidak membuat order.
  async function sendTest() {
    const c = current.config;
    const sym = state.symbol;
    const body = { secret: c.secret, symbol: sym, action: "buy", price: state.price || undefined,
                   message: `Alert uji ${new Date().toLocaleTimeString("id-ID")}`, mode: c.mode === "order" ? "notify" : c.mode };
    try {
      const res = await fetch(PATH, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const out = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(out.detail || res.statusText);
      msg(`Alert uji ${sym} diterima${c.mode === "order" ? " (tanpa order)" : ""}`);
    } catch (e) { msg(e.message, false); }
    setTimeout(() => load(false), 600);
  }

  async function copy(sel) {
    const el = $(sel);
    try { await navigator.clipboard.writeText(el.value); }
    catch { el.type = "text"; el.select(); document.execCommand("copy"); }
    msg("Disalin");
  }

  function init() {
    window.loadFeed?.();
    if (initialized) return load(true);
    initialized = true;
    $("#hookForm").onsubmit = save;
    $("#hookToggle").onclick = toggle;
    $("#hookRegen").onclick = regenerate;
    $("#hookTest").onclick = sendTest;
    $("#hookShow").onclick = () => {
      const s = $("#hookSecret");
      s.type = s.type === "password" ? "text" : "password";
      $("#hookShow").textContent = s.type === "password" ? "Lihat" : "Sembunyikan";
    };
    document.querySelectorAll("#hookPane [data-copy]").forEach((b) => { b.onclick = () => copy(b.dataset.copy); });
    load(true);
    // Saat tab terbuka, log & akun diperbarui berkala (alert bisa masuk kapan saja).
    setInterval(() => {
      if (!$("#hookPane").classList.contains("hidden")) { load(false); loadAccount(); }
    }, 10_000);
  }

  window.loadWebhook = init;
  api("/api/webhooks").then((st) => $("#hookDot").classList.toggle("on", st.config.enabled)).catch(() => {});
})();
