import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))
import jobsdata  # noqa: E402


def test_engine_labels_are_gemini_auth_modes():
    assert set(jobsdata._ENGINE_LABELS) == {"vertex", "api_key"}


def test_label_to_auth_is_inverse():
    assert jobsdata._LABEL_TO_AUTH[jobsdata._ENGINE_LABELS["vertex"]] == "vertex"
    assert jobsdata._LABEL_TO_AUTH[jobsdata._ENGINE_LABELS["api_key"]] == "api_key"


def test_engine_credential_warnings_flags_missing_api_key():
    assert jobsdata._engine_credential_warnings("api_key", project="", has_api_key=False)
    assert jobsdata._engine_credential_warnings("api_key", project="proj", has_api_key=True) == []


def test_engine_credential_warnings_flags_missing_vertex_project():
    assert jobsdata._engine_credential_warnings("vertex", project="", has_api_key=False)
    assert jobsdata._engine_credential_warnings("vertex", project="  ", has_api_key=False)  # blank
    assert jobsdata._engine_credential_warnings("vertex", project="my-proj", has_api_key=False) == []


# --- _claude_cli_warnings truth table (SP5) -------------------------------------

def test_claude_cli_warnings_cli_found_always_empty():
    assert jobsdata._claude_cli_warnings("claude", "claude", cli_found=True) == []
    assert jobsdata._claude_cli_warnings("gemini", "gemini", cli_found=True) == []


def test_claude_cli_warnings_missing_cli_tailor_claude_only():
    out = jobsdata._claude_cli_warnings("claude", "gemini", cli_found=False)
    assert len(out) == 1
    assert "tailor" in out[0].lower()


def test_claude_cli_warnings_missing_cli_scoring_claude_only():
    out = jobsdata._claude_cli_warnings("gemini", "claude", cli_found=False)
    assert len(out) == 1
    assert "fall back to Gemini" in out[0] or "fallback" in out[0].lower() or "fall back" in out[0].lower()


def test_claude_cli_warnings_missing_cli_both_claude():
    out = jobsdata._claude_cli_warnings("claude", "claude", cli_found=False)
    assert len(out) == 2


def test_claude_cli_warnings_missing_cli_neither_claude():
    assert jobsdata._claude_cli_warnings("gemini", "gemini", cli_found=False) == []


def test_projects_count_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)  # empty config.json dir
    assert jobsdata.load_projects_count() == (3, "max")


def test_projects_count_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    jobsdata.save_projects_count(5, "exact")
    assert jobsdata.load_projects_count() == (5, "exact")


