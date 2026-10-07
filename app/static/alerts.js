// Alert harga → Telegram: kotak pasang alert di panel order (saham & crypto) dan tabel di tab Notifikasi.
// Memakai helper global dari app.js: $, api, fmtPrice, escapeHtml, state.
(() => {
  let status = null;

  const isCrypto = (a) => a.market === "crypto";
  const num = (x, sym, market) => market === "crypto"
    ? Number(x).toLocaleString("id-ID", { maximumFractionDigits: Math.abs(x) >= 1000 ? 2 : Math.abs(x) >= 1 ? 4 : 8 })
    : fmtPrice(x, sym);
  const when = (ts) => ts ? new Date(ts * 1000).toLocaleString("id-ID", { timeZone: "Asia/Jakarta", day: "2-digit",
    month: "2-digit", hour: "2-digit", minute: "2-digit" }) + " WIB" : "—";
  const arrow = (a) => a.direction === "above" ? "▲ naik ≥" : "▼ turun ≤";

  // Dua kotak: panel saham (IDX) dan panel crypto. Masing-masing tahu simbol & harga yang sedang dibuka.
  const boxes = [
    { form: "#alertForm", market: "idx", current: () => ({ symbol: state.symbol, price: state.quote?.symbol === state.symbol ? state.price : null }) },
    { form: "#cAlertForm", market: "crypto", current: () => window.cryptoCurrent?.() || {} },
  ];

  function renderBox(box) {
    const f = $(box.form);
    if (!f) return;
    const { symbol, price } = box.current();
    const target = Number(f.elements.target.value);
    const hint = f.querySelector(".alert-hint");
    if (!status?.telegram_configured) hint.textContent = "⚠ Telegram belum diatur — isi bot token & chat ID di tab Notifikasi.";
    else if (target && price) {
      hint.textContent = target === price ? "Target sama dengan harga sekarang"
        : `Kabari saat ${symbol} ${target > price ? "naik ≥" : "turun ≤"} ${num(target, symbol, box.market)}` +
          ` (${target > price ? "+" : ""}${((target / price - 1) * 100).toFixed(2).replace(".", ",")}% dari harga sekarang)`;
    } else hint.textContent = symbol ? `Alert untuk ${symbol}, dicek tiap ±${status?.interval_seconds ?? 30} detik` : "";
    const mine = (status?.alerts || []).filter((a) => a.symbol === symbol);
    f.querySelector(".alert-list").innerHTML = mine.map((a) => `<li class="${a.status === "active" ? "" : "done"}"
        title="${escapeHtml(a.note || "")}"><span>${arrow(a)} <b>${num(a.target, a.symbol, a.market)}</b>` +
      `${a.repeat ? " 🔁" : ""}${a.status === "active" ? "" : ` · ✓ ${when(a.triggered_at)}`}` +
      `${a.note ? ` · ${escapeHtml(a.note)}` : ""}</span><button type="button" class="rm" data-del="${a.id}" title="Hapus">✕</button></li>`).join("");
    f.querySelectorAll("[data-del]").forEach((b) => b.onclick = () => act(`/api/alerts/${b.dataset.del}`, "DELETE", f));
  }

  function renderTable() {
    const t = $("#alertTable");
    if (!t || !status) return;
    const active = status.alerts.filter((a) => a.status === "active").length;
    $("#alertCount").textContent = status.alerts.length ? `(${active} aktif dari ${status.alerts.length})` : "";
    $("#alertInterval").textContent = status.interval_seconds;
    t.innerHTML = `<tr><th>Kode</th><th>Target</th><th>Dipasang</th><th>Status</th><th>Terpicu</th><th>Catatan</th><th></th></tr>` +
      (status.alerts.length ? status.alerts.map((a) => `<tr><td>${escapeHtml(a.symbol)}${isCrypto(a) ? " <span class='muted'>crypto</span>" : ""}</td>
        <td>${arrow(a)} ${num(a.target, a.symbol, a.market)}${a.repeat ? " 🔁" : ""}</td>
        <td>${num(a.created_price, a.symbol, a.market)} · ${when(a.created_at)}</td>
        <td>${a.status === "active" ? (a.armed ? "● aktif" : "menunggu harga kembali") : "✓ selesai"}</td>
        <td>${a.count ? `${num(a.triggered_price, a.symbol, a.market)} · ${when(a.triggered_at)}${a.count > 1 ? ` (${a.count}×)` : ""}` : "—"}</td>
        <td>${escapeHtml(a.note || "")}</td>
        <td>${a.status === "active" ? "" : `<button type="button" data-rearm="${a.id}">Aktifkan lagi</button> `}
          <button type="button" data-del="${a.id}">Hapus</button></td></tr>`).join("")
        : `<tr><td colspan="7" style="text-align:left;color:var(--muted)">Belum ada alert harga.</td></tr>`);
    t.querySelectorAll("[data-del]").forEach((b) => b.onclick = () => act(`/api/alerts/${b.dataset.del}`, "DELETE"));
    t.querySelectorAll("[data-rearm]").forEach((b) => b.onclick = () => act(`/api/alerts/${b.dataset.rearm}/rearm`, "POST"));
  }

  const renderAll = () => { boxes.forEach(renderBox); renderTable(); };

  function msg(form, text, ok = true) {
    const el = form?.querySelector(".msg");
    if (!el) { if (!ok) alert(text); return; }
    el.className = "msg " + (ok ? "ok" : "err");
    el.textContent = text;
  }

  async function act(path, method, form) {
    try { status = await api(path, { method }); renderAll(); }
    catch (e) { msg(form, e.message, false); }
  }

  async function load() {
    try { status = await api("/api/alerts"); renderAll(); } catch { /* server belum siap */ }
  }

  for (const box of boxes) {
    const f = $(box.form);
    if (!f) continue;
    f.elements.target.addEventListener("input", () => renderBox(box));
    f.onsubmit = async (ev) => {
      ev.preventDefault();
      const { symbol } = box.current();
      const target = Number(f.elements.target.value);
      if (!symbol || !target) { msg(f, "Isi target harga", false); return; }
      try {
        const res = await api("/api/alerts", { method: "POST", body: JSON.stringify({
          symbol, target, note: f.elements.note.value, repeat: f.elements.repeat.checked }) });
        status = res;
        const a = res.alerts.find((x) => x.id === res.created);
        msg(f, `Alert dipasang: ${a.symbol} ${arrow(a)} ${num(a.target, a.symbol, a.market)}` +
          (res.notes.length ? ` — ${res.notes.join(" · ")}` : ""), !res.notes.length);
        f.elements.target.value = ""; f.elements.note.value = "";
        renderAll();
      } catch (e) { msg(f, e.message, false); }
    };
  }

  load();
  setInterval(() => { if (!document.hidden) load(); }, 20_000);  // status terpicu/aktif dari server
  setInterval(() => boxes.forEach(renderBox), 2_000);            // ikut saham/pasangan yang sedang dibuka
  window.reloadAlerts = load;
})();
