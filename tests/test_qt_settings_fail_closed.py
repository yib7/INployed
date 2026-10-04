"""The Settings tab never turns a stored "off" on, and saves over a damaged
config only after naming it.

Finding 4-C2: `_checked` fell through to `bool(value)`, so a hand-edited
`"auto_apply_submit": "false"` opened ticked and any unrelated Save wrote
`true`. Finding 4-C1 (the tab's half): a damaged config shows the submit and
Jev switches off, and Save keeps the damaged file beside the new one; a config
that cannot be opened is never written over.
"""
import json
from pathlib import Path

import pytest
import settings
from PySide6 import QtWidgets
from qt.settings_tab import SettingsForm

OFF_WORDS = ("false", "off", "0", "no", "", "False", " OFF ")


def _targets(tmp_path):
    return {
        "config": tmp_path / "config.json",
        "search": tmp_path / "search_config.json",
        "scoring": tmp_path / "scoring_config.json",
        "env": tmp_path / ".env",
    }


def _form(qtbot, tmp_path):
    form = SettingsForm(targets=_targets(tmp_path), collapsed_sections=[],
                        save_collapsed=lambda s: None, show_advanced=False,
                        save_show_advanced=lambda v: None)
    qtbot.addWidget(form)
    return form


@pytest.fixture
def quiet_boxes(monkeypatch):
    """Record every message box instead of showing it."""
    shown = []
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            staticmethod(lambda *a, _n=name, **k: shown.append((_n, a))))
    monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No))
    return shown


def _stored(tmp_path) -> dict:
    return json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("word", OFF_WORDS)
def test_c2_a_false_word_plus_an_unrelated_save_keeps_submit_off(qtbot, tmp_path,
                                                                 quiet_boxes, word):
    import apply_run
    (tmp_path / "config.json").write_text(
        json.dumps({"auto_apply_submit": word, "min_score": 4}), encoding="utf-8")
    form = _form(qtbot, tmp_path)
    assert form._widgets["auto_apply_submit"].isChecked() is False
    form._setters["min_score"](3)
    form._on_field_edited("min_score")
    assert form.save() is True
    stored = _stored(tmp_path)
    assert stored["min_score"] == 3
    assert stored["auto_apply_submit"] is False
    assert apply_run.submit_on(settings.load(_targets(tmp_path))) is False


@pytest.mark.parametrize("key", sorted(settings.STRICT_SWITCHES))
@pytest.mark.parametrize("word", ("true", "yes", "on", "1"))
def test_c2_a_strict_switch_shows_a_string_as_off(qtbot, tmp_path, key, word):
    """Their runtime readers take only a stored True, so a string there runs
    off; the box shows it off and a Save keeps it off."""
    (tmp_path / "config.json").write_text(json.dumps({key: word}), encoding="utf-8")
    form = _form(qtbot, tmp_path)
    assert form._widgets[key].isChecked() is False


def test_c2_the_strict_switches_are_read_by_is_true_at_runtime(tmp_path, monkeypatch):
    import apply_run
    from resume_tailor import config as rt_config
    assert apply_run.submit_on({"auto_apply_submit": "true"}) is False
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(rt_config, "CONFIG_JSON", cfg)
    for env in ("RESUME_TAILOR_BEST_OF_N", "RESUME_TAILOR_COVER_LETTER_JEV_CHECK",
                "RESUME_TAILOR_ATS_MEANING"):
        monkeypatch.delenv(env, raising=False)
    cfg.write_text(json.dumps({k: "true" for k in settings.STRICT_SWITCHES}), encoding="utf-8")
    assert rt_config.best_of_n() is False
    assert rt_config.cover_letter_jev_check() is False
    assert rt_config.ats_meaning() is False


@pytest.mark.parametrize("word", ("false", "off", "0", "no"))
def test_c2_every_bool_field_shows_a_false_word_as_off(qtbot, tmp_path, word):
    by_target: dict = {}
    for f in settings.SETTINGS_SCHEMA:
        if f.type == "bool" and f.target != "env":
            by_target.setdefault(f.target, {})[f.key] = word
    for target, data in by_target.items():
        _targets(tmp_path)[target].write_text(json.dumps(data), encoding="utf-8")
    form = _form(qtbot, tmp_path)
    for data in by_target.values():
        for key in data:
            assert form._widgets[key].isChecked() is False, key


def test_c2_a_real_true_still_ticks(qtbot, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"auto_apply_submit": True}),
                                          encoding="utf-8")
    form = _form(qtbot, tmp_path)
    assert form._widgets["auto_apply_submit"].isChecked() is True


# --- the tab's half of 4-C1 -------------------------------------------------------

DAMAGED = '{"auto_apply_submit": false, "jev_enabled": false, "min_score": 6,}'


def test_c1_a_damaged_config_opens_with_submit_and_jev_off_and_save_keeps_it_aside(
        qtbot, tmp_path, quiet_boxes):
    (tmp_path / "config.json").write_text(DAMAGED, encoding="utf-8")
    form = _form(qtbot, tmp_path)
    assert form._widgets["auto_apply_submit"].isChecked() is False
    for key in settings.JEV_SWITCHES:
        assert form._widgets[key].isChecked() is False, key
    assert "config.json.corrupt-" in form.status.text()
    assert form.save() is True
    stored = _stored(tmp_path)
    assert stored["auto_apply_submit"] is False and stored["jev_enabled"] is False
    kept = list(tmp_path.glob("config.json.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == DAMAGED


def test_c1_a_file_damaged_after_the_tab_opened_is_refused(qtbot, tmp_path, quiet_boxes):
    (tmp_path / "config.json").write_text(json.dumps({"auto_apply_submit": False}),
                                          encoding="utf-8")
    form = _form(qtbot, tmp_path)
    (tmp_path / "config.json").write_text(DAMAGED, encoding="utf-8")
    assert form.save() is False
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == DAMAGED
    assert form.status.text() == "Save failed."
    assert any(kind == "critical" and "config.json is not a valid JSON object" in a[2]
               for kind, a in quiet_boxes)


def test_c1_a_config_that_cannot_be_opened_is_never_written_over(qtbot, tmp_path,
                                                                quiet_boxes, monkeypatch):
    good = {"auto_apply_submit": False, "hidden_columns": {"jobs": ["company"]}}
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(good), encoding="utf-8")
    real = Path.read_text

    def locked(self, *a, **k):
        if Path(self) == cfg:
            raise PermissionError(32, "sharing violation (synthetic)")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", locked)
    form = _form(qtbot, tmp_path)
    assert form._widgets["auto_apply_submit"].isChecked() is False
    assert "Save will not write to it until it opens" in form.status.text()
    assert form.save() is False
    monkeypatch.setattr(Path, "read_text", real)
    assert json.loads(cfg.read_text(encoding="utf-8")) == good
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_c1_a_rebuilt_config_names_the_submit_switch(qtbot, tmp_path):
    (tmp_path / "config.json.corrupt-20261001-120000").write_text(DAMAGED, encoding="utf-8")
    (tmp_path / "config.json").write_text(json.dumps({"hidden_columns": {}}), encoding="utf-8")
    form = _form(qtbot, tmp_path)
    assert form._widgets["auto_apply_submit"].isChecked() is False
    assert "Submit when verified shows off until you save it" in form.status.text()
