"""Bounding controls for scraper.py — cap keywords and per-input limit so a
verification run can't fire thousands of billed Bright Data collections.

Importing `scraper` at module scope (below) is also the regression guard for the
credential check: it must be deferred to run time, not fire at import, so the
module stays importable on a clean machine with no Bright Data creds."""
import asyncio
import json
import os
import urllib.error

import aiohttp

import pandas as pd
import pytest

import scraper


def test_require_credentials_exits_when_missing(monkeypatch):
    monkeypatch.setattr(scraper, "API_TOKEN", "")
    monkeypatch.setattr(scraper, "DATASET_ID", "")
    with pytest.raises(SystemExit):
        scraper.require_credentials()


def test_require_credentials_passes_when_set(monkeypatch):
    monkeypatch.setattr(scraper, "API_TOKEN", "token")
    monkeypatch.setattr(scraper, "DATASET_ID", "dataset")
    scraper.require_credentials()  # must not raise


def test_max_keywords_caps_inputs():
    inputs = scraper.build_inputs([], max_keywords=2)
    keywords = {i["keyword"] for i in inputs}
    assert len(keywords) == 2
    # one input per (kept keyword x remote type)
    assert len(inputs) == 2 * len(scraper.REMOTE_TYPES)


def test_no_cap_uses_all_keywords():
    inputs = scraper.build_inputs([])
    assert len(inputs) == len(scraper.KEYWORDS) * len(scraper.REMOTE_TYPES)


def test_max_keywords_larger_than_list_is_safe():
    inputs = scraper.build_inputs([], max_keywords=10_000)
    assert len(inputs) == len(scraper.KEYWORDS) * len(scraper.REMOTE_TYPES)


def test_exclude_ids_threaded_into_each_input():
    inputs = scraper.build_inputs(["123", "456"], max_keywords=1)
    assert all(i["jobs_to_not_include"] == ["123", "456"] for i in inputs)


def _master(tmp_path, *ids):
    master = tmp_path / "linkedin_jobs_master.csv"
    master.write_text("job_posting_id,extracted_date\n"
                      + "".join(f"{i},\n" for i in ids), encoding="utf-8")
    return master


def _days_ago(n: int) -> str:
    """An extracted_date string (YYYY-MM-DD, matching scraper.py's own format) n
    days before now — used so recency tests never rot as the calendar advances."""
    return (pd.Timestamp.now() - pd.Timedelta(days=n)).strftime("%Y-%m-%d")


def _dated_master(tmp_path, rows):
    """A master from (id, extracted_date) pairs; an empty date -> an undated row."""
    master = tmp_path / "linkedin_jobs_master.csv"
    body = "job_posting_id,extracted_date\n" + "".join(f"{jid},{dt}\n" for jid, dt in rows)
    master.write_text(body, encoding="utf-8")
    return master


def test_load_exclude_ids_windows_out_stale_ids(monkeypatch, tmp_path):
    # P1-2 step 2: the search filter is time_range="Past 24 hours", so a posting
    # scraped long ago can't reappear -- keeping its id in the exclude set is pure
    # payload that eventually overflows Bright Data's trigger-POST size limit. So a
    # far-past id is DROPPED, while a recent id (still re-collectable) and an undated
    # row (treated as recent -- superset fail direction) are kept.
    master = _dated_master(tmp_path, [
        ("old", _days_ago(400)),
        ("recent", _days_ago(3)),
        ("undated", ""),
    ])
    monkeypatch.setattr(scraper, "MASTER_CSV", master)
    monkeypatch.setattr(scraper, "EXTERNAL_EXCLUDE_FILE", tmp_path / "external_exclude_ids.json")
    monkeypatch.delenv(scraper.EXTRA_MASTER_ENV, raising=False)
    monkeypatch.delenv("EXCLUDE_WINDOW_DAYS", raising=False)
    assert set(scraper.load_exclude_ids()) == {"recent", "undated"}


def test_master_ids_excludes_stale_keeps_recent(monkeypatch, tmp_path):
    # _master_ids applies the same window: a far-past id is gone, a recent one stays.
    monkeypatch.setattr(scraper, "MASTER_CSV",
                        _dated_master(tmp_path, [("old", _days_ago(365)), ("recent", _days_ago(1))]))
    monkeypatch.delenv("EXCLUDE_WINDOW_DAYS", raising=False)
    ids = set(scraper._master_ids())
    assert "recent" in ids
    assert "old" not in ids


