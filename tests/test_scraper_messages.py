"""The scraper says what went wrong, why it matters and what to do.

The dashboard reads the scraper's output after Find new jobs and shows the user
every problem. The contract it parses:
  - a non-fatal problem is ONE line starting "WARNING: ";
  - a fatal problem ends the run with ONE line starting "ERROR: " and exit 1,
    after the traceback (stderr, for the log).
Each line says what happened, what it costs, and what to do, in plain English.
"""
import asyncio
import gzip
import io
import json
import re
import socket
import sys
import types
from pathlib import Path

import aiohttp
import pandas as pd
import pytest

import scraper

EM_DASH = "—"


def _days_ago(n: int) -> str:
    return (pd.Timestamp.now() - pd.Timedelta(days=n)).strftime("%Y-%m-%d")


def _problem_lines(out: str, kind: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.startswith(f"{kind}: ")]


def _assert_well_formed(out: str) -> None:
    """Every WARNING/ERROR starts its own line and carries no em dash."""
    for ln in out.splitlines():
        if "WARNING:" in ln:
            assert ln.startswith("WARNING: "), ln
        if "ERROR:" in ln:
            assert ln.startswith("ERROR: "), ln
        if ln.startswith(("WARNING: ", "ERROR: ")):
            assert EM_DASH not in ln, ln


@pytest.fixture
def data(monkeypatch, tmp_path):
    """Every scraper data path in tmp_path; no Drive master, no env overrides."""
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(scraper, "MASTER_CSV", tmp_path / "linkedin_jobs_master.csv")
    monkeypatch.setattr(scraper, "PREVIOUS_IDS_FILE", tmp_path / "last_run_job_ids.json")
    monkeypatch.setattr(scraper, "EXTERNAL_EXCLUDE_FILE", tmp_path / "external_exclude_ids.json")
    monkeypatch.setattr(scraper, "LEDGER_CSV", tmp_path / "collected_ids_ledger.csv")
    monkeypatch.setattr(scraper, "BLOCKLIST_FILE", tmp_path / "company_blocklist.txt")
    monkeypatch.delenv(scraper.EXTRA_MASTER_ENV, raising=False)
    monkeypatch.delenv(scraper.EXPECT_DRIVE_MASTER_ENV, raising=False)
    monkeypatch.delenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, raising=False)
    monkeypatch.delenv(scraper.MAX_EXCLUDE_IDS_ENV, raising=False)
    return tmp_path


def _write_master(path: Path, rows) -> Path:
    path.write_text("job_posting_id,extracted_date\n"
                    + "".join(f"{jid},{dt}\n" for jid, dt in rows), encoding="utf-8")
    return path


# --- the fakes main() runs against --------------------------------------------

class _Resp:
    def __init__(self, status=200, body="", payload=None):
        self.status = status
        self._body = body
        self._payload = payload

    async def text(self):
        return self._body

    async def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status >= 400:
            raise _response_error(self.status)
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _response_error(status: int) -> aiohttp.ClientResponseError:
    """What aiohttp's raise_for_status() raises for `status`."""
    return aiohttp.ClientResponseError(
        types.SimpleNamespace(real_url="https://api.brightdata.com/x"), (),
        status=status, message="refused")


class _Session:
    """post() answers the trigger, get() answers /progress or /snapshot.
    `calls` (a list) collects every GET url, to count retries."""

    def __init__(self, post=None, get=None, get_exc=None, calls=None):
        self._post = post
        self._get = get
        self._get_exc = get_exc
        self._calls = calls

    def post(self, url, json=None):
        return self._post

    def get(self, url, **kw):
        if self._calls is not None:
            self._calls.append(url)
        if self._get_exc is not None:
            raise self._get_exc
        return self._get


# Taken at import, before conftest's hermetic stub replaces it for every test.
_REAL_CHECK = scraper.check_recovered_snapshot


@pytest.fixture
def run(monkeypatch, data):
    """main() end to end through cli(), with Bright Data replaced by fakes.

    Returns a namespace: .trigger(status, body) / .snapshot(sid) set what the
    trigger answers, .progress(payload) / .progress_error(exc) what polling
    sees, .rows(list) / .download_error(exc) what the download returns, and
    .go() runs cli() and returns the exit code."""
    label = scraper.RUN_LABELS[0]
    monkeypatch.setattr(scraper, "require_credentials", lambda: None)
    base = dict(scraper.load_search_config(), keywords=["k"],
                remote_types=["remote"], limit_per_input=5)
    monkeypatch.setattr(scraper, "load_search_config", lambda: base)
    monkeypatch.setattr(scraper, "POLL_INTERVAL", 0)

    async def no_preflight(session):
        return None

    monkeypatch.setattr(scraper, "preflight", no_preflight)
    st = types.SimpleNamespace(label=label, dir=data, trigger_resp=None,
                               trigger_exc=None, progress_resp=None,
                               progress_exc=None, download_resp=None,
                               download_exc=None, get_calls=[])
    real_trigger, real_wait, real_download = (
        scraper.trigger, scraper.wait_until_ready, scraper.download)

    async def fake_trigger(session, payload, limit_per_input=None):
        if st.trigger_exc is not None:
            raise st.trigger_exc
        return await real_trigger(_Session(post=st.trigger_resp), payload,
                                  limit_per_input or 5)

    async def fake_wait(session, snapshot_id):
        return await real_wait(_Session(get=st.progress_resp, get_exc=st.progress_exc,
                                        calls=st.get_calls), snapshot_id)

    async def fake_download(session, snapshot_id):
        return await real_download(_Session(get=st.download_resp,
                                            get_exc=st.download_exc,
                                            calls=st.get_calls), snapshot_id)

    async def fake_check(session, snapshot_id):
        # A recovery run's one free /progress read: "ready" with no errors
        # unless the test set a progress answer of its own.
        resp = st.progress_resp or _Resp(payload={"status": "ready", "records": 1})
        return await _REAL_CHECK(_Session(get=resp, get_exc=st.progress_exc,
                                          calls=st.get_calls), snapshot_id)

    monkeypatch.setattr(scraper, "trigger", fake_trigger)
    monkeypatch.setattr(scraper, "wait_until_ready", fake_wait)
    monkeypatch.setattr(scraper, "download", fake_download)
    monkeypatch.setattr(scraper, "check_recovered_snapshot", fake_check)

    def snapshot(sid="sd_1"):
        st.trigger_resp = _Resp(202, json.dumps({"snapshot_id": sid}))

    def trigger(status, body):
        st.trigger_resp = _Resp(status, body)

    def progress(payload):
        st.progress_resp = _Resp(payload=payload)

    def rows(rs):
        st.download_resp = _Resp(payload=rs)

    def go(*extra):
        return scraper.cli(["--label", label, *extra])

    st.snapshot, st.trigger, st.progress, st.rows, st.go = (
        snapshot, trigger, progress, rows, go)
    return st


