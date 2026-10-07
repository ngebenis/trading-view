"""Laporan harian portofolio ke Telegram: akun simulasi saham IDX (dan crypto) sekali sehari.

Dikirim otomatis pada jam `report_time` (WIB, bawaan 16:15 — setelah bursa tutup), memakai bot &
pengaturan di tab Notifikasi. Perubahan ekuitas "hari ini" dihitung dari snapshot ekuitas yang
disimpan setiap kali laporan dibuat (setting `report_snapshots`), jadi laporan pertama belum punya
pembanding.
"""
import html
import threading
import time
from datetime import datetime

from .autotrader import WIB, rupiah
from .brokers import OrderStatus
from .idx_rules import LOT_SIZE
from .market_data import MarketDataError
from .notifier import _pct, index_value

MAX_POSITIONS = 15
KEEP_SNAPSHOTS = 60
DAYS = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]


def signed_rp(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}Rp{rupiah(abs(x))}"


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

    # ---- snapshot ekuitas ----------------------------------------------
    def _snapshots(self) -> dict:
        return (self.db.get_setting("report_snapshots", {}) if self.db is not None else {}) or {}

    def _change_since_yesterday(self, account: str, equity: float, today: str, save: bool) -> float | None:
        snaps = self._snapshots()
        series = snaps.setdefault(account, {})
        before = [d for d in series if d < today]
        prev = series[max(before)] if before else None
        if save and self.db is not None:
            series[today] = equity
            snaps[account] = {d: series[d] for d in sorted(series)[-KEEP_SNAPSHOTS:]}
            self.db.set_setting("report_snapshots", snaps)
        return None if prev is None else equity - prev

    # ---- isi laporan ---------------------------------------------------
    def _stock_section(self, today: str, save: bool) -> list[str]:
        quotes = {}
        for sym in set(self.paper.positions) | {"IHSG"}:
            try:
                quotes[sym] = self.provider.quote(sym)
            except MarketDataError:
                pass
        acct = self.paper.account({s: q.price for s, q in quotes.items()})
        change = self._change_since_yesterday("idx", acct["equity"], today, save)
        lines = ["<b>📈 Saham IDX — akun simulasi</b>",
                 f"Ekuitas: <b>Rp{rupiah(acct['equity'])}</b>" +
                 (f" ({signed_rp(change)} / {_pct(change / (acct['equity'] - change) * 100)} sejak laporan kemarin)"
                  if change is not None and acct["equity"] != change else ""),
                 f"Kas: Rp{rupiah(acct['cash'])} · Nilai saham: Rp{rupiah(acct['market_value'])}",
                 f"Total P/L: {signed_rp(acct['total_pl'])} ({_pct(acct['total_pl_pct'])})"]
        positions = sorted(acct["positions"], key=lambda p: -p["market_value"])
        if positions:
            day_pl = sum(p["shares"] * quotes[p["symbol"]].change for p in positions if p["symbol"] in quotes)
            lines.append(f"Posisi ({len(positions)}) · pergerakan hari ini {signed_rp(day_pl)}:")
            for p in positions[:MAX_POSITIONS]:
                q = quotes.get(p["symbol"])
                today_chg = f" ({_pct(q.change_pct)})" if q else ""
                lines.append(f"• {html.escape(p['symbol'])} {p['lots']} lot · {rupiah(p['last_price'])}{today_chg}"
                             f" · P/L {signed_rp(p['unrealized_pl'])} ({_pct(p['unrealized_pl_pct'])})")
            if len(positions) > MAX_POSITIONS:
                lines.append(f"… dan {len(positions) - MAX_POSITIONS} posisi lainnya")
        else:
            lines.append("Posisi: tidak ada")
        filled = [o for o in self.paper.orders() if o.status == OrderStatus.FILLED
                  and datetime.fromtimestamp(o.filled_at or o.created_at, WIB).strftime("%Y-%m-%d") == today]
        if filled:
            buys = sum(1 for o in filled if o.side.value == "BUY")
            value = sum(o.lots * LOT_SIZE * o.fill_price for o in filled)
            lines.append(f"Transaksi hari ini: {buys} beli, {len(filled) - buys} jual · nilai Rp{rupiah(value)}")
        else:
            lines.append("Transaksi hari ini: tidak ada")
        if "IHSG" in quotes:
            q = quotes["IHSG"]
            lines.append(f"IHSG: {index_value(q.price)} ({_pct(q.change_pct)})")
        return lines

    def _crypto_section(self, today: str, save: bool) -> list[str]:
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
        change = self._change_since_yesterday("crypto", acct["equity"], today, save)
        lines = ["<b>🪙 Crypto — akun simulasi (USDT)</b>",
                 f"Ekuitas: <b>{usdt(acct['equity'])} USDT</b>" +
                 (f" ({'+' if change >= 0 else '-'}{usdt(abs(change))} / {_pct(change / (acct['equity'] - change) * 100)}"
                  " sejak laporan kemarin)" if change is not None and acct["equity"] != change else ""),
                 f"Saldo USDT: {usdt(acct['cash'])} · Total P/L: {'+' if acct['total_pl'] >= 0 else '-'}"
                 f"{usdt(abs(acct['total_pl']))} ({_pct(acct['total_pl_pct'])})"]
        positions = sorted(acct["positions"], key=lambda p: -p["market_value"])
        for p in positions[:MAX_POSITIONS]:
            try:
                q = provider.quote(p["symbol"])
                chg = f" ({_pct(q.change_pct)} 24j)"
            except MarketDataError:
                chg = ""
            lines.append(f"• {html.escape(p['asset'])} {qty_str(p['quantity']).replace('.', ',')} · "
                         f"{price_str(p['last_price'])}{chg} · P/L {'+' if p['unrealized_pl'] >= 0 else '-'}"
                         f"{usdt(abs(p['unrealized_pl']))} ({_pct(p['unrealized_pl_pct'])})")
        if len(positions) > MAX_POSITIONS:
            lines.append(f"… dan {len(positions) - MAX_POSITIONS} aset lainnya")
        return lines

    def build(self, save: bool = False) -> str:
        now = datetime.fromtimestamp(self.clock(), WIB)
        today = now.strftime("%Y-%m-%d")
        sections = [self._stock_section(today, save)]
        if self.watcher.config.report_crypto:
            sections.append(self._crypto_section(today, save))
        head = [f"📊 <b>Laporan portofolio — {DAYS[now.weekday()]}, {now:%d/%m/%Y} {now:%H:%M} WIB</b>"]
        body = ["\n".join(s) for s in sections if s]
        return "\n\n".join(head + body + ["<i>Akun simulasi. Bukan rekomendasi investasi.</i>"])

    # ---- pengiriman ----------------------------------------------------
    def send_now(self) -> dict:
        """Kirim laporan sekarang (dari tombol atau jadwal) dan simpan snapshot ekuitas hari ini."""
        with self._lock:
            ok, err = self.watcher._send(self.build(save=True))
            if not ok:
                return self.watcher._record("ERROR", "-", f"Laporan harian gagal dikirim: {err}")
            self.last_sent = datetime.fromtimestamp(self.clock(), WIB).strftime("%Y-%m-%d")
            if self.db is not None:
                self.db.set_setting("daily_report", {"last_sent": self.last_sent})
            return self.watcher._record("REPORT", "-", "Laporan harian portofolio terkirim", sent=True)

    def due(self) -> bool:
        cfg = self.watcher.config
        if not cfg.report_enabled or not (self.watcher.token and self.watcher.chat_id):
            return False
        now = datetime.fromtimestamp(self.clock(), WIB)
        if cfg.report_weekdays_only and now.weekday() >= 5:
            return False
        if self.last_sent == now.strftime("%Y-%m-%d"):
            return False
        return now.strftime("%H:%M") >= cfg.report_time  # terlambat (mis. server baru nyala) tetap dikirim hari itu

    def run_due(self) -> dict | None:
        return self.send_now() if self.due() else None

    def next_run(self) -> str | None:
        cfg = self.watcher.config
        if not cfg.report_enabled:
            return None
        return f"{cfg.report_time} WIB" + (" (Senin–Jumat)" if cfg.report_weekdays_only else " (setiap hari)")

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
                self.watcher._record("ERROR", "-", f"Laporan harian gagal dibuat: {exc}")