def test_master_ids_keeps_undated_row(monkeypatch, tmp_path):
    # Superset-fail rule: a row whose extracted_date is missing/unparseable (NaT) is
    # KEPT -- an undated id is treated as recent so we never under-exclude and re-bill.
    monkeypatch.setattr(scraper, "MASTER_CSV",
                        _dated_master(tmp_path, [("undated", ""), ("garbage", "not-a-date")]))
    monkeypatch.delenv("EXCLUDE_WINDOW_DAYS", raising=False)
    assert set(scraper._master_ids()) == {"undated", "garbage"}


def test_master_ids_no_extracted_date_column_keeps_all(monkeypatch, tmp_path):
    # A master with NO extracted_date column at all can't be windowed -> degrade to the
    # pre-window keep-ALL behavior (never silently empty the exclude set).
    master = tmp_path / "linkedin_jobs_master.csv"
    master.write_text("job_posting_id\na\nb\nc\n", encoding="utf-8")
    monkeypatch.setattr(scraper, "MASTER_CSV", master)
    monkeypatch.delenv("EXCLUDE_WINDOW_DAYS", raising=False)
    assert set(scraper._master_ids()) == {"a", "b", "c"}


def test_master_ids_windowing_failure_degrades_to_keep_all(monkeypatch, tmp_path):
    # Defensive (review finding #2): windowing is an optimization on top of the
    # exclude set. If _window_ids ever raises (e.g. an unexpected extracted_date
    # dtype), _master_ids degrades to keeping ALL master ids -- a superset never
    # re-bills -- instead of letting the exception kill the pre-collection step.
    master = _dated_master(tmp_path, [("old", _days_ago(400)), ("recent", _days_ago(1))])
    monkeypatch.setattr(scraper, "MASTER_CSV", master)
    monkeypatch.delenv("EXCLUDE_WINDOW_DAYS", raising=False)

    def _boom(df, window_days):
        raise TypeError("simulated tz-aware/naive comparison failure")

    monkeypatch.setattr(scraper, "_window_ids", _boom)
    # both ids kept (the far-past "old" too) -- the whole set, not the windowed subset
    assert set(scraper._master_ids()) == {"old", "recent"}


def test_exclude_window_days_env_override_changes_cutoff(monkeypatch, tmp_path):
    # A 30-day-old id is inside the default 90-day window (kept) but outside a
    # 10-day override window (dropped) -- proving the env var moves the cutoff.
    monkeypatch.setattr(scraper, "MASTER_CSV", _dated_master(tmp_path, [("mid", _days_ago(30))]))
    # exclude_window_days() falls through to search_config.json when the env var is
    # unset, so point OUTPUT_DIR at an empty tmp_path or the repo's own config leaks in.
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)

    monkeypatch.delenv("EXCLUDE_WINDOW_DAYS", raising=False)
    assert set(scraper._master_ids()) == {"mid"}          # default 90 -> kept

    monkeypatch.setenv("EXCLUDE_WINDOW_DAYS", "10")
    assert scraper._master_ids() == []                    # window 10 -> dropped


def test_exclude_window_days_default_and_env(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)   # no search_config.json here
    monkeypatch.delenv("EXCLUDE_WINDOW_DAYS", raising=False)
    assert scraper.exclude_window_days() == 90            # default
    monkeypatch.setenv("EXCLUDE_WINDOW_DAYS", "30")
    assert scraper.exclude_window_days() == 30            # honored


def test_exclude_window_days_invalid_falls_back_to_default(monkeypatch, tmp_path):
    # A garbage / non-positive value must never empty the window (that would re-bill
    # every posting) -- it falls back to the safe default instead.
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)   # no search_config.json here
    for bad in ("banana", "0", "-5", ""):
        monkeypatch.setenv("EXCLUDE_WINDOW_DAYS", bad)
        assert scraper.exclude_window_days() == 90