def _final_error(capsys) -> tuple[str, str]:
    cap = capsys.readouterr()
    lines = [ln for ln in cap.out.splitlines() if ln.strip()]
    _assert_well_formed(cap.out)
    assert lines, "nothing printed"
    assert lines[-1].startswith("ERROR: "), lines[-1]
    assert len(_problem_lines(cap.out, "ERROR")) == 1
    return lines[-1], cap.err


# --- fatal: one final ERROR line, exit 1, traceback kept for the log -----------

def test_a_refused_token_on_the_trigger_ends_with_one_error_line(run, capsys):
    run.trigger(401, "Invalid credentials")
    assert run.go() == 1
    line, err = _final_error(capsys)
    assert "token" in line and "brightdata.com/cp/setting/users" in line
    assert "expired" in line
    assert "Traceback" in err                     # the log still gets the details


_REAL_PREFLIGHT = scraper.preflight


def test_a_refused_token_on_the_status_check_ends_with_one_error_line(
        run, monkeypatch, capsys):
    async def dead_token(session):
        return await _REAL_PREFLIGHT(_Session(get=_Resp(403, "Forbidden")))

    monkeypatch.setattr(scraper, "preflight", dead_token)
    run.snapshot()
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert "token" in line and "403" in line


def test_a_skip_list_too_large_for_every_search_is_an_error(run, capsys):
    run.snapshot("sd_big")
    run.progress({"status": "ready", "records": 0, "errors": 40,
                  "error_codes": {"child_input_size_validation": 40}})
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert "too large" in line and "nothing was collected" in line
    assert "max_exclude_ids in search_config.json" in line


def test_the_size_error_names_the_env_var_when_it_set_the_cap(run, monkeypatch, capsys):
    monkeypatch.setenv(scraper.MAX_EXCLUDE_IDS_ENV, "4000")
    run.snapshot("sd_big")
    run.progress({"status": "ready", "records": 0, "errors": 40,
                  "error_codes": {"input_size_validation": 40}})
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert "MAX_EXCLUDE_IDS environment variable (now 4000)" in line


def test_every_search_rejected_as_invalid_input_is_an_error(run, capsys):
    run.snapshot("sd_bad")
    run.progress({"status": "ready", "records": 0, "errors": 2,
                  "error_codes": {"invalid_input": 2}})
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert "invalid_input" in line and "nothing was collected" in line


@pytest.mark.parametrize("status,body", [
    (413, "Request Entity Too Large"),
    (400, "input is too large"),
    (400, "Payload Too Large"),
    (400, '{"error": "child_input_size_validation"}'),
    (400, "input_size limit exceeded"),
])
def test_a_trigger_refused_as_too_big_says_to_lower_the_cap(run, capsys, status, body):
    run.trigger(status, body)
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert "too large" in line and "max_exclude_ids" in line


@pytest.mark.parametrize("status,body", [(413, "x"), (400, "payload size limit")])
def test_trigger_puts_the_size_hint_in_its_own_error(status, body):
    with pytest.raises(scraper.RequestTooLarge) as excinfo:
        asyncio.run(scraper.trigger(_Session(post=_Resp(status, body)), {}))
    msg = str(excinfo.value)
    assert f"Trigger failed {status}" in msg and "max_exclude_ids" in msg


def test_a_plain_400_is_refused_as_invalid_not_too_large(run, capsys):
    """A 400 that says nothing about size is a bad request (a keyword or filter
    Bright Data will not take). Calling it "too large" would send the user to
    shrink the skip list for nothing."""
    run.trigger(400, '{"error": "Invalid value for field location"}')
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert "refused" in line and "invalid" in line
    assert "Invalid value for field location" in line       # Bright Data's own words
    assert "keywords and filters" in line
    assert "too large" not in line
    assert "mentions size" not in line.lower()
    assert "max_exclude_ids" not in line
    assert "try again later" not in line.lower()


@pytest.mark.parametrize("body", ['Invalid payload: keyword', '{"error": "bad payload"}',
                                  "page size must be a number"])
