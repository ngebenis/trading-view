# IDX Trading View

Aplikasi web untuk **membaca pasar saham Indonesia (IDX)** dan **melakukan aksi beli/jual**:

- 📈 Grafik candlestick **Lightweight Charts** dengan EMA, volume & **penanda transaksi** (akun simulasi / backtest), plus widget **TradingView** (`IDX:<KODE>`)
- 💹 Harga & histori dari Yahoo Finance (`<KODE>.JK`), atau data demo offline
- 📉 Pantau **IHSG** & **LQ45**: ticker IHSG di header, grafik, sinyal teknikal & notifikasi Telegram
- 📑 **Analisis fundamental** dari laporan keuangan resmi IDX (XBRL): PER, PBV, ROE, DER, pertumbuhan laba
- 🧠 Sinyal teknikal otomatis (RSI, EMA 12/26, MACD, Bollinger Band) → BELI / JUAL / TAHAN
- 🛒 Order **Market** & **Limit** dengan aturan IDX: 1 lot = 100 lembar, fraksi harga, fee beli/jual
- 💼 Portofolio, P/L, riwayat order, pembatalan order
- 🤖 **Auto-trading berbasis sinyal** (khusus akun simulasi) dengan stop-loss, take-profit, cooldown & log keputusan
- 📡 **Webhook alert TradingView**: alert dari strategi/indikator TradingView → log, Telegram, atau order simulasi
- 🔔 **Notifikasi Telegram** saat muncul sinyal BELI/JUAL baru (dan saat bot auto-trading bertransaksi)
- 📊 **Backtest** strategi auto-trading dengan data historis: return, CAGR, drawdown, Sharpe, win rate, beta, vs **IHSG** & vs beli & tahan
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

## 📈 Grafik dengan penanda transaksi