def test_projects_count_clamps_and_normalizes(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    jobsdata.save_projects_count(99, "weird")          # over the cap + bad mode
    assert jobsdata.load_projects_count() == (6, "max")
    jobsdata.save_projects_count(0, "Exact")           # under the floor + mixed case
    assert jobsdata.load_projects_count() == (1, "exact")


def test_projects_count_does_not_clobber_layout_maps(tmp_path, monkeypatch):
    # save_projects_count merges into config.json — it must not wipe resume_layout.
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    jobsdata.save_resume_layout({"Globex": {"line_targets": [2, 1]}})
    jobsdata.save_projects_count(4, "exact")
    assert jobsdata.load_resume_layout() == {"Globex": {"line_targets": [2, 1]}}
    assert jobsdata.load_projects_count() == (4, "exact")


def test_project_bullet_tiers_default_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    assert jobsdata.load_project_bullet_tiers() == []


def test_project_bullet_tiers_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    jobsdata.save_project_bullet_tiers(
        [{"projects": 2, "bullets": 3}, {"projects": 1, "bullets": 1}])
    assert jobsdata.load_project_bullet_tiers() == [
        {"projects": 2, "bullets": 3}, {"projects": 1, "bullets": 1}]


def test_project_bullet_tiers_save_clamps_and_drops(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    jobsdata.save_project_bullet_tiers([
        {"projects": 0, "bullets": 9},   # projects ->1, bullets clamp 1-5 ->5
        {"projects": 2},                 # missing 'bullets' -> dropped
        "nope",                          # not a dict -> dropped
    ])
    assert jobsdata.load_project_bullet_tiers() == [{"projects": 1, "bullets": 5}]


def test_project_bullet_tiers_does_not_clobber_layout(tmp_path, monkeypatch):
    # save merges into config.json — it must not wipe the per-name project_layout.
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    jobsdata.save_project_layout({"ProjX": {"line_targets": [2]}})
    jobsdata.save_project_bullet_tiers([{"projects": 1, "bullets": 3}])
    assert jobsdata.load_project_layout() == {"ProjX": {"line_targets": [2]}}
    assert jobsdata.load_project_bullet_tiers() == [{"projects": 1, "bullets": 3}]


def test_verbatim_blocks_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    assert jobsdata.load_verbatim_blocks() == {}
    jobsdata.save_verbatim_blocks({"Globex": ["A", "B"]})
    assert jobsdata.load_verbatim_blocks() == {"Globex": ["A", "B"]}


# P0-1: _append_dedup_csv must never treat an unreadable-but-existing master
# as empty - that path used to silently overwrite the cumulative CSV with just
# the one new record. It must raise instead, and leave the file untouched.

def test_append_dedup_csv_raises_and_preserves_file_when_read_errors(tmp_path, monkeypatch):
    path = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame([{"job_posting_id": "1", "job_title": "A"},
                  {"job_posting_id": "2", "job_title": "B"},
                  {"job_posting_id": "3", "job_title": "C"}]).to_csv(path, index=False)
    before = path.read_bytes()

    def boom(*a, **k):
        raise OSError("file locked by AV/Excel/sync")
    monkeypatch.setattr(jobsdata.pd, "read_csv", boom)

    with pytest.raises(OSError):
        jobsdata._append_dedup_csv({"job_posting_id": "4", "job_title": "D"}, path)

    assert path.read_bytes() == before          # untouched: no silent truncation


def test_append_dedup_csv_raises_and_preserves_file_when_content_is_garbage(tmp_path):
    path = tmp_path / "linkedin_jobs_master.csv"
    # Malformed CSV: an unterminated quote makes pandas' C/python parser raise a
    # real ValueError (no monkeypatching -- this is a genuine unreadable file).
    path.write_bytes(b'job_posting_id,job_title\n"1,unterminated quote\n2,B\n')
    before = path.read_bytes()

    with pytest.raises(OSError):
        jobsdata._append_dedup_csv({"job_posting_id": "9", "job_title": "New"}, path)

    assert path.read_bytes() == before          # untouched: no silent truncation


def test_append_dedup_csv_still_creates_file_when_missing(tmp_path):
    # The happy path for a brand-new master must be unaffected by the fix.
    path = tmp_path / "linkedin_jobs_master.csv"
    added = jobsdata._append_dedup_csv({"job_posting_id": "1", "job_title": "A"}, path)
    assert added is True
    assert list(pd.read_csv(path, dtype={"job_posting_id": str})["job_posting_id"]) == ["1"]


# P1-2: _append_dedup_csv's and _drop_ids_from_csv's WRITE must also be atomic
# (tmp + os.replace via csv_io.write_csv_gz_atomic) so a crash mid-write never
# truncates the cumulative master. This is separate from the P0-1 read guard above.

def test_append_dedup_csv_write_leaves_file_untouched_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame([{"job_posting_id": "1", "job_title": "A"}]).to_csv(path, index=False)
    before = path.read_bytes()

    def boom_replace(*a, **k):
        raise OSError("simulated crash right before the rename")
    monkeypatch.setattr(jobsdata.os, "replace", boom_replace)

    with pytest.raises(OSError):
        jobsdata._append_dedup_csv({"job_posting_id": "2", "job_title": "B"}, path)

    assert path.read_bytes() == before          # untouched: os.replace never landed


def test_drop_ids_from_csv_leaves_file_untouched_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame([{"job_posting_id": "1", "job_title": "A"},
                  {"job_posting_id": "2", "job_title": "B"}]).to_csv(path, index=False)
    before = path.read_bytes()

    def boom_replace(*a, **k):
        raise OSError("simulated crash right before the rename")
    monkeypatch.setattr(jobsdata.os, "replace", boom_replace)

    with pytest.raises(OSError):
        jobsdata._drop_ids_from_csv(path, {"1"})

    assert path.read_bytes() == before          # untouched: os.replace never landed


def test_drop_ids_from_csv_still_drops_ids_correctly(tmp_path):
    # Happy path unaffected by the atomic-write fix.
    path = tmp_path / "linkedin_jobs_master.csv"
    pd.DataFrame([{"job_posting_id": "1", "job_title": "A"},
                  {"job_posting_id": "2", "job_title": "B"},
                  {"job_posting_id": "3", "job_title": "C"}]).to_csv(path, index=False)
    jobsdata._drop_ids_from_csv(path, {"2"})
    out = pd.read_csv(path, dtype={"job_posting_id": str})
    assert list(out["job_posting_id"]) == ["1", "3"]


# P1-1: filter_high_unseen must never crash when a df has 'score' but no
# 'deep_score' column, and must not silently misalign against a non-default
# index (drop_duplicates/removed-jobs filtering in load_files produces one).

def test_filter_high_unseen_no_deep_score_column_does_not_raise(tmp_path):
    # Regression: out.get("deep_score", 0) returns the scalar 0 when the column
    # is absent, and pd.to_numeric(0, ...).fillna(0) raises AttributeError.
    df = pd.DataFrame([
        {"job_posting_id": "1", "score": 5, "is_seen": "no"},
        {"job_posting_id": "2", "score": 2, "is_seen": "no"},
    ])
    out = jobsdata.filter_high_unseen(df, min_score=4)
    assert list(out["job_posting_id"]) == ["1"]


def test_filter_high_unseen_nondefault_index_no_is_seen_column_returns_matches(tmp_path):
    # Regression: the is_seen fallback pd.Series(["no"] * len(df)) carries a fresh
    # RangeIndex while `score` carries df's real index, so the boolean AND
    # misaligns on any non-default index -- 3 qualifying rows silently -> 0.
    df = pd.DataFrame(
        [
            {"job_posting_id": "1", "score": 5},
            {"job_posting_id": "2", "score": 6},
            {"job_posting_id": "3", "score": 4},
        ],
        index=[5, 9, 12],
    )
    out = jobsdata.filter_high_unseen(df, min_score=4)
    assert len(out) == 3
    assert set(out["job_posting_id"]) == {"1", "2", "3"}


def test_filter_high_unseen_happy_path_unchanged(tmp_path):
    # Both columns present: behavior (selection + sort order) must be byte-identical
    # to before the fix. Sort: score desc, applicants asc (unknown last), deep_score desc.
    df = pd.DataFrame([
        {"job_posting_id": "1", "score": 5, "is_seen": "no", "deep_score": 3,
         "job_num_applicants": 10},
        {"job_posting_id": "2", "score": 5, "is_seen": "no", "deep_score": 7,
         "job_num_applicants": 10},
        {"job_posting_id": "3", "score": 6, "is_seen": "no", "deep_score": 1,
         "job_num_applicants": 5},
        {"job_posting_id": "4", "score": 2, "is_seen": "no", "deep_score": 9,
         "job_num_applicants": 1},   # below min_score -> excluded
        {"job_posting_id": "5", "score": 5, "is_seen": "yes", "deep_score": 9,
         "job_num_applicants": 1},   # already seen -> excluded
    ])
    out = jobsdata.filter_high_unseen(df, min_score=4)
    assert list(out["job_posting_id"]) == ["3", "2", "1"]
    assert "__score_num" not in out.columns
    assert "__deep_num" not in out.columns
    assert "__appl_num" not in out.columns
