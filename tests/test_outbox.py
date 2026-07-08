"""local/outbox.py — durable outbox for pushing local scrape/manual rows to the VM.

Pure pandas + tmp_path; no gcloud, no network, no Qt.
"""
import gzip
import sys
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import outbox  # noqa: E402


def _write_master(path: Path, rows: list[dict]) -> None:
    pd.DataFrame(rows).to_csv(path, index=False)


def _scored_gz(path: Path, ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"job_posting_id": ids, "score": [5] * len(ids)}).to_csv(
        path, index=False, compression="gzip")


# ---- snapshot / new_run_ids -------------------------------------------------

def test_new_run_ids_sees_new_file(tmp_path):
    before = outbox.snapshot_run_files(base=tmp_path)
    _scored_gz(tmp_path / "evening" / "linkedin_jobs_2026-07-03_evening_scored.csv.gz",
               ["11", "22"])
    assert sorted(outbox.new_run_ids(before, base=tmp_path)) == ["11", "22"]


def test_new_run_ids_sees_overwritten_file(tmp_path):
    # Same-day re-run OVERWRITES the same scored filename — mtime must catch it.
    gz = tmp_path / "evening" / "linkedin_jobs_2026-07-03_evening_scored.csv.gz"
    _scored_gz(gz, ["11"])
    before = outbox.snapshot_run_files(base=tmp_path)
    time.sleep(0.01)
    _scored_gz(gz, ["11", "22"])
    import os
    os.utime(gz, (time.time() + 2, time.time() + 2))  # force a visible mtime bump
    got = outbox.new_run_ids(before, base=tmp_path)
    assert sorted(got) == ["11", "22"]


def test_new_run_ids_empty_when_nothing_changed(tmp_path):
    _scored_gz(tmp_path / "morning" / "linkedin_jobs_2026-07-01_morning_scored.csv.gz", ["1"])
    before = outbox.snapshot_run_files(base=tmp_path)
    assert outbox.new_run_ids(before, base=tmp_path) == []


# ---- write_rows_outbox ------------------------------------------------------

def test_rows_outbox_carries_full_master_row(tmp_path):
    master = tmp_path / "master.csv"
    _write_master(master, [
        {"job_posting_id": "11", "job_title": "SWE",
         "job_description_formatted": "long JD text", "score": 4},
        {"job_posting_id": "99", "job_title": "Other",
         "job_description_formatted": "other JD", "score": 2},
    ])
    out = outbox.write_rows_outbox(["11"], master_csv=master, outbox_dir=tmp_path / "ob")
    assert out is not None and out.name.startswith("local_rows_") and out.suffix == ".gz"
    got = pd.read_csv(out, dtype={"job_posting_id": str})
    assert list(got["job_posting_id"]) == ["11"]
    assert got.loc[0, "job_description_formatted"] == "long JD text"  # JD carried


def test_rows_outbox_none_when_no_matching_ids(tmp_path):
    master = tmp_path / "master.csv"
    _write_master(master, [{"job_posting_id": "11", "job_title": "SWE"}])
    assert outbox.write_rows_outbox([], master_csv=master, outbox_dir=tmp_path / "ob") is None
    assert outbox.write_rows_outbox(["77"], master_csv=master,
                                    outbox_dir=tmp_path / "ob") is None


def test_rows_outbox_none_when_master_missing(tmp_path):
    assert outbox.write_rows_outbox(["11"], master_csv=tmp_path / "nope.csv",
                                    outbox_dir=tmp_path / "ob") is None


def test_rows_outbox_str_id_match(tmp_path):
    # Master read back with int-looking ids must still match string ids.
    master = tmp_path / "master.csv"
    _write_master(master, [{"job_posting_id": 4242, "job_title": "SWE"}])
    out = outbox.write_rows_outbox(["4242"], master_csv=master, outbox_dir=tmp_path / "ob")
    assert out is not None
    assert list(pd.read_csv(out, dtype=str)["job_posting_id"]) == ["4242"]


def test_rows_outbox_unique_filenames(tmp_path):
    master = tmp_path / "master.csv"
    _write_master(master, [{"job_posting_id": "11"}])
    a = outbox.write_rows_outbox(["11"], master_csv=master, outbox_dir=tmp_path / "ob")
    b = outbox.write_rows_outbox(["11"], master_csv=master, outbox_dir=tmp_path / "ob")
    assert a != b  # two adds in the same second must not overwrite each other