Grafik utama memakai [Lightweight Charts](https://github.com/tradingview/lightweight-charts)
(open-source dari TradingView, disertakan di `app/static/vendor/` sehingga tidak butuh CDN):
candlestick harian, EMA 12/26, volume, dan **penanda transaksi**:

- **Penanda: akun simulasi** — panah ▲ **B** (beli) / ▼ **S** (jual) dengan jumlah lot untuk setiap
  order terisi di saham itu (manual, 🤖 bot, 📡 webhook; beberapa order di hari yang sama digabung),
  plus garis putus-putus harga rata-rata posisi yang masih dipegang.
- **Penanda: backtest terakhir** — titik masuk/keluar dari hasil backtest beserta hasil per transaksi.
  Di tab **Backtest**, klik **Lihat di grafik** pada tabel per saham.

Arahkan kursor ke candle untuk melihat OHLC, volume, EMA, dan rincian transaksi di hari itu. Pilih
rentang 3 bulan – 5 tahun. Tombol **TradingView** di atas grafik beralih ke widget TradingView
(indikator lengkap, tanpa penanda). Pilihan tampilan & rentang diingat di browser.

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

## 📑 Analisis fundamental (laporan keuangan IDX)

Tab **Fundamental** menampilkan rasio dan angka laporan keuangan saham yang sedang dibuka,
diambil dari file XBRL resmi yang dipublikasikan IDX (`instance.zip`).

**Rasio:** PER, PBV, ROE, ROA, DER, current ratio, net & gross margin, pertumbuhan pendapatan
dan laba (vs periode yang sama tahun lalu), EPS, nilai buku per saham, kapitalisasi pasar.
Laba rugi di laporan IDX bersifat kumulatif sejak awal tahun (YTD), jadi PER/ROE/ROA memakai laba
yang disetahunkan (Q1 ×4, Q2 ×2, Q3 ×4/3). Jumlah saham diambil dari laporan atau dihitung dari
laba ÷ EPS. Emiten yang melapor dalam USD dikonversi dengan `USD_IDR_RATE`.

**Cara mendapatkan laporan** (situs IDX memakai verifikasi Cloudflare, jadi pengunduhan selalu
lewat browser Anda):

1. **Satu per satu:** di tab Fundamental, klik link periode (mis. "Q2 2026 ↗") → browser mengunduh
   `instance.zip` → klik **Upload laporan**. Tahun & periode dibaca otomatis dari isi file.
2. **Banyak saham sekaligus:** skrip pengunduh (diadaptasi dari
   [idx-financial-scraper](https://github.com/septianbyk/idx-financial-scraper)):
   ```bash
   pip install -r requirements-scraper.txt
   python scripts/fetch_idx_reports.py BBCA TLKM ASII --start-year 2024
   python scripts/fetch_idx_reports.py --watchlist watchlist.csv --periods FY
   ```
   Skrip membuka Chrome dengan profil terpisah; bila IDX meminta verifikasi, selesaikan di jendela
   Chrome lalu tekan Enter di terminal. Unduhan diberi jeda (default 2 detik), file yang sudah ada
   dilewati, dan setiap file dicek apakah benar laporan XBRL. Jalan di Windows, macOS & Linux.
3. **Sudah memakai idx-financial-scraper?** Arahkan `FUNDAMENTALS_XBRL_DIR` ke folder `data/XBRL`
   miliknya — tata letak foldernya sama (`<tahun>/<periode>/<KODE>_<tahun>_<periode>.xbrl`).

Pemetaan akun → tag XBRL ada di `app/fundamentals_taxonomy.csv` (urutan baris = prioritas) dan
bisa ditambah sendiri. Jenis laporan (umum / bank / asuransi) dideteksi otomatis dari isinya.
Dibanding versi aslinya, parser ini memilih angka total (bukan per segmen) dan periode YTD
berdasarkan tanggal, ikut memproses laporan tahunan, serta mengambil angka tahun lalu dari file
yang sama untuk menghitung pertumbuhan.

Gunakan sesuai ketentuan situs IDX: untuk riset pribadi, bukan untuk didistribusikan ulang.

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

## 📡 Webhook alert TradingView

Alert TradingView (fitur webhook butuh paket TradingView berbayar) bisa diteruskan ke aplikasi ini:
dicatat, dikirim ke Telegram, atau sekaligus dieksekusi sebagai **order simulasi**.

1. Buka aplikasi ke internet dengan tunnel, mis. `ngrok http 8000` atau
   `cloudflared tunnel --url http://localhost:8000`.
2. Di TradingView, buat alert → **Notifications** → centang **Webhook URL** →
   `https://<alamat-tunnel>/api/webhooks/tradingview`.
3. Salin **template pesan** dari tab **Webhook** ke kolom **Message** alert, mis. untuk strategi Pine Script:
   ```json
   {"secret": "<kode rahasia>", "symbol": "{{ticker}}", "action": "{{strategy.order.action}}",
    "price": {{close}}, "message": "{{strategy.order.comment}}"}
   ```
4. Di tab **Webhook**, pilih aksi (catat / Telegram / Telegram + order simulasi) lalu klik **Aktifkan**.
   Tombol **Kirim alert uji** mensimulasikan alert (tanpa membuat order).

Field pesan: `symbol` (wajib; `IDX:BBCA`, `BBCA.JK` juga diterima), `action` (`buy`/`sell`, juga
`long`/`short`/`exit`; kosong = alert informasi), `price`, `message` — opsional: `lots` dan
`mode` (`log`/`notify`/`order`, menimpa pengaturan untuk alert itu).

Aturan order: hanya akun Paper Trading; harga = harga pasar terakhir (harga dari alert bila data pasar
gagal); BELI tanpa `lots` memakai % ekuitas; dibatasi *maks. lot per alert* dan `MAX_POSITION_PCT`;
JUAL menjual posisi yang ada (semua bila tanpa `lots`); indeks tidak dieksekusi. Alert identik
dalam 60 detik diabaikan. Order dari webhook ditandai **📡 webhook** di riwayat order.

**Keamanan.** TradingView tidak bisa mengirim header khusus, jadi alert diautentikasi dengan
kode rahasia di isi pesan (bisa diganti kapan saja di tab Webhook). Karena aplikasi dibuka lewat
tunnel, ada pengaman bawaan (`LOCAL_ONLY_GUARD=true`): request yang datang lewat tunnel/proxy
(membawa header seperti `X-Forwarded-For` / `Cf-Connecting-Ip`) **hanya** boleh ke
`/api/webhooks/tradingview` — UI dan API lain tetap hanya bisa dibuka dari komputer Anda.

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
  waktu berinvestasi, rincian per saham dan per transaksi.
- **Dua pembanding** di grafik ekuitas dan kartu metrik:
  - **IHSG** (`^JKSE`): bila modal yang sama diinvestasikan ke indeks pasar. Ditampilkan juga
    selisih return, drawdown IHSG, dan **beta** (1 = strategi bergerak seperti IHSG, 0 = tidak
    terpengaruh pasar). Bila data IHSG gagal diambil, backtest tetap jalan dengan catatan.
  - **Beli & tahan**: semua saham yang diuji dibeli bobot sama di awal periode (tanpa fee).
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
  webhooks.py        Penerima webhook alert TradingView
  fundamentals.py    Parser laporan keuangan XBRL IDX & rasio fundamental
  fundamentals_taxonomy.csv  Pemetaan akun → tag XBRL
  brokers/
    base.py          Kontrak Broker & model Order
    paper.py         Simulasi paper trading (tersimpan di data/paper_account.json)
    external.py      Kerangka adapter Stockbit & Pluang
  chart_data.py      Data grafik: candle, EMA & penanda transaksi
  static/            UI (HTML/CSS/JS, Lightweight Charts di static/vendor, widget TradingView)
scripts/
  fetch_idx_reports.py  Pengunduh laporan XBRL dari idx.co.id (opsional, butuh Playwright)
tests/               Unit & API test
```

## API

| Method | Endpoint | Keterangan |
|---|---|---|
| GET | `/api/quote/{kode}` | Harga terakhir |
| GET | `/api/candles/{kode}?range=6mo&interval=1d` | Data OHLCV |
| GET | `/api/analysis/{kode}` | Sinyal teknikal |
| GET | `/api/chart/{kode}?range=1y` | Candle, EMA 12/26 & penanda transaksi akun simulasi |
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
| GET | `/api/fundamentals/{kode}` | Laporan keuangan, rasio & link unduh IDX |
| POST | `/api/fundamentals/upload?ticker=KODE` | Upload `instance.zip` / `.xbrl` (body mentah) |
| POST | `/api/webhooks/tradingview` | Penerima alert TradingView (satu-satunya endpoint publik) |
| GET | `/api/webhooks` | Status, template pesan & log alert |
| PUT | `/api/webhooks/config` | Aktif/nonaktif, aksi, ukuran order, simbol yang diizinkan |
| POST | `/api/webhooks/regenerate-secret` | Ganti kode rahasia |
| POST | `/api/backtest` | Jalankan backtest (`symbols`, `period`, `initial_cash`, `execution`, `strategy`) |

> Disclaimer: sinyal dihasilkan otomatis dan bukan rekomendasi investasi. Data Yahoo untuk IDX
> tertunda ±10–15 menit. Gunakan dengan risiko sendiri.
