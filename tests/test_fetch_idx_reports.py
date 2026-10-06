import base64
import importlib.util
from datetime import date
from pathlib import Path

import pytest

from app.fundamentals import FundamentalsStore
from tests.xbrl_fixture import GENERAL, make_xbrl

spec = importlib.util.spec_from_file_location("fetch_idx", Path(__file__).resolve().parents[1] / "scripts" / "fetch_idx_reports.py")
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


class FakePage:
    """Meniru page.evaluate(FETCH_JS, url) dari Playwright."""

    def __init__(self, responses):
        self.responses = responses  # url-substring -> list of (status, bytes|None)
        self.calls = []

    def evaluate(self, js, url):
        self.calls.append(url)
        for key, queue in self.responses.items():
            if key in url:
                status, content = queue.pop(0) if len(queue) > 1 else queue[0]
                return {"status": status, "b64": base64.b64encode(content).decode() if content else None}
        return {"status": 404, "b64": None}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)


def test_plan_skips_existing_duplicates_and_indices(tmp_path):
    store = FundamentalsStore(tmp_path)
    store.save_upload(make_xbrl(date(2025, 12, 31), GENERAL), "BBCA")
    tasks = fetch.plan_downloads(store, ["bbca", "BBCA", "TLKM.JK", "IHSG", ""], range(2025, 2026), ["Q3", "FY"])
    assert tasks == [(2025, "Q3", "BBCA"), (2025, "Q3", "TLKM"), (2025, "FY", "TLKM")]


def test_download_saves_valid_reports_and_logs_failures(tmp_path):
    store = FundamentalsStore(tmp_path / "XBRL")
    page = FakePage({
        "/TW2/BBCA/": [(200, make_xbrl(date(2026, 6, 30), GENERAL, as_zip=True))],
        "/TW2/TLKM/": [(200, b"PK\x03\x04 rusak")],
    })
    tasks = [(2026, "Q2", "BBCA"), (2026, "Q2", "TLKM"), (2026, "Q2", "ASII")]
    counts = fetch.download(page, store, tasks, tmp_path / "failed.csv", delay=1)
    assert counts == {"tersimpan": 1, "tidak_ada": 1, "gagal": 1}
    assert store.path_for("BBCA", 2026, "Q2").exists()
    assert not store.path_for("TLKM", 2026, "Q2").exists()
    assert "TLKM" in (tmp_path / "failed.csv").read_text()


def test_blocked_waits_for_human_then_retries(tmp_path, monkeypatch):
    waited = []
    monkeypatch.setattr(fetch, "wait_for_human", lambda page: waited.append(1))
    store = FundamentalsStore(tmp_path)
    page = FakePage({"/TW1/BBCA/": [(403, None), (200, make_xbrl(date(2026, 3, 31), GENERAL, as_zip=True))]})
    counts = fetch.download(page, store, [(2026, "Q1", "BBCA")], tmp_path / "f.csv", delay=1)
    assert waited == [1] and counts["tersimpan"] == 1


def test_still_blocked_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "wait_for_human", lambda page: None)
    page = FakePage({"BBCA": [(403, None)]})
    with pytest.raises(fetch.BlockedError):
        fetch.download(page, FundamentalsStore(tmp_path), [(2026, "Q1", "BBCA")], tmp_path / "f.csv", delay=1)


def test_args():
    a = fetch.parse_args(["BBCA", "--periods", "FY", "--delay", "0.1"])
    assert a.tickers == ["BBCA"] and a.periods == ["FY"] and a.delay == 1.0
    with pytest.raises(SystemExit):
        fetch.parse_args([])
