"""Same-day, same-label runs each get their own run file (SP5).

A second run with the same date and label used to overwrite
`<label>/linkedin_jobs_<date>_<label>.csv`. The scorer then skipped it because
`<stem>_scored.csv.gz` already existed, and on 2026-10-12 that silently lost
185 jobs. Now the later run takes `-2`, `-3`, ... and every reader still finds it.
"""
import asyncio
from datetime import datetime

import pandas as pd
import pytest

import jobsdata
import score_jobs
import scraper


@pytest.fixture
def fake_run(monkeypatch, tmp_path):
    """main() end to end on the recovery path (no trigger, no network), writing
    into tmp_path. Returns a function that runs once with the given job ids."""
    label = scraper.RUN_LABELS[0]
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(scraper, "MASTER_CSV", tmp_path / "linkedin_jobs_master.csv")
    monkeypatch.setattr(scraper, "PREVIOUS_IDS_FILE", tmp_path / "last_run_job_ids.json")
    monkeypatch.setattr(scraper, "LEDGER_CSV", tmp_path / "collected_ids_ledger.csv")
    monkeypatch.setattr(scraper, "require_credentials", lambda: None)
    # The real defaults, narrowed: the ledger write reads exclude_window_days.
    base = dict(scraper.load_search_config(), keywords=["k"],
                remote_types=["remote"], limit_per_input=5)
    monkeypatch.setattr(scraper, "load_search_config", lambda: base)
    monkeypatch.setattr(scraper, "load_blocklist", lambda: [])
    monkeypatch.setattr(scraper, "append_to_master", lambda df: len(df))
    rows: list[dict] = []

    async def fake_download(session, snapshot_id):
        return list(rows)

    monkeypatch.setattr(scraper, "download", fake_download)

    def go(ids):
        rows[:] = [{"job_posting_id": i, "job_title": f"T{i}"} for i in ids]
        asyncio.run(scraper.main(snapshot_id="snap-recover", run_label=label))

    go.label = label
    go.dir = tmp_path / label
    return go


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _scored(path, ids=("0",)) -> None:
    """A real (gzipped) scored run file: the ledger seed reads these."""
    pd.DataFrame({"job_posting_id": list(ids)}).to_csv(path, index=False,
                                                       compression="gzip")


def _ids(path) -> list[str]:
    return sorted(pd.read_csv(path, dtype={"job_posting_id": str})["job_posting_id"])


def test_a_second_same_day_run_writes_dash_2_and_leaves_the_first_alone(fake_run, capsys):
    fake_run(["1", "2"])
    label, day = fake_run.label, _today()
    first = fake_run.dir / f"linkedin_jobs_{day}_{label}.csv"
    assert first.exists()
    before = first.read_bytes()
    capsys.readouterr()

    fake_run(["3"])

    second = fake_run.dir / f"linkedin_jobs_{day}_{label}-2.csv"
    assert second.exists()
    assert first.read_bytes() == before                     # the first run is untouched
    assert _ids(first) == ["1", "2"]
    assert _ids(second) == ["3"]
    out = capsys.readouterr().out
    assert f"Saved 1 jobs -> {label}/{second.name}" in out
    assert f"an earlier {label} run today already used the base name" in out
    assert "WARNING" not in out                             # nothing is wrong
    assert "—" not in out


def test_a_first_run_keeps_the_base_name(fake_run, capsys):
    fake_run(["1"])
    names = sorted(p.name for p in fake_run.dir.glob("*.csv"))
    assert names == [f"linkedin_jobs_{_today()}_{fake_run.label}.csv"]
    assert "already used the base name" not in capsys.readouterr().out


def test_only_the_scored_file_left_still_gets_dash_2(fake_run):
    """The raw CSV can be gone (cleaned up, or synced away) while its scored
    output remains: the scorer would still skip a new file under that stem."""
    label, day = fake_run.label, _today()
    fake_run.dir.mkdir(parents=True)
    scored = fake_run.dir / f"linkedin_jobs_{day}_{label}_scored.csv.gz"
    _scored(scored)
    before = scored.read_bytes()

    fake_run(["7"])

    assert not (fake_run.dir / f"linkedin_jobs_{day}_{label}.csv").exists()
    assert _ids(fake_run.dir / f"linkedin_jobs_{day}_{label}-2.csv") == ["7"]
    assert scored.read_bytes() == before


def test_dash_3_when_dash_2_is_taken(fake_run):
    label, day = fake_run.label, _today()
    fake_run.dir.mkdir(parents=True)
    (fake_run.dir / f"linkedin_jobs_{day}_{label}.csv").write_text("job_posting_id\n1\n")
    # -2 only has its scored output left: it still counts as taken.
    _scored(fake_run.dir / f"linkedin_jobs_{day}_{label}-2_scored.csv.gz")

    fake_run(["9"])

    assert _ids(fake_run.dir / f"linkedin_jobs_{day}_{label}-3.csv") == ["9"]
    assert not (fake_run.dir / f"linkedin_jobs_{day}_{label}-2.csv").exists()


def test_the_scorer_and_the_dashboard_find_the_new_file_unscored(fake_run, monkeypatch):
    label, day = fake_run.label, _today()
    fake_run(["1"])
    first = fake_run.dir / f"linkedin_jobs_{day}_{label}.csv"
    # The first run was scored, as on 2026-10-12.
    _scored(fake_run.dir / f"{first.stem}_scored.csv.gz", ["1"])

    fake_run(["2"])
    second = fake_run.dir / f"linkedin_jobs_{day}_{label}-2.csv"

    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", fake_run.dir.parent)
    assert score_jobs.latest_input_csv() == second
    assert jobsdata.unscored_run_csvs(fake_run.dir.parent) == [second]
    # Once scored, the dashboard's local run-file scan lists it beside the first.
    scored2 = fake_run.dir / f"{second.stem}_scored.csv.gz"
    _scored(scored2, ["2"])
    assert scored2 in jobsdata.local_run_files(fake_run.dir.parent)
    assert score_jobs.latest_input_csv() is None