# ---- write_stats_outbox -----------------------------------------------------

def test_stats_outbox_copies_whole_file(tmp_path):
    stats = tmp_path / "run_stats.csv"
    pd.DataFrame([{"timestamp": "t1", "input_csv": "a.csv", "rows_in": 3}]).to_csv(
        stats, index=False)
    out = outbox.write_stats_outbox(stats_csv=stats, outbox_dir=tmp_path / "ob")
    assert out is not None and out.name.startswith("local_stats_") and out.suffix == ".csv"
    assert pd.read_csv(out).to_dict("records") == pd.read_csv(stats).to_dict("records")


def test_stats_outbox_none_when_missing(tmp_path):
    assert outbox.write_stats_outbox(stats_csv=tmp_path / "none.csv",
                                     outbox_dir=tmp_path / "ob") is None


# ---- pending_files ----------------------------------------------------------

def test_pending_files_sorted_and_scoped(tmp_path):
    ob = tmp_path / "ob"
    ob.mkdir()
    (ob / "local_rows_20260703-2.csv.gz").write_bytes(gzip.compress(b"x"))
    (ob / "local_rows_20260703-1.csv.gz").write_bytes(gzip.compress(b"x"))
    (ob / "local_stats_20260703-1.csv").write_text("t")
    (ob / "unrelated.txt").write_text("no")
    names = [p.name for p in outbox.pending_files(outbox_dir=ob)]
    assert names == ["local_rows_20260703-1.csv.gz", "local_rows_20260703-2.csv.gz",
                     "local_stats_20260703-1.csv"]


def test_pending_files_missing_dir_is_empty(tmp_path):
    assert outbox.pending_files(outbox_dir=tmp_path / "nope") == []


# ---- push_outbox ------------------------------------------------------------

class _Res:
    def __init__(self, rc):
        self.returncode = rc
        self.stdout = ""
        self.stderr = "boom" if rc else ""


def _vm_target():
    import vm_sync
    return vm_sync.VMTarget(gcloud="gcloud", instance="scraper-vm", zone="z",
                            project="p", user="yib", remote_dir="~")


def _queue(ob: Path, names: list[str]) -> None:
    ob.mkdir(parents=True, exist_ok=True)
    for n in names:
        if n.endswith(".gz"):
            (ob / n).write_bytes(gzip.compress(b"job_posting_id\n1\n"))
        else:
            (ob / n).write_text("timestamp,input_csv\n")


def test_push_outbox_deletes_only_on_success(tmp_path):
    ob = tmp_path / "ob"
    _queue(ob, ["local_rows_1.csv.gz", "local_stats_2.csv"])
    calls = []

    def runner(cmd):
        calls.append(cmd)
        # First file fails, second succeeds.
        return _Res(1 if "local_rows_1" in " ".join(cmd) else 0)

    pushed, kept = outbox.push_outbox(_vm_target(), outbox_dir=ob, runner=runner)
    assert (pushed, kept) == (1, 1)
    assert [p.name for p in outbox.pending_files(outbox_dir=ob)] == ["local_rows_1.csv.gz"]
    assert len(calls) == 2


def test_push_outbox_unconfigured_keeps_everything(tmp_path):
    ob = tmp_path / "ob"
    _queue(ob, ["local_rows_1.csv.gz"])
    import vm_sync
    pushed, kept = outbox.push_outbox(
        vm_sync.VMTarget(instance="", zone="", user=""), outbox_dir=ob,
        runner=lambda cmd: (_ for _ in ()).throw(AssertionError("must not run")))
    assert (pushed, kept) == (0, 1)
    assert len(outbox.pending_files(outbox_dir=ob)) == 1


def test_push_outbox_runner_exception_keeps_file_and_continues(tmp_path):
    ob = tmp_path / "ob"
    _queue(ob, ["local_rows_1.csv.gz", "local_stats_2.csv"])

    def runner(cmd):
        if "local_rows_1" in " ".join(cmd):
            raise RuntimeError("gcloud exploded")
        return _Res(0)

    pushed, kept = outbox.push_outbox(_vm_target(), outbox_dir=ob, runner=runner)
    assert (pushed, kept) == (1, 1)