def test_a_400_that_only_says_payload_is_not_called_too_large(body):
    """Bright Data calls the request body the payload. Only a phrase about size
    means the skip list was too big."""
    with pytest.raises(scraper.TriggerInvalid) as excinfo:
        asyncio.run(scraper.trigger(_Session(post=_Resp(400, body)), {}))
    assert not isinstance(excinfo.value, scraper.RequestTooLarge)


def test_trigger_raises_trigger_invalid_for_a_plain_400():
    long_body = "bad request " + "x" * 2000
    with pytest.raises(scraper.TriggerInvalid) as excinfo:
        asyncio.run(scraper.trigger(_Session(post=_Resp(400, long_body)), {}))
    e = excinfo.value
    assert isinstance(e, scraper.TriggerRefused)            # still a refusal
    assert not isinstance(e, scraper.RequestTooLarge)
    assert e.status == 400
    assert len(scraper._fatal_message(e)) < 1000            # the body is bounded


def test_a_trigger_server_error_says_to_try_again_later(run, capsys):
    run.trigger(503, "Service Unavailable")
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert "503" in line and "try again later" in line.lower()


def _connector_error(cls=aiohttp.ClientConnectorError):
    key = types.SimpleNamespace(host="api.brightdata.com", port=443, ssl=True)
    return cls(key, OSError(11001, "getaddrinfo failed"))


@pytest.mark.parametrize("exc", [
    _connector_error(),
    _connector_error(aiohttp.ClientConnectorDNSError),
    aiohttp.ConnectionTimeoutError("connect timed out"),
    socket.gaierror(11001, "getaddrinfo failed"),
])
def test_a_network_failure_before_sending_says_check_the_connection(run, capsys, exc):
    """Nothing reached Bright Data, so nothing was collected or billed."""
    run.trigger_exc = exc
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert "could not reach Bright Data" in line
    assert "internet connection" in line
    assert "snapshot" not in line.lower()


@pytest.mark.parametrize("exc", [
    aiohttp.ServerDisconnectedError(),
    aiohttp.ClientPayloadError("Response payload is not completed"),
    aiohttp.ClientConnectionError("Connection closed"),
    asyncio.TimeoutError(),
    ConnectionResetError("connection reset"),
])
def test_a_connection_lost_after_sending_says_check_the_snapshot_list(run, capsys, exc):
    """The request went out, so the collection may be running and billed.
    Running again blind could pay for the same jobs twice."""
    run.trigger_exc = exc
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert re.search(r"\(\d+\.\d+ MB\)", line), line          # the request size
    assert "too large" in line                                  # one possible cause
    assert "already be running" in line                        # the other
    assert "snapshot list in the Bright Data dashboard" in line
    assert "before running again" in line.lower()
    assert "--snapshot <id>" in line and "without paying again" in line
    assert "internet connection" not in line


def test_polling_that_gives_up_hands_back_the_snapshot_rerun_command(run, capsys):
    """The collection is paid for; the user must be able to fetch it for free."""
    run.snapshot("sd_paid")
    run.progress_exc = aiohttp.ClientError("503 upstream")
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert f"--snapshot sd_paid --label {run.label}" in line
    assert "without paying again" in line


def test_a_download_that_gives_up_hands_back_the_snapshot_rerun_command(run, capsys):
    run.snapshot("sd_dl")
    run.progress({"status": "ready", "records": 3, "errors": 0})
    run.download_exc = aiohttp.ClientError("reset")
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert "--snapshot sd_dl" in line and "without paying again" in line


def test_the_60_minute_wait_running_out_hands_back_the_rerun_command(
        run, monkeypatch, capsys):
    monkeypatch.setattr(scraper, "MAX_WAIT_MINUTES", -1)       # deadline already past
    run.snapshot("sd_slow")
    run.progress({"status": "running"})
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert "still running" in line and "--snapshot sd_slow" in line


def test_a_failed_recovery_rerun_also_names_the_command(run, capsys):
    run.download_exc = aiohttp.ClientError("reset")
    assert run.go("--snapshot", "sd_again") == 1
    line, _ = _final_error(capsys)
    assert "--snapshot sd_again" in line


def test_a_failed_collection_quotes_bright_datas_message(run, capsys):
    run.snapshot("sd_fail")
    run.progress({"status": "failed", "error": "Dataset is unavailable"})
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert "failed" in line and "Dataset is unavailable" in line
    assert "--snapshot" not in line                # nothing to recover


def test_an_unreadable_master_says_where_the_jobs_were_saved(run, capsys):
    master = run.dir / "linkedin_jobs_master.csv"
    master.write_bytes(b'job_posting_id,job_title\n"1,unterminated quote\n2,B\n')
    run.snapshot("sd_m")
    run.progress({"status": "ready", "records": 1, "errors": 0})
    run.rows([{"job_posting_id": "9", "job_title": "New"}])
    assert run.go() == 1
    line, _ = _final_error(capsys)
    saved = list((run.dir / run.label).glob("linkedin_jobs_*.csv"))
    assert len(saved) == 1
    assert master.name in line and saved[0].name in line
    assert "safe" in line and "scor" in line
    # The jobs are on disk already: a --snapshot rerun would only score them twice.
    assert "--snapshot" not in line


def test_anything_else_is_named_plainly(run, capsys):
    run.trigger_exc = ValueError("something odd")
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert line.startswith("ERROR: the job search stopped unexpectedly: "
                           "ValueError: something odd")


