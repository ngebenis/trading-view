# IDX Trading View

Aplikasi web untuk **membaca pasar saham Indonesia (IDX)** dan **melakukan aksi beli/jual**:

- 📈 Grafik candlestick **Lightweight Charts** dengan EMA, volume & **penanda transaksi** (akun simulasi / backtest), plus widget **TradingView** (`IDX:<KODE>`)
- 💹 Harga & histori dari Yahoo Finance (`<KODE>.JK`), atau data demo offline
- ⚡ **Mode live**: harga, watchlist, IHSG & candle terakhir diperbarui otomatis (Server-Sent Events), grafik intraday 1/5/15 menit
- 📉 Pantau **IHSG** & **LQ45**: ticker IHSG di header, grafik, sinyal teknikal & notifikasi Telegram
- 📑 **Analisis fundamental** dari laporan keuangan resmi IDX (XBRL): PER, PBV, ROE, DER, pertumbuhan laba
- 🧠 Sinyal teknikal otomatis (RSI, EMA 12/26, MACD, Bollinger Band) → BELI / JUAL / TAHAN
- 🛒 Order **Market** & **Limit** dengan aturan IDX: 1 lot = 100 lembar, fraksi harga, fee beli/jual, **ARA/ARB**
- 💼 Portofolio, P/L, riwayat order, pembatalan order
- 🤖 **Auto-trading berbasis sinyal** (khusus akun simulasi) dengan stop-loss, take-profit, cooldown & log keputusan
- 📡 **Webhook alert TradingView**: alert dari strategi/indikator TradingView → log, Telegram, atau order simulasi
- 🔔 **Notifikasi Telegram** saat muncul sinyal BELI/JUAL baru (dan saat bot auto-trading bertransaksi)
- 📊 **Backtest** strategi auto-trading dengan data historis: return, CAGR, drawdown, Sharpe, win rate, beta, vs **IHSG** & vs beli & tahan
- 🔌 Arsitektur **adapter broker**: Paper Trading (aktif), Stockbit & Pluang (lihat batasan di bawah)
- 🛡️ Pengaman: batas nilai order per % ekuitas, live trading mati secara default + konfirmasi per order
- 🪙 **Crypto lewat Binance** (API resmi): harga real-time, grafik, order & auto-trading di akun simulasi USDT,
  **Binance Spot Testnet**, atau akun Binance asli (terkunci secara default)

