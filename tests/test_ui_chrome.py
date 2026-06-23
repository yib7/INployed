"""Chrome profile resolution for opening job/resume links (local/ui.py).

The configured account's Default profile IS you@example.com. An EMPTY account must NOT
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
            "Profile 1": {"user_name": "alt@example.com", "gaia_name": "Alt Account"},
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


# --- open_in_chrome guards scraped URLs before launching the browser ----------

def test_open_in_chrome_refuses_non_http_url(monkeypatch):
    # A '-'-leading or non-http value (e.g. arg-injection / file: / javascript:)
    # must never reach Chrome's argv or the default browser.
    calls = []
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *a, **k: calls.append(("popen", a)))
    monkeypatch.setattr(ui.webbrowser, "open", lambda *a, **k: calls.append(("web", a)))
    for bad in ("--gpu-launcher=calc.exe", "file:///etc/passwd", "javascript:alert(1)", ""):
        ui.open_in_chrome(bad)
    assert calls == []  # nothing launched for any unsafe input


def test_open_in_chrome_passes_http_url_after_options_terminator(monkeypatch):
    seen = {}
    monkeypatch.setattr(ui, "_chrome_launcher", lambda: ("chrome.exe", "Default"))
    monkeypatch.setattr(ui.subprocess, "Popen", lambda args, *a, **k: seen.setdefault("args", args))
    ui.open_in_chrome("https://www.linkedin.com/jobs/view/123")
    # the URL is separated from switches by a bare '--' so it can't be read as a flag
    assert "--" in seen["args"]
    assert seen["args"].index("--") < seen["args"].index("https://www.linkedin.com/jobs/view/123")
