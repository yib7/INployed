"""SC-1 to SC-3 and JS-4 (cycle 19): the Jev scorer's switch, its questions,
the requirement-line extractor and the composition code.

Hermetic: the dashboard config is a file in tmp_path (monkeypatched onto
`jev_score.CONFIG_PATH`), the environment is a dict, the SDK probe is patched,
and every judge is scripted. No key, no network.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import jev_score
import jev_switch

REPO = Path(__file__).resolve().parents[1]
KEY = {"TYPESAFE_API_KEY": "not-a-real-key"}


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """Write the dashboard config the scorer reads; `cfg_file(None)` removes it."""
    path = tmp_path / "local" / "config.json"
    path.parent.mkdir()
    monkeypatch.setattr(jev_score, "CONFIG_PATH", path)

    def write(data) -> Path:
        if data is None:
            path.unlink(missing_ok=True)
        elif isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_text(json.dumps(data), encoding="utf-8")
        return path
    return write


@pytest.fixture
def sdk(monkeypatch):
    """The SDK probe both switches use, patched: `sdk(False)` reads it as missing."""
    state = {"found": True}
    real = importlib.util.find_spec

    def find_spec(name, *args, **kwargs):
        if name == "typesafe_sdk":
            return object() if state["found"] else None
        return real(name, *args, **kwargs)
    monkeypatch.setattr(importlib.util, "find_spec", find_spec)

    def set_found(found: bool) -> None:
        state["found"] = found
    return set_found


# --- JS-4: the switch order ---------------------------------------------------------

def test_env_on_wins_over_a_config_that_is_off(cfg_file, sdk, capsys):
    cfg_file({"jev_enabled": False})
    on, why = jev_score.use_jev({"SCORE_USE_JEV": "1", **KEY})
    assert on is True
    assert "SCORE_USE_JEV" in why
    assert capsys.readouterr().out == ""


def test_env_off_wins_over_a_config_that_is_on(cfg_file, sdk, capsys):
    cfg_file({"jev_enabled": True, "jev_scoring": True})
    for raw in ("0", "false", "no", "off", "later"):
        on, why = jev_score.use_jev({"SCORE_USE_JEV": raw, **KEY})
        assert on is False, raw
        assert "SCORE_USE_JEV" in why, raw
    assert capsys.readouterr().out == ""


def test_a_blank_env_switch_reads_as_unset(cfg_file, sdk):
    cfg_file({"jev_enabled": False})
    on, why = jev_score.use_jev({"SCORE_USE_JEV": "   ", **KEY})
    assert on is False
    assert why == "Jev is switched off in Settings"


def test_the_config_decides_when_the_env_is_silent(cfg_file, sdk):
    cfg_file({})
    assert jev_score.use_jev(KEY)[0] is True
    cfg_file({"jev_enabled": True, "jev_scoring": True})
    assert jev_score.use_jev(KEY) == (True, "Settings")
    cfg_file({"jev_enabled": False})
    assert jev_score.use_jev(KEY) == (False, "Jev is switched off in Settings")
    cfg_file({"jev_scoring": False})
    assert jev_score.use_jev(KEY) == (False, "Jev is switched off for scoring in Settings")


def test_no_config_and_no_env_stays_off_without_a_warning(cfg_file, sdk, capsys):
    """The VM case: no dashboard config, no SCORE_USE_JEV. Off by construction,
    even with a key and the SDK present, and silent about it."""
    cfg_file(None)
    on, why = jev_score.use_jev(KEY)
    assert on is False
    assert why
    assert capsys.readouterr().out == ""


def test_a_flat_copy_has_no_config_path(tmp_path, sdk, capsys):
    """The VM copies the pipeline scripts flat into ~/, where there is no
    local/config.json beside them: CONFIG_PATH is None there, so Jev is off."""
    shutil.copy(REPO / "pipeline" / "jev_score.py", tmp_path / "jev_score.py")
    spec = importlib.util.spec_from_file_location("jev_score_flat", tmp_path / "jev_score.py")
    flat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(flat)
    assert flat.CONFIG_PATH is None
    assert flat.use_jev(KEY)[0] is False
    assert capsys.readouterr().out == ""


def test_a_missing_key_prints_one_warning_and_is_off(cfg_file, sdk, capsys):
    cfg_file({})
    for env in ({}, {"TYPESAFE_API_KEY": ""}, {"TYPESAFE_API_KEY": "  "}):
        assert jev_score.use_jev(env) == (False, "no TypeSafe API key")
        out = capsys.readouterr().out
        assert out.count("\n") == 1 and "WARNING" in out and "LLM" in out


def test_a_missing_sdk_prints_one_warning_and_is_off(cfg_file, sdk, capsys):
    cfg_file({})
    sdk(False)
    assert jev_score.use_jev(KEY) == (False, "typesafe-sdk is not installed")
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "WARNING" in out
    assert "not-a-real-key" not in out


def test_a_missing_jev_module_prints_one_warning_and_is_off(cfg_file, sdk, capsys, monkeypatch):
    cfg_file({})

    def no_jev():
        raise ImportError("no module named jev")
    monkeypatch.setattr(jev_score, "_jev_module", no_jev)
    on, why = jev_score.use_jev(KEY)
    assert on is False and "jev" in why
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "WARNING" in out


def test_the_env_switch_still_needs_the_key(cfg_file, sdk, capsys):
    cfg_file(None)
    assert jev_score.use_jev({"SCORE_USE_JEV": "yes"}) == (False, "no TypeSafe API key")
    assert "WARNING" in capsys.readouterr().out


@pytest.mark.parametrize("cfg", [
    {}, {"jev_enabled": True}, {"jev_enabled": False}, {"jev_scoring": False},
    {"jev_enabled": True, "jev_scoring": False}, {"jev_enabled": False, "jev_scoring": True},
    {"jev_enabled": "false"}, {"jev_scoring": 0}, {"jev_enabled": None},
    "{not json", "[1, 2]",
])
@pytest.mark.parametrize("env", [{}, KEY])
@pytest.mark.parametrize("found", [True, False])
def test_use_jev_agrees_with_jev_switch_on_one_config_file(cfg_file, sdk, monkeypatch, capsys,
                                                           cfg, env, found):
    """The scorer's own switch (JS-4) and the dashboard's (`jev_switch`) read the
    same file the same way, so Settings and the scorer never disagree."""
    path = cfg_file(cfg)
    monkeypatch.setattr(jev_switch, "config_path", lambda: path)
    sdk(found)
    on, why = jev_score.use_jev(env)
    assert on is jev_switch.jev_on("scoring", env=env)
    assert why == (jev_switch.jev_why_off("scoring", env=env) or "Settings")


# --- SC-1: importing score_jobs stays light -------------------------------------------

def _copy_pipeline(dest: Path, names) -> Path:
    dest.mkdir(parents=True)
    for name in names:
        shutil.copy(REPO / "pipeline" / name, dest / name)
    return dest


def _run_python(code: str, cwd: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("TYPESAFE_API_KEY", "SCORE_USE_JEV")}
    env["INPLOYED_NO_DOTENV"] = "1"
    done = subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=90)
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


def test_importing_score_jobs_imports_neither_the_sdk_nor_jev(tmp_path):
    pipe = _copy_pipeline(tmp_path / "pipeline",
                          ("score_jobs.py", "jev_score.py", "keypool.py", "run_labels.py"))
    out = _run_python(
        "import sys; sys.path.insert(0, '.'); import score_jobs\n"
        "print(score_jobs.jev_score is not None)\n"
        "print(sorted(m for m in ('typesafe_sdk', 'jev') if m in sys.modules))\n",
        pipe)
    assert out.splitlines() == ["True", "[]"]


def test_score_jobs_runs_on_the_llm_path_without_jev_score_beside_it(tmp_path):
    """The VM copy may lack jev_score.py: the import guard reads that as Jev off."""
    flat = _copy_pipeline(tmp_path / "vm", ("score_jobs.py", "keypool.py", "run_labels.py"))
    out = _run_python(
        "import sys; sys.path.insert(0, '.'); import score_jobs\n"
        "print(score_jobs.jev_score is None)\n"
        "print(score_jobs.make_jev_judge() is None)\n",
        flat)
    lines = out.splitlines()
    assert lines[0] == "True"
    assert "not beside score_jobs.py" in out and "WARNING" not in out
    assert lines[-1] == "True"


def test_a_jev_score_that_fails_to_import_warns_once_and_keeps_the_llm_path(tmp_path):
    flat = _copy_pipeline(tmp_path / "vm", ("score_jobs.py", "keypool.py", "run_labels.py"))
    (flat / "jev_score.py").write_text("raise RuntimeError('broken on purpose')\n",
                                       encoding="utf-8")
    out = _run_python(
        "import sys; sys.path.insert(0, '.'); import score_jobs\n"
        "print(score_jobs.jev_score is None)\n"
        "print(score_jobs.make_jev_judge() is None)\n",
        flat)
    lines = out.splitlines()
    assert lines[0] == "True" and lines[-1] == "True"
    assert out.count("WARNING") == 1 and "RuntimeError" in out
    assert "broken on purpose" not in out
