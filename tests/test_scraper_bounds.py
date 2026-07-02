"""Bounding controls for scraper.py — cap keywords and per-input limit so a
verification run can't fire thousands of billed Bright Data collections.

Importing `scraper` at module scope (below) is also the regression guard for the
credential check: it must be deferred to run time, not fire at import, so the
module stays importable on a clean machine with no Bright Data creds."""
import json
import os

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


def test_load_exclude_ids_returns_every_master_id(monkeypatch, tmp_path):
    # Hard cost guard: an already-scraped posting must NEVER be re-collected or
    # re-billed, no matter how long ago it was scraped. load_exclude_ids returns
    # every master id — years-old, recent, and undated alike — not just a window.
    master = tmp_path / "linkedin_jobs_master.csv"
    master.write_text(
        "job_posting_id,extracted_date\n"
        "old,2020-01-01\n"
        "recent,2026-06-23\n"
        "undated,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(scraper, "MASTER_CSV", master)
    monkeypatch.setattr(scraper, "EXTERNAL_EXCLUDE_FILE", tmp_path / "external_exclude_ids.json")
    assert set(scraper.load_exclude_ids()) == {"old", "recent", "undated"}


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
