"""Adapter broker sungguhan (Stockbit, Pluang, dll).

PENTING — kenapa adapter ini belum mengirim order:
Stockbit (Stockbit Sekuritas) dan Pluang TIDAK menyediakan API trading publik
resmi untuk nasabah ritel. Mengotomasi akun lewat API internal aplikasi mereka
(hasil reverse-engineering / scraping / menyimpan password & PIN trading)
melanggar Syarat & Ketentuan, bisa membuat akun dibekukan, dan berisiko bagi
keamanan dana Anda. Karena itu adapter di sini sengaja hanya berupa kerangka.

Cara mengaktifkan bila Anda MEMILIKI akses API resmi (mis. program partner /
API institusional dari sekuritas Anda):
  1. Buat subclass ExternalBroker, isi `_connect`, `account`, `place_order`,
     `orders`, `cancel_order` sesuai dokumentasi resmi broker.
  2. Simpan kredensial di .env (jangan di-commit).
  3. Set ENABLE_LIVE_TRADING=true. Tanpa flag ini, order live selalu ditolak.

Sementara itu, gunakan tombol "Buka di Stockbit/Pluang" di UI untuk eksekusi
manual di aplikasi resmi, dan PaperBroker untuk menguji strategi.
"""
from .base import Broker, BrokerNotAvailable, Order


class ExternalBroker(Broker):
    is_live = True
    official_api = False
    app_url = ""
    note = ""

    def status(self) -> dict:
        s = super().status()
        s.update(available=False, note=self.note, app_url=self.app_url)
        return s

    def _unavailable(self):
        raise BrokerNotAvailable(f"{self.display_name}: {self.note}")

    def account(self, prices: dict[str, float]) -> dict:
        self._unavailable()

    def place_order(self, order: Order, market_price: float) -> Order:
        self._unavailable()

    def orders(self) -> list[Order]:
        self._unavailable()

    def cancel_order(self, order_id: str) -> Order:
        self._unavailable()


class StockbitBroker(ExternalBroker):
    name = "stockbit"
    display_name = "Stockbit Sekuritas"
    app_url = "https://stockbit.com/symbol/{symbol}"
    note = ("Belum ada API trading publik resmi. Eksekusi manual lewat aplikasi Stockbit, "
            "atau implementasikan adapter ini jika Anda punya akses API resmi.")


class PluangBroker(ExternalBroker):
    name = "pluang"
    display_name = "Pluang"
    app_url = "https://pluang.com"
    note = ("Belum ada API trading publik resmi. Eksekusi manual lewat aplikasi Pluang, "
            "atau implementasikan adapter ini jika Anda punya akses API resmi.")
