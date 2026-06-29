"""Bounding controls for scraper.py — cap keywords and per-input limit so a
verification run can't fire thousands of billed Bright Data collections.

Importing `scraper` at module scope (below) is also the regression guard for the
credential check: it must be deferred to run time, not fire at import, so the
module stays importable on a clean machine with no Bright Data creds."""
import json

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
