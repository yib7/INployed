"""A config file that exists and cannot be read or parsed is never merged onto {}.

Were `update_json_locked` to move a damaged config.json aside and write only
the keys being saved, the next column toggle or the watcher's gdrive_root
write would drop `auto_apply_submit`, and the runner would then read the
default (True). A transient read error would do the same with no copy kept. These
tests pin the fail-closed behaviour: the write is refused, the file stays as it
was, every caller reports it without crashing, and a damaged or rebuilt config
reads the submit switch (and the Jev switches) as off.
"""
import json
import logging
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jsonutil  # noqa: E402
import settings  # noqa: E402

DAMAGED = '{"auto_apply_submit": false, "min_score": 6,}'      # trailing comma


def _targets(tmp_path):
    return {
        "config": tmp_path / "config.json",
        "search": tmp_path / "search_config.json",
        "scoring": tmp_path / "scoring_config.json",
        "env": tmp_path / ".env",
    }


def _flaky_read(monkeypatch, path):
    """Make every read of `path` fail like a Windows sharing violation."""
    real = Path.read_text

    def read(self, *a, **k):
        if Path(self) == Path(path):
            raise PermissionError(32, "sharing violation (synthetic)")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read)


# --- jsonutil ------------------------------------------------------------------

@pytest.mark.parametrize("damaged", [DAMAGED, '["a list"]', "\xff\xfe not text"])
def test_c1_update_json_locked_refuses_a_damaged_file(tmp_path, damaged):
    p = tmp_path / "config.json"
    if damaged.startswith("\xff"):
        p.write_bytes(b"\xff\xfe\x00 not text")
    else:
        p.write_text(damaged, encoding="utf-8")
    before = p.read_bytes()
    with pytest.raises(jsonutil.JsonUnreadable) as info:
        jsonutil.update_json_locked(p, {"hidden_columns": ["company"]})
    assert info.value.damaged is True
    assert isinstance(info.value, OSError)          # callers that catch OSError keep working
    assert "config.json" in str(info.value) and "nothing was saved" in str(info.value)
    assert p.read_bytes() == before                 # the damaged file stays where readers see it
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_c1_update_json_locked_refuses_a_file_it_cannot_read(tmp_path, monkeypatch):
    """probe_config_oserror: a transient read error never writes only the new key."""
    p = tmp_path / "config.json"
    good = {"auto_apply_submit": True, "jev_enabled": True, "candidate_name": "Jane Doe"}
    p.write_text(json.dumps(good), encoding="utf-8")
    _flaky_read(monkeypatch, p)
    with pytest.raises(jsonutil.JsonUnreadable) as info:
        jsonutil.update_json_locked(p, {"hidden_columns": ["x"]})
    assert info.value.damaged is False
    assert "PermissionError" in str(info.value)
    monkeypatch.undo()
    assert json.loads(p.read_text(encoding="utf-8")) == good


def test_c1_update_json_locked_still_creates_a_missing_file(tmp_path):
    p = tmp_path / "config.json"
    assert jsonutil.update_json_locked(p, {"a": 1}) == {"a": 1}
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}


def test_c1_the_submit_switch_survives_the_next_partial_write(tmp_path, monkeypatch):
    """probe_submit_rewrite: broken file, then a column hide. The runner must
    still read the submit switch as off, with the park note."""
    import apply_run
    targets = _targets(tmp_path)
    monkeypatch.setattr(settings, "_resolve_targets", lambda t: targets if t is None else t)
    targets["config"].write_text(DAMAGED, encoding="utf-8")
    first = apply_run.load_settings()
    assert first["auto_apply_submit"] is False and first.get(apply_run.PARK_MODE_KEY)
    with pytest.raises(OSError):
        jsonutil.update_json_locked(targets["config"], {"hidden_columns": ["company"]})
    second = apply_run.load_settings()
    assert second["auto_apply_submit"] is False
    assert second.get(apply_run.PARK_MODE_KEY)
    assert settings.read_problem("config") == "config.json is not valid JSON"


# --- the callers ------------------------------------------------------------------

def test_c1_the_watcher_logs_and_carries_on(tmp_path, monkeypatch, caplog):
    import watcher
    cfg = tmp_path / "config.json"
    cfg.write_text(DAMAGED, encoding="utf-8")
    monkeypatch.setattr(watcher, "CONFIG_PATH", cfg)
    with caplog.at_level(logging.WARNING):
        watcher.save_config_key("gdrive_root", "E:/drive")      # must not raise
    assert cfg.read_text(encoding="utf-8") == DAMAGED
    assert "config.json is not a valid JSON object" in caplog.text
    assert "gdrive_root" in caplog.text


