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


def normalize_symbol(symbol: str) -> str:
    """'bbca', 'BBCA.JK', 'IDX:BBCA' -> 'BBCA'."""
    s = symbol.strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    if s.endswith(".JK"):
        s = s[:-3]
    return s


def yahoo_symbol(symbol: str) -> str:
    return f"{normalize_symbol(symbol)}.JK"


def tradingview_symbol(symbol: str) -> str:
    return f"IDX:{normalize_symbol(symbol)}"
