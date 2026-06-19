"""Chrome profile resolution for opening job/resume links (local/ui.py).

The candidate's Default profile IS you@example.com. An EMPTY account must NOT
match a blank-user_name profile (e.g. a signed-out 'Work' profile) -- it must
fall back to 'Default'. The matcher also tries gaia_name and the email local-part.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))
import ui  # noqa: E402

_STATE = {
    "profile": {
        "info_cache": {
            "Default": {"user_name": "you@example.com", "gaia_name": "You"},
            "Profile 1": {"user_name": "alt@example.com", "gaia_name": "Test User"},
            "Profile 4": {"user_name": "", "gaia_name": ""},
        }
    }
}


def _write_state(tmp_path, state, monkeypatch):
    ud = tmp_path / "Google" / "Chrome" / "User Data"
    ud.mkdir(parents=True)
    (ud / "Local State").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))


def test_matches_user_name(tmp_path, monkeypatch):
    _write_state(tmp_path, _STATE, monkeypatch)
    assert ui._chrome_profile_dir("you@example.com") == "Default"


def test_empty_account_returns_default_not_blank_profile(tmp_path, monkeypatch):
    _write_state(tmp_path, _STATE, monkeypatch)
    assert ui._chrome_profile_dir("") == "Default"


def test_no_match_falls_back_to_default(tmp_path, monkeypatch):
    _write_state(tmp_path, _STATE, monkeypatch)
    assert ui._chrome_profile_dir("nobody@example.com") == "Default"


def test_local_part_fallback(tmp_path, monkeypatch):
    state = {
        "profile": {
            "info_cache": {
                "Default": {"user_name": "someoneelse@gmail.com"},
                "Profile 9": {"user_name": "you@workmail.com"},
            }
        }
    }
    _write_state(tmp_path, state, monkeypatch)
    assert ui._chrome_profile_dir("you@example.com") == "Profile 9"
