// Mode live: menerima harga terbaru dari server lewat Server-Sent Events (/api/stream).
// Memakai helper global dari app.js: $, state, renderQuote, renderWatchQuote, renderIhsg.
(() => {
  let es = null, key = "", connected = false, resubTimer = null, lastQuote = null, lastAt = 0;

  const symbols = () => [...new Set([state.symbol, ...state.watchlist, "IHSG"].filter(Boolean))];
  const hhmmss = (ms) => new Date(ms).toLocaleTimeString("id-ID", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Jakarta" }) + " WIB";

  // Teks status: jujur soal seberapa tertinggal data dari bursa.
  function describeAge(q) {
    if (!q || q.data_age_seconds == null) return "";
    const age = q.data_age_seconds + (lastAt ? (Date.now() - lastAt) / 1000 : 0);
    if (!q.market_open) {
      return `bursa tutup · transaksi terakhir ${hhmmss(q.market_time * 1000)}`;
    }
    const via = q.source === "tradingview" ? " (TradingView)" : "";
    if (age < 90) return "data hampir real-time" + via;
    return `data tertunda ±${Math.round(age / 60)} mnt dari bursa${via}`;
  }

  function render(mode) {
    const el = $("#liveStatus");
    if (!el) return;
    const age = describeAge(lastQuote);
    const delayed = lastQuote && lastQuote.market_open && lastQuote.data_age_seconds >= 90;
    const map = {
      live: ["on" + (delayed ? " warn" : ""), `LIVE${lastAt ? ` · ${hhmmss(lastAt)}` : ""}${age ? ` · ${age}` : ""}`],
      connecting: ["", "menghubungkan…"],
      reconnecting: ["off", "koneksi live terputus, mencoba lagi…"],
      paused: ["", "live dijeda (tab tidak aktif)"],
    };
    const [cls, text] = map[mode] || map.connecting;
    el.className = "live-status " + cls;
    el.innerHTML = `<span class="dot-live"></span>${text}`;
    el.title = lastQuote ? `Sumber data: ${lastQuote.source}. Harga saham yang dibuka diperbarui tiap ` +
      `±${state.config.live_focus_seconds ?? 10} dtk, watchlist & IHSG tiap ±${state.config.live_watch_seconds ?? 30} dtk.` : "";
  }

  function onQuote(q) {
    if (q.symbol === state.symbol) {
      renderQuote(q, true);
      window.liveUpdateChart?.(q);
      lastQuote = q;
      lastAt = Date.now();
    }
    renderWatchQuote(q.symbol, q);
    if (q.symbol === "IHSG") renderIhsg(q);
    render("live");
  }

  function close() {
    if (es) { es.close(); es = null; }
    connected = false;
  }

  function connect() {
    if (document.hidden || !state.symbol) return;
    const syms = symbols();
    const k = `${syms.join(",")}|${state.symbol}`;
    if (es && k === key) return;
    close();
    key = k;
    if (lastQuote && lastQuote.symbol !== state.symbol) { lastQuote = null; lastAt = 0; }
    es = new EventSource(`/api/stream?symbols=${encodeURIComponent(syms.join(","))}&focus=${encodeURIComponent(state.symbol)}`);
    render("connecting");
    es.addEventListener("hello", () => { connected = true; render("live"); });
    es.addEventListener("quote", (e) => { connected = true; onQuote(JSON.parse(e.data)); });
    es.onerror = () => { connected = false; render("reconnecting"); }; // EventSource menyambung ulang sendiri
  }

  // Dipanggil saat saham atau watchlist berubah.
  window.liveResubscribe = () => { clearTimeout(resubTimer); resubTimer = setTimeout(connect, 300); };
  window.liveConnected = () => connected;
  // Dipanggil app.js setelah memuat quote awal, agar status langsung terisi.
  window.liveStatus = (q) => { lastQuote = q; lastAt = Date.now(); render(connected ? "live" : "connecting"); };

  // Hemat permintaan: jeda saat tab tidak terlihat.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { close(); key = ""; render("paused"); } else connect();
  });
  // Perbarui teks "tertunda ±x mnt" walau harga tidak berubah.
  setInterval(() => { if (connected) render("live"); }, 15_000);

  window.liveResubscribe();
})();
