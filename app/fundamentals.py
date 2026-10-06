"""Analisis fundamental dari laporan keuangan XBRL resmi IDX.

Diadaptasi dari pendekatan https://github.com/septianbyk/idx-financial-scraper (MIT):
laporan keuangan emiten diunduh dari idx.co.id sebagai `instance.zip` berisi file XBRL,
lalu angka-angka kunci dipetakan lewat tabel taksonomi (lihat fundamentals_taxonomy.csv).

Perbedaan dari versi aslinya:
- Konteks dipilih berdasarkan tanggal (bukan sekadar awalan tahun) dan konteks berdimensi
  (nilai per segmen/komponen) diabaikan, sehingga yang terbaca adalah angka total.
- Laporan tahunan (FY / "Audit") ikut diproses.
- Angka pembanding tahun lalu diambil dari file yang sama untuk menghitung pertumbuhan YoY.
- Jenis taksonomi (umum/bank/asuransi) dideteksi dari isi laporan, tanpa panggilan yfinance.
"""
import csv
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .idx_rules import is_index, normalize_symbol

TAXONOMY_PATH = Path(__file__).with_name("fundamentals_taxonomy.csv")

# Folder di situs IDX -> kode periode di aplikasi
PERIODS = [("TW1", "Q1"), ("TW2", "Q2"), ("TW3", "Q3"), ("Audit", "FY")]
PERIOD_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "FY": 4}
# Laba rugi IDX bersifat kumulatif sejak awal tahun (YTD); faktor untuk menyetahunkan.
ANNUALIZE = {"Q1": 4.0, "Q2": 2.0, "Q3": 4 / 3, "FY": 1.0}

IDX_URL_TEMPLATE = (
    "https://www.idx.co.id/Portals/0/StaticData/ListedCompanies/Corporate_Actions/"
    "New_Info_JSX/Jenis_Informasi/01_Laporan_Keuangan/02_Soft_Copy_Laporan_Keuangan/"
    "/Laporan%20Keuangan%20Tahun%20{year}/{period_folder}/{ticker}/instance.zip"
)


class FundamentalsError(Exception):
    pass


def idx_report_url(ticker: str, year: int, period: str) -> str:
    folder = {tag: folder for folder, tag in PERIODS}[period]
    return IDX_URL_TEMPLATE.format(year=year, period_folder=folder, ticker=normalize_symbol(ticker))


def load_taxonomy(path: Path = TAXONOMY_PATH) -> dict[str, dict[str, list[str]]]:
    """{taxonomy_type: {common_term: [tag, ...]}} — urutan baris = prioritas."""
    maps: dict[str, dict[str, list[str]]] = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            tag = row["xbrl_tag"].strip().split(":")[-1]
            maps.setdefault(row["taxonomy_type"].strip(), {}).setdefault(row["common_term"].strip(), []).append(tag)
    return maps


# ---- membaca XBRL --------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _to_date(text: str) -> date:
    return date.fromisoformat(text.strip()[:10])


@dataclass
class _Context:
    id: str
    dimensional: bool
    instant: date | None = None
    start: date | None = None
    end: date | None = None


