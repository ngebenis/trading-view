"""Aturan perdagangan Bursa Efek Indonesia (IDX) yang relevan untuk order."""
import math

LOT_SIZE = 100  # 1 lot = 100 lembar saham


def tick_size(price: float) -> int:
    """Fraksi harga IDX berdasarkan rentang harga."""
    if price < 200:
        return 1
    if price < 500:
        return 2
    if price < 2000:
        return 5
    if price < 5000:
        return 10
    return 25


def round_to_tick(price: float) -> int:
    """Bulatkan harga ke fraksi harga terdekat yang valid."""
    tick = tick_size(price)
    return int(round(price / tick) * tick)


def is_valid_price(price: float) -> bool:
    return price > 0 and price == round_to_tick(price)


# Indeks: kode di aplikasi -> (simbol Yahoo, simbol TradingView). Indeks tidak bisa diperdagangkan.
INDICES = {
    "IHSG": ("^JKSE", "COMPOSITE"),
    "LQ45": ("^JKLQ45", "LQ45"),
}
_INDEX_ALIASES = {"COMPOSITE": "IHSG", "JKSE": "IHSG", "^JKSE": "IHSG", "JCI": "IHSG",
                  "JKLQ45": "LQ45", "^JKLQ45": "LQ45"}


def normalize_symbol(symbol: str) -> str:
    """'bbca', 'BBCA.JK', 'IDX:BBCA' -> 'BBCA'; '^JKSE', 'IDX:COMPOSITE' -> 'IHSG'."""
    s = symbol.strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    if s.endswith(".JK"):
        s = s[:-3]
    return _INDEX_ALIASES.get(s, s)


def is_index(symbol: str) -> bool:
    return normalize_symbol(symbol) in INDICES


def yahoo_symbol(symbol: str) -> str:
    s = normalize_symbol(symbol)
    return INDICES[s][0] if s in INDICES else f"{s}.JK"


def tradingview_symbol(symbol: str) -> str:
    s = normalize_symbol(symbol)
    return f"IDX:{INDICES[s][1] if s in INDICES else s}"


def tradingview_url(symbol: str) -> str:
    return "https://www.tradingview.com/symbols/" + tradingview_symbol(symbol).replace(":", "-") + "/"


def stockbit_url(symbol: str) -> str:
    return f"https://stockbit.com/symbol/{normalize_symbol(symbol)}"


def reject_indices(symbols, what: str) -> None:
    """ValueError bila daftar berisi indeks (indeks tidak bisa dibeli/dijual)."""
    found = sorted({normalize_symbol(s) for s in symbols if s.strip() and is_index(s)})
    if found:
        raise ValueError(f"{', '.join(found)} adalah indeks dan tidak bisa diperdagangkan; "
                         f"hapus dari daftar {what}")


# ---- Auto Rejection (ARA / ARB) ---------------------------------------------------------
# Batas naik (ARA) & turun (ARB) harian dari harga acuan (umumnya penutupan sesi sebelumnya),
# per rentang harga acuan: Rp50–200, >200–5.000, >5.000. Nilai bawaan = aturan IDX sejak April 2025
# (ARA simetris 35/25/20%, ARB 15%). Aturan bursa bisa berubah — sesuaikan lewat .env:
#   AUTO_REJECTION_ARA=35,25,20   AUTO_REJECTION_ARB=15,15,15
# Tidak mencakup papan khusus (mis. Full Call Auction / papan pemantauan khusus, ±10%).
MIN_PRICE = 50
LIMIT_BANDS = (200, 5000)  # batas atas rentang 1 & 2; di atasnya rentang 3


def _parse_pcts(env_name: str, default: tuple[float, float, float]) -> tuple[float, float, float]:
    import os
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return default
    parts = [float(x) for x in raw.split(",")]
    if len(parts) == 1:
        parts *= 3
    if len(parts) != 3 or not all(0 < p < 100 for p in parts):
        raise ValueError(f"{env_name} harus 1 atau 3 persen (0-100) dipisah koma, mis. 35,25,20")
    return tuple(parts)


ARA_PCTS = _parse_pcts("AUTO_REJECTION_ARA", (35.0, 25.0, 20.0))
ARB_PCTS = _parse_pcts("AUTO_REJECTION_ARB", (15.0, 15.0, 15.0))


def _band(reference: float) -> int:
    return 0 if reference <= LIMIT_BANDS[0] else 1 if reference <= LIMIT_BANDS[1] else 2


def price_limits(reference: float | None, ara_pcts=None, arb_pcts=None) -> tuple[int, int] | None:
    """(ARB, ARA) dari harga acuan. ARA dibulatkan ke bawah & ARB ke atas ke fraksi harga,
    sehingga keduanya tidak melewati persentase batas. None bila harga acuan tidak ada."""
    if not reference or reference <= 0:
        return None
    band = _band(reference)
    ara_raw = reference * (1 + (ara_pcts or ARA_PCTS)[band] / 100)
    arb_raw = reference * (1 - (arb_pcts or ARB_PCTS)[band] / 100)
    eps = 1e-9
    t = tick_size(ara_raw)
    ara = int(math.floor(ara_raw / t + eps) * t)
    t = tick_size(arb_raw)
    arb = int(math.ceil(arb_raw / t - eps) * t)
    return max(arb, MIN_PRICE), max(ara, MIN_PRICE)


def limit_status(price: float, limits: tuple[int, int] | None) -> str | None:
    """'ARA' bila harga sudah di batas atas, 'ARB' bila di batas bawah, selain itu None."""
    if not limits:
        return None
    arb, ara = limits
    if price >= ara:
        return "ARA"
    if price <= arb and arb > MIN_PRICE:
        return "ARB"
    return None


def check_auto_rejection(side: str, order_type: str, market_price: float, limit_price: float | None,
                         limits: tuple[int, int] | None) -> str | None:
    """Alasan order ditolak karena batas ARA/ARB (seperti di bursa), atau None bila boleh."""
    if not limits:
        return None
    arb, ara = limits
    fmt = lambda x: f"{x:,.0f}".replace(",", ".")  # noqa: E731
    if order_type == "LIMIT" and limit_price is not None:
        if limit_price > ara:
            return f"Harga limit di atas batas ARA ({fmt(ara)}), ditolak bursa"
        if limit_price < arb:
            return f"Harga limit di bawah batas ARB ({fmt(arb)}), ditolak bursa"
    status = limit_status(market_price, limits)
    if order_type == "MARKET" and side == "BUY" and status == "ARA":
        return f"Saham sedang ARA ({fmt(ara)}): antrean beli penuh, tidak ada penjual"
    if order_type == "MARKET" and side == "SELL" and status == "ARB":
        return f"Saham sedang ARB ({fmt(arb)}): antrean jual penuh, tidak ada pembeli"
    return None