def test_missing_credentials_end_with_an_error_line(monkeypatch, capsys):
    monkeypatch.setattr(scraper, "API_TOKEN", "")
    monkeypatch.setattr(scraper, "DATASET_ID", "")
    with pytest.raises(SystemExit) as excinfo:
        scraper.require_credentials()
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines[-1].startswith("ERROR: ") and "BRIGHT_DATA_API_TOKEN" in lines[-1]


def test_a_clean_run_exits_0_with_no_error_line(run, capsys):
    run.snapshot("sd_ok")
    run.progress({"status": "ready", "records": 1, "errors": 0})
    run.rows([{"job_posting_id": "9", "job_title": "New"}])
    assert run.go() == 0
    out = capsys.readouterr().out
    assert not _problem_lines(out, "ERROR")
    assert not _problem_lines(out, "WARNING")


# --- partial rejection: keep the paid rows, warn, exit 0 ----------------------

def test_a_partly_rejected_collection_saves_its_rows_and_warns(run, capsys):
    run.snapshot("sd_part")
    run.progress({"status": "ready", "records": 2, "errors": 91,
                  "error_codes": {"child_input_size_validation": 88, "dead_page": 3}})
    run.rows([{"job_posting_id": "1", "job_title": "A"},
              {"job_posting_id": "2", "job_title": "B"}])
    assert run.go() == 0                          # scoring still runs
    out = capsys.readouterr().out
    _assert_well_formed(out)
    assert not _problem_lines(out, "ERROR")
    (warning,) = [w for w in _problem_lines(out, "WARNING") if "rejected" in w]
    assert "child_input_size_validation" in warning
    assert "max_exclude_ids in search_config.json" in warning
    # Printed before the download, so it cannot say the jobs are saved yet.
    assert "will be saved" in warning and "are saved" not in warning
    saved = list((run.dir / run.label).glob("linkedin_jobs_*.csv"))
    assert len(saved) == 1
    assert sorted(pd.read_csv(saved[0], dtype=str)["job_posting_id"]) == ["1", "2"]
    master = pd.read_csv(run.dir / "linkedin_jobs_master.csv", dtype=str)
    assert sorted(master["job_posting_id"]) == ["1", "2"]
    ledger = pd.read_csv(run.dir / "collected_ids_ledger.csv", dtype=str)
    assert sorted(ledger["job_posting_id"]) == ["1", "2"]


def test_a_partly_invalid_collection_warns_without_the_size_hint(run, capsys):
    run.snapshot("sd_part")
    run.progress({"status": "ready", "records": 1, "errors": 1,
                  "error_codes": {"invalid_input": 1}})
    run.rows([{"job_posting_id": "1", "job_title": "A"}])
    assert run.go() == 0
    out = capsys.readouterr().out
    (warning,) = [w for w in _problem_lines(out, "WARNING") if "rejected" in w]
    assert "invalid_input" in warning and "max_exclude_ids" not in warning


# --- non-fatal: one WARNING line with the cost and the fix ---------------------

def test_a_drive_master_that_is_set_but_missing_warns(data, monkeypatch, capsys):
    monkeypatch.setenv(scraper.EXTRA_MASTER_ENV, str(data / "gone.csv.gz"))
    assert scraper.load_exclude_ids() == []
    out = capsys.readouterr().out
    _assert_well_formed(out)
    (w,) = _problem_lines(out, "WARNING")
    assert "gone.csv.gz" in w and "billed again" in w and "Google Drive" in w


def test_an_unset_drive_master_is_quiet_unless_one_was_expected(data, monkeypatch, capsys):
    """Unset is normal on the VM. The dashboard sets INPLOYED_EXPECT_DRIVE_MASTER
    for a local run, where an unset Drive master means Drive is not there."""
    scraper.load_exclude_ids()
    assert not _problem_lines(capsys.readouterr().out, "WARNING")
    monkeypatch.setenv(scraper.EXPECT_DRIVE_MASTER_ENV, "1")
    scraper.load_exclude_ids()
    out = capsys.readouterr().out
    _assert_well_formed(out)
    (w,) = _problem_lines(out, "WARNING")
    assert "Google Drive" in w and "billed again" in w