## Menjalankan

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # sesuaikan bila perlu
uvicorn app.main:app --reload
```

Buka http://localhost:8000. Tanpa internet, set `MARKET_DATA_PROVIDER=demo` di `.env`.

Test: `pytest`

## 🗄️ Penyimpanan data (SQLite)

Semua data tersimpan di satu file **SQLite** `data/app.db` (bawaan Python, tanpa server; lokasi bisa
diubah dengan `DATABASE_PATH`). Isinya:

| Tabel | Isi |
|---|---|
| `accounts`, `positions`, `orders` | Akun simulasi: kas, posisi & seluruh riwayat order |
| `settings` | Pengaturan auto-trading, Telegram (termasuk sinyal terakhir yang terkirim) & webhook |
| `logs` | Log keputusan bot, riwayat notifikasi & alert webhook — tetap ada setelah restart (disimpan 180 hari) |
| `backtests` | Setiap hasil backtest — buka lagi lewat **Riwayat backtest** di tab Backtest |

Laporan keuangan XBRL tetap berupa file di `data/fundamentals/XBRL/`.

**Dari versi lama (JSON):** saat pertama dijalankan, `paper_account.json`, `autotrader.json`,
`notifications.json` dan `webhook.json` di folder `data/` otomatis dipindahkan ke database lalu
diganti nama menjadi `*.json.migrated` (tidak dihapus). **Backup:** salin `data/app.db` saat server
mati, atau `sqlite3 data/app.db ".backup backup.db"` saat berjalan. Isinya bisa dibuka dengan alat
SQLite apa pun (mis. DB Browser for SQLite).

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

## ⚡ Mode live

Harga diperbarui otomatis tanpa memuat ulang halaman: server mengambil harga terbaru dan
mengirimkannya ke browser lewat **Server-Sent Events** (`/api/stream`).

- Saham yang sedang dibuka: tiap ±`LIVE_FOCUS_SECONDS` (10 dtk); watchlist & IHSG: tiap ±`LIVE_WATCH_SECONDS` (30 dtk).
  Harga berkedip hijau/merah saat berubah; candle terakhir di grafik ikut bergerak.
- Grafik **intraday** 1 / 5 / 15 menit (pilihan "Harian" untuk candle harian), jam dalam WIB, lengkap dengan penanda transaksi.
- Indikator status di samping harga: `● LIVE · jam update · data tertunda ±x mnt dari bursa` — dihitung dari
  waktu transaksi terakhir yang dilaporkan sumber data, jadi Anda tahu persis seberapa tertinggal datanya.
- Koneksi dijeda saat tab tidak aktif, dan beberapa tab berbagi cache yang sama, agar tidak membebani sumber data.

**Seberapa "real-time"?** Mode live membuat aplikasi selalu menampilkan data terbaru *yang tersedia*,
tetapi Yahoo Finance (gratis) untuk saham IDX umumnya **tertunda ±10–15 menit** dari bursa. Data
real-time sesungguhnya hanya tersedia lewat feed berlisensi (layanan data IDX / vendor data pasar /
API resmi sekuritas). Bila Anda punya akses seperti itu, cukup buat provider baru dengan metode
`quote()` dan `candles()` seperti di `app/market_data.py` — mode live, grafik, bot & notifikasi langsung
memakainya. Dengan `MARKET_DATA_PROVIDER=demo`, harga demo bergerak sepanjang jam bursa untuk mencoba mode live.
Tersedia juga dua penyedia data IDX berbayar — lihat **Sumber data saham IDX (Invezgo / GoAPI)** di bawah.
Alternatif lain yang sudah tersedia: **price feed dari TradingView** (di bawah) bila Anda berlangganan data IDX real-time di TradingView.

## 🛰️ Sumber data saham IDX (Invezgo / GoAPI)

Untuk data yang jauh lebih segar daripada Yahoo (±10–15 menit), pilih penyedia berbayar di `.env`:

| `MARKET_DATA_PROVIDER` | API key | Yang dipakai aplikasi |
|---|---|---|
| `invezgo` | `INVEZGO_API_KEY` ([invezgo.com](https://invezgo.com/id/data-api-saham-indonesia)) | harga & harga kemarin (`/analysis/intraday-data`), waktu transaksi terakhir (`/analysis/intraday`), IHSG/LQ45 (`/analysis/intraday-index`), candle harian (`/analysis/chart/stock|index`) dan intraday 1/5/15 mnt (`/analysis/chart/multi-time`) |
| `goapi` | `GOAPI_API_KEY` ([goapi.io](https://goapi.io/api-data-saham-indonesia/)) | harga banyak saham dalam satu permintaan (`/stock/idx/prices`), candle harian (`/stock/idx/{kode}/historical`) |

- Endpoint & format mengikuti SDK resmi masing-masing ([Invezgo](https://github.com/Invezgo/invezgo-python-sdk),
  [GoAPI](https://github.com/goapi-io/php-sdk)); API key dikirim lewat header (`Authorization: Bearer` / `X-API-KEY`).
- Yang tidak disediakan penyedia — mis. candle intraday & indeks di GoAPI — atau saat penyedia gangguan/kuota habis,
  otomatis diambil dari `VENDOR_FALLBACK` (bawaan `yahoo`). Status live menuliskan "(cadangan: yahoo)" bila itu terjadi.
- GoAPI: semua saham yang sedang dipantau (2 menit terakhir) diambil dalam **satu** permintaan, agar hemat kuota.
- Mode live mengambil harga saham yang dibuka tiap `LIVE_FOCUS_SECONDS` (10 dtk) dan watchlist tiap
  `LIVE_WATCH_SECONDS` (30 dtk) — sesuaikan dengan batas permintaan paket Anda.
- **Seberapa real-time** datanya, dan apakah boleh dipakai di aplikasi seperti ini, ditentukan paket & syarat
  penyedia — pastikan dulu dengan mereka. Integrasi ini diuji dengan server tiruan berformat SDK resmi; belum
  diuji dengan akun asli.

## 🚦 ARA / ARB (Auto Rejection)

Batas naik (ARA) dan turun (ARB) harian dihitung dari **harga acuan** (penutupan sesi sebelumnya),
lalu dibulatkan ke fraksi harga: ARA ke bawah, ARB ke atas (tidak di bawah Rp50).

| Harga acuan | ARA | ARB |
|---|---|---|
| Rp50 – Rp200 | 35% | 15% |
| > Rp200 – Rp5.000 | 25% | 15% |
| > Rp5.000 | 20% | 15% |

Nilai di atas adalah aturan IDX sejak April 2025. **Aturan bursa bisa berubah** — sesuaikan lewat
`.env` (`AUTO_REJECTION_ARA=35,25,20`, `AUTO_REJECTION_ARB=15,15,15`). Papan khusus (mis. Full Call
Auction / pemantauan khusus) dengan batas berbeda belum didukung.

- **Tampilan:** batas "ARB … · ARA …" di bar harga, label **ARA**/**ARB** saat harga menyentuh batas
  (juga di watchlist), dan garis titik-titik ARA/ARB di grafik.
- **Simulasi seperti bursa:** order limit di luar rentang ARB–ARA ditolak; beli market saat saham
  **ARA** (tidak ada penjual) dan jual market saat **ARB** (tidak ada pembeli) ditolak. Berlaku untuk order
  manual, bot auto-trading, webhook, dan **backtest** (harga acuan = penutupan hari sebelumnya,
  sehingga transaksi di hari ARA/ARB tidak tereksekusi seperti di dunia nyata). Form order
  menampilkan peringatan sebelum dikirim.
- **Telegram:** pemantau sinyal juga mengabarkan saat saham yang dipantau menyentuh ARA/ARB
  (sekali per saham per hari; bisa dimatikan lewat `notify_limits`).

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

Status berjalan & pengaturan disimpan di database (`data/app.db`), jadi bot otomatis lanjut
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

**Notifikasi Telegram.** Dengan aksi "Kirim ke Telegram" atau "Telegram + order simulasi" (Telegram
diatur di tab **Notifikasi**), setiap alert dikirim ke Telegram berisi: sinyal & pesan alert, harga
terkini (% hari ini dan selisih dari harga di alert), batas ARB/ARA (dengan tanda bila sedang ARA/ARB),
hasil order (✅ berhasil / ❌ ditolak beserta alasannya / ⏭ dilewati), ringkasan posisi setelah order
(lot, harga rata-rata, P/L), serta link TradingView & Stockbit. Contoh:

```
🟢 Alert TradingView — BBCA
Sinyal: BELI @ 9.025 — EMA cross
Harga terkini: 9.050 (+0,56% hari ini) · +0,28% dari harga alert
ARB 7.650 · ARA 10.800
✅ Order simulasi: BUY 11 lot @ 9.050 (simulasi)
Posisi: 11 lot · avg 9.050 · P/L +Rp0 (+0,00%)
TradingView · Stockbit
```

Alert dengan **kode rahasia salah** dicatat di log (jenis *DITOLAK*, beserta IP pengirim) dan
memicu **peringatan keamanan** ke Telegram — maksimal satu pesan per 10 menit, berisi jumlah
percobaan & IP. Bisa dimatikan di tab Webhook.

**Keamanan.** TradingView tidak bisa mengirim header khusus, jadi alert diautentikasi dengan
kode rahasia di isi pesan (bisa diganti kapan saja di tab Webhook). Karena aplikasi dibuka lewat
tunnel, ada pengaman bawaan (`LOCAL_ONLY_GUARD=true`): request yang datang lewat tunnel/proxy
(membawa header seperti `X-Forwarded-For` / `Cf-Connecting-Ip`) **hanya** boleh ke
`/api/webhooks/tradingview` — UI dan API lain tetap hanya bisa dibuka dari komputer Anda.

## 📶 Price feed dari TradingView (via webhook)

Bila Anda punya paket TradingView yang mendukung webhook (dan langganan data real-time IDX di
TradingView), harga bisa **dikirim TradingView sendiri** ke aplikasi ini setiap menit. Aplikasi
tidak login ke TradingView dan tidak menyimpan password Anda; data datang lewat fitur alert
webhook resmi TradingView ke endpoint yang sama dengan alert sinyal.

1. Tab **Webhook → Price feed dari TradingView**: isi daftar saham (maks. 20, termasuk IHSG) lalu **Simpan**.
2. Salin **skrip Pine** yang dibuat aplikasi (memuat kode rahasia — jangan dipublikasikan) ke Pine Editor
   TradingView, **Add to chart** pada grafik **1 menit** saham yang ramai (mis. `IDX:BBCA`).
3. Buat alert: *Condition* = "IDX Trading View price feed" → **Any alert() function call**, centang
   **Webhook URL** (alamat tunnel + `/api/webhooks/tradingview`). Kolom *Message* tidak dipakai.
4. Klik **Aktifkan**. Tabel status menunjukkan harga, bar terakhir & kapan diterima untuk tiap saham.

Setiap bar ditutup, skrip mengirim satu pesan berisi OHLCV semua saham (`request.security`) plus
penutupan kemarin:

```json
{"secret": "…", "type": "bars", "tf": "1",
 "bars": [["IDX:BBCA", 1791340800000, 9025, 9050, 9000, 9050, 123400, 8950], …]}
