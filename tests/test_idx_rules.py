from app.idx_rules import is_valid_price, normalize_symbol, round_to_tick, tick_size, tradingview_symbol, yahoo_symbol


def test_tick_size_bands():
    assert [tick_size(p) for p in (150, 200, 499, 500, 1995, 2000, 4990, 5000, 9000)] == [1, 2, 2, 5, 5, 10, 10, 25, 25]


def test_round_to_tick():
    assert round_to_tick(9012) == 9000
    assert round_to_tick(1233) == 1235
    assert is_valid_price(9025) and not is_valid_price(9010)


def test_symbol_normalization():
    for raw in ("bbca", "BBCA.JK", "IDX:BBCA", " BBCA "):
        assert normalize_symbol(raw) == "BBCA"
    assert yahoo_symbol("bbca") == "BBCA.JK"
    assert tradingview_symbol("BBCA.JK") == "IDX:BBCA"
