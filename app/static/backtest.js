// Tab Backtest. Memakai helper global dari app.js: $, api, fmt, rp, cls, cleanSymbol, escapeHtml.
(() => {
  const STRATEGY_FIELDS = ["position_pct", "max_positions", "min_buy_score", "max_sell_score",
    "stop_loss_pct", "take_profit_pct", "cooldown_minutes"];
  let initialized = false;
  let lastResult = null;

  const pct = (n, d = 2) => n == null ? "—" : `${n > 0 ? "+" : ""}${fmt(n, d)}%`;
  const shortRp = (n) => {
    const a = Math.abs(n);
    if (a >= 1e12) return `Rp${fmt(n / 1e12, 1)} T`;
    if (a >= 1e9) return `Rp${fmt(n / 1e9, 1)} M`;
    if (a >= 1e6) return `Rp${fmt(n / 1e6, 0)} jt`;
    return rp(n);
  };
  const fmtDate = (d) => new Date(d + "T00:00:00").toLocaleDateString("id-ID", { day: "numeric", month: "short", year: "numeric" });

  function fillStrategy(cfg) {
    const f = $("#btForm");
    for (const k of STRATEGY_FIELDS) if (cfg[k] != null) f.elements[k].value = cfg[k];
  }

  async function copyFromAuto() {
    const st = await api("/api/autotrader");
    $("#btForm").elements.symbols.value = st.config.symbols.join(", ");
    fillStrategy(st.config);
  }

  async function init() {
    if (initialized) return;
    initialized = true;
    $("#btForm").elements.initial_cash.value = 100_000_000;
    try { await copyFromAuto(); } catch { /* biarkan kosong */ }
    $("#btCopyAuto").onclick = copyFromAuto;
    $("#btForm").onsubmit = run;
    $("#btApply").onclick = applyToAuto;
    window.addEventListener("resize", () => lastResult && drawChart(lastResult.equity_curve));
  }

  function readForm() {
    const f = $("#btForm");
    const strategy = {};
    for (const k of STRATEGY_FIELDS) strategy[k] = Number(f.elements[k].value);
    return {
      symbols: f.elements.symbols.value.split(",").map(cleanSymbol).filter(Boolean),
      period: f.elements.period.value,
      initial_cash: Number(f.elements.initial_cash.value),
      execution: f.elements.execution.value,
      strategy,
    };
  }

  async function run(ev) {
    ev.preventDefault();
    const msg = $("#btMsg"), btn = $("#btRun");
    const body = readForm();
    msg.className = "msg"; msg.textContent = `Menjalankan backtest ${body.symbols.length} saham… (bisa beberapa detik)`;
    btn.disabled = true;
    try {
      lastResult = await api("/api/backtest", { method: "POST", body: JSON.stringify(body) });
      render(lastResult);
      msg.textContent = "";
      $("#btApply").classList.remove("hidden");
    } catch (e) {
      msg.className = "msg err"; msg.textContent = e.message;
    } finally { btn.disabled = false; }
  }

  async function applyToAuto() {
    if (!lastResult) return;
    const p = lastResult.params;
    if (!confirm("Terapkan simbol & parameter backtest ini ke pengaturan auto-trading?")) return;
    try {
      await api("/api/autotrader/config", { method: "PUT", body: JSON.stringify({ symbols: p.symbols, ...p.strategy }) });
      $("#btMsg").className = "msg ok"; $("#btMsg").textContent = "Pengaturan auto-trading diperbarui";
      window.loadAuto?.(true);
    } catch (e) { $("#btMsg").className = "msg err"; $("#btMsg").textContent = e.message; }
  }

  // ---------- hasil ----------
  function render(r) {
    const m = r.metrics, p = r.params;
    $("#btResult").classList.remove("hidden");
    $("#btRange").textContent = `${fmtDate(p.start)} – ${fmtDate(p.end)} · ${p.trading_days} hari bursa · ` +
      `${p.symbols.length} saham · eksekusi ${p.execution === "next_open" ? "open hari berikutnya" : "close hari sinyal"} · ` +
      `fee beli ${fmt(p.buy_fee_pct, 2)}% / jual ${fmt(p.sell_fee_pct, 2)}%`;

    const vs = (other) => other == null ? null : m.total_return_pct - other;
    const vsIhsg = vs(m.ihsg_return_pct), vsBench = vs(m.benchmark_return_pct);
    const ddCompare = [m.ihsg_max_drawdown_pct != null && `IHSG ${fmt(m.ihsg_max_drawdown_pct, 2)}%`,
      m.benchmark_max_drawdown_pct != null && `Beli & tahan ${fmt(m.benchmark_max_drawdown_pct, 2)}%`].filter(Boolean).join(" · ");
    const tiles = [
      ["Total return", `<span class="${cls(m.total_return_pct)}">${pct(m.total_return_pct)}</span>`, `Akhir ${rp(m.final_equity)}`],
      ["vs IHSG", `<span class="${cls(vsIhsg)}">${pct(vsIhsg)}</span>`, m.ihsg_return_pct == null ? "Data IHSG tidak tersedia" : `IHSG ${pct(m.ihsg_return_pct)}`],
      ["vs Beli & tahan", `<span class="${cls(vsBench)}">${pct(vsBench)}</span>`, `Beli & tahan ${pct(m.benchmark_return_pct)}`],
      ["CAGR", pct(m.cagr_pct), "Return per tahun"],
      ["Max drawdown", `<span class="down">${fmt(m.max_drawdown_pct, 2)}%</span>`, ddCompare],
      ["Sharpe", fmt(m.sharpe, 2), "Tahunan, risk-free 0"],
      ["Beta vs IHSG", m.beta_vs_ihsg == null ? "—" : fmt(m.beta_vs_ihsg, 2), "1 = bergerak seperti IHSG, 0 = tidak terpengaruh"],
      ["Win rate", m.win_rate_pct == null ? "—" : `${fmt(m.win_rate_pct, 1)}%`, `${m.trades} transaksi selesai`],
      ["Profit factor", m.profit_factor == null ? "—" : fmt(m.profit_factor, 2), `Rata-rata ${pct(m.avg_trade_pct)}/transaksi`],
      ["Fee dibayar", shortRp(m.fees_paid), `Waktu berinvestasi ${fmt(m.exposure_pct, 0)}%`],
    ];
    $("#btTiles").innerHTML = tiles.map(([k, v, sub]) =>
      `<div class="tile"><span>${k}</span><b>${v}</b><small class="muted">${sub}</small></div>`).join("");

    const w = $("#btWarnings");
    w.classList.toggle("hidden", !r.warnings.length);
    w.innerHTML = r.warnings.length ? "Catatan: " + r.warnings.map(escapeHtml).join("; ") : "";

    $("#btSymbols").innerHTML = `<tr><th>Kode</th><th>Transaksi</th><th>Menang</th><th>P/L strategi</th><th>Beli & tahan</th><th></th></tr>` +
      r.per_symbol.map((s) => `<tr><td>${s.symbol}</td><td>${s.trades}</td><td>${s.wins}</td>
        <td class="${cls(s.pl)}">${rp(s.pl)}</td><td class="${cls(s.buy_hold_pct)}">${pct(s.buy_hold_pct)}</td>
        <td><button type="button" data-chart="${escapeHtml(s.symbol)}" ${s.trades ? "" : "disabled"}>Lihat di grafik</button></td></tr>`).join("");
    $("#btSymbols").querySelectorAll("[data-chart]").forEach((b) => { b.onclick = () => window.showBacktestOnChart?.(b.dataset.chart); });

    $("#btTradeCount").textContent = `(${r.trades.length})`;
    $("#btTrades").innerHTML = `<tr><th>Kode</th><th>Masuk</th><th>Harga masuk</th><th>Keluar</th><th>Harga keluar</th><th>Lot</th><th>Hari</th><th>P/L</th></tr>` +
      (r.trades.length ? r.trades.slice().reverse().map((t) => `<tr><td>${t.symbol}</td><td>${fmtDate(t.entry_date)}</td>
        <td>${fmt(t.entry_price, 2)}</td><td>${t.closed ? fmtDate(t.exit_date) : "<i>masih terbuka</i>"}</td>
        <td>${fmt(t.exit_price)}</td><td>${fmt(t.lots)}</td><td>${t.holding_days ?? "—"}</td>
        <td class="${cls(t.pl)}">${rp(t.pl)} (${pct(t.pl_pct)})</td></tr>`).join("")
        : `<tr><td colspan="8" style="text-align:left;color:var(--muted)">Tidak ada transaksi — coba longgarkan skor beli atau perpanjang periode.</td></tr>`);

    drawChart(r.equity_curve);
  }

  // ---------- grafik ekuitas (SVG) ----------
  function niceTicks(min, max, count = 5) {
    const span = max - min || Math.abs(max) || 1;
    const raw = span / count, mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((k) => k * mag).find((s) => s >= raw);
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let k = 0; lo + k * step <= hi + step * 1e-6; k++) ticks.push(lo + k * step);
    return ticks;
  }

  // Urutan = urutan gambar (yang terakhir paling atas). Warna mengikuti entitas, bukan urutan.
  const SERIES = [
    { key: "benchmark", label: "Beli & tahan", legend: "Beli & tahan (bobot sama, tanpa fee)", n: 2 },
    { key: "ihsg", label: "IHSG", legend: "IHSG", n: 3 },
    { key: "equity", label: "Strategi", legend: "Strategi", n: 1 },
  ];

  // Geser label ujung agar tidak bertumpuk (jarak minimal `gap` px), tetap di dalam area plot.
  function spreadLabels(items, gap, top, bottom) {
    const sorted = items.slice().sort((a, b) => a.y - b.y);
    for (let i = 1; i < sorted.length; i++) sorted[i].y = Math.max(sorted[i].y, sorted[i - 1].y + gap);
    const overflow = sorted.length ? sorted[sorted.length - 1].y - bottom : 0;
    if (overflow > 0) sorted.forEach((it) => { it.y -= overflow; });
    for (let i = sorted.length - 2; i >= 0; i--) sorted[i].y = Math.min(sorted[i].y, sorted[i + 1].y - gap);
    sorted.forEach((it) => { it.y = Math.max(it.y, top); });
    return items;
  }

  function drawChart(curve) {
    const box = $("#btChart");
    const W = box.clientWidth, H = box.clientHeight;
    const pad = { l: 64, r: 96, t: 10, b: 26 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    const series = SERIES.filter((s) => curve[0][s.key] != null);
    $("#btLegend").innerHTML = series.slice().reverse()
      .map((s) => `<span><i class="sw s${s.n}"></i>${escapeHtml(s.legend)}</span>`).join("");

    const vals = curve.flatMap((p) => series.map((s) => p[s.key]));
    const ticks = niceTicks(Math.min(...vals), Math.max(...vals));
    const y0 = ticks[0], y1 = ticks[ticks.length - 1];
    const x = (i) => pad.l + (curve.length === 1 ? 0 : (i / (curve.length - 1)) * iw);
    const y = (v) => pad.t + ih - ((v - y0) / (y1 - y0 || 1)) * ih;
    const path = (key) => curve.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join("");

    // sumbu X: ~6 label tanggal
    const nX = Math.min(6, curve.length);
    const xIdx = [...new Set(Array.from({ length: nX }, (_, k) => Math.round((k / Math.max(nX - 1, 1)) * (curve.length - 1))))];
    const monthFmt = (d) => new Date(d + "T00:00:00").toLocaleDateString("id-ID", { month: "short", year: "2-digit" });

    const last = curve[curve.length - 1], xEnd = x(curve.length - 1);
    const labels = spreadLabels(series.map((s) => ({ s, y: y(last[s.key]) })), 15, pad.t + 4, pad.t + ih);

    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      ${ticks.map((t) => `<line class="gridline" x1="${pad.l}" x2="${W - pad.r}" y1="${y(t)}" y2="${y(t)}"/>
        <text class="axis-label" x="${pad.l - 8}" y="${y(t) + 4}" text-anchor="end">${shortRp(t)}</text>`).join("")}
      ${xIdx.map((i) => `<text class="axis-label" x="${x(i)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === curve.length - 1 ? "end" : "middle"}">${monthFmt(curve[i].date)}</text>`).join("")}
      <path class="area" d="${path("equity")}L${xEnd},${y(y0)}L${x(0)},${y(y0)}Z"/>
      ${series.map((s) => `<path class="line l${s.n}" d="${path(s.key)}"/>`).join("")}
      ${labels.map((l) => `<text class="end-label" x="${xEnd + 8}" y="${l.y + 4}">${escapeHtml(l.s.label)}</text>`).join("")}
      <g id="btHover" visibility="hidden">
        <line class="xhair" y1="${pad.t}" y2="${pad.t + ih}"/>
        ${series.map((s) => `<circle class="dot d${s.n}" data-key="${s.key}" r="4"/>`).join("")}
      </g>
      <rect x="${pad.l}" y="${pad.t}" width="${iw}" height="${ih}" fill="transparent" id="btHit"/>
    </svg>`;

    const hover = box.querySelector("#btHover"), tip = $("#btTip");
    const hide = () => { hover.setAttribute("visibility", "hidden"); tip.classList.add("hidden"); };
    box.querySelector("#btHit").addEventListener("mousemove", (ev) => {
      const rect = box.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const i = Math.max(0, Math.min(curve.length - 1, Math.round(((mx - pad.l) / iw) * (curve.length - 1))));
      const p = curve[i], cx = x(i);
      hover.setAttribute("visibility", "visible");
      hover.querySelector(".xhair").setAttribute("x1", cx);
      hover.querySelector(".xhair").setAttribute("x2", cx);
      hover.querySelectorAll(".dot").forEach((d) => { d.setAttribute("cx", cx); d.setAttribute("cy", y(p[d.dataset.key])); });
      const base = curve[0].equity;
      // Baris tooltip diurutkan dari nilai tertinggi, sama seperti posisi garis.
      const rows = series.slice().sort((a, b) => p[b.key] - p[a.key]).map((s) =>
        `<i style="background:var(--series-${s.n})"></i>${escapeHtml(s.label)} ${rp(p[s.key])} (${pct((p[s.key] / base - 1) * 100)})`);
      tip.innerHTML = `<b>${fmtDate(p.date)}</b><br>${rows.join("<br>")}`;
      tip.classList.remove("hidden");
      const card = box.parentElement.getBoundingClientRect();
      const left = rect.left - card.left + cx;
      tip.style.top = `${rect.top - card.top + 8}px`;
      tip.style.left = left + tip.offsetWidth + 16 > card.width ? `${left - tip.offsetWidth - 12}px` : `${left + 12}px`;
    });
    box.querySelector("#btHit").addEventListener("mouseleave", hide);
  }

  window.initBacktest = init;
  // Transaksi backtest terakhir untuk satu saham (dipakai grafik untuk penanda).
  window.getBacktestTrades = (symbol) => (lastResult ? lastResult.trades.filter((t) => t.symbol === symbol) : []);
})();
