from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.fundamentals import (FundamentalsError, FundamentalsStore, compute_ratios, idx_report_url,
                              parse_report, recent_periods)
from app.main import create_app
from app.market_data import DemoProvider
from tests.xbrl_fixture import GENERAL, GENERAL_PRIOR, make_xbrl


def test_parse_general_report_picks_totals_and_ytd():
    r = parse_report(make_xbrl(date(2026, 6, 30), GENERAL, GENERAL_PRIOR, entity="ABCD"))
    assert (r.ticker, r.year, r.period, r.period_end) == ("ABCD", 2026, "Q2", "2026-06-30")
    assert r.taxonomy_type == "general" and r.currency == "IDR"
    m = r.metrics
    # Nilai total (bukan segmen /10) dan YTD 6 bulan (bukan kuartal /3)
    assert m["total_assets"] == 1_000_000_000_000 and m["revenue"] == 300_000_000_000
    # Ekuitas & laba: yang diatribusikan ke pemilik entitas induk diutamakan
    assert m["total_equity"] == 380_000_000_000 and m["net_income"] == 30_000_000_000
    assert r.sources["net_income"] == "ProfitLossAttributableToParentEntity"
    assert r.prior == {"revenue": 250_000_000_000, "net_income": 24_000_000_000}


def test_period_from_end_date_and_zip_input():
    for end, period in [(date(2026, 3, 31), "Q1"), (date(2026, 9, 30), "Q3"), (date(2025, 12, 31), "FY")]:
        r = parse_report(make_xbrl(end, GENERAL, as_zip=True), "bbca")
        assert (r.ticker, r.year, r.period) == ("BBCA", end.year, period)


def test_banking_and_usd_detection():
    bank = {"Assets": 1e12, "Liabilities": 8e11, "Equity": 2e11, "DepositsFromCustomers": 7e11,
            "TotalInterestAndShariaIncome": 9e10, "ProfitLoss": 3e10}
    r = parse_report(make_xbrl(date(2026, 3, 31), bank, currency="USD"), "BANK")
    assert r.taxonomy_type == "banking" and r.currency == "USD"
    assert r.metrics["revenue"] == 9e10 and r.metrics["customer_deposits"] == 7e11


def test_invalid_inputs():
    for junk in (b"<html>Just a moment...</html>", b"<html>Just a moment...</html", b"\x00\x01binary"):
        with pytest.raises(FundamentalsError, match="Bukan laporan XBRL"):
            parse_report(junk, "X")
    with pytest.raises(FundamentalsError, match="Kode emiten"):
        parse_report(make_xbrl(date(2026, 3, 31), GENERAL, entity=None))


def test_ratios():
    r = parse_report(make_xbrl(date(2026, 6, 30), GENERAL, GENERAL_PRIOR), "ABCD")
    x = compute_ratios(r, price=300, usd_idr=16000)
    assert x["shares"] == 2_000_000_000 and x["shares_source"] == "laba ÷ EPS"
    assert x["eps_annualized"] == 30.0          # 30 M × 2 / 2 M lembar
    assert x["per"] == 10.0 and x["bvps"] == 190.0
    assert x["pbv"] == pytest.approx(1.58, abs=0.01)
    assert x["roe_pct"] == pytest.approx(15.79, abs=0.01)  # 60 M / 380 M
    assert x["roa_pct"] == 6.0 and x["der"] == pytest.approx(1.58, abs=0.01)
    assert x["current_ratio"] == 2.0 and x["net_margin_pct"] == 10.0 and x["gross_margin_pct"] == 30.0
    assert x["revenue_growth_pct"] == 20.0 and x["net_income_growth_pct"] == 25.0
    assert x["market_cap"] == 600_000_000_000


def test_ratios_usd_and_losses():
    usd = dict(GENERAL, BasicEarningsLossPerShare=0.001)  # USD per lembar
    usd["ProfitLossAttributableToParentEntity"] = 2_000_000  # USD
    usd["EquityAttributableToEquityOwnersOfParentEntity"] = 20_000_000
    r = parse_report(make_xbrl(date(2025, 12, 31), usd, currency="USD"), "USDC")
    x = compute_ratios(r, price=1600, usd_idr=16000)
    assert x["fx_rate"] == 16000 and x["eps_annualized"] == 16.0 and x["per"] == 100.0
    loss = dict(GENERAL, ProfitLossAttributableToParentEntity=-30_000_000_000, BasicEarningsLossPerShare=-15)
    x = compute_ratios(parse_report(make_xbrl(date(2026, 6, 30), loss), "RUGI"), 300, 16000)
    assert x["per"] is None and x["roe_pct"] < 0  # PER tidak bermakna saat rugi


def test_store_layout_upload_and_listing(tmp_path):
    store = FundamentalsStore(tmp_path / "XBRL")
    rep = store.save_upload(make_xbrl(date(2026, 6, 30), GENERAL, as_zip=True), "abcd")
    path = tmp_path / "XBRL" / "2026" / "Q2" / "ABCD_2026_Q2.xbrl"
    assert path.exists() and rep.period == "Q2"
    store.save_upload(make_xbrl(date(2025, 12, 31), GENERAL), "ABCD")
    (tmp_path / "XBRL" / "2026" / "Q1").mkdir(parents=True)
    (tmp_path / "XBRL" / "2026" / "Q1" / "ABCD_2026_Q1.xbrl").write_text("rusak")
    reports, errors = store.reports("ABCD")
    assert [(r.year, r.period) for r in reports] == [(2026, "Q2"), (2025, "FY")]
    assert len(errors) == 1 and "ABCD_2026_Q1" in errors[0]
    assert store.reports("ABC")[0] == []  # tidak tertukar dengan kode yang mirip
    with pytest.raises(FundamentalsError, match="Indeks"):
        store.save_upload(make_xbrl(date(2026, 6, 30), GENERAL), "IHSG")


def test_url_and_recent_periods():
    assert idx_report_url("bbca", 2025, "FY").endswith("/Laporan%20Keuangan%20Tahun%202025/Audit/BBCA/instance.zip")
    assert idx_report_url("BBCA", 2026, "Q1").endswith("/TW1/BBCA/instance.zip")
    assert recent_periods(date(2026, 10, 6), 4) == [(2026, "Q3"), (2026, "Q2"), (2026, "Q1"), (2025, "FY")]
    assert recent_periods(date(2026, 1, 15), 2) == [(2025, "FY"), (2025, "Q3")]


def test_api(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider()))
    empty = client.get("/api/fundamentals/BBCA").json()
    assert empty["reports"] == [] and len(empty["download_links"]) == 6
    assert empty["download_links"][0]["url"].startswith("https://www.idx.co.id/")
    r = client.post("/api/fundamentals/upload?ticker=BBCA", content=make_xbrl(date(2026, 6, 30), GENERAL, GENERAL_PRIOR, as_zip=True))
    assert r.status_code == 200 and r.json()["period"] == "Q2"
    body = client.get("/api/fundamentals/bbca").json()
    latest = body["reports"][0]
    assert latest["period"] == "Q2" and latest["ratios"]["per"] is not None
    assert latest["ratios"]["price"] == client.get("/api/quote/BBCA").json()["price"]
    assert client.post("/api/fundamentals/upload?ticker=BBCA", content=b"bukan xbrl").status_code == 400
    assert client.get("/api/fundamentals/IHSG").status_code == 400
