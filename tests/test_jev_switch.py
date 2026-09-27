"""JS-1, JS-2 (cycle 19): one switch answers whether Jev runs for scoring, the
résumé tailor, the auto-apply difficulty check and the auto-apply run.

Hermetic: the config is passed in or written to the conftest's sandboxed
config.json, the environment is a dict or monkeypatched, and the SDK probe is
patched so the answer never depends on what this machine has installed.
"""
import json
import logging

import pytest

import jev
import jev_switch
import settings

ON = {"jev_enabled": True, "jev_scoring": True, "jev_tailor": True, "jev_difficulty": True}
KEY = {"TYPESAFE_API_KEY": "not-a-real-key"}


@pytest.fixture
def sdk(monkeypatch):
    """The SDK probe, patched: `sdk(False)` makes typesafe_sdk read as missing."""
    state = {"found": True}
    monkeypatch.setattr(jev_switch, "sdk_installed", lambda: state["found"])

    def set_found(found: bool) -> None:
        state["found"] = found
    return set_found


def _on(area, config=None, env=None):
    return jev_switch.jev_on(area, config=ON if config is None else config,
                             env=KEY if env is None else env)


def _why(area, config=None, env=None):
    return jev_switch.jev_why_off(area, config=ON if config is None else config,
                                  env=KEY if env is None else env)


def test_the_four_areas_are_the_ones_the_plan_names():
    assert jev_switch.AREAS == ("scoring", "tailor", "difficulty", "apply")


def test_every_area_is_on_with_the_switches_on_a_key_and_the_sdk(sdk):
    for area in jev_switch.AREAS:
        assert _on(area) is True, area
        assert _why(area) == "", area


def test_the_defaults_are_on_when_the_config_holds_no_switch(sdk):
    for area in jev_switch.AREAS:
        assert jev_switch.jev_on(area, config={}, env=KEY) is True, area


@pytest.mark.parametrize("mode", ["typesafe", "fake", "replay"])
def test_the_master_switch_off_turns_every_area_off_in_every_mode(sdk, mode):
    cfg = dict(ON, jev_enabled=False, auto_apply_jev_mode=mode)
    for env in (KEY, dict(KEY, AUTO_APPLY_JEV_MODE=mode)):
        for area in jev_switch.AREAS:
            assert _on(area, cfg, env) is False, (area, env)
            assert _why(area, cfg, env) == "Jev is switched off in Settings", area


@pytest.mark.parametrize("area,key,words", [
    ("scoring", "jev_scoring", "scoring"),
    ("tailor", "jev_tailor", "tailoring"),
    ("difficulty", "jev_difficulty", "the difficulty check"),
])
def test_an_area_switch_turns_off_only_its_own_area(sdk, area, key, words):
    cfg = dict(ON, **{key: False})
    assert _on(area, cfg) is False
    assert _why(area, cfg) == f"Jev is switched off for {words} in Settings"
    for other in jev_switch.AREAS:
        if other != area:
            assert _on(other, cfg) is True, other


def test_auto_apply_ignores_the_area_switches(sdk):
    """Auto-apply runs on Jev only, so it has no switch of its own: the three
    area switches never reach it, and only the master turns it off."""
    cfg = dict(ON, jev_scoring=False, jev_tailor=False, jev_difficulty=False)
    assert _on("apply", cfg) is True
    assert _why("apply", cfg) == ""


def test_a_missing_or_blank_key_turns_every_area_off(sdk):
    for env in ({}, {"TYPESAFE_API_KEY": ""}, {"TYPESAFE_API_KEY": "   "}):
        for area in jev_switch.AREAS:
            assert _on(area, env=env) is False, (area, env)
            assert _why(area, env=env) == "no TypeSafe API key", area


def test_a_missing_sdk_turns_every_area_off(sdk):
    sdk(False)
    for area in jev_switch.AREAS:
        assert _on(area) is False, area
        assert _why(area) == "typesafe-sdk is not installed", area


def test_the_first_failing_check_names_the_reason(sdk):
    """Master, then the area switch, then the key, then the SDK."""
    sdk(False)
    assert _why("tailor", dict(ON, jev_enabled=False, jev_tailor=False), {}) == \
        "Jev is switched off in Settings"
    assert _why("tailor", dict(ON, jev_tailor=False), {}) == \
        "Jev is switched off for tailoring in Settings"
    assert _why("tailor", ON, {}) == "no TypeSafe API key"
    assert _why("tailor", ON, KEY) == "typesafe-sdk is not installed"


@pytest.mark.parametrize("mode", ["fake", "replay", " Fake "])
def test_fake_and_replay_skip_the_key_and_sdk_for_apply_and_difficulty(sdk, mode):
    """The test judges need neither, so the suite runs keyless; scoring and the
    tailor always use the live judge and still need both."""
    sdk(False)
    for env, cfg in (({"AUTO_APPLY_JEV_MODE": mode}, ON),
                     ({}, dict(ON, auto_apply_jev_mode=mode))):
        assert _on("apply", cfg, env) is True
        assert _on("difficulty", cfg, env) is True
        assert _why("scoring", cfg, env) == "no TypeSafe API key"
        assert _why("tailor", cfg, env) == "no TypeSafe API key"


