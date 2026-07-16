"""Regression test for the Bright Data 'ready/building' download race.

The /progress endpoint can report 'ready' a beat before the /snapshot data
endpoint is actually servable. The first download then returns HTTP 200 with a
JSON body like {"status": "building", "message": "...try again in 30s"} instead
of the rows array. download() must keep polling until the rows arrive, NOT
return the not-ready dict as if it were data (which used to abort the whole run
with "Unexpected response shape").
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scraper  # noqa: E402


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def raise_for_status(self):
        return None

    async def json(self):
        return self._payload


class FakeSession:
    """Returns a scripted sequence of /snapshot responses, one per get()."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = 0

    def get(self, _url):
        self.calls += 1
        return FakeResp(self._payloads.pop(0))


def test_download_waits_out_building_race():
    scraper.POLL_INTERVAL = 0  # don't actually sleep between polls
    building = {"status": "building", "message": "Dataset is not ready yet, try again in 30s"}
    rows = [{"job_posting_id": "1"}, {"job_posting_id": "2"}]
    session = FakeSession([building, building, rows])

    result = asyncio.run(scraper.download(session, "snap_test"))

    assert result == rows, f"expected the rows list, got {result!r}"
    assert session.calls == 3, f"expected 3 polls (2 building + 1 ready), got {session.calls}"


def test_download_returns_immediately_when_ready():
    scraper.POLL_INTERVAL = 0
    rows = [{"job_posting_id": "1"}]
    session = FakeSession([rows])

    result = asyncio.run(scraper.download(session, "snap_test"))

    assert result == rows
    assert session.calls == 1


if __name__ == "__main__":
    test_download_waits_out_building_race()
    test_download_returns_immediately_when_ready()
    print("DOWNLOAD RACE TESTS OK")


# -- exception chaining on the bounded-retry raises (audit P2 #4) --------------

import aiohttp  # noqa: E402
import pytest  # noqa: E402


class ErrorSession:
    """Every get() raises a transient client error."""

    def __init__(self):
        self.calls = 0

    def get(self, _url):
        self.calls += 1
        raise aiohttp.ClientError("503 upstream hiccup")


def test_wait_until_ready_retry_exhaustion_chains_cause(monkeypatch):
    monkeypatch.setattr(scraper, "POLL_INTERVAL", 0)
    with pytest.raises(RuntimeError, match="Progress polling failed") as ei:
        asyncio.run(scraper.wait_until_ready(ErrorSession(), "snap_x"))
    assert isinstance(ei.value.__cause__, aiohttp.ClientError)


def test_wait_until_ready_timeout_during_errors_chains_cause(monkeypatch):
    monkeypatch.setattr(scraper, "POLL_INTERVAL", 0)
    monkeypatch.setattr(scraper, "MAX_WAIT_MINUTES", -1)   # deadline already past
    with pytest.raises(RuntimeError, match="Timeout") as ei:
        asyncio.run(scraper.wait_until_ready(ErrorSession(), "snap_x"))
    assert isinstance(ei.value.__cause__, aiohttp.ClientError)


def test_download_retry_exhaustion_chains_cause(monkeypatch):
    monkeypatch.setattr(scraper, "POLL_INTERVAL", 0)
    with pytest.raises(RuntimeError, match="Snapshot download failed") as ei:
        asyncio.run(scraper.download(ErrorSession(), "snap_x"))
    assert isinstance(ei.value.__cause__, aiohttp.ClientError)


def test_download_timeout_during_errors_chains_cause(monkeypatch):
    monkeypatch.setattr(scraper, "POLL_INTERVAL", 0)
    monkeypatch.setattr(scraper, "MAX_WAIT_MINUTES", -1)
    with pytest.raises(RuntimeError, match="Timeout") as ei:
        asyncio.run(scraper.download(ErrorSession(), "snap_x"))
    assert isinstance(ei.value.__cause__, aiohttp.ClientError)