def _gz_bytes(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"))


@pytest.mark.parametrize("payload", [
    _gz_bytes("job_posting_id,extracted_date\n" + "1,2026-10-01\n" * 500)[:-30],   # EOFError
    b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03" + b"\xff" * 64,                   # zlib.error
    b"PK\x03\x04 not gzip at all",                                                # BadGzipFile
])
def test_a_damaged_drive_master_warns_and_the_run_goes_on(data, monkeypatch, capsys, payload):
    _write_master(data / "linkedin_jobs_master.csv", [("own", _days_ago(1))])
    drive = data / "drive_linkedin_jobs_master.csv.gz"
    drive.write_bytes(payload)
    monkeypatch.setenv(scraper.EXTRA_MASTER_ENV, str(drive))
    assert scraper.load_exclude_ids() == ["own"]
    out = capsys.readouterr().out
    _assert_well_formed(out)
    (w,) = _problem_lines(out, "WARNING")
    assert drive.name in w and "billed again" in w


def test_a_drive_master_that_is_not_utf8_warns(data, monkeypatch, capsys):
    drive = data / "drive.csv"
    drive.write_bytes(b"job_posting_id,title\n1,\xff\xfe\xfa bad\n")
    monkeypatch.setenv(scraper.EXTRA_MASTER_ENV, str(drive))
    scraper.load_exclude_ids()
    (w,) = _problem_lines(capsys.readouterr().out, "WARNING")
    assert "drive.csv" in w


def test_a_drive_master_without_job_ids_warns(data, monkeypatch, capsys):
    drive = data / "drive.csv"
    drive.write_text("company,title\nAcme,Engineer\n", encoding="utf-8")
    monkeypatch.setenv(scraper.EXTRA_MASTER_ENV, str(drive))
    assert scraper.load_exclude_ids() == []
    (w,) = _problem_lines(capsys.readouterr().out, "WARNING")
    assert "job_posting_id" in w and "billed again" in w


def test_an_unreadable_own_master_says_what_was_used_instead(data, monkeypatch, capsys):
    _write_master(data / "linkedin_jobs_master.csv", [("m", _days_ago(1))])
    (data / "last_run_job_ids.json").write_text(json.dumps(["prev"]), encoding="utf-8")
    real = pd.read_csv

    def locked(path, *a, **k):
        if str(path).endswith("linkedin_jobs_master.csv"):
            raise PermissionError("locked by another program")
        return real(path, *a, **k)

    monkeypatch.setattr(pd, "read_csv", locked)
    assert scraper.load_exclude_ids() == ["prev"]
    out = capsys.readouterr().out
    _assert_well_formed(out)
    (w,) = _problem_lines(out, "WARNING")
    assert "last run" in w and "billed again" in w


def test_a_master_without_job_ids_says_what_was_used_instead(data, capsys):
    (data / "linkedin_jobs_master.csv").write_text("company,title\nAcme,Eng\n",
                                                   encoding="utf-8")
    scraper.load_exclude_ids()
    (w,) = _problem_lines(capsys.readouterr().out, "WARNING")
    assert "job_posting_id" in w and "last run" in w and "billed again" in w


def test_a_master_without_dates_warns(data, capsys):
    (data / "linkedin_jobs_master.csv").write_text("job_posting_id\na\nb\n",
                                                   encoding="utf-8")
    assert set(scraper.load_exclude_ids()) == {"a", "b"}
    out = capsys.readouterr().out
    _assert_well_formed(out)
    (w,) = _problem_lines(out, "WARNING")
    assert "extracted_date" in w


def test_a_missing_master_falls_back_to_last_run_ids_and_says_so(data, capsys):
    (data / "last_run_job_ids.json").write_text(json.dumps(["prev"]), encoding="utf-8")
    assert scraper.load_exclude_ids() == ["prev"]
    (w,) = _problem_lines(capsys.readouterr().out, "WARNING")
    assert "linkedin_jobs_master.csv" in w and "last run" in w and "billed again" in w


def test_a_first_run_with_nothing_yet_is_quiet(data, capsys):
    assert scraper.load_exclude_ids() == []
    assert not _problem_lines(capsys.readouterr().out, "WARNING")


def test_unreadable_last_run_and_external_files_warn(data, capsys):
    (data / "last_run_job_ids.json").write_text("not json{", encoding="utf-8")
    (data / "external_exclude_ids.json").write_text("not json{", encoding="utf-8")
    assert scraper.load_exclude_ids() == []
    out = capsys.readouterr().out
    _assert_well_formed(out)
    warnings = _problem_lines(out, "WARNING")
    assert any("last_run_job_ids.json" in w for w in warnings)
    assert any("external_exclude_ids.json" in w and "billed again" in w for w in warnings)


def test_an_unreadable_search_config_warns_once_per_process(data, capsys):
    (data / "search_config.json").write_text("{not json", encoding="utf-8")
    for _ in range(4):
        cfg = scraper.load_search_config()
    assert cfg["keywords"] == scraper.KEYWORDS
    out = capsys.readouterr().out
    _assert_well_formed(out)
    (w,) = _problem_lines(out, "WARNING")
    assert "search_config.json" in w and "built-in" in w


def test_an_unreadable_blocklist_warns(data, capsys):
    (data / "company_blocklist.txt").mkdir()          # reading a folder is an OSError
    assert scraper.load_blocklist() == scraper.COMPANY_BLOCKLIST
    out = capsys.readouterr().out
    _assert_well_formed(out)
    (w,) = _problem_lines(out, "WARNING")
    assert "company_blocklist.txt" in w and "blocked" in w


@pytest.mark.parametrize("status", [500, 503, 429])
def test_a_status_check_that_answers_oddly_warns_and_goes_on(capsys, status):
    asyncio.run(scraper.preflight(_Session(get=_Resp(status, "boom"))))
    out = capsys.readouterr().out
    _assert_well_formed(out)
    (w,) = _problem_lines(out, "WARNING")
    assert str(status) in w and "went ahead" in w


def test_a_status_check_that_cannot_be_reached_warns_and_goes_on(capsys):
    asyncio.run(scraper.preflight(_Session(get_exc=aiohttp.ClientError("offline"))))
    (w,) = _problem_lines(capsys.readouterr().out, "WARNING")
    assert "went ahead" in w


def test_a_healthy_status_check_is_quiet(capsys):
    asyncio.run(scraper.preflight(_Session(get=_Resp(200, "{}"))))
    assert not _problem_lines(capsys.readouterr().out, "WARNING")


def test_zero_rows_with_page_errors_is_an_info_line(capsys):
    """A quiet day looks like this, and so does the small test run: a WARNING
    here would open the problems box on normal runs."""
    asyncio.run(scraper.wait_until_ready(_Session(get=_Resp(payload={
        "status": "ready", "records": 0, "errors": 7,
        "error_codes": {"dead_page": 7}})), "sd_q"))
    out = capsys.readouterr().out
    _assert_well_formed(out)
    assert not _problem_lines(out, "WARNING")
    (info,) = [ln for ln in out.splitlines() if "0 jobs" in ln]
    assert "dead_page" in info and "several runs" in info
    assert "No search was rejected" in info


def test_zero_rows_with_errors_but_no_codes_claims_nothing_about_rejection(capsys):
    asyncio.run(scraper.wait_until_ready(_Session(get=_Resp(payload={
        "status": "ready", "records": 0, "errors": 3})), "sd_q"))
    out = capsys.readouterr().out
    assert not _problem_lines(out, "WARNING")
    (info,) = [ln for ln in out.splitlines() if "0 jobs" in ln]
    assert "No search was rejected" not in info
    assert "did not say" in info


def test_a_bad_env_cap_or_window_warns_once(data, monkeypatch, capsys):
    monkeypatch.setenv(scraper.MAX_EXCLUDE_IDS_ENV, "lots")
    monkeypatch.setenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, "-3")
    for _ in range(3):
        scraper.max_exclude_ids()
        scraper.exclude_window_days()
    out = capsys.readouterr().out
    _assert_well_formed(out)
    warnings = _problem_lines(out, "WARNING")
    assert len(warnings) == 2
    assert any("MAX_EXCLUDE_IDS" in w for w in warnings)
    assert any("EXCLUDE_WINDOW_DAYS" in w for w in warnings)


