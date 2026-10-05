# IDX Trading View

Aplikasi web untuk **membaca pasar saham Indonesia (IDX)** dan **melakukan aksi beli/jual**:

- 📈 Grafik interaktif **TradingView** (`IDX:<KODE>`) dengan RSI, MACD, EMA
- 💹 Harga & histori dari Yahoo Finance (`<KODE>.JK`), atau data demo offline
- 📉 Pantau **IHSG** & **LQ45**: ticker IHSG di header, grafik, sinyal teknikal & notifikasi Telegram
- 🧠 Sinyal teknikal otomatis (RSI, EMA 12/26, MACD, Bollinger Band) → BELI / JUAL / TAHAN
- 🛒 Order **Market** & **Limit** dengan aturan IDX: 1 lot = 100 lembar, fraksi harga, fee beli/jual
- 💼 Portofolio, P/L, riwayat order, pembatalan order
- 🤖 **Auto-trading berbasis sinyal** (khusus akun simulasi) dengan stop-loss, take-profit, cooldown & log keputusan
- 🔔 **Notifikasi Telegram** saat muncul sinyal BELI/JUAL baru (dan saat bot auto-trading bertransaksi)
- 📊 **Backtest** strategi auto-trading dengan data historis: return, CAGR, drawdown, Sharpe, win rate, vs beli & tahan
- 🔌 Arsitektur **adapter broker**: Paper Trading (aktif), Stockbit & Pluang (lihat batasan di bawah)
- 🛡️ Pengaman: batas nilai order per % ekuitas, live trading mati secara default + konfirmasi per order

## Menjalankan

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # sesuaikan bila perlu
uvicorn app.main:app --reload
```

Buka http://localhost:8000. Tanpa internet, set `MARKET_DATA_PROVIDER=demo` di `.env`.

Test: `pytest`

## 📉 Indeks IHSG & LQ45

Ketik `IHSG` (atau `^JKSE`, `COMPOSITE`, `JCI`) / `LQ45` di kotak pencarian, atau klik ticker IHSG
di header. Indeks bisa dibuka di grafik TradingView (`IDX:COMPOSITE`, `IDX:LQ45`), dianalisis
sinyalnya, dimasukkan ke watchlist, dan dipantau lewat notifikasi Telegram.

Indeks **tidak bisa dibeli/dijual**, jadi form order dikunci, dan indeks ditolak di daftar
auto-trading maupun backtest.

| Indeks | Yahoo Finance | TradingView |
|---|---|---|
| IHSG | `^JKSE` | `IDX:COMPOSITE` |
| LQ45 | `^JKLQ45` | `IDX:LQ45` |

Indeks lain bisa ditambahkan di `INDICES` pada `app/idx_rules.py` (dan `app/static/app.js`).

## 🤖 Auto-trading (mode simulasi)

Buka tab **Auto-Trading** di bagian bawah, atur parameter, lalu klik **Mulai**.
Bot hanya bertransaksi di akun **Paper Trading** — tidak pernah ke broker sungguhan.

Setiap siklus (default 5 menit), untuk setiap simbol yang dipantau:

1. Order limit yang sudah tersentuh harganya dieksekusi.
2. **Keluar posisi** bila: rugi ≥ stop-loss %, untung ≥ take-profit %, atau skor sinyal ≤ batas jual
   (stop-loss/take-profit selalu jalan, sinyal jual menunggu cooldown).
3. **Buka posisi** bila skor sinyal ≥ batas beli, belum punya posisi/order di simbol itu, belum lewat
   maks. jumlah posisi, dan tidak sedang cooldown. Ukuran = `% ekuitas` (≤ `MAX_POSITION_PCT`),
   dibulatkan ke bawah ke lot penuh, sudah termasuk fee.

| Parameter | Default | Keterangan |
|---|---|---|
| Simbol | BBCA, BBRI, TLKM, ASII, BMRI | Saham yang dipantau |
| Interval | 300 detik | Jeda antar siklus (min. 30) |
| Ukuran posisi | 10% ekuitas | Per posisi baru |
| Maks. posisi | 5 | Jumlah saham yang boleh dipegang bersamaan |
| Beli bila skor ≥ / Jual bila skor ≤ | 2 / −2 | Lihat `app/strategy.py` |
| Stop-loss / Take-profit | 5% / 10% | 0 = nonaktif |
| Cooldown | 60 menit | Jeda per simbol setelah transaksi otomatis |
| Hanya saat jam bursa | mati | Bila aktif: Sen–Kam 09:00–12:00 & 13:30–15:49, Jum 09:00–11:30 & 14:00–15:49 WIB |

Status berjalan & pengaturan disimpan di `data/autotrader.json`, jadi bot otomatis lanjut
setelah server di-restart. Order dari bot ditandai **🤖 auto** di riwayat order.
Catatan: sinyal memakai candle harian, jadi biasanya hanya berubah sekali sehari;
interval pendek terutama berguna untuk memantau stop-loss/take-profit.

## 🔔 Notifikasi Telegram

1. Di Telegram, chat [@BotFather](https://t.me/BotFather) → `/newbot` → salin **token**.
2. Buka tab **Notifikasi**, tempel token, klik **Simpan pengaturan**.
3. Kirim pesan apa saja ke bot Anda (untuk grup: tambahkan bot ke grup lalu kirim pesan di sana).
4. Klik **Cari chat ID**, pilih chat Anda, lalu **Kirim pesan uji**.
5. Atur saham yang dipantau, lalu klik **Mulai**.

Token & chat ID juga bisa diisi lewat `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) — nilai `.env`
lebih diutamakan. Token yang diisi lewat UI disimpan lokal di `data/notifications.json` (tidak
di-commit) dan selalu disamarkan di API/UI.