```

- Selama feed **segar**, harga terkini, mode live (status "data hampir real-time (TradingView)"),
  kedipan harga, ARA/ARB, bot, notifikasi & order simulasi memakai harga dari TradingView. Grafik
  intraday 1/5/15 menit dan candle harian hari ini digabung dengan bar dari feed.
- Bila tidak ada bar baru lebih lama dari batas "basi" (bawaan 180 detik, min. 2,5× panjang bar) saat
  bursa buka, aplikasi otomatis kembali ke sumber data biasa. Setelah bursa tutup, harga terakhir feed
  hari itu tetap dipakai.
- Hanya saham di daftar yang diterima; bar lebih lama dari 2 hari diabaikan. Bar disimpan di SQLite
  (tabel `feed_bars`, 7 hari) agar grafik tetap utuh setelah server dimulai ulang.
- Bila kode rahasia diganti, salin ulang skrip ke TradingView.

Batasan: harga datang per penutupan bar (paling cepat 1 menit), bukan per transaksi; jumlah saham per
skrip dibatasi `request.security` Pine (20 saham); alert TradingView punya batas jumlah alert aktif
per paket.

## 🔔 Notifikasi Telegram

1. Di Telegram, chat [@BotFather](https://t.me/BotFather) → `/newbot` → salin **token**.
2. Buka tab **Notifikasi**, tempel token, klik **Simpan pengaturan**.
3. Kirim pesan apa saja ke bot Anda (untuk grup: tambahkan bot ke grup lalu kirim pesan di sana).
4. Klik **Cari chat ID**, pilih chat Anda, lalu **Kirim pesan uji**.
5. Atur saham yang dipantau, lalu klik **Mulai**.

Token & chat ID juga bisa diisi lewat `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) — nilai `.env`
lebih diutamakan. Token yang diisi lewat UI disimpan lokal di database `data/app.db` (tidak
di-commit) dan selalu disamarkan di API/UI.

