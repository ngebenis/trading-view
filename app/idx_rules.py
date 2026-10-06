"""Aturan perdagangan Bursa Efek Indonesia (IDX) yang relevan untuk order."""

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
