"""Konfigurasi aplikasi dari environment variable (lihat .env.example)."""
import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Loader .env minimal agar tidak butuh dependensi tambahan."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


ROOT = Path(__file__).resolve().parent.parent
_load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    market_data_provider: str = os.getenv("MARKET_DATA_PROVIDER", "yahoo")  # yahoo / demo / invezgo / goapi
    # Penyedia data IDX berbayar (API key Anda sendiri). Data yang tidak tersedia / saat gangguan
    # diambil dari VENDOR_FALLBACK (yahoo / demo / none).
    invezgo_api_key: str = os.getenv("INVEZGO_API_KEY", "")
    invezgo_base_url: str = os.getenv("INVEZGO_BASE_URL", "https://api.invezgo.com")
    goapi_api_key: str = os.getenv("GOAPI_API_KEY", "")
    goapi_base_url: str = os.getenv("GOAPI_BASE_URL", "https://api.goapi.io")
    vendor_fallback: str = os.getenv("VENDOR_FALLBACK", "yahoo")
    paper_starting_cash: float = float(os.getenv("PAPER_STARTING_CASH", "100000000"))
    buy_fee_pct: float = float(os.getenv("BUY_FEE_PCT", "0.15"))
    sell_fee_pct: float = float(os.getenv("SELL_FEE_PCT", "0.25"))
    max_position_pct: float = float(os.getenv("MAX_POSITION_PCT", "20"))
    enable_live_trading: bool = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    # Folder XBRL laporan keuangan; bisa diarahkan ke folder data/XBRL milik idx-financial-scraper.
    fundamentals_xbrl_dir: str = os.getenv("FUNDAMENTALS_XBRL_DIR", "")
    usd_idr_rate: float = float(os.getenv("USD_IDR_RATE", "16000"))
    # Bila true, request lewat tunnel/proxy hanya boleh ke endpoint webhook (lihat README).
    local_only_guard: bool = os.getenv("LOCAL_ONLY_GUARD", "true").lower() != "false"
    # Mode live: seberapa sering harga diambil ulang (detik). Jangan terlalu kecil agar tidak
    # kena batas permintaan sumber data (Yahoo).
    # Alert harga Telegram: seberapa sering harga dicek terhadap target (detik).
    price_alert_seconds: float = float(os.getenv("PRICE_ALERT_SECONDS", "30"))
    live_focus_seconds: float = float(os.getenv("LIVE_FOCUS_SECONDS", "10"))   # saham yang sedang dibuka
    live_watch_seconds: float = float(os.getenv("LIVE_WATCH_SECONDS", "30"))   # watchlist & IHSG
    # Umur maksimum satu koneksi stream; browser (EventSource) otomatis menyambung ulang.
    live_stream_max_seconds: float = float(os.getenv("LIVE_STREAM_MAX_SECONDS", "1800"))
    # ---- crypto (Binance) ----
    # Data pasar publik (tanpa API key). Bila api.binance.com diblokir di jaringan Anda, coba
    # https://data-api.binance.vision (endpoint resmi khusus data pasar).
    binance_data_url: str = os.getenv("BINANCE_DATA_URL", "https://api.binance.com")
    binance_api_url: str = os.getenv("BINANCE_API_URL", "https://api.binance.com")
    binance_api_key: str = os.getenv("BINANCE_API_KEY", "")
    binance_api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    binance_testnet_url: str = os.getenv("BINANCE_TESTNET_URL", "https://testnet.binance.vision")
    binance_testnet_api_key: str = os.getenv("BINANCE_TESTNET_API_KEY", "")
    binance_testnet_api_secret: str = os.getenv("BINANCE_TESTNET_API_SECRET", "")
    crypto_paper_starting_cash: float = float(os.getenv("CRYPTO_PAPER_STARTING_USDT", "10000"))
    crypto_fee_pct: float = float(os.getenv("CRYPTO_FEE_PCT", "0.1"))
    crypto_max_position_pct: float = float(os.getenv("CRYPTO_MAX_POSITION_PCT", "20"))
    # ---- saham Amerika (Alpaca Markets) ----
    # Feed data: "iex" (gratis, real-time tetapi hanya volume bursa IEX) atau "sip" (berbayar, seluruh bursa AS).
    alpaca_data_url: str = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")
    alpaca_data_feed: str = os.getenv("ALPACA_DATA_FEED", "iex")
    alpaca_paper_url: str = os.getenv("ALPACA_PAPER_URL", "https://paper-api.alpaca.markets")
    alpaca_paper_api_key: str = os.getenv("ALPACA_PAPER_API_KEY", "")
    alpaca_paper_api_secret: str = os.getenv("ALPACA_PAPER_API_SECRET", "")
    alpaca_live_url: str = os.getenv("ALPACA_LIVE_URL", "https://api.alpaca.markets")
    alpaca_live_api_key: str = os.getenv("ALPACA_LIVE_API_KEY", "")
    alpaca_live_api_secret: str = os.getenv("ALPACA_LIVE_API_SECRET", "")
    us_max_position_pct: float = float(os.getenv("US_MAX_POSITION_PCT", "20"))
    us_max_order_usd: float = float(os.getenv("US_MAX_ORDER_USD", "1000"))  # batas nilai satu order di akun live
    data_dir: Path = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
    # Database SQLite; kosong = <DATA_DIR>/app.db
    database_path: str = os.getenv("DATABASE_PATH", "")


settings = Settings()
