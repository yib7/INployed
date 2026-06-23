"""Headless tests for local/config_form.py — the shared config GUI form.

Builds the real Tkinter widgets against a temp config dir, so they verify the
behaviours that keep users safe: secret boxes start blank (the stored token is
never shown), a blank secret is omitted on save (so it isn't wiped), dropdowns
and multichoice round-trip, and Restore defaults resets the widgets.

Skips automatically if no display/Tk is available.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

tk = pytest.importorskip("tkinter")

import config_form  # noqa: E402
import settings  # noqa: E402

# `root` is the session-scoped Tk fixture from conftest.py (shared by all GUI
# tests; only one Tk interpreter per process to avoid Windows flakiness).


def _targets(tmp_path: Path) -> dict[str, Path]:
    return {
        "config": tmp_path / "config.json",
        "search": tmp_path / "search_config.json",
        "scoring": tmp_path / "scoring_config.json",
        "apply": tmp_path / "apply_config.json",
        "env": tmp_path / ".env",
    }


def _form(root, tmp_path):
    return config_form.ConfigForm(tk.Frame(root), targets=_targets(tmp_path))


def test_form_builds_one_widget_per_field_type(root, tmp_path):
    form = _form(root, tmp_path)
    assert "min_score" in form.vars                 # int  -> entry
    assert "gemini_auth" in form.vars               # choice -> dropdown
    assert "keywords" in form.texts                 # list -> multi-line
    assert "remote_types" in form.multi             # multichoice -> checkboxes
    assert "RESUME_TAILOR_OUTPUT" in form.vars      # path -> entry + Browse
    assert "BRIGHT_DATA_API_TOKEN" in form.vars     # secret -> masked entry


def test_secret_box_starts_blank_and_shows_status(root, tmp_path):
    targets = _targets(tmp_path)
    (tmp_path / ".env").write_text("BRIGHT_DATA_API_TOKEN=seeded\n", encoding="utf-8")
    form = config_form.ConfigForm(tk.Frame(root), targets=targets)
    assert form.vars["BRIGHT_DATA_API_TOKEN"].get() == ""  # never pre-filled
    assert "configured" in form._secret_labels["BRIGHT_DATA_API_TOKEN"].cget("text")


def test_collect_omits_blank_secret(root, tmp_path):
    form = _form(root, tmp_path)
    values, errors = form.collect()
    assert errors == {}
    assert "BRIGHT_DATA_API_TOKEN" not in values  # blank -> preserved, not wiped


def test_collect_includes_typed_secret_and_explicit_clear(root, tmp_path):
    form = _form(root, tmp_path)
    form.vars["GEMINI_API_KEYS"].set("k1,k2")
    values, _ = form.collect()
    assert values["GEMINI_API_KEYS"] == "k1,k2"
    form.vars["GEMINI_API_KEYS"].set("")
    form.clear_vars["GEMINI_API_KEYS"].set(True)
    values, _ = form.collect()
    assert values["GEMINI_API_KEYS"] == ""  # Clear ticked -> explicit unset


def test_save_roundtrips_choice_and_multichoice(root, tmp_path):
    targets = _targets(tmp_path)
    form = config_form.ConfigForm(tk.Frame(root), targets=targets)
    form.vars["gemini_auth"].set("api_key")
    form.multi["remote_types"]["Remote"].set(True)
    assert form.save() is True
    reloaded = settings.load(targets)
    assert reloaded["gemini_auth"] == "api_key"
    assert "Remote" in reloaded["remote_types"]


def test_save_writes_secret_to_env_then_clears_box(root, tmp_path):
    targets = _targets(tmp_path)
    form = config_form.ConfigForm(tk.Frame(root), targets=targets)
    form.vars["GEMINI_API_KEYS"].set("k1,k2")
    assert form.save() is True
    assert settings.secret_status(targets)["GEMINI_API_KEYS"] is True
    assert form.vars["GEMINI_API_KEYS"].get() == ""  # box cleared after save
    assert "configured" in form._secret_labels["GEMINI_API_KEYS"].cget("text")


def test_invalid_number_blocks_save(root, tmp_path, monkeypatch):
    form = _form(root, tmp_path)
    form.vars["min_score"].set("not-a-number")
    # don't pop a real dialog in a headless run
    monkeypatch.setattr(config_form.messagebox, "showerror", lambda *a, **k: None)
    assert form.save() is False


def test_restore_defaults_resets_widgets(root, tmp_path):
    form = _form(root, tmp_path)
    form.vars["min_score"].set("1")
    form.multi["remote_types"]["Remote"].set(True)
    form.restore_defaults()
    assert form.vars["min_score"].get() == "4"
    assert form.multi["remote_types"]["Remote"].get() is False  # not in default


def test_on_saved_callback_fires(root, tmp_path):
    fired = []
    form = config_form.ConfigForm(
        tk.Frame(root), targets=_targets(tmp_path), on_saved=lambda: fired.append(1))
    assert form.save() is True
    assert fired == [1]
