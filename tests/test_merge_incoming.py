"""merge_incoming.py — folds ~/incoming outbox files into the VM master / run stats.

Pure pandas + tmp_path. Import from the repo root (the script is deployed standalone
beside scraper.py, so it must not import from local/).
"""
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import merge_incoming  # noqa: E402


def _gz(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, compression="gzip")


def _setup(tmp_path):
    inc = tmp_path / "incoming"
    master = tmp_path / "linkedin_jobs_master.csv"
    stats = tmp_path / "run_stats.csv"
    return inc, master, stats


def test_idle_run_creates_incoming_and_exits_zero(tmp_path):
    inc, master, stats = _setup(tmp_path)
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    assert inc.is_dir()


def test_rows_merge_master_wins_and_column_union(tmp_path):
    inc, master, stats = _setup(tmp_path)
    pd.DataFrame([
        {"job_posting_id": "1", "job_title": "VM title", "vm_only_col": "keep"},
    ]).to_csv(master, index=False)
    _gz(inc / "local_rows_a.csv.gz", pd.DataFrame([
        {"job_posting_id": "1", "job_title": "LOCAL title", "local_only_col": "x"},
        {"job_posting_id": "2", "job_title": "New local", "local_only_col": "y"},
    ]))
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    got = pd.read_csv(master, dtype={"job_posting_id": str})
    assert sorted(got["job_posting_id"]) == ["1", "2"]
    row1 = got[got["job_posting_id"] == "1"].iloc[0]
    assert row1["job_title"] == "VM title"          # master wins
    assert row1["vm_only_col"] == "keep"            # column union kept both ways
    assert "local_only_col" in got.columns
    assert not (inc / "local_rows_a.csv.gz").exists()  # consumed after the write


def test_rows_merge_str_id_cast(tmp_path):
    inc, master, stats = _setup(tmp_path)
    pd.DataFrame([{"job_posting_id": 1}]).to_csv(master, index=False)  # int on disk
    _gz(inc / "local_rows_a.csv.gz", pd.DataFrame([{"job_posting_id": "1"}]))
    merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0)
    got = pd.read_csv(master, dtype={"job_posting_id": str})
    assert len(got) == 1  # "1" and 1 deduped, not kept as twins


def test_rows_merge_missing_master_creates_it(tmp_path):
    inc, master, stats = _setup(tmp_path)
    _gz(inc / "local_rows_a.csv.gz", pd.DataFrame([{"job_posting_id": "7", "score": 4}]))
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    assert list(pd.read_csv(master, dtype=str)["job_posting_id"]) == ["7"]


def test_unreadable_master_aborts_loud_and_keeps_files(tmp_path):
    inc, master, stats = _setup(tmp_path)
    master.write_bytes(b'a,b\n"unclosed quote never ends\nx')  # unparseable CSV
    _gz(inc / "local_rows_a.csv.gz", pd.DataFrame([{"job_posting_id": "7"}]))
    rc = merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0)
    assert rc == 1
    assert (inc / "local_rows_a.csv.gz").exists()          # nothing consumed
    assert master.read_bytes().startswith(b"a,b")          # master untouched


def test_bad_rows_file_quarantined_others_merge(tmp_path):
    inc, master, stats = _setup(tmp_path)
    inc.mkdir(parents=True)
    (inc / "local_rows_bad.csv.gz").write_bytes(b"this is not gzip")
    _gz(inc / "local_rows_noid.csv.gz", pd.DataFrame([{"job_title": "no id col"}]))
    _gz(inc / "local_rows_ok.csv.gz", pd.DataFrame([{"job_posting_id": "5"}]))
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    assert list(pd.read_csv(master, dtype=str)["job_posting_id"]) == ["5"]
    bad = sorted(p.name for p in (inc / "bad").iterdir())
    assert bad == ["local_rows_bad.csv.gz", "local_rows_noid.csv.gz"]
    assert not (inc / "local_rows_ok.csv.gz").exists()


