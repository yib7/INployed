"""Edge cases of Bright Data's replies that used to cost a billed collection.

A collection that comes back with the POST has no snapshot to fetch again, so
reading its body wrong loses jobs that are already paid for. The poll and the
download have a snapshot, but one odd body there ended a run that was nearly
done. Every test here runs against fakes: nothing reaches the network.
"""
import asyncio

import pandas as pd
import pytest

import scraper


# --- the body of a collection that finished inside the sync window -------------

@pytest.mark.parametrize("sep", [" ", " ", "\x85"])
def test_a_line_separator_inside_a_description_keeps_the_record_whole(sep):
    # json.dumps escapes these by default; Bright Data's writer need not, and
    # str.splitlines() breaks a line on each of them.
    body = ('{"job_posting_id": "1", "job_summary": "one' + sep + 'two"}\n'
            '{"job_posting_id": "2", "job_summary": "three"}\n')
    got = scraper._parse_collection_body(body)
    assert [r["job_posting_id"] for r in got.rows] == ["1", "2"]
    assert got.rows[0]["job_summary"] == "one" + sep + "two"


def test_crlf_line_ends_still_read_as_ndjson():
    body = '{"job_posting_id": "1"}\r\n{"job_posting_id": "2"}\r\n'
    assert len(scraper._parse_collection_body(body).rows) == 2


class _BytesResp:
    """A response whose body is raw bytes, decoded the way aiohttp decodes."""

    status = 200

    def __init__(self, raw: bytes):
        self._raw = raw

    async def text(self, encoding=None, errors="strict"):
        return self._raw.decode(encoding or "utf-8", errors)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _PostSession:
    def __init__(self, resp):
        self._resp = resp

    def post(self, url, json=None):
        return self._resp


def test_one_bad_byte_in_the_reply_does_not_lose_the_records():
    raw = (b'{"job_posting_id": "1", "job_summary": "caf\xe9"}\n'
           b'{"job_posting_id": "2"}\n')
    got = asyncio.run(scraper.trigger(_PostSession(_BytesResp(raw)), {}))
    assert [r["job_posting_id"] for r in got.rows] == ["1", "2"]
    assert "�" in got.rows[0]["job_summary"]


# --- the progress poll and the download ------------------------------------------

class _JsonResp:
    def __init__(self, item):
        self._item = item

    async def json(self):
        if isinstance(self._item, Exception):
            raise self._item
        return self._item

    def raise_for_status(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _GetSession:
    def __init__(self, *items):
        self._items = list(items)
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        return _JsonResp(self._items.pop(0))


@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr(scraper, "POLL_INTERVAL", 0)


def test_a_progress_body_that_is_not_json_is_retried(fast_poll):
    session = _GetSession(ValueError("Expecting value: line 1 column 1"),
                          {"status": "ready", "records": 3, "errors": 0})
    asyncio.run(scraper.wait_until_ready(session, "sd_x"))
    assert session.calls == 2


def test_a_progress_body_that_is_not_an_object_keeps_polling(fast_poll):
    session = _GetSession(["odd"], {"status": "ready", "records": 3, "errors": 0})
    asyncio.run(scraper.wait_until_ready(session, "sd_x"))
    assert session.calls == 2


def test_a_download_body_that_is_not_json_is_retried(fast_poll):
    session = _GetSession(ValueError("Expecting value"), [{"job_posting_id": "1"}])
    assert asyncio.run(scraper.download(session, "sd_x")) == [{"job_posting_id": "1"}]


def test_a_body_that_is_never_json_still_stops_with_the_snapshot_named(fast_poll):
    session = _GetSession(*[ValueError("Expecting value")] * scraper.MAX_POLL_FAILURES)
    with pytest.raises(scraper.SnapshotNotFetched) as excinfo:
        asyncio.run(scraper.wait_until_ready(session, "sd_x"))
    assert excinfo.value.snapshot_id == "sd_x"


# --- records with no job id ---------------------------------------------------------

def test_records_without_a_job_id_are_left_out_with_a_warning(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(scraper, "MASTER_CSV", tmp_path / "linkedin_jobs_master.csv")
    monkeypatch.setattr(scraper, "PREVIOUS_IDS_FILE", tmp_path / "last_run_job_ids.json")
    monkeypatch.setattr(scraper, "LEDGER_CSV", tmp_path / "collected_ids_ledger.csv")
    monkeypatch.setattr(scraper, "load_blocklist", lambda: [])
    filed = []
    monkeypatch.setattr(scraper, "append_to_master", lambda df: filed.append(df) or len(df))
    rows = [{"job_posting_id": "1", "job_title": "Data Analyst"},
            {"error": "dead_page", "error_code": "dead_page", "input": {"url": "x"}},
            {"job_posting_id": "", "job_title": "blank id"}]
    scraper._save_results(rows, scraper.RUN_LABELS[0])
    (df,) = filed
    assert df["job_posting_id"].tolist() == ["1"]
    (run_csv,) = (tmp_path / scraper.RUN_LABELS[0]).glob("linkedin_jobs_*.csv")
    assert pd.read_csv(run_csv, dtype=str)["job_posting_id"].tolist() == ["1"]
    warnings = [ln for ln in capsys.readouterr().out.splitlines()
                if ln.startswith("WARNING: ")]
    assert len(warnings) == 1 and "2 record(s)" in warnings[0]


# --- Ctrl+C after the reply's rows were kept -------------------------------------

def test_the_ctrl_c_line_names_the_kept_reply_rows(monkeypatch):
    monkeypatch.setattr(scraper, "_RUN_CONTEXT", {"raw_rows": "morning/rows_raw.json"})
    msg = scraper._stopped_message(KeyboardInterrupt())
    assert "morning/rows_raw.json" in msg and "cannot be downloaded again" in msg


def test_the_ctrl_c_line_says_nothing_of_kept_rows_when_there_are_none(monkeypatch):
    monkeypatch.setattr(scraper, "_RUN_CONTEXT", {})
    assert "kept in" not in scraper._stopped_message(KeyboardInterrupt())
