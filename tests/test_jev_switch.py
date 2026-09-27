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
    for area in jev_switch.AREAS:
        assert _on(area, cfg) is False, area
        assert _why(area, cfg) == "Jev is switched off in Settings", area


def test_master_on_is_the_master_switch_alone():
    """For the setup checks, which list every missing piece at once (JS-5): off
    only for False, so a stray value reads as the Settings checkbox shows it."""
    assert jev_switch.master_on(config={}) is True
    assert jev_switch.master_on(config={"jev_enabled": "no"}) is True
    assert jev_switch.master_on(config={"jev_enabled": True, "jev_scoring": False}) is True
    assert jev_switch.master_on(config={"jev_enabled": False}) is False
    jev_switch.config_path().write_text(json.dumps({"jev_enabled": False}), encoding="utf-8")
    assert jev_switch.master_on() is False


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
    cfg = dict(ON, auto_apply_jev_mode=mode)
    assert _on("apply", cfg, {}) is True
    assert _on("difficulty", cfg, {}) is True
    assert _why("scoring", cfg, {}) == "no TypeSafe API key"
    assert _why("tailor", cfg, {}) == "no TypeSafe API key"


@pytest.mark.parametrize("mode", ["fake", "replay"])
def test_an_exported_test_mode_opens_nothing(sdk, monkeypatch, mode):
    """SP1 review B: the drain never reads AUTO_APPLY_JEV_MODE, so the gate does
    not either. A shell's fake judge leaves a keyless live setup off, and
    client() builds nothing for it."""
    monkeypatch.setenv("AUTO_APPLY_JEV_MODE", mode)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    for area in ("apply", "difficulty"):
        assert jev_switch.jev_why_off(area, config=ON) == "no TypeSafe API key", area
        assert jev_switch.client(area) is None, area
    assert jev_switch.apply_blocked(config=ON) == \
        "Auto-apply runs on Jev. Add the TypeSafe API key in Settings > Jev."


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


def test_the_key_value_never_reaches_a_reason_the_log_or_the_console(sdk, monkeypatch,
                                                                     caplog, capsys):
    """The key sits in the environment and every judge build fails with the key
    in its message, the way a client library can echo a rejected key back. The
    reasons, the log and the console carry none of it; the log names the
    failure by its type."""
    secret = "sk-do-not-print-me"
    monkeypatch.setenv("TYPESAFE_API_KEY", secret)

    def echo_the_key(*a, **kw):
        raise jev.JevUnavailable(f"the key {secret} was rejected")
    monkeypatch.setattr(jev, "TypeSafeJev", echo_the_key)
    caplog.set_level(logging.DEBUG)
    for area in jev_switch.AREAS:
        assert jev_switch.client(area) is None, area
    texts = [jev_switch.jev_why_off(a, config=ON) for a in jev_switch.AREAS]
    sdk(False)
    texts += [jev_switch.jev_why_off(a, config=ON) for a in jev_switch.AREAS]
    texts.append(jev_switch.apply_blocked(config=ON))
    out, err = capsys.readouterr()
    assert caplog.text.count("JevUnavailable") == len(jev_switch.AREAS)
    for text in (*texts, caplog.text, out, err):
        assert secret not in text


@pytest.mark.parametrize("error", [ImportError, ValueError])
def test_a_find_spec_that_raises_reads_as_no_sdk(monkeypatch, error):
    """find_spec raises ImportError when a parent package fails to import and
    ValueError when a loaded module has no __spec__. Either reads as no SDK,
    so the gate names the fix and the caller keeps running."""
    def fail(name):
        raise error("the probe failed")
    monkeypatch.setattr(jev_switch.importlib.util, "find_spec", fail)
    assert jev_switch.sdk_installed() is False
    assert jev_switch.apply_blocked(config=ON, env=KEY) == \
        "Auto-apply runs on Jev. Install typesafe-sdk (pip install -r requirements.txt)."


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

def test_apply_mode_reads_the_flag_then_the_setting_then_typesafe(monkeypatch):
    """SP1 review B: the drain's own order, its --jev flag, else the Auto-apply
    judge setting, else typesafe, stripped and lower-cased. A blank setting reads
    as typesafe, and the environment's AUTO_APPLY_JEV_MODE is never read."""
    monkeypatch.setenv("AUTO_APPLY_JEV_MODE", "replay")
    fake = {"auto_apply_jev_mode": "fake"}
    assert jev_switch.apply_mode(config={}) == "typesafe"
    assert jev_switch.apply_mode(config=fake) == "fake"
    assert jev_switch.apply_mode("typesafe", config=fake) == "typesafe"
    assert jev_switch.apply_mode(" Fake ", config={}) == "fake"
    assert jev_switch.apply_mode("  ", config={"auto_apply_jev_mode": " TypeSafe "}) == \
        "typesafe"
    for blank in ("", "   ", None):
        assert jev_switch.apply_mode(config={"auto_apply_jev_mode": blank}) == "typesafe"


