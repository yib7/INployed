"""The scraper says what went wrong, why it matters and what to do.

The dashboard reads the scraper's output after Find new jobs and shows the user
every problem (SP4). The contract it parses:
  - a non-fatal problem is ONE line starting "WARNING: ";
  - a fatal problem ends the run with ONE line starting "ERROR: " and exit 1,
    after the traceback (stderr, for the log).
Each line says what happened, what it costs, and what to do, in plain English.
"""
import asyncio
import gzip
import json
import re
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
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """post() answers the trigger, get() answers /progress or /snapshot."""

    def __init__(self, post=None, get=None, get_exc=None):
        self._post = post
        self._get = get
        self._get_exc = get_exc

    def post(self, url, json=None):
        return self._post

    def get(self, url, **kw):
        if self._get_exc is not None:
            raise self._get_exc
        return self._get


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
                               download_exc=None)
    real_trigger, real_wait, real_download = (
        scraper.trigger, scraper.wait_until_ready, scraper.download)

    async def fake_trigger(session, payload, limit_per_input=None):
        if st.trigger_exc is not None:
            raise st.trigger_exc
        return await real_trigger(_Session(post=st.trigger_resp), payload,
                                  limit_per_input or 5)

    async def fake_wait(session, snapshot_id):
        return await real_wait(_Session(get=st.progress_resp, get_exc=st.progress_exc),
                               snapshot_id)

    async def fake_download(session, snapshot_id):
        return await real_download(_Session(get=st.download_resp,
                                            get_exc=st.download_exc), snapshot_id)

    monkeypatch.setattr(scraper, "trigger", fake_trigger)
    monkeypatch.setattr(scraper, "wait_until_ready", fake_wait)
    monkeypatch.setattr(scraper, "download", fake_download)

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
    (400, "payload exceeds the limit"),
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
    assert "if the message mentions size" in line.lower()
    assert "max_exclude_ids in search_config.json" in line
    assert "try again later" not in line.lower()


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


@pytest.mark.parametrize("exc", [
    aiohttp.ClientConnectionError("Cannot connect to host api.brightdata.com"),
    asyncio.TimeoutError(),
    ConnectionResetError("connection reset"),
])
def test_a_network_failure_says_check_the_connection(run, capsys, exc):
    run.trigger_exc = exc
    assert run.go() == 1
    line, _ = _final_error(capsys)
    assert "could not reach Bright Data" in line
    assert "internet connection" in line


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
    assert "--snapshot sd_m" in line


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


def test_zero_rows_with_page_errors_says_why_and_what_to_do(capsys):
    asyncio.run(scraper.wait_until_ready(_Session(get=_Resp(payload={
        "status": "ready", "records": 0, "errors": 7,
        "error_codes": {"dead_page": 7}})), "sd_q"))
    out = capsys.readouterr().out
    _assert_well_formed(out)
    (w,) = _problem_lines(out, "WARNING")
    assert "0 jobs" in w and "dead_page" in w and "several runs" in w


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
