// Tab Notifikasi Telegram. Memakai helper global dari app.js: $, api, cleanSymbol, escapeHtml, state.
(() => {
  let initialized = false;
  let lastStatus = null;

  const msg = (text, ok = true) => {
    $("#notifMsg").className = "msg " + (ok ? "ok" : "err");
    $("#notifMsg").textContent = text;
  };
  const time = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString("id-ID") : "—";

  function fillForm(st) {
    const f = $("#notifForm"), c = st.config;
    f.elements.bot_token.value = c.bot_token;
    f.elements.chat_id.value = c.chat_id;
    f.elements.symbols.value = c.symbols.join(", ");
    f.elements.crypto_symbols.value = c.crypto_symbols.join(", ");
    f.elements.crypto_candle_interval.value = c.crypto_candle_interval;
    f.elements.crypto_move_pct.value = c.crypto_move_pct;
    for (const k of ["interval_seconds", "min_buy_score", "max_sell_score"]) f.elements[k].value = c[k];
    f.elements.market_hours_only.checked = c.market_hours_only;
    f.elements.notify_trades.checked = c.notify_trades;
    f.elements.notify_orders.checked = c.notify_orders;
    f.elements.report_enabled.checked = c.report_enabled;
    f.elements.report_time.value = c.report_time;
    f.elements.report_weekdays_only.checked = c.report_weekdays_only;
    f.elements.report_crypto.checked = c.report_crypto;
    f.elements.weekly_enabled.checked = c.weekly_enabled;
    f.elements.weekly_day.value = String(c.weekly_day);
    f.elements.weekly_time.value = c.weekly_time;
    f.elements.monthly_enabled.checked = c.monthly_enabled;
    f.elements.monthly_day.value = String(c.monthly_day);
    f.elements.monthly_time.value = c.monthly_time;
    f.elements.bot_token.disabled = st.token_from_env;
    f.elements.chat_id.disabled = st.chat_id_from_env;
    $("#tokenHint").textContent = st.token_from_env ? "Diatur lewat TELEGRAM_BOT_TOKEN di .env" : "Disimpan lokal di database data/app.db";
    $("#chatHint").textContent = st.chat_id_from_env ? "Diatur lewat TELEGRAM_CHAT_ID di .env" : "";
    $("#notifGuide").open = !st.configured;
  }

  function render(st, fill = false) {
    lastStatus = st;
    if (fill) fillForm(st);
    $("#notifDot").classList.toggle("on", st.running);
    $("#cNotifDot")?.classList.toggle("on", st.running && st.config.crypto_symbols.length > 0);
    const btn = $("#notifToggle");
    btn.textContent = st.running ? "Hentikan" : "Mulai";
    btn.className = "primary " + (st.running ? "sell" : "buy");
    btn.disabled = !st.configured;
    $("#notifTest").disabled = $("#notifRunOnce").disabled = !st.configured;
    const active = Object.entries(st.last_action).filter(([, a]) => a !== "HOLD")
      .map(([s, a]) => `${s} ${a === "BUY" ? "BELI" : "JUAL"}`);
    $("#notifStatus").textContent = !st.configured ? "Belum tersambung ke Telegram — lihat panduan di bawah"
      : (st.running ? "● Memantau" : "○ Berhenti") + ` · ${st.config.symbols.length} saham · ${st.config.crypto_symbols.length} crypto` +
        ` · pindaian terakhir ${time(st.last_run)}` +
        (st.next_run ? ` · berikutnya ±${time(st.next_run)}` : "") + (active.length ? ` · sinyal aktif: ${active.join(", ")}` : "") +
        (st.report_schedule ? ` · laporan harian ${st.report_schedule}` : "") +
        (st.weekly_schedule ? ` · laporan mingguan ${st.weekly_schedule}` : "") +
        (st.monthly_schedule ? ` · laporan bulanan ${st.monthly_schedule}` : "");
    for (const id of ["#notifReportSend", "#notifWeeklySend", "#notifMonthlySend"]) $(id).disabled = !st.configured;

    const labels = { BUY: "BELI", SELL: "JUAL", TRADE: "BOT", TEST: "UJI", INFO: "INFO", WARN: "PERINGATAN", ERROR: "GAGAL",
                     MOVE: "GERAK 24J", TARGET: "TARGET", ORDER: "ORDER", REPORT: "LAPORAN" };
    $("#notifLog").innerHTML = `<tr><th>Waktu</th><th>Jenis</th><th>Kode</th><th>Keterangan</th><th>Telegram</th></tr>` +
      (st.history.length ? st.history.map((h) => `<tr><td>${new Date(h.time * 1000).toLocaleString("id-ID")}</td>
        <td class="kind kind-${h.kind}">${labels[h.kind] || h.kind}</td><td>${escapeHtml(h.symbol)}</td>
        <td>${escapeHtml(h.message)}</td><td>${h.sent ? "✓ terkirim" : ""}</td></tr>`).join("")
        : `<tr><td colspan="5" style="color:var(--muted)">Belum ada notifikasi.</td></tr>`);
  }

  async function load(fill) {
    try { render(await api("/api/notifications"), fill); } catch { /* server belum siap */ }
  }

  async function save(ev) {
    ev?.preventDefault();
    const f = $("#notifForm");
    const body = {
      symbols: f.elements.symbols.value.split(",").map(cleanSymbol).filter(Boolean),
      crypto_symbols: f.elements.crypto_symbols.value.split(",").map((x) => x.trim().toUpperCase().replace(/[^A-Z0-9]/g, "")).filter(Boolean),
      crypto_candle_interval: f.elements.crypto_candle_interval.value,
      crypto_move_pct: Number(f.elements.crypto_move_pct.value),
      interval_seconds: Number(f.elements.interval_seconds.value),
      min_buy_score: Number(f.elements.min_buy_score.value),
      max_sell_score: Number(f.elements.max_sell_score.value),
      market_hours_only: f.elements.market_hours_only.checked,
      notify_trades: f.elements.notify_trades.checked,
      notify_orders: f.elements.notify_orders.checked,
      report_enabled: f.elements.report_enabled.checked,
      report_time: f.elements.report_time.value || "16:15",
      report_weekdays_only: f.elements.report_weekdays_only.checked,
      report_crypto: f.elements.report_crypto.checked,
      weekly_enabled: f.elements.weekly_enabled.checked,
      weekly_day: Number(f.elements.weekly_day.value),
      weekly_time: f.elements.weekly_time.value || "16:30",
      monthly_enabled: f.elements.monthly_enabled.checked,
      monthly_day: Number(f.elements.monthly_day.value),
      monthly_time: f.elements.monthly_time.value || "16:45",
    };
    if (!f.elements.bot_token.disabled) body.bot_token = f.elements.bot_token.value;
    if (!f.elements.chat_id.disabled) body.chat_id = f.elements.chat_id.value;
    try {
      render(await api("/api/notifications/config", { method: "PUT", body: JSON.stringify(body) }), true);
      msg("Tersimpan");
      return true;
    } catch (e) { msg(e.message, false); return false; }
  }

  async function action(path, okText) {
    try {
      const st = await api(`/api/notifications/${path}`, { method: "POST" });
      render(st);
      msg(okText(st));
    } catch (e) { msg(e.message, false); }
  }

  async function findChat() {
    if (!(await save())) return;
    const list = $("#chatList");
    try {
      const chats = await api("/api/notifications/chats");
      list.classList.remove("hidden");
      list.innerHTML = chats.length
        ? "Pilih chat: " + chats.map((c) => `<button type="button" data-id="${escapeHtml(c.id)}">${escapeHtml(c.name || c.id)} <span class="muted">(${escapeHtml(c.type)} · ${escapeHtml(c.id)})</span></button>`).join("")
        : "Belum ada pesan masuk. Kirim pesan apa saja ke bot Anda di Telegram, lalu klik <b>Cari chat ID</b> lagi.";
      list.querySelectorAll("button[data-id]").forEach((b) => b.onclick = async () => {
        $("#notifForm").elements.chat_id.value = b.dataset.id;
        list.classList.add("hidden");
        if (await save()) msg("Chat ID tersimpan — klik “Kirim pesan uji” untuk mencoba");
      });
    } catch (e) { msg(e.message, false); }
  }

  function init() {
    if (initialized) return load(true);
    initialized = true;
    $("#notifForm").onsubmit = save;
    $("#notifFindChat").onclick = findChat;
    $("#notifUseWatchlist").onclick = () => { $("#notifForm").elements.symbols.value = state.watchlist.join(", "); };
    $("#notifUseCryptoWatch").onclick = () => { $("#notifForm").elements.crypto_symbols.value = (window.cryptoWatchlist?.() || []).join(", "); };
    $("#notifTest").onclick = () => action("test", () => "Pesan uji terkirim — cek Telegram Anda");
    let shownPeriod = null;
    const PERIOD = { daily: "harian", weekly: "mingguan", monthly: "bulanan" };
    for (const id of ["#notifReportSend", "#notifWeeklySend", "#notifMonthlySend"]) {
      const period = $(id).dataset.period;
      $(id).onclick = () => action(`report?period=${period}`, () => `Laporan ${PERIOD[period]} terkirim — cek Telegram Anda`);
    }
    for (const id of ["#notifReportPreview", "#notifWeeklyPreview", "#notifMonthlyPreview"]) {
      $(id).onclick = async () => {
        const box = $("#reportPreview"), period = $(id).dataset.period;
        if (!box.classList.contains("hidden") && shownPeriod === period) { box.classList.add("hidden"); return; }
        try {
          // Isi dari server sudah di-escape; hanya tag format Telegram (<b>, <i>, <a>) yang tersisa.
          box.innerHTML = (await api(`/api/notifications/report/preview?period=${period}`)).text;
          box.classList.remove("hidden");
          shownPeriod = period;
        } catch (e) { msg(e.message, false); }
      };
    }
    $("#notifRunOnce").onclick = () => action("run-once", (st) => {
      const sent = st.history.filter((h) => h.time >= st.last_run && h.sent).length;
      return sent ? `${sent} notifikasi sinyal terkirim` : "Pemindaian selesai, tidak ada sinyal baru";
    });
    $("#notifToggle").onclick = () => action(lastStatus?.running ? "stop" : "start",
      (st) => st.running ? "Pemantauan dimulai" : "Pemantauan dihentikan");
    load(true);
    setInterval(() => { if (!$("#notifPane").classList.contains("hidden")) load(false); }, 15_000);
  }

  window.initNotifications = init;
  // Titik status di tab tetap akurat walau tab belum dibuka.
  api("/api/notifications").then((st) => {
    $("#notifDot").classList.toggle("on", st.running);
    $("#cNotifDot")?.classList.toggle("on", st.running && st.config.crypto_symbols.length > 0);
  }).catch(() => {});
})();
