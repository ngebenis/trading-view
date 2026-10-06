"""Unduh laporan keuangan XBRL (instance.zip) dari idx.co.id untuk analisis fundamental.

Diadaptasi dari model/fetch_idx.py di https://github.com/septianbyk/idx-financial-scraper (MIT).

Cara kerja: situs IDX dilindungi Cloudflare, jadi skrip ini membuka Chrome biasa dengan profil
terpisah. ANDA yang membuka idx.co.id dan menyelesaikan verifikasi (bila muncul) di jendela itu,
lalu skrip mengunduh file memakai sesi browser tersebut — pelan-pelan (jeda antar-unduhan) agar
tidak membebani server IDX. Skrip tidak mencoba melewati verifikasi secara otomatis.

Pemakaian (dari folder repo):
    pip install -r requirements-scraper.txt
    python scripts/fetch_idx_reports.py BBCA TLKM ASII --start-year 2024
    python scripts/fetch_idx_reports.py --watchlist data/watchlist.csv --periods FY

File disimpan ke folder yang dibaca aplikasi (FUNDAMENTALS_XBRL_DIR atau data/fundamentals/XBRL);
file yang sudah ada dilewati, jadi skrip aman dijalankan ulang.
"""
import argparse
import base64
import csv
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.fundamentals import (PERIODS, FundamentalsError, FundamentalsStore, extract_xbrl,  # noqa: E402
                              idx_report_url, parse_report)
from app.idx_rules import is_index, normalize_symbol  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("fetch_idx")

IDX_HOME = "https://www.idx.co.id/id"
REQUEST_DELAY_SECONDS = 2.0
PERIOD_DELAY_SECONDS = 10.0
MAX_CHALLENGE_RETRIES = 2

FETCH_JS = """
async (url) => {
    const response = await fetch(url, { credentials: 'include' });
    const result = { status: response.status, b64: null };
    if (response.status === 200) {
        const bytes = new Uint8Array(await response.arrayBuffer());
        let binary = '';
        for (let i = 0; i < bytes.length; i += 0x8000) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
        }
        result.b64 = btoa(binary);
    }
    return result;
}
"""


class BlockedError(RuntimeError):
    pass


def chrome_candidates() -> list[str]:
    env = os.environ.get
    return [
        env("PROGRAMFILES", r"C:\Program Files") + r"\Google\Chrome\Application\chrome.exe",
        env("PROGRAMFILES(X86)", r"C:\Program Files (x86)") + r"\Google\Chrome\Application\chrome.exe",
        env("LOCALAPPDATA", "") + r"\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        *filter(None, (shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"))),
    ]


def find_chrome(explicit: str | None) -> str:
    if explicit:
        if not Path(explicit).exists():
            raise FileNotFoundError(f"Chrome tidak ditemukan: {explicit}")
        return explicit
    for cand in chrome_candidates():
        if cand and Path(cand).exists():
            return cand
    raise FileNotFoundError("Chrome tidak ditemukan. Isi --chrome-path dengan lokasi file Chrome.")


