"""Deleting a job recycles its Generated_Resumes folder — the helper.

recycle_resume_folder must refuse (return False, delete nothing) anything that
is not an existing directory strictly inside config.OUTPUT_ROOT, and after a
recycle prune the now-empty ancestor dirs (<Company>/, and <Company>/<Title>/
for the dated layout) without ever removing the root itself. send2trash is
faked throughout — nothing here touches the real Recycle Bin or the user's
real ~/Downloads/Generated_Resumes (OUTPUT_ROOT is pointed at tmp_path).

Run:  python -m pytest tests/ -v
"""
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import resume_trash  # noqa: E402
from resume_tailor import config  # noqa: E402
from resume_trash import recycle_resume_folder  # noqa: E402


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A throwaway OUTPUT_ROOT; the helper reads config at call time, so the
    monkeypatch (the same override the env var feeds at import) is honored."""
    r = tmp_path / "Generated_Resumes"
    r.mkdir()
    monkeypatch.setattr(config, "OUTPUT_ROOT", r)
    return r


@pytest.fixture
def fake_trash(monkeypatch):
    """Record send2trash calls and mimic its visible effect (folder vanishes)."""
    calls = []

    def fake(path):
        calls.append(Path(path))
        shutil.rmtree(path)

    monkeypatch.setattr(resume_trash, "send2trash", fake)
    return calls


# ---- safety gates: refuse -> False, and the trash is never touched ----------

def test_refuses_falsy_input(root, fake_trash):
    assert recycle_resume_folder(None) is False
    assert recycle_resume_folder("") is False
    assert fake_trash == []


def test_refuses_nonexistent_path(root, fake_trash):
    assert recycle_resume_folder(str(root / "Acme" / "Engineer")) is False
    assert fake_trash == []


def test_refuses_plain_file(root, fake_trash):
    f = root / "stray.txt"
    f.write_text("not a folder", encoding="utf-8")
    assert recycle_resume_folder(str(f)) is False
    assert f.exists() and fake_trash == []


def test_refuses_path_outside_root(root, fake_trash, tmp_path):
    outside = tmp_path / "elsewhere" / "Acme"
    outside.mkdir(parents=True)
    assert recycle_resume_folder(str(outside)) is False
    assert outside.is_dir() and fake_trash == []


def test_refuses_the_root_itself(root, fake_trash):
    assert recycle_resume_folder(str(root)) is False
    assert root.is_dir() and fake_trash == []


# ---- the recycle + ancestor pruning ------------------------------------------

def test_recycles_folder_and_prunes_empty_company_dir(root, fake_trash):
    folder = root / "Acme" / "Engineer"
    folder.mkdir(parents=True)
    (folder / "resume.pdf").write_text("pdf", encoding="utf-8")
    assert recycle_resume_folder(str(folder)) is True
    assert fake_trash == [folder.resolve()]
    assert not (root / "Acme").exists()   # now-empty ancestor pruned
    assert root.is_dir()                  # the root is never removed


def test_recycles_dated_layout_and_prunes_both_ancestors(root, fake_trash):
    dated = root / "Acme" / "Engineer" / "2026-07-04"
    dated.mkdir(parents=True)
    assert recycle_resume_folder(str(dated)) is True
    assert fake_trash == [dated.resolve()]
    assert not (root / "Acme" / "Engineer").exists()
    assert not (root / "Acme").exists()
    assert root.is_dir()


def test_prune_stops_at_first_non_empty_ancestor(root, fake_trash):
    folder = root / "Acme" / "Engineer"
    folder.mkdir(parents=True)
    sibling = root / "Acme" / "Analyst"   # keeps <Company>/ non-empty
    sibling.mkdir()
    assert recycle_resume_folder(str(folder)) is True
    assert (root / "Acme").is_dir() and sibling.is_dir()


def test_send2trash_failure_propagates(root, monkeypatch):
    """Gates say 'refuse' with False; a real deletion failure must raise so the
    caller can report it (folder locked by Explorer etc.)."""
    folder = root / "Acme" / "Engineer"
    folder.mkdir(parents=True)

    def boom(path):
        raise OSError("folder is locked")

    monkeypatch.setattr(resume_trash, "send2trash", boom)
    with pytest.raises(OSError, match="locked"):
        recycle_resume_folder(str(folder))
    assert folder.is_dir()   # nothing was pruned after the failure