def test_push_outbox_logs(tmp_path):
    import io
    ob = tmp_path / "ob"
    _queue(ob, ["local_rows_1.csv.gz"])
    log = io.StringIO()
    outbox.push_outbox(_vm_target(), outbox_dir=ob, runner=lambda cmd: _Res(0), log=log)
    assert "local_rows_1.csv.gz" in log.getvalue()


# ---- prune_stats ------------------------------------------------------------

def test_prune_stats_keeps_only_newest(tmp_path):
    # Every local_stats file is a FULL copy of run_stats.csv (monotonic), so the
    # newest supersedes the rest — pushing all of them is pure waste.
    ob = tmp_path / "ob"
    _queue(ob, ["local_stats_20260704-1.csv", "local_stats_20260705-1.csv",
                "local_stats_20260706-1.csv", "local_rows_1.csv.gz"])
    assert outbox.prune_stats(outbox_dir=ob) == 2
    names = [p.name for p in outbox.pending_files(outbox_dir=ob)]
    assert names == ["local_rows_1.csv.gz", "local_stats_20260706-1.csv"]


def test_prune_stats_zero_or_one_file_is_noop(tmp_path):
    ob = tmp_path / "ob"
    assert outbox.prune_stats(outbox_dir=ob) == 0     # missing dir
    _queue(ob, ["local_stats_1.csv"])
    assert outbox.prune_stats(outbox_dir=ob) == 0
    assert len(outbox.pending_files(outbox_dir=ob)) == 1


def test_push_outbox_prunes_redundant_stats_first(tmp_path):
    ob = tmp_path / "ob"
    _queue(ob, ["local_stats_1.csv", "local_stats_2.csv", "local_stats_3.csv"])
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return _Res(0)

    pushed, kept = outbox.push_outbox(_vm_target(), outbox_dir=ob, runner=runner)
    assert (pushed, kept) == (1, 0)                    # only the newest survived
    assert len(calls) == 1 and "local_stats_3" in " ".join(calls[0])


# ---- pushed-ids memory (in-flight to the VM, not yet visible in Drive) -------

def _rows_gz(ob: Path, name: str, ids: list[str]) -> None:
    ob.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"job_posting_id": ids}).to_csv(ob / name, index=False,
                                                 compression="gzip")


def test_push_outbox_records_pushed_row_ids(tmp_path):
    ob = tmp_path / "ob"
    _rows_gz(ob, "local_rows_1.csv.gz", ["11", "22"])
    outbox.push_outbox(_vm_target(), outbox_dir=ob, runner=lambda cmd: _Res(0))
    assert outbox.load_pushed_ids(outbox_dir=ob) == {"11", "22"}


def test_push_outbox_failed_push_records_nothing(tmp_path):
    ob = tmp_path / "ob"
    _rows_gz(ob, "local_rows_1.csv.gz", ["11"])
    outbox.push_outbox(_vm_target(), outbox_dir=ob, runner=lambda cmd: _Res(1))
    assert outbox.load_pushed_ids(outbox_dir=ob) == set()


def test_prune_pushed_ids_forgets_round_tripped(tmp_path):
    outbox.record_pushed_ids(["1", "2", "3"], outbox_dir=tmp_path)
    outbox.prune_pushed_ids({"2"}, outbox_dir=tmp_path)
    assert outbox.load_pushed_ids(outbox_dir=tmp_path) == {"1", "3"}


def test_load_pushed_ids_missing_or_corrupt_is_empty(tmp_path):
    assert outbox.load_pushed_ids(outbox_dir=tmp_path) == set()
    (tmp_path / "pushed_ids.json").write_text("{not json", encoding="utf-8")
    assert outbox.load_pushed_ids(outbox_dir=tmp_path) == set()


# ---- unsynced_master_ids (the catch-all sweep) --------------------------------

def _gz_ids(path: Path, ids: list[str]) -> None:
    pd.DataFrame({"job_posting_id": ids}).to_csv(path, index=False,
                                                 compression="gzip")


def test_unsynced_ids_local_minus_drive(tmp_path):
    master = tmp_path / "master.csv"
    _write_master(master, [{"job_posting_id": i} for i in ("1", "2", "3")])
    drive = tmp_path / "drive.csv.gz"
    _gz_ids(drive, ["1"])
    got = outbox.unsynced_master_ids(drive, master_csv=master,
                                     outbox_dir=tmp_path / "ob")
    assert got == ["2", "3"]


