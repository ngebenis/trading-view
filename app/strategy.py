"""Analisis teknikal & sinyal beli/jual sederhana.

Sinyal ini adalah alat bantu, BUKAN rekomendasi investasi.
"""
from .indicators import bollinger, ema, macd, rsi


def _last(series):
    return series[-1] if series else None


def analyze(closes: list[float]) -> dict:
    if len(closes) < 35:
        return {"action": "HOLD", "score": 0, "reasons": ["Data historis belum cukup (min. 35 candle)"], "indicators": {}}

    rsi_s = rsi(closes, 14)
    ema_fast, ema_slow = ema(closes, 12), ema(closes, 26)
    macd_line, signal_line, hist = macd(closes)
    upper, mid, lower = bollinger(closes, 20)
    price = closes[-1]

    score = 0
    reasons: list[str] = []

    r = _last(rsi_s)
    if r is not None:
        if r < 30:
            score += 2
            reasons.append(f"RSI {r:.1f} < 30 (oversold)")
        elif r > 70:
            score -= 2
            reasons.append(f"RSI {r:.1f} > 70 (overbought)")
        else:
            reasons.append(f"RSI {r:.1f} netral")

    if ema_fast[-2] is not None and ema_slow[-2] is not None:
        if ema_fast[-2] <= ema_slow[-2] and ema_fast[-1] > ema_slow[-1]:
            score += 2
            reasons.append("EMA12 golden cross EMA26")
        elif ema_fast[-2] >= ema_slow[-2] and ema_fast[-1] < ema_slow[-1]:
            score -= 2
            reasons.append("EMA12 death cross EMA26")
        elif ema_fast[-1] > ema_slow[-1]:
            score += 1
            reasons.append("Tren naik (EMA12 > EMA26)")
        else:
            score -= 1
            reasons.append("Tren turun (EMA12 < EMA26)")

    if hist[-1] is not None and hist[-2] is not None:
        if hist[-2] <= 0 < hist[-1]:
            score += 1
            reasons.append("Histogram MACD berbalik positif")
        elif hist[-2] >= 0 > hist[-1]:
            score -= 1
            reasons.append("Histogram MACD berbalik negatif")

    if lower[-1] is not None and price < lower[-1]:
        score += 1
        reasons.append("Harga di bawah Bollinger Band bawah")
    elif upper[-1] is not None and price > upper[-1]:
        score -= 1
        reasons.append("Harga di atas Bollinger Band atas")

    action = "BUY" if score >= 2 else "SELL" if score <= -2 else "HOLD"
    return {
        "action": action,
        "score": score,
        "reasons": reasons,
        "indicators": {
            "price": price,
            "rsi14": r,
            "ema12": ema_fast[-1],
            "ema26": ema_slow[-1],
            "macd": macd_line[-1],
            "macd_signal": signal_line[-1],
            "bb_upper": upper[-1],
            "bb_middle": mid[-1],
            "bb_lower": lower[-1],
        },
    }
