"""P2-9: run.py's master-CSV default must not bake in a machine-specific drive
letter (was hardcoded `E:/My Drive/...`). It now resolves via jobsdata's
`gdrive_root_dir` (config.json's gdrive_root) or the repo-root master, and falls
back to REQUIRING --csv when nothing resolves.
"""
import inspect
from pathlib import Path

from resume_tailor import run


def test_run_source_has_no_hardcoded_drive_letter():
    src = inspect.getsource(run)
    assert "E:/" not in src and "E:\\" not in src
    assert not hasattr(run, "_DEFAULT_CSV")   # the hardcoded literal is gone


def test_default_csv_resolves_via_gdrive_root(monkeypatch, tmp_path):
    import jobsdata
    master = tmp_path / "linkedin_jobs_master.csv.gz"
    master.write_bytes(b"")                    # just has to exist
    monkeypatch.setattr(jobsdata, "gdrive_root_dir", lambda _paths: tmp_path)
    assert run._default_csv() == str(master)   # Drive root wins, no drive letter baked in


def test_default_csv_returns_none_when_nothing_resolves(monkeypatch):
    import jobsdata
    monkeypatch.setattr(jobsdata, "gdrive_root_dir", lambda _paths: None)
    result = run._default_csv()
    # Either nothing resolves (None -> CLI requires --csv) or the repo-root master
    # is found; never a hardcoded E:/ path.
    assert result is None or Path(result).name in run._MASTER_NAMES
    if result is not None:
        assert "E:/" not in result and "E:\\" not in result
