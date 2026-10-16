"""Settings > Resume "Show U.S. Citizen" adds the claim to the end of the
résumé's contact line, and only when the user opted in."""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import settings  # noqa: E402
from resume_tailor import assets, config, render  # noqa: E402

_BASICS = {"name": "Ada Lovelace", "location": "Springfield, IL",
           "email": "ada@example.com", "github": "github.com/ada"}


def _contact_line(tex: str) -> str:
    return next(ln for ln in tex.splitlines() if "ada@example.com" in ln)


def test_header_ends_with_citizenship_when_on():
    line = _contact_line(render._header(_BASICS, True))
    assert line.endswith("$|$ \\small{U.S. Citizen}")


def test_header_has_no_citizenship_by_default():
    assert "Citizen" not in render._header(_BASICS)
    assert "Citizen" not in render._header(_BASICS, False)


def test_citizenship_alone_is_the_whole_contact_line():
    tex = render._header({"name": "Ada Lovelace"}, True)
    assert "\\small{U.S. Citizen}\n" in tex
    assert "$|$" not in tex


@pytest.mark.parametrize("stored, expected", [
    ({}, False),
    ({"resume_show_citizenship": False}, False),
    ({"resume_show_citizenship": "true"}, False),   # a stray string is not an opt-in
    ({"resume_show_citizenship": 1}, False),
    ({"resume_show_citizenship": True}, True),
])
def test_show_citizenship_reads_only_a_true_opt_in(monkeypatch, stored, expected):
    monkeypatch.setattr(config, "_config_json", lambda: stored)
    assert config.show_citizenship() is expected


@pytest.mark.parametrize("on", [True, False])
def test_render_follows_the_setting(monkeypatch, on):
    monkeypatch.setattr(assets, "load_master", lambda: {"basics": _BASICS})
    monkeypatch.setattr(assets, "template_head", lambda: "")
    monkeypatch.setattr(config, "_config_json", lambda: {"resume_show_citizenship": on})
    for name in ("_education", "_experience", "_projects", "_leadership", "_skills"):
        monkeypatch.setattr(render, name, lambda *a, **k: "")
    tex = render.render({}, {}, [])
    assert ("U.S. Citizen" in tex) is on


def test_setting_row_is_an_off_by_default_resume_toggle():
    field = next(f for f in settings.SETTINGS_SCHEMA if f.key == "resume_show_citizenship")
    assert (field.type, field.default, field.section, field.target) == (
        "bool", False, "Resume", "config")
