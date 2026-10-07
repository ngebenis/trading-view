"""Laporan portofolio ke Telegram: harian, mingguan dan bulanan (akun simulasi saham IDX, plus crypto).

- Harian  : jam `report_time` (WIB, bawaan 16:15 — setelah bursa tutup), opsional hanya Senin–Jumat.
- Mingguan: hari `weekly_day` (0 = Senin … 6 = Minggu, bawaan Jumat) jam `weekly_time` (bawaan 16:30);
            merangkum 7 hari terakhir.
- Bulanan : tanggal `monthly_day` (0 = hari terakhir bulan, atau 1–28) jam `monthly_time` (bawaan 16:45);
            merangkum bulan berjalan dibanding akhir bulan sebelumnya, plus ekuitas tertinggi/terendah.
- Kuartalan: hari terakhir tiap kuartal (31 Mar, 30 Jun, 30 Sep, 31 Des) jam `quarterly_time` (bawaan 16:48);
            merangkum kuartal berjalan dibanding akhir kuartal lalu, plus return per bulan & tertinggi/terendah.
- Semesteran: 30 Juni (semester 1) & 31 Desember (semester 2) jam `semiannual_time` (bawaan 16:49);
            merangkum semester berjalan dibanding akhir semester lalu, plus return per bulan & tertinggi/terendah.
- Tahunan : 31 Desember jam `yearly_time` (bawaan 16:50); merangkum tahun berjalan dibanding akhir tahun
            lalu, plus return per bulan dan ekuitas tertinggi/terendah setahun.

Perubahan ekuitas dihitung dari snapshot ekuitas harian (setting `report_snapshots`) yang dicatat
otomatis sekali sehari setelah bursa tutup (dan setiap kali laporan dikirim).
"""
import calendar
import html
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .autotrader import WIB, rupiah
from .brokers import OrderStatus
from .idx_rules import LOT_SIZE
from .market_data import MarketDataError
from .notifier import _pct, index_value

MAX_POSITIONS = 15
KEEP_SNAPSHOTS = 400  # ±13 bulan: cukup untuk pembanding bulanan & ekuitas tertinggi/terendah
SNAPSHOT_TIME = "16:00"  # snapshot otomatis harian (WIB), setelah penutupan bursa
DAYS = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
MONTHS = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober",
          "November", "Desember"]
MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
PERIOD_NAME = {"daily": "harian", "weekly": "mingguan", "monthly": "bulanan", "quarterly": "kuartalan",
               "semiannual": "semesteran", "yearly": "tahunan"}
LAST_SENT_ATTR = {"daily": "last_sent", "weekly": "last_weekly_sent", "monthly": "last_monthly_sent",
                  "quarterly": "last_quarterly_sent", "semiannual": "last_semiannual_sent",
                  "yearly": "last_yearly_sent"}
SEMESTER_ENDS = ((6, 30), (12, 31))
QUARTER_END_MONTHS = (3, 6, 9, 12)


def signed_rp(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}Rp{rupiah(abs(x))}"


def ddmm(day: str) -> str:
    return f"{day[8:10]}/{day[5:7]}"


def is_last_day_of_month(d: date) -> bool:
    return d.day == calendar.monthrange(d.year, d.month)[1]