def test_apply_mode_reads_the_setting_from_the_config_file(monkeypatch):
    monkeypatch.setenv("AUTO_APPLY_JEV_MODE", "replay")
    jev_switch.config_path().write_text(json.dumps({"auto_apply_jev_mode": "fake"}),
                                        encoding="utf-8")
    assert jev_switch.apply_mode() == "fake"


def test_the_default_mode_is_the_settings_default():
    """A config with no judge setting reads as the Settings row's default here and
    in `apply_run.load_settings` alike."""
    schema = {f.key: f.default for f in settings.SETTINGS_SCHEMA}
    assert jev_switch.DEFAULT_MODE == schema[jev_switch.MODE_KEY] == "typesafe"


# --- the saved key ------------------------------------------------------------------

def test_key_saved_is_the_presence_of_the_key_in_the_settings_env_file():
    """Presence only, through `settings.secret_status`: the panel's Start gate
    and Test my answers share this one probe."""
    assert jev_switch.key_saved() is False
    settings.target_path("env").write_text("TYPESAFE_API_KEY=not-a-real-key\n",
                                           encoding="utf-8")
    assert jev_switch.key_saved() is True
    settings.target_path("env").write_text("TYPESAFE_API_KEY=\n", encoding="utf-8")
    assert jev_switch.key_saved() is False


def test_key_saved_reads_a_broken_settings_backend_as_no_key(monkeypatch):
    def broken():
        raise OSError("unreadable env file")
    monkeypatch.setattr(settings, "secret_status", broken)
    assert jev_switch.key_saved() is False


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
    """`apply_run.py` passes the mode its --jev flag names; with no flag the
    gate reads the setting, as the drain does."""
    fake = dict(ON, auto_apply_jev_mode="fake")
    assert jev_switch.apply_blocked(config=fake, env={}) == ""
    assert jev_switch.apply_blocked(config=fake, env={}, mode="typesafe") == \
        "Auto-apply runs on Jev. Add the TypeSafe API key in Settings > Jev."
    assert jev_switch.apply_blocked(config=ON, env={}, mode="fake") == ""


def test_apply_blocked_keeps_the_master_switch_in_a_test_mode(sdk):
    assert jev_switch.apply_blocked(config=dict(ON, jev_enabled=False), env={},
                                    mode="fake") == \
        "Auto-apply runs on Jev. Turn Jev on in Settings > Jev."


# --- the Start gate: the drain's refusal of a test judge comes first (SP1 fix round 2) ---

def test_the_fixture_only_sentence_is_the_drains_refusal_of_a_test_judge():
    """The words `apply_run.py drain` and `one` print for the fake and replay
    judges (cycle 16), which the Auto-apply panel's Start shows too."""
    assert jev_switch.FIXTURE_ONLY == (
        "Fake and replay judges are fixture-only; use typesafe for a production queue.")
    for mode in jev_switch.TEST_MODES:
        assert jev_switch.fixture_only(mode) == jev_switch.FIXTURE_ONLY, mode
    assert jev_switch.fixture_only("typesafe") == ""


@pytest.mark.parametrize("mode", ["fake", "replay"])
def test_start_blocked_refuses_a_test_judge_first_as_the_drain_does(sdk, mode):
    """The drain refuses a test judge before its Jev gate, so the Start gate
    names that refusal with the switch on or off, no key and no SDK. The
    probes ask `apply_blocked` (Test my answers, `probe --judge`), which keeps
    the key and SDK skip."""
    sdk(False)
    for switch in (True, False):
        cfg = dict(ON, jev_enabled=switch, auto_apply_jev_mode=mode)
        assert jev_switch.start_blocked(config=cfg, env={}) == jev_switch.FIXTURE_ONLY, switch
        assert jev_switch.start_blocked(config=dict(ON, jev_enabled=switch), env={},
                                        mode=mode) == jev_switch.FIXTURE_ONLY, switch
    assert jev_switch.apply_blocked(config=dict(ON, auto_apply_jev_mode=mode), env={}) == ""


def test_start_blocked_is_the_jev_gate_for_the_live_judge(sdk):
    lead = "Auto-apply runs on Jev. "
    assert jev_switch.start_blocked(config=ON, env=KEY) == ""
    assert jev_switch.start_blocked(config=ON, env={}) == \
        lead + "Add the TypeSafe API key in Settings > Jev."
    assert jev_switch.start_blocked(config=ON, env={}, saved_key=True) == ""
    assert jev_switch.start_blocked(config=dict(ON, jev_enabled=False), env=KEY) == \
        lead + "Turn Jev on in Settings > Jev."
    sdk(False)
    assert jev_switch.start_blocked(config=ON, env=KEY) == \
        lead + "Install typesafe-sdk (pip install -r requirements.txt)."


_UNKNOWN_JUDGE = ("Unknown Auto-apply judge 'typesaf'; tick \"Show advanced settings\" "
                  "and pick typesafe in Settings > Auto-apply.")


