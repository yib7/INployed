"""The watcher pops the dashboard only for a job the High Score tab will show.

`watcher.has_unseen_high_score` used to answer from the CSV alone (score and
is_seen), while High Score also hides an unseen repost of a job marked seen or
applied within `repost_window_days` and ORs seen.db's marks into is_seen. A
watcher that saw only the repost opened a dashboard whose High Score tab was
empty. These tests pin the watcher to the dashboard's own filter.
"""
import gzip
import logging
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jobsdata  # noqa: E402
import watcher  # noqa: E402
from seen_db import SeenRegistry  # noqa: E402


def _write_master(path: Path, rows: list[dict]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        pd.DataFrame(rows).to_csv(fh, index=False)


def _row(jid, score, is_seen, title="Data Analyst", company="Acme",
         location="Austin, TX"):
    return {"job_posting_id": jid, "job_title": title, "company_name": company,
            "job_location": location, "score": score, "is_seen": is_seen,
            "job_description": "long text the probe never needs"}


# A low-score job the user marked seen, and an unseen high-score repost of it.
_REPOST_ROWS = [_row("100", 3, "yes"), _row("200", 8, "no")]


@pytest.fixture
def registry(tmp_path):
    reg = SeenRegistry(tmp_path / "seen.db")
    yield reg
    reg.close()


def test_only_a_recent_repost_does_not_launch(tmp_path, registry):
    p = tmp_path / "master.csv.gz"
    _write_master(p, _REPOST_ROWS)
    registry.mark(["100"])
    cfg = {"repost_window_days": 30}
    assert watcher.has_unseen_high_score(p, 4, registry=registry, cfg=cfg) is False


def test_a_genuine_unseen_high_score_row_launches(tmp_path, registry):
    p = tmp_path / "master.csv.gz"
    _write_master(p, _REPOST_ROWS + [_row("300", 7, "no", title="ML Engineer")])
    registry.mark(["100"])
    cfg = {"repost_window_days": 30}
    assert watcher.has_unseen_high_score(p, 4, registry=registry, cfg=cfg) is True


def test_window_zero_keeps_the_old_answer(tmp_path, registry):
    p = tmp_path / "master.csv.gz"
    _write_master(p, _REPOST_ROWS)
    registry.mark(["100"])
    cfg = {"repost_window_days": 0}
    assert watcher.has_unseen_high_score(p, 4, registry=registry, cfg=cfg) is True


def test_default_window_applies_when_config_has_no_key(tmp_path, registry):
    p = tmp_path / "master.csv.gz"
    _write_master(p, _REPOST_ROWS)
    registry.mark(["100"])
    assert watcher.has_unseen_high_score(p, 4, registry=registry, cfg={}) is False


def test_a_row_seen_db_marked_but_csv_says_no_does_not_launch(tmp_path, registry):
    """The dashboard ORs seen.db into is_seen (csv_io.reconcile_is_seen)."""
    p = tmp_path / "master.csv.gz"
    _write_master(p, [_row("300", 7, "no")])
    registry.mark(["300"])
    assert watcher.has_unseen_high_score(
        p, 4, registry=registry, cfg={"repost_window_days": 30}) is False


def test_fuller_check_raising_falls_back_to_the_prefilter(tmp_path, registry,
                                                          monkeypatch, caplog):
    p = tmp_path / "master.csv.gz"
    _write_master(p, _REPOST_ROWS)
    registry.mark(["100"])

    def boom(*a, **k):
        raise RuntimeError("filter broke")

    monkeypatch.setattr(jobsdata, "filter_high_unseen_with_count", boom)
    with caplog.at_level(logging.WARNING, logger="watcher"):
        assert watcher.has_unseen_high_score(
            p, 4, registry=registry, cfg={"repost_window_days": 30}) is True
    assert "filter broke" in caplog.text


def test_prefilter_miss_skips_the_fuller_check(tmp_path, monkeypatch):
    p = tmp_path / "master.csv.gz"
    _write_master(p, [_row("100", 3, "no")])

    class _NoTouch:
        def marked_at_all(self):
            raise AssertionError("fuller check ran after a prefilter miss")

        all_ids = marked_at_all

    assert watcher.has_unseen_high_score(
        p, 4, registry=_NoTouch(), cfg={"repost_window_days": 30}) is False


def test_fuller_check_reads_multiple_chunks(tmp_path, registry, monkeypatch):
    monkeypatch.setattr(watcher, "_SCAN_CHUNK", 1)
    p = tmp_path / "master.csv.gz"
    filler = [_row(str(i), 1, "no", title=f"Filler {i}") for i in range(400, 404)]
    _write_master(p, filler + [_row("200", 8, "no"), _row("100", 3, "yes")])
    registry.mark(["100"])
    assert watcher.has_unseen_high_score(
        p, 4, registry=registry, cfg={"repost_window_days": 30}) is False