@dataclass
class Window:
    """Rentang laporan: `cutoff` = tanggal pembanding (None = harian), `start` = awal transaksi yang dihitung."""
    period: str
    cutoff: date | None
    start: date
    label: str       # "hari ini" / "7 hari" / "bulan ini"
    move_label: str  # keterangan perubahan IHSG

    @classmethod
    def of(cls, period: str, today: date) -> "Window":
        if period == "weekly":
            return cls(period, today - timedelta(days=7), today - timedelta(days=6), "7 hari", "dalam 7 hari")
        if period == "monthly":
            start = today.replace(day=1)
            return cls(period, start - timedelta(days=1), start, "bulan ini", "bulan ini")
        if period == "quarterly":
            start = today.replace(month=(today.month - 1) // 3 * 3 + 1, day=1)
            return cls(period, start - timedelta(days=1), start, "kuartal ini", "kuartal ini")
        if period == "semiannual":
            start = today.replace(month=1 if today.month <= 6 else 7, day=1)
            return cls(period, start - timedelta(days=1), start, "semester ini", "semester ini")
        if period == "yearly":
            start = today.replace(month=1, day=1)
            return cls(period, start - timedelta(days=1), start, "tahun ini", "tahun ini")
        return cls("daily", None, today, "hari ini", "")


class DailyReporter:
    def __init__(self, watcher, paper, provider, db, crypto: dict | None = None, clock=time.time):
        self.watcher = watcher      # SignalWatcher: bot Telegram, pengaturan & riwayat
        self.paper = paper          # PaperBroker saham
        self.provider = provider
        self.db = db
        self.crypto = crypto or {}  # {"provider", "brokers"} dari register_crypto
        self.clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        state = db.get_setting("daily_report", {}) if db is not None else {}
        self.last_sent: str | None = state.get("last_sent")
        self.last_weekly_sent: str | None = state.get("last_weekly_sent")
        self.last_monthly_sent: str | None = state.get("last_monthly_sent")
        self.last_quarterly_sent: str | None = state.get("last_quarterly_sent")
        self.last_semiannual_sent: str | None = state.get("last_semiannual_sent")
        self.last_yearly_sent: str | None = state.get("last_yearly_sent")

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), WIB)

    def _save_state(self) -> None:
        if self.db is not None:
            self.db.set_setting("daily_report", {attr: getattr(self, attr) for attr in LAST_SENT_ATTR.values()})

    # ---- snapshot ekuitas ----------------------------------------------
    def _snapshots(self) -> dict:
        return (self.db.get_setting("report_snapshots", {}) if self.db is not None else {}) or {}

    def _save_snapshot(self, account: str, today: str, equity: float) -> None:
        if self.db is None:
            return
        snaps = self._snapshots()
        series = snaps.setdefault(account, {})
        series[today] = equity
        snaps[account] = {d: series[d] for d in sorted(series)[-KEEP_SNAPSHOTS:]}
        self.db.set_setting("report_snapshots", snaps)

    def _reference(self, account: str, today: str, window: Window) -> tuple[str, float] | None:
        """Snapshot pembanding: harian = hari sebelumnya; mingguan/bulanan = snapshot terakhir <= tanggal
        pembanding (±7 hari lalu / akhir bulan lalu), atau yang tertua bila belum ada."""
        series = self._snapshots().get(account, {})
        before = sorted(d for d in series if d < today)
        if not before:
            return None
        if window.cutoff is None:
            ref = before[-1]
        else:
            older = [d for d in before if d <= window.cutoff.isoformat()]
            ref = older[-1] if older else before[0]
        return ref, series[ref]

    def _equity_line(self, equity: float, ref, money, unit_suffix="") -> str:
        line = f"Ekuitas: <b>{money(equity)}{unit_suffix}</b>"
        if ref is not None and ref[1]:
            change = equity - ref[1]
            sign = "+" if change >= 0 else "-"
            line += f" ({sign}{money(abs(change))}{unit_suffix} / {_pct(change / ref[1] * 100)} sejak {ddmm(ref[0])})"
        return line

    def _range_line(self, account: str, today: str, window: Window, equity: float, money, unit_suffix="") -> str | None:
        """Bulanan: ekuitas tertinggi & terendah bulan ini (dari snapshot harian + nilai sekarang)."""
        points = {d: v for d, v in self._snapshots().get(account, {}).items() if d >= window.start.isoformat()}
        points[today] = equity
        if len(points) < 2:
            return None
        hi, lo = max(points, key=points.get), min(points, key=points.get)
        return (f"Tertinggi {money(points[hi])}{unit_suffix} ({ddmm(hi)}) · "
                f"terendah {money(points[lo])}{unit_suffix} ({ddmm(lo)})")

    def _monthly_returns(self, account: str, today: str, window: Window, equity: float) -> str | None:
        """Kuartalan/semesteran/tahunan: return tiap bulan dari snapshot akhir bulan (pembanding bulan pertama = akhir
        periode sebelumnya)."""
        series = dict(self._snapshots().get(account, {}))
        series[today] = equity
        month_end: dict[str, float] = {}
        for d in sorted(series):
            month_end[d[:7]] = series[d]  # nilai terakhir tiap bulan
        prev = month_end.get(window.cutoff.isoformat()[:7])  # akhir periode sebelumnya
        parts = []
        for ym in sorted(m for m in month_end if m >= window.start.isoformat()[:7]):
            value = month_end[ym]
            if prev:
                parts.append(f"{MONTHS_SHORT[int(ym[5:7]) - 1]} {_pct((value / prev - 1) * 100)}")
            prev = value
        return "Per bulan: " + " · ".join(parts) if parts else None

    # ---- harga pada tanggal pembanding ----------------------------------
    @staticmethod
    def _close_before(candles, cutoff: date) -> float | None:
        closes = [c.close for c in candles if datetime.fromtimestamp(c.time, WIB).date() <= cutoff]
        return closes[-1] if closes else None

    def _change_since(self, provider, sym: str, price: float, cutoff: date, crypto: bool = False) -> float | None:
        """Perubahan harga (%) sejak penutupan pada/terakhir sebelum `cutoff`."""
        days = (self._now().date() - cutoff).days
        try:
            if crypto:
                candles = provider.candles(sym, None, "1d", 400 if days > 40 else 45 if days > 20 else 15)
            else:
                candles = provider.candles(sym, "2y" if days > 80 else "3mo" if days > 20 else "1mo", "1d")
        except MarketDataError:
            return None
        prev = self._close_before(candles, cutoff)
        return (price / prev - 1) * 100 if prev else None

    # ---- isi laporan ---------------------------------------------------
    def _stock_section(self, now: datetime, window: Window, save: bool) -> list[str]:
        today = now.strftime("%Y-%m-%d")
        quotes = {}
        for sym in set(self.paper.positions) | {"IHSG"}:
            try:
                quotes[sym] = self.provider.quote(sym)
            except MarketDataError:
                pass
        acct = self.paper.account({s: q.price for s, q in quotes.items()})
        ref = self._reference("idx", today, window)
        money = lambda x: f"Rp{rupiah(x)}"  # noqa: E731
        lines = ["<b>📈 Saham IDX — akun simulasi</b>", self._equity_line(acct["equity"], ref, money)]
        if window.period in ("monthly", "quarterly", "semiannual", "yearly"):
            rng = self._range_line("idx", today, window, acct["equity"], money)
            if rng:
                lines.append(rng)
        if window.period in ("quarterly", "semiannual", "yearly"):
            months = self._monthly_returns("idx", today, window, acct["equity"])
            if months:
                lines.append(months)
        if save:
            self._save_snapshot("idx", today, acct["equity"])
        lines += [f"Kas: Rp{rupiah(acct['cash'])} · Nilai saham: Rp{rupiah(acct['market_value'])}",
                  f"Total P/L: {signed_rp(acct['total_pl'])} ({_pct(acct['total_pl_pct'])})"]
        positions = sorted(acct["positions"], key=lambda p: -p["market_value"])
        moves = {}
        if positions:
            if window.cutoff is None:
                day_pl = sum(p["shares"] * quotes[p["symbol"]].change for p in positions if p["symbol"] in quotes)
                lines.append(f"Posisi ({len(positions)}) · pergerakan hari ini {signed_rp(day_pl)}:")
            else:
                lines.append(f"Posisi ({len(positions)}) · perubahan harga {window.label}:")
            for p in positions[:MAX_POSITIONS]:
                q = quotes.get(p["symbol"])
                if window.cutoff is None:
                    chg = q.change_pct if q else None
                else:
                    chg = self._change_since(self.provider, p["symbol"], p["last_price"], window.cutoff)
                    if chg is not None:
                        moves[p["symbol"]] = chg
                lines.append(f"• {html.escape(p['symbol'])} {p['lots']} lot · {rupiah(p['last_price'])}"
                             f"{f' ({_pct(chg)})' if chg is not None else ''}"
                             f" · P/L {signed_rp(p['unrealized_pl'])} ({_pct(p['unrealized_pl_pct'])})")
            if len(positions) > MAX_POSITIONS:
                lines.append(f"… dan {len(positions) - MAX_POSITIONS} posisi lainnya")
            if len(moves) >= 2:
                best, worst = max(moves, key=moves.get), min(moves, key=moves.get)
                lines.append(f"Terbaik: {best} {_pct(moves[best])} · Terburuk: {worst} {_pct(moves[worst])}")
        else:
            lines.append("Posisi: tidak ada")
        start = window.start.isoformat()
        filled = [o for o in self.paper.orders() if o.status == OrderStatus.FILLED
                  and start <= datetime.fromtimestamp(o.filled_at or o.created_at, WIB).strftime("%Y-%m-%d") <= today]
        if filled:
            buys = sum(1 for o in filled if o.side.value == "BUY")
            value = sum(o.lots * LOT_SIZE * o.fill_price for o in filled)
            lines.append(f"Transaksi {window.label}: {buys} beli, {len(filled) - buys} jual · nilai Rp{rupiah(value)}")
        else:
            lines.append(f"Transaksi {window.label}: tidak ada")
        if "IHSG" in quotes:
            q = quotes["IHSG"]
            if window.cutoff is None:
                lines.append(f"IHSG: {index_value(q.price)} ({_pct(q.change_pct)})")
            else:
                chg = self._change_since(self.provider, "IHSG", q.price, window.cutoff)
                lines.append(f"IHSG: {index_value(q.price)}" +
                             (f" ({_pct(chg)} {window.move_label})" if chg is not None else ""))
        return lines

    def _crypto_section(self, now: datetime, window: Window, save: bool) -> list[str]:
        from .crypto import price_str, qty_str, usdt
        provider, brokers = self.crypto.get("provider"), self.crypto.get("brokers") or {}
        paper = brokers.get("paper")
        if provider is None or paper is None:
            return []
        try:
            prices = provider.all_prices()
        except MarketDataError:
            prices = {}
        acct = paper.account(prices)
        if not acct["positions"] and not paper.orders():
            return []  # akun crypto belum pernah dipakai: tidak perlu dilaporkan
        today = now.strftime("%Y-%m-%d")
        ref = self._reference("crypto", today, window)
        lines = ["<b>🪙 Crypto — akun simulasi (USDT)</b>", self._equity_line(acct["equity"], ref, usdt, " USDT")]
        if window.period in ("monthly", "quarterly", "semiannual", "yearly"):
            rng = self._range_line("crypto", today, window, acct["equity"], usdt, " USDT")
            if rng:
                lines.append(rng)
        if window.period in ("quarterly", "semiannual", "yearly"):
            months = self._monthly_returns("crypto", today, window, acct["equity"])
            if months:
                lines.append(months)
        if save:
            self._save_snapshot("crypto", today, acct["equity"])
        lines.append(f"Saldo USDT: {usdt(acct['cash'])} · Total P/L: {'+' if acct['total_pl'] >= 0 else '-'}"
                     f"{usdt(abs(acct['total_pl']))} ({_pct(acct['total_pl_pct'])})")
        positions = sorted(acct["positions"], key=lambda p: -p["market_value"])
        short = {"weekly": "7h", "monthly": "bln", "quarterly": "kw", "semiannual": "smt", "yearly": "thn"}
        for p in positions[:MAX_POSITIONS]:
            if window.cutoff is None:
                try:
                    chg, label = provider.quote(p["symbol"]).change_pct, "24j"
                except MarketDataError:
                    chg, label = None, ""
            else:
                chg = self._change_since(provider, p["symbol"], p["last_price"], window.cutoff, crypto=True)
                label = short[window.period]
            lines.append(f"• {html.escape(p['asset'])} {qty_str(p['quantity']).replace('.', ',')} · "
                         f"{price_str(p['last_price'])}{f' ({_pct(chg)} {label})' if chg is not None else ''} · P/L "
                         f"{'+' if p['unrealized_pl'] >= 0 else '-'}{usdt(abs(p['unrealized_pl']))} ({_pct(p['unrealized_pl_pct'])})")
        if len(positions) > MAX_POSITIONS:
            lines.append(f"… dan {len(positions) - MAX_POSITIONS} aset lainnya")
        return lines

    def build(self, save: bool = False, period: str = "daily") -> str:
        now = self._now()
        window = Window.of(period, now.date())
        sections = [self._stock_section(now, window, save)]
        if self.watcher.config.report_crypto:
            sections.append(self._crypto_section(now, window, save))
        made = f"{DAYS[now.weekday()]} {now:%H:%M} WIB"
        if window.period == "daily":
            head = f"📊 <b>Laporan portofolio — {DAYS[now.weekday()]}, {now:%d/%m/%Y} {now:%H:%M} WIB</b>"
        elif window.period == "weekly":
            head = (f"📅 <b>Laporan mingguan portofolio — {window.start:%d/%m} s/d {now:%d/%m/%Y}</b>\n"
                    f"<i>Dibuat {made}</i>")
        elif window.period == "quarterly":
            q = (now.month - 1) // 3 + 1
            head = (f"🧾 <b>Laporan kuartalan portofolio — Q{q} {now.year}</b> "
                    f"({MONTHS_SHORT[window.start.month - 1]}–{MONTHS_SHORT[window.start.month + 1]})\n"
                    f"<i>{window.start:%d/%m} s/d {now:%d/%m/%Y} · dibanding akhir kuartal lalu ({window.cutoff:%d/%m/%Y}) · "
                    f"dibuat {made}</i>")
        elif window.period == "semiannual":
            half = 1 if now.month <= 6 else 2
            head = (f"📘 <b>Laporan semesteran portofolio — Semester {half} {now.year}</b> "
                    f"({'Jan–Jun' if half == 1 else 'Jul–Des'})\n"
                    f"<i>{window.start:%d/%m} s/d {now:%d/%m/%Y} · dibanding akhir semester lalu "
                    f"({window.cutoff:%d/%m/%Y}) · dibuat {made}</i>")
        elif window.period == "yearly":
            head = (f"🎆 <b>Laporan tahunan portofolio — {now.year}</b>\n"
                    f"<i>{window.start:%d/%m} s/d {now:%d/%m/%Y} · dibanding akhir tahun lalu ({window.cutoff:%d/%m/%Y}) · "
                    f"dibuat {made}</i>")
        else:
            head = (f"🗓️ <b>Laporan bulanan portofolio — {MONTHS[now.month - 1]} {now.year}</b>\n"
                    f"<i>{window.start:%d/%m} s/d {now:%d/%m/%Y} · dibanding akhir bulan lalu ({window.cutoff:%d/%m}) · "
                    f"dibuat {made}</i>")
        body = ["\n".join(s) for s in sections if s]
        return "\n\n".join([head] + body + ["<i>Akun simulasi. Bukan rekomendasi investasi.</i>"])

    def build_weekly(self, save: bool = False) -> str:
        return self.build(save, "weekly")

    # ---- pengiriman ----------------------------------------------------
    def send_now(self, period: str = "daily") -> dict:
        """Kirim laporan sekarang (dari tombol atau jadwal) dan simpan snapshot ekuitas hari ini."""
        with self._lock:
            ok, err = self.watcher._send(self.build(save=True, period=period))
            name = PERIOD_NAME[period]
            if not ok:
                return self.watcher._record("ERROR", "-", f"Laporan {name} gagal dikirim: {err}")
            today = self._now().strftime("%Y-%m-%d")
            setattr(self, LAST_SENT_ATTR[period], today)
            self._save_state()
            return self.watcher._record("REPORT", "-", f"Laporan {name} portofolio terkirim", sent=True)

    def _ready(self) -> bool:
        return bool(self.watcher.token and self.watcher.chat_id)

    def due(self) -> bool:
        cfg, now = self.watcher.config, self._now()
        if not cfg.report_enabled or not self._ready():
            return False
        if cfg.report_weekdays_only and now.weekday() >= 5:
            return False
        if self.last_sent == now.strftime("%Y-%m-%d"):
            return False
        return now.strftime("%H:%M") >= cfg.report_time  # terlambat (mis. server baru nyala) tetap dikirim hari itu

    def weekly_due(self) -> bool:
        cfg, now = self.watcher.config, self._now()
        if not cfg.weekly_enabled or not self._ready() or now.weekday() != cfg.weekly_day:
            return False
        if self.last_weekly_sent == now.strftime("%Y-%m-%d"):
            return False
        return now.strftime("%H:%M") >= cfg.weekly_time

    def monthly_due(self) -> bool:
        cfg, now = self.watcher.config, self._now()
        if not cfg.monthly_enabled or not self._ready():
            return False
        on_day = is_last_day_of_month(now.date()) if cfg.monthly_day == 0 else now.day == cfg.monthly_day
        if not on_day or self.last_monthly_sent == now.strftime("%Y-%m-%d"):
            return False
        return now.strftime("%H:%M") >= cfg.monthly_time

    def quarterly_due(self) -> bool:
        cfg, now = self.watcher.config, self._now()
        if not cfg.quarterly_enabled or not self._ready():
            return False
        if now.month not in QUARTER_END_MONTHS or not is_last_day_of_month(now.date()):
            return False
        if self.last_quarterly_sent == now.strftime("%Y-%m-%d"):
            return False
        return now.strftime("%H:%M") >= cfg.quarterly_time

    def semiannual_due(self) -> bool:
        cfg, now = self.watcher.config, self._now()
        if not cfg.semiannual_enabled or not self._ready() or (now.month, now.day) not in SEMESTER_ENDS:
            return False
        if self.last_semiannual_sent == now.strftime("%Y-%m-%d"):
            return False
        return now.strftime("%H:%M") >= cfg.semiannual_time

    def yearly_due(self) -> bool:
        cfg, now = self.watcher.config, self._now()
        if not cfg.yearly_enabled or not self._ready() or (now.month, now.day) != (12, 31):
            return False
        if self.last_yearly_sent == now.strftime("%Y-%m-%d"):
            return False
        return now.strftime("%H:%M") >= cfg.yearly_time

    def maybe_snapshot(self) -> bool:
        """Catat ekuitas sekali sehari setelah bursa tutup, walau laporan harian tidak aktif."""
        now = self._now()
        today = now.strftime("%Y-%m-%d")
        if now.strftime("%H:%M") < SNAPSHOT_TIME or today in self._snapshots().get("idx", {}):
            return False
        self.build(save=True)  # menghitung ekuitas akun & menyimpan snapshot (tanpa mengirim)
        return True

    def run_due(self) -> list[dict]:
        out = []
        if self.due():
            out.append(self.send_now("daily"))
        if self.weekly_due():
            out.append(self.send_now("weekly"))
        if self.monthly_due():
            out.append(self.send_now("monthly"))
        if self.quarterly_due():
            out.append(self.send_now("quarterly"))
        if self.semiannual_due():
            out.append(self.send_now("semiannual"))
        if self.yearly_due():
            out.append(self.send_now("yearly"))
        self.maybe_snapshot()
        return out

    def next_run(self) -> str | None:
        cfg = self.watcher.config
        if not cfg.report_enabled:
            return None
        return f"{cfg.report_time} WIB" + (" (Senin–Jumat)" if cfg.report_weekdays_only else " (setiap hari)")

    def next_weekly(self) -> str | None:
        cfg = self.watcher.config
        return f"{DAYS[cfg.weekly_day]} {cfg.weekly_time} WIB" if cfg.weekly_enabled else None

    def next_monthly(self) -> str | None:
        cfg = self.watcher.config
        if not cfg.monthly_enabled:
            return None
        day = "hari terakhir tiap bulan" if cfg.monthly_day == 0 else f"tanggal {cfg.monthly_day} tiap bulan"
        return f"{day} {cfg.monthly_time} WIB"

    def next_quarterly(self) -> str | None:
        cfg = self.watcher.config
        return (f"hari terakhir tiap kuartal (31 Mar, 30 Jun, 30 Sep, 31 Des) {cfg.quarterly_time} WIB"
                if cfg.quarterly_enabled else None)

    def next_semiannual(self) -> str | None:
        cfg = self.watcher.config
        return f"30 Juni & 31 Desember {cfg.semiannual_time} WIB" if cfg.semiannual_enabled else None

    def next_yearly(self) -> str | None:
        cfg = self.watcher.config
        return f"31 Desember {cfg.yearly_time} WIB" if cfg.yearly_enabled else None

    # ---- loop latar belakang -------------------------------------------
    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="daily-report", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(30):
            try:
                self.run_due()
            except Exception as exc:  # loop tidak boleh mati
                self.watcher._record("ERROR", "-", f"Laporan portofolio gagal dibuat: {exc}")