def test_the_difficulty_switch_still_applies_in_a_test_mode(sdk):
    cfg = dict(ON, jev_difficulty=False, auto_apply_jev_mode="fake")
    assert _on("difficulty", cfg, {}) is False
    assert _on("apply", cfg, {}) is True


def test_the_live_mode_checks_the_key_for_apply_and_difficulty(sdk):
    cfg = dict(ON, auto_apply_jev_mode="typesafe")
    assert _why("apply", cfg, {}) == "no TypeSafe API key"
    assert _why("difficulty", cfg, {}) == "no TypeSafe API key"


def test_an_unknown_area_is_a_bug_and_raises(sdk):
    for fn in (jev_switch.jev_on, jev_switch.jev_why_off, jev_switch.client):
        with pytest.raises(ValueError):
            fn("tailoring")


# --- read at call time ------------------------------------------------------------

def test_config_path_is_the_config_target_settings_writes_to():
    assert jev_switch.config_path() == settings.target_path("config")
    assert settings.TARGET_FILES["config"].name == "config.json"
    assert settings.TARGET_FILES["config"].parent.name == "local"


def test_a_switch_written_to_the_config_file_applies_on_the_next_call(sdk, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    path = jev_switch.config_path()
    path.write_text(json.dumps({"jev_enabled": False}), encoding="utf-8")
    assert jev_switch.jev_on("scoring") is False
    path.write_text(json.dumps({"jev_enabled": True, "jev_scoring": False}), encoding="utf-8")
    assert jev_switch.jev_on("scoring") is False
    assert jev_switch.jev_on("tailor") is True
    path.write_text(json.dumps({"jev_enabled": True}), encoding="utf-8")
    assert jev_switch.jev_on("scoring") is True


def test_the_key_is_read_from_the_environment_at_call_time(sdk, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert jev_switch.jev_on("tailor", config=ON) is False
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    assert jev_switch.jev_on("tailor", config=ON) is True


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", "", "null"])
def test_an_unreadable_config_file_reads_as_the_defaults(sdk, text):
    jev_switch.config_path().write_text(text, encoding="utf-8")
    assert jev_switch.jev_on("scoring", env=KEY) is True
    assert jev_switch.jev_why_off("apply", env=KEY) == ""


def test_a_missing_config_file_reads_as_the_defaults(sdk):
    assert not jev_switch.config_path().exists()
    assert jev_switch.jev_on("difficulty", env=KEY) is True


def test_a_config_file_saved_with_a_bom_is_still_read(sdk):
    jev_switch.config_path().write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"jev_enabled": False}).encode("utf-8"))
    assert jev_switch.jev_on("scoring", env=KEY) is False


def test_a_config_path_that_cannot_be_resolved_reads_as_the_defaults(sdk, monkeypatch):
    def broken():
        raise OSError("no config")
    monkeypatch.setattr(jev_switch, "config_path", broken)
    assert jev_switch.jev_on("tailor", env=KEY) is True


def test_the_key_value_never_reaches_a_reason_or_the_log(sdk, caplog):
    secret = "sk-do-not-print-me"
    sdk(False)
    caplog.set_level(logging.DEBUG)
    env = {"TYPESAFE_API_KEY": secret}
    texts = [_why(a, env=env) for a in jev_switch.AREAS]
    texts.append(jev_switch.apply_blocked(config=ON, env=env))
    assert jev_switch.client("tailor") is None
    assert all(secret not in t for t in texts)
    assert secret not in caplog.text


def test_the_sdk_probe_asks_find_spec_for_typesafe_sdk(monkeypatch):
    asked = []
    monkeypatch.setattr(jev_switch.importlib.util, "find_spec",
                        lambda name: asked.append(name) or None)
    assert jev_switch.sdk_installed() is False
    monkeypatch.setattr(jev_switch.importlib.util, "find_spec",
                        lambda name: asked.append(name) or object())
    assert jev_switch.sdk_installed() is True
    assert asked == ["typesafe_sdk", "typesafe_sdk"]


# --- the auto-apply mode ------------------------------------------------------------

def test_apply_mode_reads_the_environment_then_the_setting_then_typesafe():
    assert jev_switch.apply_mode(config={}, env={}) == "typesafe"
    assert jev_switch.apply_mode(config={"auto_apply_jev_mode": "fake"}, env={}) == "fake"
    assert jev_switch.apply_mode(config={"auto_apply_jev_mode": "fake"},
                                 env={"AUTO_APPLY_JEV_MODE": "replay"}) == "replay"
    assert jev_switch.apply_mode(config={"auto_apply_jev_mode": " TypeSafe "},
                                 env={"AUTO_APPLY_JEV_MODE": "  "}) == "typesafe"


