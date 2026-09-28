"""One switch for every Jev use: scoring, the resume tailor, the auto-apply
difficulty check and the auto-apply run itself (JS-1, JS-2).

`jev_on(area)` is True when every check below passes, read at call time, so
flipping a switch in Settings needs no restart (a new key still does, as
before: the dashboard's environment is its startup snapshot of `.env`):

1. the master switch `jev_enabled` in the dashboard's config.json (default on);
2. for scoring, tailor and difficulty, that area's own switch (`jev_scoring`,
   `jev_tailor`, `jev_difficulty`, default on). Auto-apply runs on Jev only, so
   it has no switch of its own and only the master turns it off;
3. a TypeSafe API key in the environment (`TYPESAFE_API_KEY`), checked for
   presence only: the value never reaches a message or a log;
4. the `typesafe_sdk` package importable.

For apply and difficulty the key and SDK checks follow the auto-apply judge
mode: the fake and replay judges need neither, so the suite runs keyless. The
master switch turns those modes off too. `apply_mode` is the one reader of
that mode, in the drain's order (its --jev flag, else the Auto-apply judge
setting, else typesafe), so the panel's Start button, Test my answers,
`apply_run.py probe --judge` and the drain all ask about the judge the drain
builds. The environment's AUTO_APPLY_JEV_MODE is read by none of them.

`jev_why_off(area)` names the first check that fails, in one line (the
scorer's `pipeline/jev_score.py` gives the same words for scoring; the
dashboard shows the gate sentences below). `apply_blocked()` builds the Jev gate's sentence,
the one `apply_run.py` drain, one and probe --judge print and Test my answers
shows; the two probes name a mode `jev.get` does not build first
(`unknown_mode`). `start_blocked()` is the Auto-apply panel's Start gate: it gives the
drain's refusal of the judge mode first (`mode_refusal`: a test judge is
`FIXTURE_ONLY`, cycle 16, and a mode `jev.get` does not build is
`UNKNOWN_MODE`), then `apply_blocked()`, the order the drain checks them in,
so Start predicts the drain it launches. `difficulty_blocked()` is the
difficulty check's Jev gate, the sentence `apply_assess.py` prints and the
panel's Check difficulty shows after the same mode refusal, and
`switched_off(area)` reads the switches alone, so the panel hides that button
while a switch turns it off. `key_saved()` is the saved-key probe the
dashboard passes the gates. `client(area)` returns a `jev.Guarded` judge, or None when the area is
off or the judge cannot be built. Its one production caller is the tailor
(`jev_assist`), which keeps its LLM path on None; that judge retries briefly
(`jev.QUICK_RETRY_DELAYS_S`), so an outage reaches that path in seconds. The
scorer builds its own judge (`jev_score.make_judge`, since the VM has no
`jev_switch`), and the drain and the difficulty check build theirs with
`jev.get`.

A switch reads by `settings.switch_on`, the rule the Settings checkbox
shows it by (SP1 follow-up 3): a missing key is on (the default), a bool is
itself, and any other value is on only as "true", "yes", "on" or "1", any
case, spaces stripped. A hand-edited null, 0, "", "false" or "off" reads off
here and in Settings alike, so a stray value spends no TypeSafe credit.
"""
from __future__ import annotations

import importlib.util
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jev
from jsonutil import read_json_dict

log = logging.getLogger("jev_switch")

AREAS = ("scoring", "tailor", "difficulty", "apply")

MASTER_KEY = "jev_enabled"
AREA_KEYS = {"scoring": "jev_scoring", "tailor": "jev_tailor",
             "difficulty": "jev_difficulty"}
_AREA_WORDS = {"scoring": "scoring", "tailor": "tailoring",
               "difficulty": "the difficulty check"}
# The areas whose judge is the auto-apply mode's (`jev.get`): the fake and
# replay judges skip the key and SDK checks there.
_MODE_AREAS = ("apply", "difficulty")
MODE_KEY = "auto_apply_jev_mode"      # the Auto-apply judge setting
DEFAULT_MODE = "typesafe"
TEST_MODES = ("fake", "replay")
SDK_MODULE = "typesafe_sdk"

