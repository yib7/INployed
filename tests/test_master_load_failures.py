"""3.12: what the user sees when the file they hand-edit is broken or absent.

`master_experience.yaml` is git-ignored personal data the user is told to edit by
hand, so a broken edit and a fresh clone with no file at all are the two most
likely first failures in the whole résumé engine. Both used to surface as a raw
`yaml.ParserError` / `FileNotFoundError` traceback. They are now one `ValueError`
naming the file, the position and the fix — and `ValueError` is already in the
tuple `qt/resume_data_tab.py` catches, so the dashboard shows it as a message.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import assets, config  # noqa: E402


@pytest.fixture
def master_at(tmp_path, monkeypatch):
    """Point config.MASTER_YAML at a path in an EMPTY dir, so the committed
    example cannot be found as a sibling and the real failure path runs."""
    def _set(text=None, name="master_experience.yaml"):
        p = tmp_path / name
        if text is not None:
            p.write_text(text, encoding="utf-8")
        monkeypatch.setattr(config, "MASTER_YAML", p)
        assets.load_master.cache_clear()
        return p

    yield _set
    assets.load_master.cache_clear()


def test_malformed_yaml_names_the_file_and_the_line(master_at):
    master_at("basics: [unclosed\n  - nope\n")
    with pytest.raises(ValueError) as ei:
        assets.load_master()
    msg = str(ei.value)
    assert "master_experience.yaml" in msg
    assert "not valid YAML" in msg
    assert "line" in msg and "column" in msg
    assert "master_experience.example.yaml" in msg      # names the way out
    # the raw parser traceback is gone: no chained cause to print
    assert ei.value.__cause__ is None


def test_bad_indentation_is_reported_the_same_way(master_at):
    master_at("basics:\n  name: Jane\n   email: jane@example.com\n")
    with pytest.raises(ValueError, match="not valid YAML"):
        assets.load_master()


def test_missing_file_with_no_example_points_at_setup(master_at):
    master_at(None)                                     # nothing written
    with pytest.raises(ValueError) as ei:
        assets.load_master()
    msg = str(ei.value)
    assert "No resume master file found" in msg
    assert "setup.ps1" in msg
    assert "master_experience.example.yaml" in msg
    assert ei.value.__cause__ is None


def test_a_fresh_clone_still_falls_back_to_the_committed_example(monkeypatch):
    """The missing-file message must only appear when the example is gone too:
    a fresh clone has the example and the engine has to keep working on it."""
    real = REPO / "resume_tailor_files" / "master_experience.yaml"
    monkeypatch.setattr(config, "MASTER_YAML", real)
    assets.load_master.cache_clear()
    try:
        assert (real.with_name("master_experience.example.yaml")).exists()
        data = assets.load_master()
        assert isinstance(data, dict) and data
    finally:
        assets.load_master.cache_clear()


def test_a_yaml_scalar_is_still_the_mapping_error_not_the_parse_error(master_at):
    master_at("just a string, valid yaml, wrong shape\n")
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        assets.load_master()


def test_an_empty_file_is_reported_as_empty(master_at):
    master_at("")
    with pytest.raises(ValueError, match="empty file"):
        assets.load_master()