def test_all_duplicate_rows_still_consume_file(tmp_path):
    inc, master, stats = _setup(tmp_path)
    pd.DataFrame([{"job_posting_id": "1", "job_title": "t"}]).to_csv(master, index=False)
    before = master.read_bytes()
    _gz(inc / "local_rows_a.csv.gz", pd.DataFrame([{"job_posting_id": "1"}]))
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    assert not (inc / "local_rows_a.csv.gz").exists()   # re-push is a no-op, file drained
    assert master.read_bytes() == before                 # nothing added -> no rewrite


def test_stats_merge_dedups_on_timestamp_and_input(tmp_path):
    inc, master, stats = _setup(tmp_path)
    pd.DataFrame([
        {"timestamp": "t1", "input_csv": "a.csv", "rows_in": 10},
    ]).to_csv(stats, index=False)
    inc.mkdir(parents=True)
    pd.DataFrame([
        {"timestamp": "t1", "input_csv": "a.csv", "rows_in": 999},   # dupe: existing wins
        {"timestamp": "t2", "input_csv": "b.csv", "rows_in": 20},    # new
    ]).to_csv(inc / "local_stats_x.csv", index=False)
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    got = pd.read_csv(stats)
    assert len(got) == 2
    assert got[got["timestamp"] == "t1"].iloc[0]["rows_in"] == 10
    assert not (inc / "local_stats_x.csv").exists()


def test_stats_created_when_missing(tmp_path):
    inc, master, stats = _setup(tmp_path)
    inc.mkdir(parents=True)
    pd.DataFrame([{"timestamp": "t1", "input_csv": "a.csv"}]).to_csv(
        inc / "local_stats_x.csv", index=False)
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    assert len(pd.read_csv(stats)) == 1


def test_fresh_file_skipped_not_quarantined(tmp_path):
    # Mid-upload guard: a just-written file (default min_age_seconds=60) is left
    # queued — an scp may still be writing it. No merge, no quarantine, rc 0.
    inc, master, stats = _setup(tmp_path)
    _gz(inc / "local_rows_fresh.csv.gz", pd.DataFrame([{"job_posting_id": "9"}]))
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats) == 0
    assert (inc / "local_rows_fresh.csv.gz").exists()
    assert not (inc / "bad").exists()
    assert not master.exists()


def _full_load_reference(existing, incoming):  # the CURRENT semantics, for equivalence
    combined = pd.concat([existing, incoming], ignore_index=True)
    combined["job_posting_id"] = combined["job_posting_id"].astype(str)
    return combined.drop_duplicates(subset=["job_posting_id"], keep="first").reset_index(drop=True)


def test_merge_rows_matches_full_load_reference():
    # merge_rows() is the in-memory reference/spec (used directly, and as the
    # per-incoming-file folding step inside main()) -- this pins its contract:
    # master wins on collision (id "3"), column union preserved either way.
    existing = pd.DataFrame({
        "job_posting_id": ["1", "2", "3"],
        "job_title": ["a", "b", "c"],
        "company_name": ["x", "y", "z"],
    })
    incoming = pd.DataFrame({
        "job_posting_id": ["3", "4"],
        "job_title": ["C2", "d"],
        "company_name": ["z", "w"],
        "local_only_col": ["keep-me", "also-me"],
    })

    got = merge_incoming.merge_rows(existing.astype({"job_posting_id": str}), incoming)
    got = got.sort_values("job_posting_id").reset_index(drop=True)

    ref = _full_load_reference(existing.astype({"job_posting_id": str}), incoming)
    ref = ref.sort_values("job_posting_id").reset_index(drop=True)

    assert got["job_title"].tolist() == ref["job_title"].tolist()  # id 3 keeps existing "c"
    assert sorted(got.columns) == sorted(ref.columns)
    assert "local_only_col" in got.columns  # column union preserved
    assert got.set_index("job_posting_id").loc["4", "local_only_col"] == "also-me"