# --- the source keeps to the contract ------------------------------------------

def test_every_warning_goes_through_warn():
    """A hand-rolled print("  WARNING: ...") would be indented or multi-line,
    and the dashboard reads lines that START with "WARNING: "."""
    src = Path(scraper.__file__).read_text(encoding="utf-8")
    allowed = {"warn", "require_credentials", "run"}     # the three that print the prefix
    offenders = []
    for m in re.finditer(r"print\(\s*f?[\"']\s*(?:WARNING|ERROR|NOTE)", src):
        enclosing = re.findall(r"^(?:async )?def (\w+)", src[:m.start()], re.MULTILINE)[-1]
        if enclosing not in allowed:
            offenders.append(enclosing)
    assert offenders == []


# --- after the run file is saved, a failure says the jobs are safe -------------

def _today() -> str:
    return pd.Timestamp.now().strftime("%Y-%m-%d")


def _run_files(st) -> list[str]:
    d = st.dir / st.label
    return sorted(p.name for p in d.glob("linkedin_jobs_*")) if d.is_dir() else []


def _lock_master(monkeypatch, master: Path) -> list:
    """os.replace onto the master fails the way it does while Excel has it open.
    Returns a list; clear it to unlock."""
    locked = [True]
    real = scraper.os.replace

    def replace(src, dst):
        if locked and Path(dst) == master:
            raise PermissionError(13, "The process cannot access the file because it "
                                      "is being used by another process", str(dst))
        return real(src, dst)

    monkeypatch.setattr(scraper.os, "replace", replace)
    return locked


def test_a_master_open_in_excel_says_the_jobs_are_safe(run, monkeypatch, capsys):
    master = _write_master(run.dir / "linkedin_jobs_master.csv", [("old", _days_ago(1))])
    _lock_master(monkeypatch, master)
    run.snapshot("sd_x")
    run.progress({"status": "ready", "records": 2, "errors": 0})
    run.rows([{"job_posting_id": "1", "job_title": "A"},
              {"job_posting_id": "2", "job_title": "B"}])
    assert run.go() == 1
    line, err = _final_error(capsys)
    (saved,) = _run_files(run)
    assert saved in line and master.name in line
    assert "2 job(s)" in line and "safe" in line and "scor" in line
    assert "Excel" in line and "restore" in line
    assert "--snapshot" not in line                 # rerunning it would score them twice
    assert "PermissionError" in err                 # the log keeps the details
    # The ledger still has them, so the next run skips them.
    ledger = pd.read_csv(run.dir / "collected_ids_ledger.csv", dtype=str)
    assert sorted(ledger["job_posting_id"]) == ["1", "2"]


def test_a_failed_last_run_ids_save_is_a_master_not_updated_error(run, monkeypatch, capsys):
    def boom(ids):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(scraper, "save_current_ids", boom)
    run.snapshot("sd_x")
    run.progress({"status": "ready", "records": 1, "errors": 0})
    run.rows([{"job_posting_id": "1", "job_title": "A"}])
    assert run.go() == 1
    line, _ = _final_error(capsys)
    (saved,) = _run_files(run)
    assert saved in line and "safe" in line and "No space left" in line
    assert "--snapshot" not in line


def test_a_failed_save_then_a_snapshot_rerun_gives_one_run_file(run, monkeypatch, capsys):
    """The user followed the old advice (rerun with --snapshot) and got a -2 file
    holding the same jobs, which the scorer then paid to score again."""
    master = _write_master(run.dir / "linkedin_jobs_master.csv", [("old", _days_ago(1))])
    locked = _lock_master(monkeypatch, master)
    run.snapshot("sd_x")
    run.progress({"status": "ready", "records": 2, "errors": 0})
    run.rows([{"job_posting_id": "1", "job_title": "A"},
              {"job_posting_id": "2", "job_title": "B"}])
    assert run.go() == 1
    first = _run_files(run)
    assert first == [f"linkedin_jobs_{_today()}_{run.label}.csv"]
    capsys.readouterr()

    locked.clear()                                   # Excel closed
    assert run.go("--snapshot", "sd_x") == 0
    assert _run_files(run) == first                  # the same file, no -2
    out = capsys.readouterr().out
    assert first[0] in out and "earlier attempt" in out
    master_ids = pd.read_csv(master, dtype=str)["job_posting_id"]
    assert sorted(master_ids) == ["1", "2", "old"]


