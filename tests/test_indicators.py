import math

import pytest

from app.indicators import bollinger, ema, macd, rsi, sma
from app.strategy import analyze


def test_sma():
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]


def test_ema_seed_and_step():
    out = ema([1, 2, 3, 4], 3)
    assert out[:2] == [None, None]
    assert out[2] == 2
    assert out[3] == pytest.approx(3.0)


def test_rsi_extremes():
    up = [float(i) for i in range(30)]
    assert rsi(up)[-1] == 100.0
    down = list(reversed(up))
    assert rsi(down)[-1] == pytest.approx(0.0)


def test_macd_and_bollinger_lengths():
    closes = [100 + (i % 7) for i in range(60)]
    line, sig, hist = macd(closes)
    assert len(line) == len(sig) == len(hist) == 60
    assert hist[-1] is not None
    upper, mid, lower = bollinger(closes)
    assert lower[-1] < mid[-1] < upper[-1]


def test_analyze_signals():
    assert analyze([1.0] * 10)["action"] == "HOLD"
    # Turun terus: RSI oversold (+2) melawan tren turun (-1) -> condong beli tapi belum BUY.
    falling = analyze([1000 - i * 10 for i in range(60)])
    assert falling["score"] == 1 and falling["action"] == "HOLD"
    assert any("oversold" in r for r in falling["reasons"])
    # Gelombang harga: lembah (oversold + MACD berbalik naik) -> BUY, puncak (overbought) -> SELL.
    wave = [1000 + 200 * math.sin(i / 8) for i in range(60)]
    assert analyze(wave[:42])["action"] == "BUY"
    assert analyze(wave[:52])["action"] == "SELL"


def test_bollinger_matches_naive_std():
    from statistics import pstdev
    closes = [1000 + 37 * math.sin(i / 3) + i for i in range(80)]
    upper, mid, lower = bollinger(closes, 20, 2.0)
    for i in (19, 50, 79):
        sd = pstdev(closes[i - 19 : i + 1])
        assert upper[i] == pytest.approx(mid[i] + 2 * sd, rel=1e-9)
        assert lower[i] == pytest.approx(mid[i] - 2 * sd, rel=1e-9)