def test_apply_mode_reads_the_setting_from_the_config_file(monkeypatch):
    monkeypatch.delenv("AUTO_APPLY_JEV_MODE", raising=False)
    jev_switch.config_path().write_text(json.dumps({"auto_apply_jev_mode": "fake"}),
                                        encoding="utf-8")
    assert jev_switch.apply_mode() == "fake"


# --- the blocked-Start sentence (JS-5) ------------------------------------------------

def test_apply_blocked_is_empty_while_jev_is_on(sdk):
    assert jev_switch.apply_blocked(config=ON, env=KEY) == ""


def test_apply_blocked_names_the_fix_for_each_reason(sdk):
    lead = "Auto-apply runs on Jev. "
    assert jev_switch.apply_blocked(config=dict(ON, jev_enabled=False), env=KEY) == \
        lead + "Turn Jev on in Settings > Jev."
    assert jev_switch.apply_blocked(config=ON, env={}) == \
        lead + "Add the TypeSafe API key in Settings > Jev."
    sdk(False)
    assert jev_switch.apply_blocked(config=ON, env=KEY) == \
        lead + "Install typesafe-sdk (pip install -r requirements.txt)."


def test_apply_blocked_counts_a_key_saved_in_settings(sdk):
    """The drain runs in a child process that loads `.env` itself, so a key
    saved in Settings reaches it even before the dashboard restarts."""
    assert jev_switch.apply_blocked(config=ON, env={}, saved_key=True) == ""


def test_apply_blocked_uses_the_mode_the_run_will_use(sdk):
    """`apply_run.py` resolves its own mode (the --jev flag, then the setting)
    and passes it in, so an exported test mode cannot wave a live run through."""
    env = {"AUTO_APPLY_JEV_MODE": "fake"}
    assert jev_switch.apply_blocked(config=ON, env=env) == ""
    assert jev_switch.apply_blocked(config=ON, env=env, mode="typesafe") == \
        "Auto-apply runs on Jev. Add the TypeSafe API key in Settings > Jev."
    assert jev_switch.apply_blocked(config=ON, env={}, mode="fake") == ""


def test_apply_blocked_keeps_the_master_switch_in_a_test_mode(sdk):
    assert jev_switch.apply_blocked(config=dict(ON, jev_enabled=False), env={},
                                    mode="fake") == \
        "Auto-apply runs on Jev. Turn Jev on in Settings > Jev."


# --- the judge -------------------------------------------------------------------------

class _StubTypeSafe:
    built = 0

    def __init__(self, *a, **kw):
        type(self).built += 1

    def judge(self, state, questions):
        return {}


def test_client_is_none_while_the_area_is_off_and_builds_nothing(sdk, monkeypatch):
    def never(*a, **kw):
        raise AssertionError("a judge was built while Jev is off")
    monkeypatch.setattr(jev, "TypeSafeJev", never)
    monkeypatch.setattr(jev, "get", never)
    jev_switch.config_path().write_text(json.dumps({"jev_enabled": False}), encoding="utf-8")
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    for area in jev_switch.AREAS:
        assert jev_switch.client(area) is None, area


def test_client_for_scoring_and_tailor_is_a_guarded_typesafe_judge(sdk, monkeypatch):
    monkeypatch.setattr(jev, "TypeSafeJev", _StubTypeSafe)
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    monkeypatch.setenv("AUTO_APPLY_JEV_MODE", "fake")     # never reaches these two
    for area in ("scoring", "tailor"):
        judge = jev_switch.client(area)
        assert isinstance(judge, jev.Guarded), area
        assert isinstance(judge.inner, _StubTypeSafe), area


@pytest.mark.parametrize("area", ["apply", "difficulty"])
def test_client_for_apply_and_difficulty_is_the_auto_apply_modes_judge(sdk, monkeypatch, area):
    sdk(False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("AUTO_APPLY_JEV_MODE", "fake")
    judge = jev_switch.client(area)
    assert isinstance(judge, jev.Guarded)
    assert isinstance(judge.inner, jev.FakeJev)


def test_client_reads_the_auto_apply_judge_setting_when_the_env_is_unset(sdk, monkeypatch):
    monkeypatch.delenv("AUTO_APPLY_JEV_MODE", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    jev_switch.config_path().write_text(json.dumps({"auto_apply_jev_mode": "fake"}),
                                        encoding="utf-8")
    judge = jev_switch.client("difficulty")
    assert isinstance(judge, jev.Guarded) and isinstance(judge.inner, jev.FakeJev)


def test_client_is_none_when_the_judge_cannot_be_built(sdk, monkeypatch, caplog):
    def refuse(*a, **kw):
        raise jev.JevUnavailable("detail that stays out of the log")
    monkeypatch.setattr(jev, "TypeSafeJev", refuse)
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    caplog.set_level(logging.WARNING)
    assert jev_switch.client("tailor") is None
    assert "JevUnavailable" in caplog.text
    assert "detail that stays out of the log" not in caplog.text