def test_unsynced_ids_fail_closed_when_drive_missing_or_unreadable(tmp_path):
    # NEVER queue the whole local master just because the Drive copy couldn't
    # be read — that would push thousands of already-synced rows.
    master = tmp_path / "master.csv"
    _write_master(master, [{"job_posting_id": "1"}])
    assert outbox.unsynced_master_ids(tmp_path / "nope.csv.gz", master_csv=master,
                                      outbox_dir=tmp_path / "ob") == []
    bad = tmp_path / "drive.csv.gz"
    bad.write_bytes(b"not a gzip at all")
    assert outbox.unsynced_master_ids(bad, master_csv=master,
                                      outbox_dir=tmp_path / "ob") == []
    assert outbox.unsynced_master_ids(None, master_csv=master,
                                      outbox_dir=tmp_path / "ob") == []


def test_unsynced_ids_skips_pending_and_pushed(tmp_path):
    master = tmp_path / "master.csv"
    _write_master(master, [{"job_posting_id": i} for i in ("1", "2", "3", "4")])
    drive = tmp_path / "drive.csv.gz"
    _gz_ids(drive, ["1"])
    ob = tmp_path / "ob"
    _rows_gz(ob, "local_rows_1.csv.gz", ["2"])          # already queued
    outbox.record_pushed_ids(["3"], outbox_dir=ob)      # in flight to the VM
    assert outbox.unsynced_master_ids(drive, master_csv=master, outbox_dir=ob) == ["4"]


def test_unsynced_ids_prunes_pushed_state_on_round_trip(tmp_path):
    master = tmp_path / "master.csv"
    _write_master(master, [{"job_posting_id": "1"}])
    drive = tmp_path / "drive.csv.gz"
    _gz_ids(drive, ["1"])
    ob = tmp_path / "ob"
    outbox.record_pushed_ids(["1", "9"], outbox_dir=ob)
    outbox.unsynced_master_ids(drive, master_csv=master, outbox_dir=ob)
    assert outbox.load_pushed_ids(outbox_dir=ob) == {"9"}  # "1" round-tripped


# ---- sync_back ----------------------------------------------------------------

def test_sync_back_queues_and_pushes(tmp_path):
    master = tmp_path / "master.csv"
    _write_master(master, [{"job_posting_id": "1", "job_title": "A"},
                           {"job_posting_id": "2", "job_title": "B"}])
    drive = tmp_path / "drive.csv.gz"
    _gz_ids(drive, ["1"])
    ob = tmp_path / "ob"
    queued, pushed, kept = outbox.sync_back(
        _vm_target(), drive, master_csv=master, outbox_dir=ob,
        runner=lambda cmd: _Res(0))
    assert (queued, pushed, kept) == (1, 1, 0)
    assert outbox.pending_files(outbox_dir=ob) == []
    assert outbox.load_pushed_ids(outbox_dir=ob) == {"2"}


def test_sync_back_noop_when_in_sync(tmp_path):
    master = tmp_path / "master.csv"
    _write_master(master, [{"job_posting_id": "1"}])
    drive = tmp_path / "drive.csv.gz"
    _gz_ids(drive, ["1"])
    got = outbox.sync_back(
        _vm_target(), drive, master_csv=master, outbox_dir=tmp_path / "ob",
        runner=lambda cmd: (_ for _ in ()).throw(AssertionError("nothing to push")))
    assert got == (0, 0, 0)


def test_sync_back_never_raises(tmp_path):
    master = tmp_path / "master.csv"
    _write_master(master, [{"job_posting_id": "1", "job_title": "A"},
                           {"job_posting_id": "2", "job_title": "B"}])
    drive = tmp_path / "drive.csv.gz"
    _gz_ids(drive, ["1"])
    got = outbox.sync_back(
        _vm_target(), drive, master_csv=master, outbox_dir=tmp_path / "ob",
        runner=lambda cmd: (_ for _ in ()).throw(RuntimeError("gcloud exploded")))
    assert got[0] == 1 and got[1] == 0    # queued but nothing pushed; no raise