def debug_port_open(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
            return True
    except OSError:
        return False


def ensure_chrome(port: int, profile_dir: Path, chrome_path: str | None) -> None:
    if debug_port_open(port):
        log.info("Memakai Chrome yang sudah terbuka di port %d", port)
        return
    exe = find_chrome(chrome_path)
    profile_dir.mkdir(parents=True, exist_ok=True)
    log.info("Membuka Chrome (profil terpisah: %s)", profile_dir)
    subprocess.Popen([exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile_dir}",
                      "--no-first-run", "--no-default-browser-check", IDX_HOME],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 30
    while time.time() < deadline:
        if debug_port_open(port):
            return
        time.sleep(0.5)
    raise RuntimeError(f"Chrome tidak membuka port {port}. Tutup Chrome yang memakai profil yang sama lalu coba lagi.")


def load_watchlist(path: Path) -> list[str]:
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "ticker" not in reader.fieldnames:
            raise ValueError(f"{path} harus punya kolom 'ticker'")
        return [r["ticker"] for r in reader if (r["ticker"] or "").strip()]


def plan_downloads(store: FundamentalsStore, tickers: list[str], years: range, periods: list[str]) -> list[tuple]:
    """(tahun, periode, kode) yang belum ada di disk, dikelompokkan per periode seperti aslinya."""
    seen, clean = set(), []
    for t in tickers:
        s = normalize_symbol(t)
        if s and s not in seen and not is_index(s):
            seen.add(s)
            clean.append(s)
    return [(y, p, t) for y in years for p in periods for t in clean if not store.path_for(t, y, p).exists()]


def page_is_challenge(page) -> bool:
    try:
        title = (page.title() or "").lower()
    except Exception:
        return False
    return "just a moment" in title or "attention required" in title


def wait_for_human(page) -> None:
    if not sys.stdin or not sys.stdin.isatty():
        raise BlockedError("Verifikasi Cloudflare diperlukan; jalankan skrip ini dari terminal interaktif.")
    try:
        page.goto(IDX_HOME, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        log.warning("Membuka idx.co.id belum selesai: %s", exc)
    print("\nVerifikasi mungkin diperlukan. Di jendela Chrome, pastikan situs IDX terbuka normal.")
    if input("Tekan Enter untuk lanjut, atau ketik q untuk berhenti: ").strip().lower() == "q":
        raise BlockedError("Dihentikan oleh pengguna.")


def log_failure(path: Path, ticker: str, year: int, period: str, reason: str) -> None:
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ticker", "year", "period", "reason", "logged_at"])
        w.writerow([ticker, year, period, reason, datetime.now().isoformat(timespec="seconds")])


def download(page, store: FundamentalsStore, tasks: list[tuple], failed_log: Path, delay: float) -> dict:
    counts = {"tersimpan": 0, "tidak_ada": 0, "gagal": 0}
    last_period = None
    for year, period, ticker in tasks:
        if last_period and last_period != (year, period):
            time.sleep(PERIOD_DELAY_SECONDS)
        last_period = (year, period)
        url = idx_report_url(ticker, year, period)
        for attempt in range(MAX_CHALLENGE_RETRIES + 1):
            result = page.evaluate(FETCH_JS, url)
            status = result["status"]
            content = base64.b64decode(result["b64"]) if result.get("b64") else None
            if status == 200 and content and content[:2] == b"PK":
                try:
                    xbrl = extract_xbrl(content)
                    parse_report(xbrl, ticker, store.taxonomy)  # pastikan isinya laporan yang terbaca
                    path = store.path_for(ticker, year, period)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(xbrl)
                    counts["tersimpan"] += 1
                    log.info("Tersimpan %s %s %s", ticker, period, year)
                except FundamentalsError as exc:
                    counts["gagal"] += 1
                    log_failure(failed_log, ticker, year, period, str(exc))
                    log.warning("%s %s %s: %s", ticker, period, year, exc)
                break
            if status == 404:
                counts["tidak_ada"] += 1  # laporan belum terbit / emiten belum tercatat
                break
            if status in (200, 403, 429) and attempt < MAX_CHALLENGE_RETRIES:
                log.warning("%s: diblokir (HTTP %s), perlu verifikasi", ticker, status)
                wait_for_human(page)
                continue
            if status in (200, 403, 429):
                log_failure(failed_log, ticker, year, period, f"blocked_http_{status}")
                raise BlockedError(f"Masih diblokir di {ticker} {period} {year}. Coba lagi nanti.")
            counts["gagal"] += 1
            log_failure(failed_log, ticker, year, period, f"http_{status}")
            log.warning("%s %s %s: HTTP %s", ticker, period, year, status)
            break
        time.sleep(delay)
    return counts


def parse_args(argv=None) -> argparse.Namespace:
    year = datetime.now().year
    ap = argparse.ArgumentParser(description="Unduh laporan keuangan XBRL dari idx.co.id")
    ap.add_argument("tickers", nargs="*", help="Kode saham, mis. BBCA TLKM")
    ap.add_argument("--watchlist", type=Path, help="CSV berkolom 'ticker' (format sama dengan idx-financial-scraper)")
    ap.add_argument("--start-year", type=int, default=year - 1)
    ap.add_argument("--end-year", type=int, default=year)
    ap.add_argument("--periods", nargs="+", choices=[p for _, p in PERIODS], default=[p for _, p in PERIODS])
    ap.add_argument("--xbrl-dir", type=Path, help="Folder tujuan (default: sama dengan aplikasi)")
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY_SECONDS, help="Jeda antar-unduhan (detik, min. 1)")
    ap.add_argument("--port", type=int, default=9222, help="Port remote debugging Chrome")
    ap.add_argument("--chrome-path", help="Lokasi Chrome bila tidak terdeteksi otomatis")
    args = ap.parse_args(argv)
    if not args.tickers and not args.watchlist:
        ap.error("isi kode saham atau --watchlist")
    args.delay = max(args.delay, 1.0)
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    xbrl_dir = args.xbrl_dir or (Path(settings.fundamentals_xbrl_dir) if settings.fundamentals_xbrl_dir
                                 else settings.data_dir / "fundamentals" / "XBRL")
    store = FundamentalsStore(xbrl_dir)
    tickers = list(args.tickers) + (load_watchlist(args.watchlist) if args.watchlist else [])
    tasks = plan_downloads(store, tickers, range(args.start_year, args.end_year + 1), args.periods)
    if not tasks:
        log.info("Semua laporan yang diminta sudah ada di %s", xbrl_dir)
        return 0
    log.info("%d laporan akan diunduh ke %s", len(tasks), xbrl_dir)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright belum terpasang: pip install -r requirements-scraper.txt")
        return 1

    ensure_chrome(args.port, settings.data_dir / "chrome_profile", args.chrome_path)
    failed_log = xbrl_dir.parent / "failed_downloads.csv"
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        try:
            if "idx.co.id" not in page.url or page_is_challenge(page):
                page.goto(IDX_HOME, wait_until="domcontentloaded", timeout=60000)
            if page_is_challenge(page):
                wait_for_human(page)
            counts = download(page, store, tasks, failed_log, args.delay)
            log.info("Selesai: %d tersimpan, %d belum terbit (404), %d gagal",
                     counts["tersimpan"], counts["tidak_ada"], counts["gagal"])
        except BlockedError as exc:
            log.error("%s File yang sudah terunduh tidak akan diunduh ulang.", exc)
            return 2
        except KeyboardInterrupt:
            log.error("Dihentikan. File yang sudah terunduh tidak akan diunduh ulang.")
            return 130
        finally:
            browser.close()
    if failed_log.exists():
        log.info("Ada unduhan gagal; lihat %s", failed_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