def test_chunked_merge_via_main_matches_full_load(tmp_path, monkeypatch):
    # The brief's required equivalence test: the chunked master-wins merge
    # (main()'s on-disk path, exercising the tempfile-streaming + os.replace
    # machinery, not just the in-memory merge_rows() helper) frame-equals the
    # current concat(existing, incoming).drop_duplicates(keep="first") result.
    # Fixture large enough (7 master rows) that CHUNK=2 forces 4 chunks, with a
    # collision (id "3") to prove master-wins, plus an incoming-only column to
    # prove column union survives the chunked rewrite.
    inc, master, stats = _setup(tmp_path)
    existing = pd.DataFrame({
        "job_posting_id": ["1", "2", "3", "4", "5", "6", "7"],
        "job_title": ["a", "b", "c", "d", "e", "f", "g"],
        "company_name": ["v", "w", "x", "y", "z", "p", "q"],
    })
    existing.to_csv(master, index=False)
    incoming = pd.DataFrame({
        "job_posting_id": ["3", "8"],
        "job_title": ["C2", "h"],
        "company_name": ["x", "r"],
        "local_only_col": ["keep-me", "also-me"],
    })
    _gz(inc / "local_rows_a.csv.gz", incoming)
    monkeypatch.setattr(merge_incoming, "CHUNK", 2)  # force multi-chunk

    orig_read_csv = pd.read_csv
    chunk_calls = []
    master_calls = []  # every pd.read_csv call whose target is the MASTER path

    def _spy(*args, **kwargs):
        if kwargs.get("chunksize"):
            chunk_calls.append(kwargs["chunksize"])
        target = args[0] if args else kwargs.get("filepath_or_buffer")
        if target is not None and Path(target) == master:
            master_calls.append(kwargs)
        return orig_read_csv(*args, **kwargs)

    monkeypatch.setattr(pd, "read_csv", _spy)
    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    assert chunk_calls == [2]  # confirms the streaming path (not a full load) actually ran

    # Mechanical bounded-reads assertion: every read of the MASTER during this
    # merge run must be bounded (nrows, usecols, or chunksize set) -- zero bare
    # full-frame reads of the master anywhere in main(). Scoped to the master
    # path only; incoming files are small and may legitimately be read whole.
    assert len(master_calls) == 3  # nrows=0 header probe, usecols id probe, chunksize stream
    for kwargs in master_calls:
        assert kwargs.get("nrows") is not None or kwargs.get("usecols") is not None \
            or kwargs.get("chunksize") is not None, f"unbounded master read: {kwargs}"

    got = pd.read_csv(master, dtype={"job_posting_id": str}).sort_values("job_posting_id").reset_index(drop=True)
    ref = _full_load_reference(existing.astype({"job_posting_id": str}), incoming)
    ref = ref.sort_values("job_posting_id").reset_index(drop=True)
    assert got["job_title"].tolist() == ref["job_title"].tolist()
    assert sorted(got.columns) == sorted(ref.columns)
    assert "local_only_col" in got.columns
    assert got.set_index("job_posting_id").loc["8", "local_only_col"] == "also-me"