def test_load_extra_master_ids_windows_stale(monkeypatch, tmp_path):
    # The extra (synced Drive) master is windowed the same way _master_ids is.
    extra = tmp_path / "drive_master.csv"
    extra.write_text(
        "job_posting_id,extracted_date\n"
        f"xold,{_days_ago(200)}\n"
        f"xnew,{_days_ago(2)}\n"
        "xun,\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(scraper.EXTRA_MASTER_ENV, str(extra))
    monkeypatch.delenv("EXCLUDE_WINDOW_DAYS", raising=False)
    assert set(scraper.load_extra_master_ids()) == {"xnew", "xun"}


def test_load_exclude_ids_unions_external_file(monkeypatch, tmp_path):
    # The VM must skip ids a manual run on another machine just collected: those land
    # in external_exclude_ids.json and are unioned on top of this host's master.
    monkeypatch.setattr(scraper, "MASTER_CSV", _master(tmp_path, "a", "b"))
    ext = tmp_path / "external_exclude_ids.json"
    ext.write_text(json.dumps(["b", "c", "d"]), encoding="utf-8")
    monkeypatch.setattr(scraper, "EXTERNAL_EXCLUDE_FILE", ext)
    assert set(scraper.load_exclude_ids()) == {"a", "b", "c", "d"}  # union, deduped


def _gz_master(tmp_path, *ids):
    """A gzipped master (the synced Drive master is .csv.gz) with the given ids."""
    import pandas as pd
    p = tmp_path / "drive_master.csv.gz"
    pd.DataFrame({"job_posting_id": list(ids)}).to_csv(p, index=False, compression="gzip")
    return p


def test_load_extra_master_ids_reads_named_csv(monkeypatch, tmp_path):
    # The local dashboard points $LINKEDIN_EXTRA_MASTER at the synced Drive master so a
    # local run also excludes jobs the VM already collected (the local master is a stub).
    extra = _master(tmp_path, "x", "y")
    monkeypatch.setenv(scraper.EXTRA_MASTER_ENV, str(extra))
    assert set(scraper.load_extra_master_ids()) == {"x", "y"}


def test_load_extra_master_ids_reads_gzip(monkeypatch, tmp_path):
    monkeypatch.setenv(scraper.EXTRA_MASTER_ENV, str(_gz_master(tmp_path, "g1", "g2")))
    assert set(scraper.load_extra_master_ids()) == {"g1", "g2"}


def test_load_extra_master_ids_unset_or_missing_is_empty(monkeypatch, tmp_path):
    monkeypatch.delenv(scraper.EXTRA_MASTER_ENV, raising=False)
    assert scraper.load_extra_master_ids() == []                     # unset -> []
    monkeypatch.setenv(scraper.EXTRA_MASTER_ENV, str(tmp_path / "nope.csv.gz"))
    assert scraper.load_extra_master_ids() == []                     # missing -> [] (never crashes)


def test_load_exclude_ids_unions_extra_master(monkeypatch, tmp_path):
    # The local-side fix: a local run excludes the local stub master, the external file,
    # AND the synced Drive master named by $LINKEDIN_EXTRA_MASTER -> no re-billed dupes.
    monkeypatch.setattr(scraper, "MASTER_CSV", _master(tmp_path, "a", "b"))
    monkeypatch.setattr(scraper, "EXTERNAL_EXCLUDE_FILE", tmp_path / "external_exclude_ids.json")
    monkeypatch.setenv(scraper.EXTRA_MASTER_ENV, str(_gz_master(tmp_path, "b", "c", "d")))
    assert set(scraper.load_exclude_ids()) == {"a", "b", "c", "d"}   # union, deduped


def test_load_exclude_ids_ignores_extra_master_when_unset(monkeypatch, tmp_path):
    # On the VM (no $LINKEDIN_EXTRA_MASTER) exclusion is unchanged — its own master is full.
    monkeypatch.setattr(scraper, "MASTER_CSV", _master(tmp_path, "a", "b"))
    monkeypatch.setattr(scraper, "EXTERNAL_EXCLUDE_FILE", tmp_path / "external_exclude_ids.json")
    monkeypatch.delenv(scraper.EXTRA_MASTER_ENV, raising=False)
    assert set(scraper.load_exclude_ids()) == {"a", "b"}


def test_load_external_exclude_ids_absent_or_bad_is_empty(monkeypatch, tmp_path):
    missing = tmp_path / "external_exclude_ids.json"
    monkeypatch.setattr(scraper, "EXTERNAL_EXCLUDE_FILE", missing)
    assert scraper.load_external_exclude_ids() == []
    missing.write_text("not json{", encoding="utf-8")
    assert scraper.load_external_exclude_ids() == []  # unreadable -> [] (never crashes a run)


def test_write_external_exclude_ids_dumps_full_known_set(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper, "MASTER_CSV", _master(tmp_path, "a", "b"))
    out = tmp_path / "push.json"
    monkeypatch.setattr(scraper, "EXTERNAL_EXCLUDE_FILE", tmp_path / "external_exclude_ids.json")
    written = scraper.write_external_exclude_ids(out)
    assert written == out
    assert set(json.loads(out.read_text(encoding="utf-8"))) == {"a", "b"}


# P1-2: append_to_master must ABORT (never fabricate an empty/partial master) when
# an existing master can't be read, and all writes must be atomic (tmp + os.replace)
# so a crash mid-write never truncates the cumulative master.

def test_append_to_master_aborts_when_existing_master_unreadable(monkeypatch, tmp_path):
    # A master corrupted by a previous partial write (or locked by AV/sync) must
    # never be silently treated as empty -- that would blow away the exclude set
    # and re-bill already-collected jobs. It must abort loudly instead.
    master = tmp_path / "linkedin_jobs_master.csv"
    master.write_bytes(b'job_posting_id,job_title\n"1,unterminated quote\n2,B\n')
    before = master.read_bytes()
    monkeypatch.setattr(scraper, "MASTER_CSV", master)

    df = pd.DataFrame([{"job_posting_id": "9", "job_title": "New"}])
    with pytest.raises((OSError, SystemExit)):
        scraper.append_to_master(df)

    assert master.read_bytes() == before          # untouched: no silent truncation


def test_append_to_master_abort_message_names_file_and_recovery(monkeypatch, tmp_path):
    master = tmp_path / "linkedin_jobs_master.csv"
    master.write_bytes(b'job_posting_id,job_title\n"1,unterminated quote\n2,B\n')
    monkeypatch.setattr(scraper, "MASTER_CSV", master)

    df = pd.DataFrame([{"job_posting_id": "9", "job_title": "New"}])
    with pytest.raises((OSError, SystemExit)) as exc_info:
        scraper.append_to_master(df)

    msg = str(exc_info.value)
    assert master.name in msg
    assert "--snapshot" in msg


def test_append_to_master_still_works_when_master_missing(monkeypatch, tmp_path):
    # Happy path unaffected: a brand-new master (no existing file) still writes.
    master = tmp_path / "linkedin_jobs_master.csv"
    monkeypatch.setattr(scraper, "MASTER_CSV", master)
    df = pd.DataFrame([{"job_posting_id": "1", "job_title": "A"}])
    total = scraper.append_to_master(df)
    assert total == 1
    assert master.exists()


def test_append_to_master_leaves_master_untouched_on_replace_failure(monkeypatch, tmp_path):
    # A crash mid-write must never truncate the cumulative master -- the final
    # write must go through _atomic_to_csv (tmp + os.replace), not a naked to_csv
    # straight onto MASTER_CSV. Failing os.replace proves the real destination
    # was never opened for write (a naked to_csv would already have clobbered it).
    master = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame([{"job_posting_id": "1", "job_title": "A"}]).to_csv(master, index=False)
    before = master.read_bytes()
    monkeypatch.setattr(scraper, "MASTER_CSV", master)

    def boom_replace(*a, **k):
        raise OSError("simulated crash right before the rename")
    monkeypatch.setattr(os, "replace", boom_replace)

    df = pd.DataFrame([{"job_posting_id": "2", "job_title": "B"}])
    with pytest.raises(OSError):
        scraper.append_to_master(df)

    assert master.read_bytes() == before            # untouched: os.replace never landed


def test_atomic_to_csv_writes_correct_content_and_replaces_file(tmp_path):
    path = tmp_path / "out.csv"
    df = pd.DataFrame([{"job_posting_id": "1", "job_title": "A"},
                       {"job_posting_id": "2", "job_title": "B"}])
    scraper._atomic_to_csv(df, path)
    round_tripped = pd.read_csv(path, dtype={"job_posting_id": str})
    assert list(round_tripped["job_posting_id"]) == ["1", "2"]
    assert list(round_tripped["job_title"]) == ["A", "B"]
    # no stray tmp files left behind in the target directory
    leftovers = [p for p in tmp_path.iterdir() if p.name != "out.csv"]
    assert leftovers == []


def test_atomic_to_csv_cleans_up_tmp_on_failure_and_leaves_target_untouched(monkeypatch, tmp_path):
    path = tmp_path / "out.csv"
    path.write_text("job_posting_id,job_title\n1,Original\n", encoding="utf-8")
    before = path.read_bytes()

    def boom(self, *a, **k):
        raise ValueError("kaboom mid-write")
    monkeypatch.setattr(pd.DataFrame, "to_csv", boom)

    df = pd.DataFrame([{"job_posting_id": "2", "job_title": "New"}])
    with pytest.raises(ValueError):
        scraper._atomic_to_csv(df, path)

    assert path.read_bytes() == before             # target untouched
    leftovers = [p for p in tmp_path.iterdir() if p.name != "out.csv"]
    assert leftovers == []                          # no stray *.tmp left


def test_save_current_ids_round_trips(monkeypatch, tmp_path):
    ids_path = tmp_path / "last_run_job_ids.json"
    monkeypatch.setattr(scraper, "PREVIOUS_IDS_FILE", ids_path)
    scraper.save_current_ids(["a", "b", "c"])
    assert json.loads(ids_path.read_text(encoding="utf-8")) == ["a", "b", "c"]


def test_save_current_ids_leaves_file_untouched_on_replace_failure(monkeypatch, tmp_path):
    ids_path = tmp_path / "last_run_job_ids.json"
    ids_path.write_text(json.dumps(["old"]), encoding="utf-8")
    before = ids_path.read_bytes()
    monkeypatch.setattr(scraper, "PREVIOUS_IDS_FILE", ids_path)

    def boom_replace(*a, **k):
        raise OSError("simulated crash right before the rename")
    monkeypatch.setattr(os, "replace", boom_replace)

    with pytest.raises(OSError):
        scraper.save_current_ids(["new"])

    assert ids_path.read_bytes() == before          # untouched: os.replace never landed


# P1-3: load_previous_ids must never raise on a corrupt/truncated file -- a bare
# json.load used to let a bad last_run_job_ids.json kill the whole scrape before
# _master_ids' fallback could help. A non-list JSON shape must not silently yield
# garbage ids either.

def test_load_previous_ids_missing_file_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(scraper, "PREVIOUS_IDS_FILE", tmp_path / "last_run_job_ids.json")
    assert scraper.load_previous_ids() == []


def test_load_previous_ids_corrupt_json_is_empty_not_raise(monkeypatch, tmp_path):
    ids_path = tmp_path / "last_run_job_ids.json"
    ids_path.write_text("not json{", encoding="utf-8")
    monkeypatch.setattr(scraper, "PREVIOUS_IDS_FILE", ids_path)
    assert scraper.load_previous_ids() == []


def test_load_previous_ids_dict_shape_is_empty(monkeypatch, tmp_path):
    # A dict instead of a list must not silently yield garbage ids (e.g. dict keys).
    ids_path = tmp_path / "last_run_job_ids.json"
    ids_path.write_text(json.dumps({"a": "b"}), encoding="utf-8")
    monkeypatch.setattr(scraper, "PREVIOUS_IDS_FILE", ids_path)
    assert scraper.load_previous_ids() == []


def test_load_previous_ids_coerces_list_items_to_str(monkeypatch, tmp_path):
    ids_path = tmp_path / "last_run_job_ids.json"
    ids_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    monkeypatch.setattr(scraper, "PREVIOUS_IDS_FILE", ids_path)
    assert scraper.load_previous_ids() == ["1", "2", "3"]


def test_load_previous_ids_valid_list_happy_path_unchanged(monkeypatch, tmp_path):
    ids_path = tmp_path / "last_run_job_ids.json"
    ids_path.write_text(json.dumps(["a", "b", "c"]), encoding="utf-8")
    monkeypatch.setattr(scraper, "PREVIOUS_IDS_FILE", ids_path)
    assert scraper.load_previous_ids() == ["a", "b", "c"]


# --- preflight: catch a DEAD token, and nothing else --------------------------
#
# Hard-won: /status reports PROXY capability. This account has scraped for months
# while reporting can_make_requests=false / zone_not_found, because it only uses the
# Web Scraper (datasets/v3) API and owns no proxy zones. An earlier version of this
# preflight aborted on that field and would have blocked every run forever. These
# tests pin the rule: only an outright 401/403 (token dead) may stop a run.

class _StatusResp:
    def __init__(self, status=200, body="{}"):
        self.status = status
        self._body = body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _StatusSession:
    def __init__(self, resp=None, get_exc=None):
        self._resp = resp
        self._get_exc = get_exc

    def get(self, url, timeout=None):
        if self._get_exc is not None:
            raise self._get_exc
        return self._resp


def test_preflight_blocks_a_dead_token():
    for code in (401, 403):
        session = _StatusSession(_StatusResp(code, "Invalid credentials"))
        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(scraper.preflight(session))
        msg = str(excinfo.value)
        assert "rejected the API token" in msg
        assert "brightdata.com/cp/setting/users" in msg


def test_preflight_ignores_proxy_zone_fields():
    """THE regression guard. can_make_requests=false / zone_not_found is the normal,
    permanent state of a datasets-only account -- gating on it blocks every run."""
    session = _StatusSession(_StatusResp(200, json.dumps(
        {"status": "active", "customer": "hl_42ba1ee2",
         "can_make_requests": False, "auth_fail_reason": "zone_not_found"})))
    asyncio.run(scraper.preflight(session))       # must NOT raise


def test_preflight_fails_open_when_probe_is_unreachable():
    for get_exc in (asyncio.TimeoutError(), aiohttp.ClientError("connection reset")):
        asyncio.run(scraper.preflight(_StatusSession(get_exc=get_exc)))


def test_preflight_fails_open_on_server_error():
    asyncio.run(scraper.preflight(_StatusSession(_StatusResp(500, "boom"))))


def _trigger_session(status, body):
    class _Resp:
        async def text(self):
            return body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    _Resp.status = status

    class _Session:
        def post(self, url, json=None):
            return _Resp()

    return _Session()


def test_trigger_401_explains_the_permission_trap():
    """The real 2026-08-26 failure: a replacement token that reads fine but is
    refused permission to start a collection, reported as 'Invalid credentials'."""
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(scraper.trigger(_trigger_session(401, "Invalid credentials"), {}))
    msg = str(excinfo.value)
    assert "Trigger failed 401" in msg                      # raw API detail kept
    assert "brightdata.com/cp/setting/users" in msg         # plus what it means
    assert "read-only" in msg


def test_trigger_non_auth_error_stays_unannotated():
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(scraper.trigger(_trigger_session(500, "upstream boom"), {}))
    msg = str(excinfo.value)
    assert "Trigger failed 500" in msg
    assert "read-only" not in msg


# --- account_problems(): same rule, for the dashboard's Check setup button -----

def _fake_urlopen(monkeypatch, status=200, calls=None):
    class _Resp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(req, timeout=None):
        if calls is not None:
            calls.append(req)
        if status >= 400:
            raise urllib.error.HTTPError(scraper.STATUS_URL, status, "err", {}, None)
        return _Resp()

    monkeypatch.setattr(scraper.urllib.request, "urlopen", _open)


def test_account_problems_reports_a_dead_token(monkeypatch):
    monkeypatch.setattr(scraper, "API_TOKEN", "tok")
    monkeypatch.setattr(scraper, "DATASET_ID", "ds")
    _fake_urlopen(monkeypatch, 401)
    problems = scraper.account_problems()
    assert len(problems) == 1
    assert "rejected the API token" in problems[0]


def test_account_problems_silent_for_a_datasets_only_account(monkeypatch):
    # 200 with can_make_requests=false is HEALTHY here -- must report nothing.
    monkeypatch.setattr(scraper, "API_TOKEN", "tok")
    monkeypatch.setattr(scraper, "DATASET_ID", "ds")
    _fake_urlopen(monkeypatch, 200)
    assert scraper.account_problems() == []


def test_account_problems_silent_when_probe_is_unreachable(monkeypatch):
    monkeypatch.setattr(scraper, "API_TOKEN", "tok")
    monkeypatch.setattr(scraper, "DATASET_ID", "ds")
    _fake_urlopen(monkeypatch, 500)
    monkeypatch.setattr(scraper.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    assert scraper.account_problems() == []


def test_account_problems_flags_missing_credentials_without_a_call(monkeypatch):
    monkeypatch.setattr(scraper, "API_TOKEN", "")
    monkeypatch.setattr(scraper, "DATASET_ID", "")
    calls = []
    _fake_urlopen(monkeypatch, 200, calls=calls)
    problems = scraper.account_problems()
    assert len(problems) == 1
    assert "BRIGHT_DATA_API_TOKEN" in problems[0]
    assert calls == []          # no credentials -> nothing to ask the API about


# --- jobs_to_not_include payload cap -----------------------------------------
# Bright Data expands one search input into up to `limit_per_input` CHILD inputs,
# each carrying a verbatim copy of the parent's jobs_to_not_include. On 2026-08-26
# a 2,679-id exclude set (34.9 KB per input) passed at limit_per_input=1 and was
# rejected on 100% of inputs at limit_per_input=150 with `child_input_size_validation`
# -- 0 rows collected, no error raised, the run just looked like "no new jobs".
# These tests pin the bound that makes that unreachable.

def test_exclude_ids_are_capped_so_bright_data_accepts_the_input():
    """THE regression guard for the 2026-08-26 silent-zero-rows outage."""
    huge = [str(4_400_000_000 + n) for n in range(5_000)]
    inputs = scraper.build_inputs(huge, max_keywords=1, limit_per_input=150)
    for i in inputs:
        kept = i["jobs_to_not_include"]
        assert len(kept) < len(huge), "exclude set was not capped at all"
        budget = scraper.MAX_EXCLUDE_PAYLOAD_BYTES
        assert len(json.dumps(kept)) * 150 <= budget, (
            f"{len(kept)} ids x 150 children exceeds the {budget}-byte budget")


def test_exclude_cap_scales_with_the_per_input_limit():
    """A bigger fan-out means each id is paid for more times, so fewer ids fit."""
    huge = [str(4_400_000_000 + n) for n in range(5_000)]
    few = scraper.build_inputs(huge, max_keywords=1, limit_per_input=200)
    many = scraper.build_inputs(huge, max_keywords=1, limit_per_input=10)
    assert len(few[0]["jobs_to_not_include"]) < len(many[0]["jobs_to_not_include"])


def test_exclude_cap_keeps_the_NEWEST_ids():
    """Order matters: load_exclude_ids() returns oldest-first, and only a recently
    scraped posting can resurface in a time_range='Past 24 hours' search. Dropping
    the tail instead of the head would keep exactly the ids that cannot recur."""
    ids = [str(4_400_000_000 + n) for n in range(5_000)]
    kept = scraper.build_inputs(ids, max_keywords=1, limit_per_input=150)[0]["jobs_to_not_include"]
    assert kept == ids[-len(kept):]
    assert ids[-1] in kept


def test_small_exclude_set_is_passed_through_untouched():
    """The cap is a backstop, not a trim: under budget nothing is dropped, or the
    scraper would re-collect (and re-bill) jobs it already has."""
    ids = [str(4_400_000_000 + n) for n in range(40)]
    inputs = scraper.build_inputs(ids, max_keywords=1, limit_per_input=150)
    assert all(i["jobs_to_not_include"] == ids for i in inputs)


def test_exclude_cap_never_empties_the_set():
    """Even an absurd limit_per_input must leave a floor of recent ids: an empty
    exclusion re-collects everything and bills for all of it."""
    ids = [str(4_400_000_000 + n) for n in range(5_000)]
    kept = scraper.build_inputs(ids, max_keywords=1,
                                limit_per_input=10_000)[0]["jobs_to_not_include"]
    assert len(kept) >= scraper.MIN_EXCLUDE_IDS


def test_build_inputs_defaults_the_limit_from_config(monkeypatch, tmp_path):
    """main() may not thread limit_per_input through; the cap must still apply."""
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    huge = [str(4_400_000_000 + n) for n in range(5_000)]
    kept = scraper.build_inputs(huge, max_keywords=1)[0]["jobs_to_not_include"]
    assert len(kept) < len(huge)


# --- eviction is by DATE, and the target is the newest 2,000 ------------------
# Measured against Bright Data 2026-08-27, one search at limit_per_input=150:
# 2,000 ids (4,239,000 bytes of children) was ACCEPTED and collected 128 jobs;
# 2,679 ids (5,262,000 bytes) was REJECTED. 5 MiB = 5,242,880 sits between them.
# MAX_EXCLUDE_PAYLOAD_BYTES is set so 150 x 2,000 lands on the measured-good side.

def test_cap_target_is_2000_ids_at_the_configured_limit():
    ids = [str(4_400_000_000 + n) for n in range(9_000)]
    kept = scraper.cap_exclude_ids(ids, 150)
    assert len(kept) == 2000
    assert len(kept) == scraper.MAX_EXCLUDE_IDS


def test_cap_stays_inside_the_measured_good_payload():
    """150 children x the kept array must not exceed what Bright Data accepted."""
    ids = [str(4_400_000_000 + n) for n in range(9_000)]
    kept = scraper.cap_exclude_ids(ids, 150)
    assert len(json.dumps(kept)) * 150 <= scraper.MAX_EXCLUDE_PAYLOAD_BYTES <= 4_239_000


def test_master_ids_come_back_oldest_date_first(monkeypatch, tmp_path):
    """Eviction keeps the TAIL, so the tail has to be the newest BY DATE.

    The master is append-ordered, which is usually the same thing -- but a
    merge_incoming fold on the VM, or a manual row, can land an older posting
    after a newer one. Relying on row order would then evict a recent id and keep
    a stale one, which is exactly backwards for a 'Past 24 hours' search."""
    master = _dated_master(tmp_path, [
        ("newest", _days_ago(1)),
        ("oldest", _days_ago(10)),
        ("middle", _days_ago(5)),
    ])
    monkeypatch.setattr(scraper, "MASTER_CSV", master)
    monkeypatch.delenv("EXCLUDE_WINDOW_DAYS", raising=False)
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    assert scraper._master_ids() == ["oldest", "middle", "newest"]


def test_undated_rows_are_evicted_before_dated_recent_ones(monkeypatch, tmp_path):
    """An undated row is KEPT by the window (fail toward a superset) but must not
    outrank an id we can prove is recent when there is only room for some."""
    master = _dated_master(tmp_path, [
        ("dated_recent", _days_ago(1)),
        ("undated", ""),
    ])
    monkeypatch.setattr(scraper, "MASTER_CSV", master)
    monkeypatch.delenv("EXCLUDE_WINDOW_DAYS", raising=False)
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    ids = scraper._master_ids()
    assert set(ids) == {"dated_recent", "undated"}
    assert ids.index("undated") < ids.index("dated_recent")   # undated evicted first


# --- a rejected collection must not look like a quiet day ---------------------
# THE reason the 2026-08-26 outage ran unnoticed for weeks: when Bright Data
# refuses every input, the snapshot still finishes with status="ready". download()
# then returned [], main() printed "No new jobs returned this run" and exited 0.
# The VM cron logged a clean success twice a day while collecting nothing.

def _progress_session(payload):
    class _Resp:
        async def json(self):
            return payload

        def raise_for_status(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def get(self, url, **kw):
            return _Resp()

    return _Session()


def test_ready_with_zero_rows_and_errors_raises():
    """100% rejection is a failed run, not an empty one."""
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(scraper.wait_until_ready(_progress_session({
            "status": "ready", "records": 0, "errors": 40,
            "error_codes": {"child_input_size_validation": 40}}), "sd_x"))
    msg = str(excinfo.value)
    assert "child_input_size_validation" in msg     # name what Bright Data said
    assert "0 rows" in msg or "no rows" in msg.lower()


def test_zero_rows_error_names_the_size_cause():
    """The size failure is self-inflicted and fixable, so say which knob to turn."""
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(scraper.wait_until_ready(_progress_session({
            "status": "ready", "records": 0, "errors": 2,
            "error_codes": {"child_input_size_validation": 2}}), "sd_x"))
    assert "exclude" in str(excinfo.value).lower()


def test_zero_rows_with_NO_errors_is_a_normal_quiet_run():
    """Nothing new posted in the last 24h is legitimate — it must not raise."""
    asyncio.run(scraper.wait_until_ready(_progress_session({
        "status": "ready", "records": 0, "errors": 0}), "sd_x"))


def test_partial_errors_with_rows_collected_do_not_raise():
    """dead_page / page_too_big happen on every healthy run (08-14: 252 rows,
    62 errors). Only a run that collected NOTHING while erroring is a failure."""
    asyncio.run(scraper.wait_until_ready(_progress_session({
        "status": "ready", "records": 252, "errors": 62,
        "error_codes": {"dead_page": 24, "page_too_big": 38}}), "sd_x"))