def test_a_snapshot_rerun_of_a_scored_file_writes_no_new_run_file(run, capsys):
    stem = f"linkedin_jobs_{_today()}_{run.label}"
    d = run.dir / run.label
    d.mkdir()
    pd.DataFrame({"job_posting_id": ["1", "2"], "score": [7, 3]}).to_csv(
        d / f"{stem}_scored.csv.gz", index=False, compression="gzip")
    run.rows([{"job_posting_id": "2", "job_title": "B"},
              {"job_posting_id": "1", "job_title": "A"}])
    assert run.go("--snapshot", "sd_x") == 0
    assert _run_files(run) == [f"{stem}_scored.csv.gz"]
    out = capsys.readouterr().out
    assert "already scored" in out and "no new run file" in out
    assert not _problem_lines(out, "WARNING")
    master = pd.read_csv(run.dir / "linkedin_jobs_master.csv", dtype=str)
    assert sorted(master["job_posting_id"]) == ["1", "2"]    # the master still catches up


def test_a_rerun_with_different_jobs_still_gets_its_own_file(run, capsys):
    stem = f"linkedin_jobs_{_today()}_{run.label}"
    d = run.dir / run.label
    d.mkdir()
    (d / f"{stem}.csv").write_text("job_posting_id\n1\n", encoding="utf-8")
    run.rows([{"job_posting_id": "1"}, {"job_posting_id": "2"}])
    assert run.go("--snapshot", "sd_y") == 0
    assert _run_files(run) == [f"{stem}-2.csv", f"{stem}.csv"]


# --- the ERROR line survives a console that cannot print the character ---------