REASON_SWITCH = "Jev is switched off in Settings"
REASON_KEY = "no TypeSafe API key"
REASON_SDK = "typesafe-sdk is not installed"

BLOCKED_LEAD = "Auto-apply runs on Jev. "
_FIXES = {
    "switch": "Turn Jev on in Settings > Jev.",
    "key": "Add the TypeSafe API key in Settings > Jev.",
    "sdk": "Install typesafe-sdk (pip install -r requirements.txt).",
}
# `apply_run.py drain` and `one` refuse a test judge before their Jev gate
# (cycle 16), and a mode `jev.get` does not build (a hand-edited Auto-apply
# judge setting, SP1 follow-up 2) with it. The panel's Start gate
# (`start_blocked`), Check setup and the doctor (`setup_check.auto_apply_warnings`)
# give the same sentences, through `mode_refusal`. Test my answers and
# `probe --judge` give UNKNOWN_MODE (`unknown_mode`) and run the test judges,
# since they are probes (SP1 follow-up 3). The Auto-apply judge row is
# advanced, so UNKNOWN_MODE names the disclosure that shows it, in the Settings
# tab's own words (SP1 follow-up 3).
FIXTURE_ONLY = "Fake and replay judges are fixture-only; use typesafe for a production queue."
UNKNOWN_MODE = ("Unknown Auto-apply judge {mode!r}; tick \"Show advanced settings\" and pick "
                "typesafe in Settings > Auto-apply.")
# What `apply_assess.py` prints while the difficulty check's own switch is off
# (`difficulty_blocked`); the Auto-apply panel hides Check difficulty then.
DIFFICULTY_OFF = ("The difficulty check is switched off. Turn on Jev difficulty check in "
                  "Settings > Jev (tick Show advanced settings).")


def config_path() -> Path:
    """The file Settings writes `jev_enabled` and the area switches to: the
    dashboard's config.json (`settings.TARGET_FILES["config"]`), resolved the
    way `settings.load()` resolves it."""
    import settings
    return settings.target_path("config")


def _config() -> dict[str, Any]:
    """The config file as a dict; {} when it is missing, unreadable, not a JSON
    object, or its path cannot be resolved, so every switch reads its default."""
    try:
        return read_json_dict(config_path())
    except Exception:       # noqa: BLE001  (a broken config reads as the defaults)
        return {}


def apply_mode(flag: str | None = None, *,
               config: Mapping[str, Any] | None = None) -> str:
    """The auto-apply judge mode a run uses, in the drain's order: `flag` (the
    --jev flag, or a mode already resolved here), else the Auto-apply judge
    setting in `config` (default: the config file, read on every call), else
    "typesafe"; stripped and lower-cased. A blank reads as the next source.

    The one reader of the mode: the panel's Start gate, Test my answers,
    the difficulty check, the doctor, Check setup, and
    `apply_run.py` drain, one and probe all resolve it here, so the gate asks
    about the judge the drain it launches builds. AUTO_APPLY_JEV_MODE is not
    read: `jev.get` falls back to it only when handed no mode, and every
    caller here hands it one."""
    raw = str(flag or "").strip()
    if not raw:
        cfg = _config() if config is None else config
        raw = str(cfg.get(MODE_KEY) or "").strip()
    return (raw or DEFAULT_MODE).lower()


def key_saved() -> bool:
    """Is a TypeSafe API key saved in Settings (the `.env` file)? Presence only,
    through `settings.secret_status`, so the value never leaves that module.
    False when the settings files cannot be read. The Auto-apply panel's Start
    gate (`start_blocked`) and Test my answers (`apply_blocked`) pass it as
    `saved_key`."""
    try:
        import settings
        return bool(settings.secret_status().get(jev.KEY_ENV))
    except Exception:       # noqa: BLE001  (an unreadable settings file counts as no saved key)
        return False


def sdk_installed() -> bool:
    """Is `typesafe_sdk` importable? A find_spec probe, so nothing is imported."""
    try:
        return importlib.util.find_spec(SDK_MODULE) is not None
    except (ImportError, ValueError):
        return False


def master_on(*, config: Mapping[str, Any] | None = None) -> bool:
    """Is the master switch `jev_enabled` on? `config` defaults to the config
    file, read on every call. The setup checks read it alone: they list every
    missing piece at once."""
    cfg = _config() if config is None else config
    return _switch(cfg, MASTER_KEY)