Cara kerja:
- Setiap interval (default 15 menit) semua saham dipindai. Pesan dikirim saat sinyal **berubah**
  menjadi BELI (skor ≥ batas beli) atau JUAL (skor ≤ batas jual). Sinyal yang sama tidak dikirim
  ulang sampai mereda ke TAHAN lalu muncul lagi — jadi tidak spam. Status ini tersimpan, sehingga
  restart server tidak memicu pesan ganda.
- Bila pengiriman gagal (mis. internet putus), sinyal dicoba kirim lagi di pemindaian berikutnya.
- Opsional: setiap transaksi bot auto-trading (beli/jual, stop-loss, take-profit) ikut dikirim.
- Opsi "Hanya saat jam bursa" melewati pemindaian di luar jam perdagangan IDX.

Contoh pesan:

```
🟢 SINYAL BELI — AAMB
Harga: 450 (-1,75%)
Skor: +3
• RSI 56.6 netral
• EMA12 golden cross EMA26
• Histogram MACD berbalik positif
TradingView · Stockbit
Sinyal otomatis, bukan rekomendasi investasi.
```

## 📊 Backtest

Buka tab **Backtest**, pilih saham, periode (6 bulan – 5 tahun), modal awal, dan parameter strategi
(atau klik **Salin pengaturan auto-trading**), lalu **Jalankan backtest**.

Backtest memutar ulang **kode auto-trader yang sama** hari demi hari di akun simulasi terpisah
(akun Paper Trading Anda tidak tersentuh), sehingga ukuran posisi, lot, fraksi harga, fee,
stop-loss/take-profit, cooldown, dan maks. posisi diperlakukan persis seperti bot live.

- **Tanpa look-ahead:** sinyal dihitung dari candle s.d. penutupan hari *d*; order dieksekusi
  di harga **open hari berikutnya** (default). Opsi "close hari sinyal" tersedia tapi lebih optimistis.
- Data diambil lebih panjang dari periode agar indikator sudah siap di hari pertama.
- **Hasil:** total return, CAGR, max drawdown, Sharpe, win rate, profit factor, fee, persentase
  waktu berinvestasi, grafik ekuitas vs **beli & tahan** (bobot sama, tanpa fee), rincian per saham
  dan per transaksi.