Cara kerja:
- Setiap interval (default 15 menit) semua saham dipindai. Pesan dikirim saat sinyal **berubah**
  menjadi BELI (skor ≥ batas beli) atau JUAL (skor ≤ batas jual). Sinyal yang sama tidak dikirim
  ulang sampai mereda ke TAHAN lalu muncul lagi — jadi tidak spam. Status ini tersimpan, sehingga
  restart server tidak memicu pesan ganda.
- Bila pengiriman gagal (mis. internet putus), sinyal dicoba kirim lagi di pemindaian berikutnya.
- Opsional: setiap transaksi bot auto-trading (beli/jual, stop-loss, take-profit) ikut dikirim.
- Opsi "Hanya saat jam bursa" melewati pemindaian **saham** di luar jam perdagangan IDX.

**🎯 Alert harga (target).** Di panel order (saham maupun crypto) isi *Target harga* lalu klik **Pasang**.
Arah ditentukan otomatis dari harga sekarang: target di atas harga → dikabarkan saat **naik tembus**, di bawah →
saat **turun tembus**. Harga dicek tiap `PRICE_ALERT_SECONDS` (bawaan 30 detik) — terpisah dari pemindaian sinyal
dan tetap berjalan walau pemantau sinyal dihentikan; cukup bot token & chat ID terisi.
- Alert biasa terkirim **sekali** lalu selesai (bisa *Aktifkan lagi* di tab Notifikasi). Alert **berulang** aktif lagi
  setelah harga kembali ke sisi semula, dengan jeda minimal 30 menit antar pesan.
- Untuk saham IDX, aplikasi mengingatkan bila target di luar rentang ARB–ARA hari ini (baru bisa tercapai di hari
  bursa berikutnya). Ketepatan waktu mengikuti sumber data: Yahoo tertunda ±10–15 menit, Invezgo/GoAPI/price feed
  TradingView lebih segar.
- Daftar semua alert (status, kapan terpicu, catatan) ada di tab **Notifikasi**.

```
🎯 BBCA naik tembus 9.500
Harga: 9.525 (+1,33% hari ini)
Dipasang saat harga 9.000, 07/10 09:15 WIB
Sumber: invezgo · transaksi 10:14:00 WIB
Catatan: breakout resistance
TradingView · Stockbit
```