def test_c1_the_watcher_logs_a_read_error_too(tmp_path, monkeypatch, caplog):
    import watcher
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"auto_apply_submit": False}), encoding="utf-8")
    monkeypatch.setattr(watcher, "CONFIG_PATH", cfg)
    _flaky_read(monkeypatch, cfg)
    with caplog.at_level(logging.WARNING):
        watcher.save_config_key("gdrive_root", "E:/drive")
    monkeypatch.undo()
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"auto_apply_submit": False}
    assert "could not be opened" in caplog.text


def test_c1_jobsdata_save_cfg_logs_and_keeps_the_file(tmp_path, monkeypatch, caplog):
    import jobsdata
    cfg = tmp_path / "config.json"
    cfg.write_text(DAMAGED, encoding="utf-8")
    monkeypatch.setattr(jobsdata, "_cfg_path", lambda: cfg)
    with caplog.at_level(logging.WARNING):
        jobsdata.save_hidden_columns(["company"])
    assert cfg.read_text(encoding="utf-8") == DAMAGED
    assert "config.json is not a valid JSON object" in caplog.text


# --- settings.save and settings.load ---------------------------------------------

def test_c1_settings_save_refuses_a_damaged_file_unless_asked(tmp_path):
    targets = _targets(tmp_path)
    targets["config"].write_text(DAMAGED, encoding="utf-8")
    with pytest.raises(jsonutil.JsonUnreadable):
        settings.save({"min_score": 3}, targets)
    assert targets["config"].read_text(encoding="utf-8") == DAMAGED
    settings.save({"min_score": 3, "auto_apply_submit": False}, targets,
                  replace_damaged={"config"})
    assert json.loads(targets["config"].read_text(encoding="utf-8")) == {
        "min_score": 3, "auto_apply_submit": False}
    kept = list(tmp_path.glob("config.json.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == DAMAGED


def test_c1_settings_save_never_replaces_a_file_it_cannot_read(tmp_path, monkeypatch):
    targets = _targets(tmp_path)
    good = {"auto_apply_submit": False, "hidden_columns": ["x"]}
    targets["config"].write_text(json.dumps(good), encoding="utf-8")
    _flaky_read(monkeypatch, targets["config"])
    with pytest.raises(jsonutil.JsonUnreadable):
        settings.save({"min_score": 3}, targets, replace_damaged={"config"})
    monkeypatch.undo()
    assert json.loads(targets["config"].read_text(encoding="utf-8")) == good
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_c1_a_damaged_config_reads_submit_and_jev_off(tmp_path):
    targets = _targets(tmp_path)
    targets["config"].write_text(DAMAGED, encoding="utf-8")
    values = settings.load(targets)
    assert values["auto_apply_submit"] is False
    for key in settings.JEV_SWITCHES:
        assert values[key] is False, key
    assert settings.submit_problem(targets) == "config.json is not valid JSON"


def test_c1_a_healthy_config_keeps_its_defaults(tmp_path):
    targets = _targets(tmp_path)
    targets["config"].write_text(json.dumps({"min_score": 5}), encoding="utf-8")
    values = settings.load(targets)
    assert values["auto_apply_submit"] is True
    assert all(values[k] is True for k in settings.JEV_SWITCHES)
    assert settings.submit_problem(targets) == ""


def test_c1_a_config_rebuilt_beside_a_damaged_copy_reads_submit_off(tmp_path):
    """The state an earlier version left: the damaged file kept aside and a new
    config.json holding only the keys of one partial write."""
    targets = _targets(tmp_path)
    (tmp_path / "config.json.corrupt-20261001-120000").write_text(DAMAGED, encoding="utf-8")
    targets["config"].write_text(json.dumps({"hidden_columns": ["company"]}),
                                 encoding="utf-8")
    assert settings.load(targets)["auto_apply_submit"] is False
    problem = settings.submit_problem(targets)
    assert "config.json.corrupt-20261001-120000" in problem
    # a Settings Save writes the switch, and from then on the stored value rules
    targets["config"].write_text(json.dumps({"auto_apply_submit": True}), encoding="utf-8")
    assert settings.load(targets)["auto_apply_submit"] is True
    assert settings.submit_problem(targets) == ""
