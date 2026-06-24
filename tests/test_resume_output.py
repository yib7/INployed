"""Concurrency-safe output-dir resolution + usage gating (cycle 11 SP2).

When several résumés tailor in parallel, two jobs with the SAME company+title must
NOT resolve to the same folder and clobber each other's PDF (the résumé file isn't
written until much later, so the on-disk check alone can't catch a same-batch race).
And the parallel orchestrator resets token accounting once, so per-job tailor() calls
must be able to skip their own reset.
"""
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import config, output  # noqa: E402
from resume_tailor import run as run_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(output, "resume_filename", lambda: "R.pdf")
    output._claimed.clear()
    yield
    output._claimed.clear()


# -- resolve_dir concurrency safety -------------------------------------------
def test_first_resolve_returns_base():
    d = output.resolve_dir("Acme", "Engineer")
    assert d == config.OUTPUT_ROOT / "Acme" / "Engineer"
    assert d.exists()


def test_same_company_title_in_batch_gets_distinct_dirs():
    d1 = output.resolve_dir("Acme", "Engineer")
    d2 = output.resolve_dir("Acme", "Engineer")
    d3 = output.resolve_dir("Acme", "Engineer")
    assert len({d1, d2, d3}) == 3          # no clobber
    assert all(d.exists() for d in (d1, d2, d3))
    assert d1 == config.OUTPUT_ROOT / "Acme" / "Engineer"   # first still the base


def test_distinct_titles_each_get_their_base():
    a = output.resolve_dir("Acme", "Engineer")
    b = output.resolve_dir("Acme", "Analyst")
    assert a != b
    assert a == config.OUTPUT_ROOT / "Acme" / "Engineer"
    assert b == config.OUTPUT_ROOT / "Acme" / "Analyst"


def test_existing_resume_on_disk_still_nests():
    base = config.OUTPUT_ROOT / "Acme" / "Engineer"
    base.mkdir(parents=True)
    (base / "R.pdf").write_text("x", encoding="utf-8")   # a prior run's résumé
    output._claimed.clear()                              # simulate a fresh process
    d = output.resolve_dir("Acme", "Engineer")
    assert d != base                                     # nested, didn't overwrite


def test_concurrent_resolve_dirs_are_unique():
    results: list[Path] = []
    lock = threading.Lock()

    def worker():
        d = output.resolve_dir("Acme", "Engineer")
        with lock:
            results.append(d)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8
    assert len(set(results)) == 8          # every thread got a different folder


# -- tailor(reset_usage=...) gating -------------------------------------------
def test_tailor_reset_usage_gate(monkeypatch):
    calls = []
    monkeypatch.setattr(run_mod.llm, "reset_usage", lambda: calls.append(1))
    monkeypatch.setattr(run_mod, "pdflatex_available", lambda: False)  # raise right after the gate
    job = {"company_name": "A", "job_title": "B", "job_description_formatted": "x" * 100}

    with pytest.raises(RuntimeError):
        run_mod.tailor(job, reset_usage=False)
    assert calls == []                     # skipped its own reset

    with pytest.raises(RuntimeError):
        run_mod.tailor(job, reset_usage=True)
    assert calls == [1]                    # default still resets