def _switch(cfg: Mapping[str, Any], key: str) -> bool:
    """Is switch `key` on in `cfg`? A missing key is on (the default); a
    stored value reads by `settings.switch_on`, the Settings checkbox's rule."""
    import settings
    return settings.switch_on(cfg.get(key, True))


def switched_off(area: str, *, config: Mapping[str, Any] | None = None) -> bool:
    """Is Jev switched off for `area` in Settings: the master switch or the
    area's own switch? The key and the SDK play no part. `config` defaults to
    the config file, read on every call. The dashboard hands its scorer
    SCORE_USE_JEV=0 on this alone and leaves the key and SDK to the scorer.
    An unknown area raises ValueError, as `jev_on` does."""
    _check_area(area)
    cfg = _config() if config is None else config
    return _switch_reason(area, cfg) != ""


def _check_area(area: str) -> None:
    """Raise ValueError for an unknown area: a typo there would otherwise read
    as a silent answer forever."""
    if area not in AREAS:
        raise ValueError(f"unknown Jev area {area!r}; expected one of {', '.join(AREAS)}")


def _switch_reason(area: str, cfg: Mapping[str, Any]) -> str:
    """The reason a Settings switch turns Jev off for `area`, or ""."""
    if not master_on(config=cfg):
        return REASON_SWITCH
    area_key = AREA_KEYS.get(area)
    if area_key is not None and not _switch(cfg, area_key):
        return f"Jev is switched off for {_AREA_WORDS[area]} in Settings"
    return ""


def _check(area: str, config: Mapping[str, Any] | None, env: Mapping[str, str] | None,
           *, mode: str | None = None, saved_key: bool = False) -> tuple[str, str]:
    """(kind, reason) of the first failing check, or ("", "") when Jev is on.
    `kind` is "switch", "key" or "sdk". An unknown area raises ValueError: a
    typo there would otherwise read as a silent answer forever."""
    _check_area(area)
    cfg = _config() if config is None else config
    env = os.environ if env is None else env
    reason = _switch_reason(area, cfg)
    if reason:
        return "switch", reason
    if area in _MODE_AREAS and apply_mode(mode, config=cfg) in TEST_MODES:
        return "", ""
    if not (saved_key or str(env.get(jev.KEY_ENV) or "").strip()):
        return "key", REASON_KEY
    if not sdk_installed():
        return "sdk", REASON_SDK
    return "", ""


def jev_on(area: str, *, config: Mapping[str, Any] | None = None,
           env: Mapping[str, str] | None = None) -> bool:
    """Does Jev run for `area` right now? `config` and `env` default to the
    config file and `os.environ`, both read on every call."""
    return _check(area, config, env)[0] == ""


def jev_why_off(area: str, *, config: Mapping[str, Any] | None = None,
                env: Mapping[str, str] | None = None) -> str:
    """The one-line reason Jev is off for `area`, or "" when it is on."""
    return _check(area, config, env)[1]


def blocked_sentence(kind: str) -> str:
    """"Auto-apply runs on Jev. " plus the fix for a `_check` kind."""
    return BLOCKED_LEAD + _FIXES[kind]


def apply_blocked(*, config: Mapping[str, Any] | None = None,
                  env: Mapping[str, str] | None = None, mode: str | None = None,
                  saved_key: bool = False) -> str:
    """Why an auto-apply run cannot start on Jev (switched off, no key, no
    SDK), in the sentence `apply_run.py` drain, one and probe --judge print and
    Test my answers shows; "" when it can. The panel's Start gate
    (`start_blocked`) gives it too, after the drain's refusal of the judge mode.

    `mode` is the drain's --jev flag or a mode `apply_mode` resolved; None
    reads the setting (`apply_mode`), as a drain with no flag does. `saved_key`
    counts a key saved in Settings (`key_saved`): the drain runs in a child
    process that loads `.env` itself, so such a key reaches it before the
    dashboard restarts."""
    kind, _reason = _check("apply", config, env, mode=mode, saved_key=saved_key)
    return blocked_sentence(kind) if kind else ""