def test_incoming_vs_incoming_collision_first_file_wins(tmp_path, monkeypatch):
    # Ordering mechanism (read from main(), merge_incoming.py lines ~176-178):
    # row_paths = sorted(p for p in incoming_dir.glob("local_rows_*.csv.gz") ...)
    # -- a plain lexicographic sort of the Path objects (by filename, since
    # they share a directory). st_mtime is used ONLY by _is_old_enough's age
    # guard, never for ordering. So "local_rows_a.csv.gz" is guaranteed to
    # sort and process before "local_rows_b.csv.gz" regardless of which was
    # written to disk first -- deterministic by name alone.
    inc, master, stats = _setup(tmp_path)
    # 5 master rows so CHUNK=2 forces multiple chunks (streaming path, not a
    # one-shot full read).
    existing = pd.DataFrame({
        "job_posting_id": ["1", "2", "3", "4", "5"],
        "job_title": ["a", "b", "c", "d", "e"],
    })
    existing.to_csv(master, index=False)
    monkeypatch.setattr(merge_incoming, "CHUNK", 2)

    # Both incoming files share id "50" (not in master) with a distinguishing
    # job_title -- first-file-wins (file "a") must keep "from_file_1". Both
    # also carry id "3", which collides with the MASTER -- master must win
    # over both. Plus one unique-new id per file ("60" file a, "70" file b).
    _gz(inc / "local_rows_a.csv.gz", pd.DataFrame({
        "job_posting_id": ["50", "3", "60"],
        "job_title": ["from_file_1", "master_should_win_1", "unique_a"],
    }))
    _gz(inc / "local_rows_b.csv.gz", pd.DataFrame({
        "job_posting_id": ["50", "3", "70"],
        "job_title": ["from_file_2", "master_should_win_2", "unique_b"],
    }))

    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0

    got = pd.read_csv(master, dtype={"job_posting_id": str}).set_index("job_posting_id")

    # (a) shared incoming id "50" lands with file 1's ("a") distinguishing value.
    assert got.loc["50", "job_title"] == "from_file_1"
    # (b) master-colliding id "3" keeps the MASTER's original value, not either
    # incoming file's "master_should_win_*" value.
    assert got.loc["3", "job_title"] == "c"
    # (c) both unique-new ids present.
    assert got.loc["60", "job_title"] == "unique_a"
    assert got.loc["70", "job_title"] == "unique_b"
    # (d) final row count exact: 5 master rows + "50" + "60" + "70" = 8
    # ("3" is a collision, not a new row).
    assert len(got) == 8
    # (e) both incoming files consumed/deleted (existing behavior contract,
    # e.g. test_rows_merge_master_wins_and_column_union).
    assert not (inc / "local_rows_a.csv.gz").exists()
    assert not (inc / "local_rows_b.csv.gz").exists()


def test_stats_unlink_oserror_does_not_fail_merge(tmp_path, monkeypatch):
    # P2-2: merge runs BEFORE the scrape under `set -e`, so an unguarded OSError
    # from the stats-file unlink would exit main() nonzero and kill the day's run
    # over a bookkeeping delete. The delete failure must be swallowed (the module's
    # "per-file problems never fail the cron" contract) and the merge still finish.
    inc, master, stats = _setup(tmp_path)
    inc.mkdir(parents=True)
    pd.DataFrame([{"timestamp": "t1", "input_csv": "a.csv", "rows_in": 5}]).to_csv(
        inc / "local_stats_x.csv", index=False)

    real_unlink = Path.unlink

    def flaky_unlink(self, *a, **k):
        if self.name.startswith("local_stats_"):
            raise OSError("simulated delete failure on the merged stats file")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    assert merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0) == 0
    assert len(pd.read_csv(stats)) == 1  # the stats write still landed despite the delete failure


def test_unreadable_master_still_aborts_with_chunk_set(tmp_path, monkeypatch):
    # Confirm the exit-1-on-unreadable-master policy survives the rewrite even
    # when CHUNK is small enough to force streaming.
    inc, master, stats = _setup(tmp_path)
    master.write_bytes(b'a,b\n"unclosed quote never ends\nx')  # unparseable CSV
    _gz(inc / "local_rows_a.csv.gz", pd.DataFrame([{"job_posting_id": "7"}]))
    monkeypatch.setattr(merge_incoming, "CHUNK", 2)
    rc = merge_incoming.main(incoming_dir=inc, master_csv=master, stats_csv=stats, min_age_seconds=0)
    assert rc == 1
    assert (inc / "local_rows_a.csv.gz").exists()          # nothing consumed
    assert master.read_bytes().startswith(b"a,b")          # master untouched