def test_a_mode_jev_get_does_not_build_has_one_sentence():
    """SP1 follow-up 2 (Minor 2): a hand-edited `"auto_apply_jev_mode":
    "typesaf"` reaches `jev.get`, which raises. `unknown_mode` names every mode
    outside `jev.MODES` in the words the drain prints, and `mode_refusal`
    gives it beside the fixture-only refusal."""
    assert jev_switch.unknown_mode("typesaf") == _UNKNOWN_JUDGE
    with pytest.raises(ValueError):
        jev.get("typesaf")
    for mode in jev.MODES:
        assert jev_switch.unknown_mode(mode) == "", mode
    assert jev_switch.mode_refusal("typesaf") == _UNKNOWN_JUDGE
    for mode in jev_switch.TEST_MODES:
        assert jev_switch.mode_refusal(mode) == jev_switch.FIXTURE_ONLY, mode
    assert jev_switch.mode_refusal("typesafe") == ""


def test_start_blocked_refuses_an_unknown_mode_before_the_jev_gate(sdk):
    """The drain refuses the mode before its Jev gate, so Start names it with
    the switch on or off, with or without a key and the SDK. Asked the gate's
    way, a keyless setup would send the user to add a key for a judge that
    cannot run, and a keyed one would leave Start on."""
    for found in (True, False):
        sdk(found)
        for switch in (True, False):
            cfg = dict(ON, jev_enabled=switch, auto_apply_jev_mode=" TypeSaf ")
            for env in ({}, KEY):
                assert jev_switch.start_blocked(config=cfg, env=env) == _UNKNOWN_JUDGE, \
                    (found, switch, env)
            assert jev_switch.start_blocked(config=dict(ON, jev_enabled=switch), env=KEY,
                                            mode="typesaf") == _UNKNOWN_JUDGE, (found, switch)


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
    jev_switch.config_path().write_text(json.dumps({"auto_apply_jev_mode": "fake"}),
                                        encoding="utf-8")      # never reaches these two
    for area in ("scoring", "tailor"):
        judge = jev_switch.client(area)
        assert isinstance(judge, jev.Guarded), area
        assert isinstance(judge.inner, _StubTypeSafe), area


@pytest.mark.parametrize("area", ["apply", "difficulty"])
def test_client_for_apply_and_difficulty_is_the_auto_apply_judge_settings_judge(
        sdk, monkeypatch, area):
    sdk(False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    jev_switch.config_path().write_text(json.dumps({"auto_apply_jev_mode": "fake"}),
                                        encoding="utf-8")
    judge = jev_switch.client(area)
    assert isinstance(judge, jev.Guarded)
    assert isinstance(judge.inner, jev.FakeJev)


def test_client_builds_the_settings_judge_whatever_the_shell_exports(sdk, monkeypatch):
    """With a key, the live setting builds the live judge though the shell
    exports AUTO_APPLY_JEV_MODE=fake: the drain would build that one too."""
    monkeypatch.setattr(jev, "TypeSafeJev", _StubTypeSafe)
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    monkeypatch.setenv("AUTO_APPLY_JEV_MODE", "fake")
    judge = jev_switch.client("apply")
    assert isinstance(judge, jev.Guarded) and isinstance(judge.inner, _StubTypeSafe)


def test_client_is_none_when_the_judge_cannot_be_built(sdk, monkeypatch, caplog):
    def refuse(*a, **kw):
        raise jev.JevUnavailable("detail that stays out of the log")
    monkeypatch.setattr(jev, "TypeSafeJev", refuse)
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    caplog.set_level(logging.WARNING)
    assert jev_switch.client("tailor") is None
    assert "JevUnavailable" in caplog.text
    assert "detail that stays out of the log" not in caplog.text


class _Unreachable:
    """A judge whose every request drops its connection: a transient failure,
    which `jev.Guarded` tries again after each of its waits."""

    def __init__(self, *a, **kw):
        pass

    def judge(self, state, questions):
        raise ConnectionError("the service did not answer")


@pytest.mark.parametrize("area, quick", [
    ("scoring", True), ("tailor", True), ("apply", False), ("difficulty", False),
])
def test_the_judges_with_an_llm_fallback_retry_on_the_quick_waits(sdk, monkeypatch,
                                                                    area, quick):
    """Fix round 2: scoring and the tailor fall back to their LLM path, so
    their judge's breaker opens after the quick waits (`QUICK_RETRY_DELAYS_S`,
    a few seconds). Auto-apply has no fallback and keeps the run's waits
    (`RETRY_DELAYS_S`, about a minute). The sleep is a recorder, so nothing
    waits for real."""
    monkeypatch.setattr(jev, "TypeSafeJev", _Unreachable)
    monkeypatch.setattr(jev, "get", lambda mode="": _Unreachable())
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    judge = jev_switch.client(area)
    slept = []
    judge.sleep = slept.append
    request = ({"page": "a form"}, {"q": {"type": "boolean", "instructions": "Is it?"}})
    with pytest.raises(jev.JudgeOutage):
        judge.judge(*request)
    waits = list(jev.QUICK_RETRY_DELAYS_S if quick else jev.RETRY_DELAYS_S)
    assert slept == waits
    assert judge.down == "ConnectionError"          # the breaker is open
    with pytest.raises(jev.JudgeOutage):
        judge.judge(*request)                       # at once, with no wait
    assert slept == waits
    assert sum(jev.QUICK_RETRY_DELAYS_S) < 5 < sum(jev.RETRY_DELAYS_S)