def extract_xbrl(content: bytes) -> bytes:
    """Terima isi instance.zip atau XBRL mentah; kembalikan XBRL-nya."""
    if content[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                names = z.namelist()
                name = next((n for n in names if n.lower().endswith(".xbrl")), None) \
                    or next((n for n in names if n.lower().endswith(".xml")), None)
                if name is None:
                    raise FundamentalsError("File zip tidak berisi laporan XBRL")
                return z.read(name)
        except zipfile.BadZipFile as exc:
            raise FundamentalsError("File zip rusak") from exc
    return content


def _parse(xbrl: bytes):
    not_xbrl = ("Bukan laporan XBRL. Pastikan yang diunggah adalah instance.zip dari IDX "
                "(bila isinya halaman 'Just a moment…', selesaikan verifikasi di browser lalu unduh ulang)")
    try:
        root = ET.fromstring(xbrl)
    except ET.ParseError as exc:
        raise FundamentalsError(not_xbrl) from exc
    if _local(root.tag) != "xbrl":
        raise FundamentalsError(not_xbrl)
    contexts: dict[str, _Context] = {}
    units: dict[str, str] = {}
    facts: list[tuple[str, str, str | None, float]] = []  # (tag, contextRef, unitRef, nilai)
    text_facts: dict[str, str] = {}
    for el in root:
        name = _local(el.tag)
        if name == "context":
            ctx = _Context(el.get("id", ""), dimensional=any(
                _local(c.tag) in ("segment", "scenario") for c in el.iter()))
            for c in el.iter():
                n = _local(c.tag)
                if n == "instant":
                    ctx.instant = _to_date(c.text)
                elif n == "startDate":
                    ctx.start = _to_date(c.text)
                elif n == "endDate":
                    ctx.end = _to_date(c.text)
            contexts[ctx.id] = ctx
        elif name == "unit":
            measures = [(m.text or "").strip() for m in el.iter() if _local(m.tag) == "measure"]
            units[el.get("id", "")] = measures[0] if len(measures) == 1 else "/".join(measures)
        elif el.get("contextRef"):
            if any(k.endswith("}nil") and v == "true" for k, v in el.attrib.items()):
                continue
            text = (el.text or "").strip()
            try:
                facts.append((name, el.get("contextRef"), el.get("unitRef"), float(text)))
            except ValueError:
                if text:
                    text_facts.setdefault(name, text)
    return contexts, units, facts, text_facts


def _period_of(end: date) -> str:
    return {3: "Q1", 6: "Q2", 9: "Q3", 12: "FY"}.get(end.month, f"M{end.month:02d}")


@dataclass
class Report:
    ticker: str
    year: int
    period: str
    period_end: str
    taxonomy_type: str
    currency: str
    metrics: dict = field(default_factory=dict)  # periode berjalan
    prior: dict = field(default_factory=dict)    # pembanding tahun lalu (dari file yang sama)
    sources: dict = field(default_factory=dict)  # common_term -> tag XBRL yang dipakai

    def to_dict(self) -> dict:
        return asdict(self)


def detect_taxonomy(tags: set[str]) -> str:
    if tags & {"RevenueFromInsurancePremiums", "NetPremiumIncome", "PremiumIncome"}:
        return "insurance"
    if tags & {"DepositsFromCustomers", "TotalInterestAndShariaIncome", "NetInterestAndShariaIncome"}:
        return "banking"
    return "general"


def parse_report(content: bytes, ticker: str | None = None, taxonomy: dict | None = None) -> Report:
    """Baca satu laporan (instance.zip / .xbrl) menjadi angka-angka kunci."""
    contexts, units, facts, text_facts = _parse(extract_xbrl(content))
    plain = [c for c in contexts.values() if not c.dimensional]
    instants = sorted({c.instant for c in plain if c.instant})
    ends = sorted({c.end for c in plain if c.end})
    if not instants and not ends:
        raise FundamentalsError("Tidak menemukan periode laporan di file XBRL")
    period_end = max(instants[-1] if instants else date.min, ends[-1] if ends else date.min)

    def duration_ids(end: date) -> set[str]:
        """Konteks durasi terpanjang (YTD) yang berakhir di `end`."""
        cands = [c for c in plain if c.end == end and c.start]
        if not cands:
            return set()
        start = min(c.start for c in cands)
        return {c.id for c in cands if c.start == start}

    prior_end = next((e for e in reversed(ends) if (period_end - e).days in range(350, 380)), None)
    current_ids = {c.id for c in plain if c.instant == period_end} | duration_ids(period_end)
    prior_instant = next((i for i in reversed(instants) if (period_end - i).days in range(350, 380)), None)
    prior_ids = ({c.id for c in plain if c.instant == prior_instant} if prior_instant else set()) | \
        (duration_ids(prior_end) if prior_end else set())

    by_tag: dict[str, list[tuple[str, str | None, float]]] = {}
    for tag, ctx, unit, value in facts:
        by_tag.setdefault(tag, []).append((ctx, unit, value))

    def resolve(tags: list[str], ids: set[str]):
        for tag in tags:
            for ctx, unit, value in by_tag.get(tag, []):
                if ctx in ids:
                    return value, tag, unit
        return None, None, None

    tax_type = detect_taxonomy(set(by_tag))
    terms = (taxonomy or load_taxonomy()).get(tax_type, {})
    report_ticker = normalize_symbol(ticker or text_facts.get("EntityCode", "") or "")
    if not report_ticker:
        raise FundamentalsError("Kode emiten tidak ditemukan di file; isi kode saham secara manual")

    metrics, prior, sources, currencies = {}, {}, {}, []
    for term, tags in terms.items():
        value, tag, unit = resolve(tags, current_ids)
        if value is not None:
            metrics[term], sources[term] = value, tag
            measure = units.get(unit or "", "")
            if measure.lower().startswith("iso4217:"):
                currencies.append(measure.split(":")[1].upper())
        pvalue, _, _ = resolve(tags, prior_ids)
        if pvalue is not None:
            prior[term] = pvalue
    currency = max(set(currencies), key=currencies.count) if currencies else "IDR"

    period = _period_of(period_end)
    return Report(report_ticker, period_end.year, period, period_end.isoformat(), tax_type, currency,
                  metrics, prior, sources)


# ---- rasio -------------------------------------------------------------------
def _div(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def compute_ratios(report: Report, price: float | None, usd_idr: float) -> dict:
    """Rasio valuasi & kinerja. Laba disetahunkan dari angka YTD (lihat ANNUALIZE)."""
    m, p = report.metrics, report.prior
    factor = ANNUALIZE.get(report.period, 1.0)
    fx = usd_idr if report.currency == "USD" else 1.0  # harga saham selalu dalam Rupiah
    ni, equity = m.get("net_income"), m.get("total_equity")
    shares = m.get("outstanding_shares") or None
    shares_source = "laporan" if shares else None
    if not shares and m.get("eps") and ni:
        shares, shares_source = ni / m["eps"], "laba ÷ EPS"  # EPS & laba dalam mata uang yang sama
    eps_annual = (ni * factor / shares) if ni is not None and shares else None
    bvps = _div(equity, shares)
    pct = lambda x: round(x * 100, 2) if x is not None else None  # noqa: E731
    r2 = lambda x: round(x, 2) if x is not None else None  # noqa: E731
    return {
        "price": price,
        "shares": round(shares) if shares else None,
        "shares_source": shares_source,
        "eps_annualized": r2(eps_annual * fx) if eps_annual is not None else None,
        "per": r2(_div(price, eps_annual * fx)) if price and eps_annual and eps_annual > 0 else None,
        "bvps": r2(bvps * fx) if bvps is not None else None,
        "pbv": r2(_div(price, bvps * fx)) if price and bvps and bvps > 0 else None,
        "market_cap": round(price * shares) if price and shares else None,
        "roe_pct": pct(_div(ni * factor, equity)) if ni is not None else None,
        "roa_pct": pct(_div(ni * factor, m.get("total_assets"))) if ni is not None else None,
        "der": r2(_div(m.get("total_liabilities"), equity)),
        "current_ratio": r2(_div(m.get("current_assets"), m.get("current_liabilities"))),
        "net_margin_pct": pct(_div(ni, m.get("revenue"))),
        "gross_margin_pct": pct(_div(m.get("gross_profit"), m.get("revenue"))),
        "revenue_growth_pct": pct(_div((m.get("revenue") or 0) - p["revenue"], abs(p["revenue"])))
        if m.get("revenue") is not None and p.get("revenue") else None,
        "net_income_growth_pct": pct(_div((ni or 0) - p["net_income"], abs(p["net_income"])))
        if ni is not None and p.get("net_income") else None,
        "annualize_factor": round(factor, 4),
        "fx_rate": fx if report.currency == "USD" else None,
    }


# ---- penyimpanan -------------------------------------------------------------
_FILE_RE = re.compile(r"^([A-Z0-9]+)_(\d{4})_(Q1|Q2|Q3|FY)\.xbrl$")


class FundamentalsStore:
    """File XBRL disimpan dengan tata letak yang sama seperti idx-financial-scraper:
    <xbrl_dir>/<tahun>/<periode>/<KODE>_<tahun>_<periode>.xbrl
    sehingga folder hasil scraper itu bisa dipakai langsung (FUNDAMENTALS_XBRL_DIR)."""

    def __init__(self, xbrl_dir: Path, taxonomy_path: Path = TAXONOMY_PATH):
        self.xbrl_dir = xbrl_dir
        self.taxonomy = load_taxonomy(taxonomy_path)
        self._cache: dict[Path, tuple[float, Report]] = {}

    def path_for(self, ticker: str, year: int, period: str) -> Path:
        t = normalize_symbol(ticker)
        return self.xbrl_dir / str(year) / period / f"{t}_{year}_{period}.xbrl"

    def files_for(self, ticker: str) -> list[Path]:
        t = normalize_symbol(ticker)
        if not self.xbrl_dir.exists():
            return []
        out = []
        for path in self.xbrl_dir.glob(f"*/*/{t}_*.xbrl"):
            if (m := _FILE_RE.match(path.name)) and m.group(1) == t:
                out.append(path)
        return out

    def load(self, path: Path) -> Report:
        mtime = path.stat().st_mtime
        hit = self._cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        ticker = _FILE_RE.match(path.name).group(1)
        report = parse_report(path.read_bytes(), ticker, self.taxonomy)
        self._cache[path] = (mtime, report)
        return report

    def reports(self, ticker: str) -> tuple[list[Report], list[str]]:
        """Semua laporan emiten, terbaru dulu, plus daftar file yang gagal dibaca."""
        reports, errors = [], []
        for path in self.files_for(ticker):
            try:
                reports.append(self.load(path))
            except (FundamentalsError, OSError) as exc:
                errors.append(f"{path.name}: {exc}")
        reports.sort(key=lambda r: (r.year, PERIOD_ORDER.get(r.period, 0)), reverse=True)
        return reports, errors

    def save_upload(self, content: bytes, ticker: str | None = None) -> Report:
        """Simpan instance.zip / .xbrl yang diunggah; tahun & periode dibaca dari isi laporan."""
        xbrl = extract_xbrl(content)
        report = parse_report(xbrl, ticker, self.taxonomy)
        if is_index(report.ticker):
            raise FundamentalsError("Indeks tidak memiliki laporan keuangan")
        if report.period not in PERIOD_ORDER:
            raise FundamentalsError(f"Periode laporan tidak dikenali (berakhir {report.period_end})")
        path = self.path_for(report.ticker, report.year, report.period)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(xbrl)
        self._cache.pop(path, None)
        return report


def recent_periods(today: date, count: int = 6) -> list[tuple[int, str]]:
    """Periode laporan terakhir yang kemungkinan sudah terbit, terbaru dulu."""
    order = ["Q1", "Q2", "Q3", "FY"]
    done = (today.month - 1) // 3  # kuartal tahun ini yang sudah selesai (laporan bisa sudah terbit)
    year, idx = (today.year, done) if done else (today.year - 1, 4)
    out = []
    while len(out) < count:
        out.append((year, order[idx - 1]))
        idx -= 1
        if idx == 0:
            year, idx = year - 1, 4
    return out