- Jika hasilnya cocok, klik **Terapkan ke auto-trading** untuk memakai simbol & parameter yang sama.

Batasan: memakai candle harian, mengabaikan slippage, likuiditas/volume, ARA/ARB, dan antrean order.
Hasil masa lalu tidak menjamin hasil masa depan. Waktu proses ±1–2 detik per saham untuk 2 tahun data.

## ⚠️ Tentang eksekusi order di Stockbit / Pluang

Stockbit dan Pluang **tidak menyediakan API trading publik resmi** untuk nasabah ritel.
Mengotomasi akun lewat API internal aplikasi (reverse-engineering, scraping, atau
menyimpan password/PIN trading di aplikasi pihak ketiga) melanggar Syarat & Ketentuan
mereka, berisiko akun dibekukan, dan membahayakan keamanan dana Anda. Karena itu:

- **Paper Trading** (default) mensimulasikan beli/jual secara penuh — cocok untuk menguji strategi.
- Untuk **Stockbit/Pluang**, aplikasi menampilkan analisis + tombol "Buka di Stockbit/Pluang"
  agar Anda mengeksekusi order secara manual di aplikasi resmi.
- Jika Anda mendapatkan **akses API resmi** (mis. program partner/API institusional dari sekuritas),
  implementasikan method di `app/brokers/external.py` (`account`, `place_order`, `orders`,
  `cancel_order`), simpan kredensial di `.env`, lalu set `ENABLE_LIVE_TRADING=true`.
  Broker lain yang punya API resmi bisa ditambahkan dengan subclass `Broker` yang sama.

## Struktur

```
app/
  main.py            API FastAPI + penyajian UI
  config.py          Pengaturan dari .env
  idx_rules.py       Lot, fraksi harga, normalisasi kode saham
  market_data.py     Yahoo Finance & data demo
  indicators.py      SMA, EMA, RSI, MACD, Bollinger
  strategy.py        Skor & sinyal BELI/JUAL/TAHAN
  autotrader.py      Bot auto-trading (simulasi)
  backtest.py        Backtest bot dengan data historis
  notifier.py        Notifikasi Telegram & pemantau sinyal
  brokers/
    base.py          Kontrak Broker & model Order
    paper.py         Simulasi paper trading (tersimpan di data/paper_account.json)
    external.py      Kerangka adapter Stockbit & Pluang
  static/            UI (HTML/CSS/JS + widget TradingView)
tests/               Unit & API test
```

## API

| Method | Endpoint | Keterangan |
|---|---|---|
| GET | `/api/quote/{kode}` | Harga terakhir |
| GET | `/api/candles/{kode}?range=6mo&interval=1d` | Data OHLCV |
| GET | `/api/analysis/{kode}` | Sinyal teknikal |
| GET | `/api/brokers` | Daftar broker & statusnya |
| GET | `/api/account?broker=paper` | Saldo, posisi, P/L |
| GET/POST | `/api/orders` | Riwayat / kirim order |
| DELETE | `/api/orders/{id}` | Batalkan order OPEN |
| POST | `/api/paper/reset` | Reset akun simulasi |
| GET | `/api/autotrader` | Status, pengaturan & log bot |
| PUT | `/api/autotrader/config` | Ubah pengaturan bot |
| POST | `/api/autotrader/start` · `/stop` · `/run-once` | Kendalikan bot |
| GET | `/api/notifications` | Status, pengaturan & riwayat notifikasi |
| PUT | `/api/notifications/config` | Ubah pengaturan (token, chat ID, saham, ambang skor) |
| GET | `/api/notifications/chats` | Cari chat ID dari pesan terbaru ke bot |
| POST | `/api/notifications/test` · `/start` · `/stop` · `/run-once` | Pesan uji & kendali pemantau |
| POST | `/api/backtest` | Jalankan backtest (`symbols`, `period`, `initial_cash`, `execution`, `strategy`) |

> Disclaimer: sinyal dihasilkan otomatis dan bukan rekomendasi investasi. Data Yahoo untuk IDX
> tertunda ±10–15 menit. Gunakan dengan risiko sendiri.