def difficulty_blocked(*, config: Mapping[str, Any] | None = None,
                       env: Mapping[str, str] | None = None, mode: str | None = None,
                       saved_key: bool = False) -> str:
    """Why the difficulty check cannot run on Jev, in the sentence
    `apply_assess.py` prints; "" when it can. Shaped like `apply_blocked`, for
    the "difficulty" area: the master switch off gives the drain's sentence
    (JS-5), the check's own switch off gives `DIFFICULTY_OFF`, and a missing
    key or SDK gives the drain's sentence for it. The key and SDK checks follow
    the judge mode as they do for the drain (`mode` as in `apply_blocked`), and
    `saved_key` counts a key saved in Settings (`key_saved`), since
    `apply_assess.py` loads `.env` itself."""
    cfg = _config() if config is None else config
    kind, _reason = _check("difficulty", cfg, env, mode=mode, saved_key=saved_key)
    if kind == "switch" and master_on(config=cfg):
        return DIFFICULTY_OFF
    return blocked_sentence(kind) if kind else ""


def fixture_only(mode: str) -> str:
    """The refusal `apply_run.py drain` and `one` print for a test judge before
    their Jev gate (cycle 16), `FIXTURE_ONLY`; "" for the live judge. `mode` is
    a mode `apply_mode` resolved."""
    return FIXTURE_ONLY if mode in TEST_MODES else ""


def unknown_mode(mode: str) -> str:
    """`UNKNOWN_MODE` naming `mode` when `jev.get` does not build it (it raises
    ValueError for any mode outside `jev.MODES`); "" for a mode it builds.
    `mode` is a mode `apply_mode` resolved. Test my answers and `probe
    --judge` ask it before `apply_blocked`; the drain asks it through
    `mode_refusal`."""
    return "" if mode in jev.MODES else UNKNOWN_MODE.format(mode=mode)


def mode_refusal(mode: str) -> str:
    """Why `apply_run.py drain` and `one` refuse the judge `mode` before their
    Jev gate: a test judge (`fixture_only`) or a mode `jev.get` does not build
    (`unknown_mode`); "" for the live judge. The panel's Start gate
    (`start_blocked`), Check setup and the doctor give the same sentence."""
    return fixture_only(mode) or unknown_mode(mode)


def start_blocked(*, config: Mapping[str, Any] | None = None,
                  env: Mapping[str, str] | None = None, mode: str | None = None,
                  saved_key: bool = False) -> str:
    """Why the drain the Auto-apply panel's Start launches would stop before it
    claims a job, in the sentence it prints; "" when it would run. It asks in
    the drain's order: its refusal of the judge mode (`mode_refusal`), then the
    Jev gate (`apply_blocked`, with the same arguments). Test my answers and
    `probe --judge` ask `unknown_mode`, then `apply_blocked`: they are probes,
    and the test judges run there."""
    cfg = _config() if config is None else config
    resolved = apply_mode(mode, config=cfg)
    return mode_refusal(resolved) or apply_blocked(config=cfg, env=env, mode=resolved,
                                                   saved_key=saved_key)


def client(area: str) -> Any:
    """The judge for `area`, or None when Jev is off for it or the judge cannot
    be built. The tailor (`jev_assist`, the one production caller) and
    scoring get a guarded TypeSafe judge that retries briefly
    (`jev.QUICK_RETRY_DELAYS_S`): the tailor falls back to its LLM path, so
    an outage costs it seconds. Apply and the difficulty check get the
    auto-apply mode's judge (`jev.get`) with the run's retries
    (`jev.RETRY_DELAYS_S`); the drain and `apply_assess` build theirs the
    same way without this function. A build failure
    is logged by its type only, since its message can carry request detail.
    An unknown area raises ValueError (`_check`)."""
    cfg = _config()
    if _check(area, cfg, None)[0]:
        return None
    try:
        if area in _MODE_AREAS:
            return jev.Guarded(jev.get(apply_mode(config=cfg)))
        return jev.Guarded(jev.TypeSafeJev(), delays=jev.QUICK_RETRY_DELAYS_S)
    except Exception as e:      # noqa: BLE001  (None keeps the caller on its own path)
        log.warning("jev_switch: the %s judge could not be built (%s)", area,
                    type(e).__name__)
        return None
