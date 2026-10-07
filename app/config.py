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
    market_data_provider: str = os.getenv("MARKET_DATA_PROVIDER", "yahoo")
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
    data_dir: Path = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
    # Database SQLite; kosong = <DATA_DIR>/app.db
    database_path: str = os.getenv("DATABASE_PATH", "")


settings = Settings()