**Sinyal crypto (Binance).** Isi kolom *Pasangan crypto yang dipantau* (mis. `BTCUSDT, ETHUSDT`, atau klik
**Pakai watchlist crypto**); tombol *Notifikasi Telegram* di tampilan Crypto langsung membuka pengaturan ini.
Bot token, chat ID, interval, ambang skor & tombol Mulai dipakai bersama dengan saham.
- Sinyal dihitung dari candle pilihan (15 mnt / 1 jam / 4 jam / harian, bawaan 1 jam) dan dikirim saat
  **berubah** menjadi BELI/JUAL, sama seperti saham. Crypto selalu dipindai (pasar 24 jam).
- Pengganti ARA/ARB untuk crypto: **gerakan besar 24 jam** — bila perubahan 24 jam ≥ ±x% (bawaan 5%,
  0 = mati), dikabarkan sekali per pasangan per hari per arah.
- Transaksi bot auto-trading crypto ikut dikirim bila "Kirim juga transaksi bot" aktif.

```
🟢 SINYAL BELI — BTCUSDT (crypto · candle 1 jam)
Harga: 65.025,07 USDT (+2,00% 24 jam)
Skor: +2
• RSI 23.6 < 30 (oversold)
• Histogram MACD berbalik positif
Binance · TradingView

🚀 BTCUSDT naik +5,73% dalam 24 jam
Harga: 65.025,07 USDT · 24 jam lalu 61.500,50 · ambang ±5%
```

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

## 🪙 Crypto (Binance)

Klik **Crypto** di header untuk beralih dari saham IDX ke pasar crypto. Semua lewat **API resmi
Binance Spot** — aplikasi tidak pernah meminta password Binance.

- **Harga & grafik real-time** dari endpoint publik Binance (tanpa API key): watchlist pasangan
  (mis. `BTCUSDT`, `ETH/USDT`), harga diperbarui ±3 detik dengan kedipan naik/turun, candle 1 mnt–harian,
  EMA 12/26, volume, sinyal teknikal dan penanda transaksi akun yang dipilih.