def _cp1252_stdout(monkeypatch):
    buf = io.BytesIO()
    out = io.TextIOWrapper(buf, encoding="cp1252", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stdout", out)
    return buf


def test_cli_prints_a_non_cp1252_error_line_as_utf8(run, monkeypatch):
    """Under the dashboard's pipe the child's stdout is cp1252, so an arrow or
    CJK in Bright Data's answer raised UnicodeEncodeError and lost the ERROR line."""
    buf = _cp1252_stdout(monkeypatch)
    run.trigger(503, "Service → 不可用")
    assert run.go() == 1
    sys.stdout.flush()
    lines = [ln for ln in buf.getvalue().decode("utf-8").splitlines() if ln.strip()]
    assert lines[-1].startswith("ERROR: ")
    assert "→" in lines[-1] and "不可用" in lines[-1]


def test_run_falls_back_to_ascii_when_stdout_cannot_take_the_line(run, monkeypatch):
    buf = _cp1252_stdout(monkeypatch)                 # run() alone does not reconfigure
    run.trigger(503, "Service → down")
    assert scraper.run(run_label=run.label) == 1
    lines = [ln for ln in buf.getvalue().decode("cp1252").splitlines() if ln.strip()]
    assert lines[-1].startswith("ERROR: ") and "\\u2192" in lines[-1]


def test_a_warning_with_a_non_cp1252_character_still_prints(monkeypatch):
    buf = _cp1252_stdout(monkeypatch)
    scraper.warn("the file 不.csv is gone")
    text = buf.getvalue().decode("cp1252")
    assert text.startswith("WARNING: ") and "\\u4e0d" in text


# --- rows that came back with the POST are never lost --------------------------

def _raw_files(st) -> list[Path]:
    d = st.dir / st.label
    return sorted(d.glob("*_raw_*.json")) if d.is_dir() else []


def test_synchronous_rows_are_kept_in_a_raw_file_when_the_save_fails(run, monkeypatch, capsys):
    real = scraper._atomic_to_csv

    def no_run_csv(df, path, **kw):
        if Path(path).parent.name == run.label:
            raise OSError(28, "No space left on device")
        return real(df, path, **kw)

    monkeypatch.setattr(scraper, "_atomic_to_csv", no_run_csv)
    run.trigger(200, json.dumps([{"job_posting_id": "1", "job_title": "A"},
                                 {"job_posting_id": "2", "job_title": "B"}]))
    assert run.go() == 1
    line, _ = _final_error(capsys)
    (raw,) = _raw_files(run)
    assert re.fullmatch(rf"linkedin_jobs_{_today()}_{run.label}_raw_\d{{6}}\.json", raw.name)
    assert raw.name in line and "keep" in line.lower()
    assert [r["job_posting_id"] for r in json.loads(raw.read_text(encoding="utf-8"))] == ["1", "2"]


def test_synchronous_rows_raw_file_is_removed_once_saved(run, capsys):
    run.trigger(200, json.dumps([{"job_posting_id": "1", "job_title": "A"}]))
    assert run.go() == 0
    assert _raw_files(run) == []
    assert _run_files(run) == [f"linkedin_jobs_{_today()}_{run.label}.csv"]


def test_synchronous_rows_with_a_locked_master_point_at_the_run_file(run, monkeypatch, capsys):
    master = _write_master(run.dir / "linkedin_jobs_master.csv", [("old", _days_ago(1))])
    _lock_master(monkeypatch, master)
    run.trigger(200, json.dumps([{"job_posting_id": "1", "job_title": "A"}]))
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert _raw_files(run) == []                    # the run file holds the jobs
    (saved,) = _run_files(run)
    assert saved in line and "safe" in line


def test_the_raw_rows_file_is_invisible_to_every_run_file_reader(tmp_path, monkeypatch):
    import jobsdata
    import score_jobs
    label = scraper.RUN_LABELS[0]
    d = tmp_path / label
    d.mkdir()
    raw = d / f"linkedin_jobs_{_today()}_{label}_raw_013000.json"
    raw.write_text('[{"job_posting_id": "1"}]', encoding="utf-8")
    assert not list(d.glob("linkedin_jobs_*.csv"))
    assert not list(d.glob("*_scored.csv.gz")) and not list(d.glob("*_scored.csv*"))
    assert jobsdata.unscored_run_csvs(tmp_path) == []
    assert jobsdata.local_run_files(tmp_path) == []
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    assert score_jobs.latest_input_csv() is None
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    assert scraper.seed_ledger_from_run_files().empty


def test_synchronous_zero_rows_is_an_info_line(run, capsys):
    run.trigger(200, "[]")
    assert run.go() == 0
    out = capsys.readouterr().out
    assert not _problem_lines(out, "WARNING")
    assert "0 jobs" in out


# --- Ctrl+C ----------------------------------------------------------------------

def test_ctrl_c_after_the_trigger_says_how_to_fetch_the_paid_collection(run, capsys):
    run.snapshot("sd_int")
    run.progress_exc = KeyboardInterrupt()
    assert run.go() == 130
    line, _ = _final_error(capsys)
    assert "stopped" in line and "--snapshot sd_int" in line
    assert "without paying again" in line


def test_ctrl_c_before_the_trigger_has_nothing_to_fetch(run, capsys):
    run.trigger_exc = KeyboardInterrupt()
    assert run.go() == 130
    line, _ = _final_error(capsys)
    assert "stopped" in line and "--snapshot" not in line


# --- a --snapshot recovery reads /progress once, for free ------------------------

def test_a_recovery_of_a_fully_rejected_snapshot_says_so(run, capsys):
    run.progress({"status": "ready", "records": 0, "errors": 4,
                  "error_codes": {"child_input_size_validation": 4}})
    run.rows([])
    assert run.go("--snapshot", "sd_rej") == 1
    line, _ = _final_error(capsys)
    assert "too large" in line and "nothing was collected" in line


def test_a_recovery_of_a_partly_rejected_snapshot_warns_and_saves(run, capsys):
    run.progress({"status": "ready", "records": 1, "errors": 4,
                  "error_codes": {"invalid_input": 4}})
    run.rows([{"job_posting_id": "1"}])
    assert run.go("--snapshot", "sd_part") == 0
    out = capsys.readouterr().out
    (w,) = [w for w in _problem_lines(out, "WARNING") if "rejected" in w]
    assert "invalid_input" in w
    assert len(_run_files(run)) == 1


def test_a_recovery_whose_progress_check_fails_warns_and_goes_on(run, capsys):
    run.progress_exc = aiohttp.ClientConnectionError("reset")
    run.rows([{"job_posting_id": "1"}])
    assert run.go("--snapshot", "sd_ok") == 0
    out = capsys.readouterr().out
    _assert_well_formed(out)
    (w,) = _problem_lines(out, "WARNING")
    assert "sd_ok" in w and "rejected" in w
    assert len(_run_files(run)) == 1


def test_a_recovery_progress_check_costs_one_request(run, capsys):
    run.rows([{"job_posting_id": "1"}])
    assert run.go("--snapshot", "sd_ok") == 0
    assert len([u for u in run.get_calls if "/progress/sd_ok" in u]) == 1


# --- a 4xx while polling or downloading is permanent ------------------------------

def test_a_mistyped_snapshot_id_fails_at_once_with_a_clear_message(run, capsys):
    run.progress_exc = _response_error(404)
    run.download_exc = _response_error(404)
    assert run.go("--snapshot", "sd_typo") == 1
    line, _ = _final_error(capsys)
    assert "sd_typo" in line and "404" in line and "check the" in line.lower()
    assert "already paid for" not in line
    assert len(run.get_calls) == 1                   # no retries, no download after it


def test_a_404_on_the_download_is_not_retried(run, capsys):
    run.snapshot("sd_gone")
    run.progress({"status": "ready", "records": 3, "errors": 0})
    run.download_exc = _response_error(404)
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert "sd_gone" in line and "404" in line
    assert len([u for u in run.get_calls if "/snapshot/" in u]) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_while_polling_fails_at_once(run, capsys, status):
    run.snapshot("sd_tok")
    run.progress_exc = _response_error(status)
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert "token" in line and str(status) in line
    assert "--snapshot sd_tok" in line             # paid for; fetch it once the token works
    assert len([u for u in run.get_calls if "/progress/" in u]) == 1


def test_a_429_while_polling_is_still_retried(run, capsys):
    run.snapshot("sd_busy")
    run.progress_exc = _response_error(429)
    assert run.go() == 1
    assert len([u for u in run.get_calls if "/progress/" in u]) == scraper.MAX_POLL_FAILURES
    line, _ = _final_error(capsys)
    assert "--snapshot sd_busy" in line
