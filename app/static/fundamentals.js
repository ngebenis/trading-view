// Tab Fundamental. Memakai helper global dari app.js: $, fmt, cls, escapeHtml, state, isIndex.
(() => {
  const PERIOD_LABEL = { Q1: "Q1", Q2: "Q2", Q3: "Q3", FY: "Tahunan" };
  const MAX_COLUMNS = 6;

  // Angka laporan bisa sangat besar: tampilkan dalam jt / M (miliar) / T (triliun).
  function money(n, currency) {
    if (n == null) return "—";
    const sym = currency === "USD" ? "US$" : "Rp";
    const a = Math.abs(n);
    const [v, unit] = a >= 1e12 ? [n / 1e12, " T"] : a >= 1e9 ? [n / 1e9, " M"] : a >= 1e6 ? [n / 1e6, " jt"] : [n, ""];
    return `${sym}${fmt(v, unit ? (Math.abs(v) < 10 ? 2 : 1) : 0)}${unit}`;
  }
  const pct = (n) => n == null ? "—" : `${n > 0 ? "+" : ""}${fmt(n, 1)}%`;
  const num = (n, d = 2) => n == null ? "—" : fmt(n, d);
  const times = (n) => n == null ? "—" : `${fmt(n, 1)}×`;
  const periodName = (r) => `${PERIOD_LABEL[r.period] || r.period} ${r.year}`;

  const ROWS = [
    ["Neraca", null],
    ["Total aset", "total_assets"], ["Aset lancar", "current_assets"], ["Kas & setara kas", "cash_and_equivalents"],
    ["Total liabilitas", "total_liabilities"], ["Liabilitas jangka pendek", "current_liabilities"],
    ["Dana pihak ketiga (bank)", "customer_deposits"], ["Ekuitas (pemilik entitas induk)", "total_equity"],
    ["Laba rugi (YTD)", null],
    ["Pendapatan", "revenue"], ["Pendapatan bunga bersih (bank)", "net_interest_income"], ["Laba kotor", "gross_profit"],
    ["Laba sebelum pajak", "profit_before_tax"], ["Laba bersih (pemilik entitas induk)", "net_income"],
    ["EPS (YTD)", "eps"],
    ["Arus kas (YTD)", null],
    ["Arus kas operasi", "operating_cash_flow"], ["Belanja modal (capex)", "capital_expenditure"],
  ];

  function render(data) {
    const reports = data.reports;
    $("#fundSymbol").textContent = data.symbol;
    $("#fundResult").classList.toggle("hidden", !reports.length);
    const empty = $("#fundEmpty");
    empty.classList.toggle("hidden", reports.length > 0);
    empty.innerHTML = `Belum ada laporan keuangan <b>${escapeHtml(data.symbol)}</b>. Unduh <code>instance.zip</code> dari link di bawah,
      lalu klik <b>Upload laporan</b>. File disimpan di <code>${escapeHtml(data.xbrl_dir)}</code>.`;
    const errs = $("#fundErrors");
    errs.classList.toggle("hidden", !data.errors.length);
    errs.innerHTML = data.errors.length ? "Catatan: " + data.errors.map(escapeHtml).join("; ") : "";

    $("#fundLinks").innerHTML = data.download_links.map((l) => l.stored
      ? `<span title="Sudah tersimpan">✓ ${PERIOD_LABEL[l.period]} ${l.year}</span>`
      : `<a href="${escapeHtml(l.url)}" target="_blank" rel="noopener">${PERIOD_LABEL[l.period]} ${l.year} ↗</a>`).join("");

    if (!reports.length) return;
    const latest = reports[0], x = latest.ratios;
    $("#fundAsOf").textContent = `Laporan terbaru: ${periodName(latest)} (per ${latest.period_end}) · ` +
      `jenis ${{ general: "umum", banking: "bank/keuangan", insurance: "asuransi" }[latest.taxonomy_type] || latest.taxonomy_type} · ` +
      `mata uang ${latest.currency}` + (x.fx_rate ? ` (kurs asumsi Rp${fmt(x.fx_rate)}/US$)` : "") +
      (x.price ? ` · harga Rp${fmtPrice(x.price, data.symbol)}` : "");

    const bank = latest.taxonomy_type !== "general";
    const tiles = [
      ["PER", times(x.per), x.eps_annualized == null ? "Butuh EPS / jumlah saham" : `EPS setahun Rp${num(x.eps_annualized)}`],
      ["PBV", times(x.pbv), x.bvps == null ? "Butuh jumlah saham" : `Nilai buku Rp${num(x.bvps)}/lembar`],
      ["ROE", pct(x.roe_pct), "Laba setahun ÷ ekuitas"],
      ["ROA", pct(x.roa_pct), "Laba setahun ÷ aset"],
      ["DER", times(x.der), "Liabilitas ÷ ekuitas"],
      bank ? ["Net margin", pct(x.net_margin_pct), "Laba ÷ pendapatan"]
        : ["Current ratio", times(x.current_ratio), "Aset lancar ÷ liabilitas lancar"],
      ["Pertumbuhan laba", `<span class="${cls(x.net_income_growth_pct)}">${pct(x.net_income_growth_pct)}</span>`, "vs periode sama tahun lalu"],
      ["Pertumbuhan pendapatan", `<span class="${cls(x.revenue_growth_pct)}">${pct(x.revenue_growth_pct)}</span>`, "vs periode sama tahun lalu"],
      ["Kapitalisasi pasar", x.market_cap == null ? "—" : money(x.market_cap, "IDR"),
        x.shares ? `${fmt(x.shares / 1e9, 2)} M lembar (${x.shares_source})` : "Jumlah saham tidak diketahui"],
    ];
    if (!bank) tiles.splice(6, 0, ["Net margin", pct(x.net_margin_pct), `Margin kotor ${pct(x.gross_margin_pct)}`]);
    $("#fundTiles").innerHTML = tiles.map(([k, v, sub]) =>
      `<div class="tile"><span>${k}</span><b>${v}</b><small class="muted">${sub}</small></div>`).join("");

    const cols = reports.slice(0, MAX_COLUMNS);
    const present = new Set(cols.flatMap((r) => Object.keys(r.metrics)));
    // Tampilkan hanya akun yang ada; judul bagian hanya bila bagiannya berisi.
    const rows = [];
    let header = null;
    for (const row of ROWS) {
      if (!row[1]) { header = row; continue; }
      if (!present.has(row[1])) continue;
      if (header) { rows.push(header); header = null; }
      rows.push(row);
    }
    $("#fundTable").innerHTML = `<tr><th>Akun</th>${cols.map((r) => `<th>${periodName(r)}</th>`).join("")}</tr>` +
      rows.map(([label, key]) => key
        ? `<tr><td>${label}</td>${cols.map((r) => `<td title="${escapeHtml(r.sources[key] || "")}">${
          key === "eps" ? num(r.metrics[key]) : money(r.metrics[key], r.currency)}</td>`).join("")}</tr>`
        : `<tr class="section"><td colspan="${cols.length + 1}">${label}</td></tr>`).join("");
  }

  async function load() {
    const sym = state.symbol;
    $("#fundSymbol").textContent = sym;
    if (isIndex(sym)) {
      $("#fundResult").classList.add("hidden");
      $("#fundLinks").innerHTML = "";
      $("#fundEmpty").classList.remove("hidden");
      $("#fundEmpty").innerHTML = `<b>${escapeHtml(sym)}</b> adalah indeks dan tidak memiliki laporan keuangan. Pilih kode saham.`;
      return;
    }
    try {
      render(await api(`/api/fundamentals/${sym}`));
    } catch (e) {
      $("#fundMsg").className = "msg err"; $("#fundMsg").textContent = e.message;
    }
  }

  async function upload(ev) {
    const files = [...ev.target.files];
    ev.target.value = "";
    const msg = $("#fundMsg");
    const done = [], failed = [];
    for (const f of files) {
      msg.className = "msg"; msg.textContent = `Memproses ${f.name}…`;
      try {
        const res = await fetch(`/api/fundamentals/upload?ticker=${encodeURIComponent(state.symbol)}`,
          { method: "POST", body: f, headers: { "Content-Type": "application/octet-stream" } });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.detail || res.statusText);
        done.push(`${body.ticker} ${PERIOD_LABEL[body.period] || body.period} ${body.year}`);
      } catch (e) { failed.push(`${f.name}: ${e.message}`); }
    }
    msg.className = "msg " + (failed.length ? "err" : "ok");
    msg.textContent = [done.length && `Tersimpan: ${done.join(", ")}`, failed.length && `Gagal: ${failed.join("; ")}`]
      .filter(Boolean).join(" · ");
    load();
  }

  $("#fundFile").addEventListener("change", upload);
  window.loadFundamentals = load;
})();
