"""Indikator teknikal murni Python. Nilai pemanasan (warm-up) diisi None."""
from math import sqrt
from typing import Optional

Series = list[Optional[float]]


def sma(values: list[float], period: int) -> Series:
    out: Series = [None] * len(values)
    window = 0.0
    for i, v in enumerate(values):
        window += v
        if i >= period:
            window -= values[i - period]
        if i >= period - 1:
            out[i] = window / period
    return out


def ema(values: list[float], period: int) -> Series:
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> Series:
    """RSI dengan smoothing Wilder."""
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0)
        losses += max(-change, 0)
    avg_gain, avg_loss = gains / period, losses / period

    def _rsi(g: float, l: float) -> float:
        if l == 0:
            return 100.0
        return 100 - 100 / (1 + g / l)

    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
        out[i] = _rsi(avg_gain, avg_loss)
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Mengembalikan (macd_line, signal_line, histogram)."""
    fast_ema, slow_ema = ema(values, fast), ema(values, slow)
    line: Series = [
        f - s if f is not None and s is not None else None for f, s in zip(fast_ema, slow_ema)
    ]
    start = next((i for i, v in enumerate(line) if v is not None), len(line))
    signal_part = ema([v for v in line[start:] if v is not None], signal)
    signal_line: Series = [None] * start + signal_part
    hist: Series = [
        m - s if m is not None and s is not None else None for m, s in zip(line, signal_line)
    ]
    return line, signal_line, hist


def bollinger(values: list[float], period: int = 20, mult: float = 2.0):
    """Mengembalikan (upper, middle, lower). Standar deviasi populasi, dihitung bergulir O(n)."""
    mid = sma(values, period)
    upper: Series = [None] * len(values)
    lower: Series = [None] * len(values)
    sum_sq = 0.0
    for i, v in enumerate(values):
        sum_sq += v * v
        if i >= period:
            sum_sq -= values[i - period] ** 2
        if i >= period - 1:
            mean = mid[i]
            std = sqrt(max(sum_sq / period - mean * mean, 0.0))
            upper[i] = mean + mult * std
            lower[i] = mean - mult * std
    return upper, mid, lower