- **Tiga akun** (pilih di form order):

  | Akun | Uang | Yang dibutuhkan |
  |---|---|---|
  | Simulasi (USDT) | palsu, saldo awal `CRYPTO_PAPER_STARTING_USDT` | tidak ada |
  | Binance Spot Testnet | palsu, di server Binance | `BINANCE_TESTNET_API_KEY/SECRET` dari [testnet.binance.vision](https://testnet.binance.vision) |
  | Binance asli | **sungguhan** | `BINANCE_API_KEY/SECRET` + `ENABLE_LIVE_TRADING=true` + konfirmasi per order |

- **Order** Market/Limit, jumlah dalam aset dasar (BTC) atau nilai (USDT). Jumlah dibulatkan ke `stepSize`,
  harga ke `tickSize`, dan nilai minimal (`minNotional`) dicek — sama seperti aturan pasangan di Binance.
  Fee simulasi `CRYPTO_FEE_PCT` (0,1%); di Binance fee yang tercatat adalah fee sebenarnya dari bursa.
- **Auto-trading crypto** (tab *Auto-Trading Crypto*): strategi sinyal yang sama dengan bot saham, dengan
  candle 15 mnt / 1 jam / 4 jam / harian, stop-loss, take-profit, cooldown, maks. posisi, dan **batas nilai
  per order (USDT)**. Pasar crypto buka 24 jam, jadi tidak ada pengecekan jam bursa. Transaksi bot ikut
  dikirim ke Telegram bila notifikasi transaksi aktif.
- **Bot hanya mengelola posisi yang dibukanya sendiri** (dihitung dari order "auto" yang terisi): saldo lain
  di akun Binance Anda tidak pernah ikut dijual. Di akun asli bot butuh `ENABLE_LIVE_TRADING=true`, batas nilai
  per order > 0, dan konfirmasi saat mulai; tombol "Jalankan 1 siklus" dinonaktifkan.

**Membuat API key Binance (akun asli) dengan aman:** aktifkan hanya *Enable Spot & Margin Trading*,
**jangan** aktifkan izin withdraw, batasi ke IP komputer Anda, dan simpan di `.env` (jangan di-commit).
Mulailah di Testnet. Bila `api.binance.com` tidak bisa diakses dari jaringan Anda, data pasar bisa
diambil dari endpoint resmi khusus data `BINANCE_DATA_URL=https://data-api.binance.vision`. Pastikan
juga layanan Binance boleh Anda gunakan di negara Anda.

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
  db.py              Penyimpanan SQLite (skema, migrasi dari JSON, log, riwayat backtest)
  idx_rules.py       Lot, fraksi harga, normalisasi kode saham
  market_data.py     Yahoo Finance & data demo
  indicators.py      SMA, EMA, RSI, MACD, Bollinger
  strategy.py        Skor & sinyal BELI/JUAL/TAHAN
  autotrader.py      Bot auto-trading (simulasi)
  backtest.py        Backtest bot dengan data historis
  notifier.py        Notifikasi Telegram & pemantau sinyal
  webhooks.py        Penerima webhook alert TradingView
  idx_vendors.py     Penyedia data IDX berbayar (Invezgo, GoAPI) + cadangan otomatis
  price_alerts.py    Alert harga (target) ke Telegram untuk saham & crypto
  price_feed.py      Price feed dari alert TradingView (skrip Pine, penyimpanan bar, provider pembungkus)
  binance.py         Klien API resmi Binance Spot (data publik, request bertanda tangan, aturan simbol)
  crypto.py          Akun simulasi crypto, broker Binance (Testnet/asli) & auto-trading crypto
  crypto_api.py      Endpoint /api/crypto/*
  fundamentals.py    Parser laporan keuangan XBRL IDX & rasio fundamental
  fundamentals_taxonomy.csv  Pemetaan akun → tag XBRL
  brokers/
    base.py          Kontrak Broker & model Order
    paper.py         Simulasi paper trading (tersimpan di SQLite data/app.db)
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
| GET | `/api/chart/{kode}?range=1y&interval=1d` | Candle (harian atau `1m`/`5m`/`15m`), EMA 12/26 & penanda transaksi |
| GET | `/api/stream?symbols=BBCA,TLKM&focus=BBCA` | Mode live (Server-Sent Events): event `quote` setiap harga berubah |
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
| POST | `/api/webhooks/tradingview` | Penerima alert TradingView & price feed (`"type": "bars"`) — satu-satunya endpoint publik |
| GET | `/api/webhooks` | Status, template pesan & log alert |
| PUT | `/api/webhooks/config` | Aktif/nonaktif, aksi, ukuran order, simbol yang diizinkan |
| POST | `/api/webhooks/regenerate-secret` | Ganti kode rahasia |
| GET/POST | `/api/alerts` | Daftar / pasang alert harga (`symbol`, `target`, `note`, `repeat`) |
| DELETE | `/api/alerts/{id}` | Hapus alert |
| POST | `/api/alerts/{id}/rearm` · `/api/alerts/check` | Aktifkan lagi alert / periksa sekarang |
| GET | `/api/feed` | Status price feed TradingView per saham |
| PUT | `/api/feed/config` | Aktif/nonaktif, daftar saham, batas data basi |
| GET | `/api/feed/pine` | Skrip Pine siap salin (memuat kode rahasia) |
| GET | `/api/crypto/config` | Status akun crypto (simulasi / testnet / binance) |
| GET | `/api/crypto/quote/{pasangan}` | Harga 24 jam + aturan Binance (stepSize, tickSize, minNotional) |
| GET | `/api/crypto/chart/{pasangan}?interval=1h&broker=paper` | Candle, EMA, sinyal & penanda transaksi |
| GET | `/api/crypto/account?broker=paper` | Saldo & aset (dinilai dalam USDT) |
| GET/POST | `/api/crypto/orders` | Riwayat / kirim order (`quantity` atau `quote_amount`; `confirm_live` untuk akun asli) |
| DELETE | `/api/crypto/orders/{id}?broker=` | Batalkan order OPEN |
| POST | `/api/crypto/paper/reset` | Reset simulasi crypto |
| GET/PUT | `/api/crypto/autotrader` · `/config` | Status & pengaturan bot crypto |
| POST | `/api/crypto/autotrader/start` · `/stop` · `/run-once` | Kendalikan bot crypto |
| POST | `/api/backtest` | Jalankan backtest (`symbols`, `period`, `initial_cash`, `execution`, `strategy`) — hasil disimpan |
| GET | `/api/backtests` · `/api/backtests/{id}` | Riwayat backtest / hasil lengkap satu backtest |
| DELETE | `/api/backtests/{id}` | Hapus backtest dari riwayat |

> Disclaimer: sinyal dihasilkan otomatis dan bukan rekomendasi investasi. Data Yahoo untuk IDX
> tertunda ±10–15 menit. Gunakan dengan risiko sendiri.
