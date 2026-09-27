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

`jev_why_off(area)` names the first check that fails, as the one-line reason
the UI and the logs show. `apply_blocked()` builds the Jev gate's sentence,
the one `apply_run.py` drain, one and probe --judge print and Test my answers
shows. `start_blocked()` is the Auto-apply panel's Start gate: it gives the
drain's refusal of a test judge first (`FIXTURE_ONLY`, cycle 16), then
`apply_blocked()`, the order the drain checks them in, so Start predicts the
drain it launches. `key_saved()` is the saved-key probe the dashboard passes
both. `client(area)` returns a `jev.Guarded` judge, or None when the area is
off or the judge cannot be built; a caller outside auto-apply keeps its LLM
path on None, and its judge retries briefly (`jev.QUICK_RETRY_DELAYS_S`), so
an outage reaches that path in seconds.

The switches are read with `is not False`, the spelling
`resume_tailor/config.py` uses for every default-on toggle, so a stray
non-bool value reads the same way the Settings checkbox shows it (on).
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
# (cycle 16). The panel's Start gate (`start_blocked`), Check setup and the
# doctor (`setup_check.auto_apply_warnings`) give the same sentence.
FIXTURE_ONLY = "Fake and replay judges are fixture-only; use typesafe for a production queue."


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
    `client("apply")` / `client("difficulty")`, the doctor, Check setup, and
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
    return cfg.get(MASTER_KEY, True) is not False


def _check(area: str, config: Mapping[str, Any] | None, env: Mapping[str, str] | None,
           *, mode: str | None = None, saved_key: bool = False) -> tuple[str, str]:
    """(kind, reason) of the first failing check, or ("", "") when Jev is on.
    `kind` is "switch", "key" or "sdk". An unknown area raises ValueError: a
    typo there would otherwise read as a silent answer forever."""
    if area not in AREAS:
        raise ValueError(f"unknown Jev area {area!r}; expected one of {', '.join(AREAS)}")
    cfg = _config() if config is None else config
    env = os.environ if env is None else env
    if not master_on(config=cfg):
        return "switch", REASON_SWITCH
    area_key = AREA_KEYS.get(area)
    if area_key is not None and cfg.get(area_key, True) is False:
        return "switch", f"Jev is switched off for {_AREA_WORDS[area]} in Settings"
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
    (`start_blocked`) gives it too, after the drain's refusal of a test judge.

    `mode` is the drain's --jev flag or a mode `apply_mode` resolved; None
    reads the setting (`apply_mode`), as a drain with no flag does. `saved_key`
    counts a key saved in Settings (`key_saved`): the drain runs in a child
    process that loads `.env` itself, so such a key reaches it before the
    dashboard restarts."""
    kind, _reason = _check("apply", config, env, mode=mode, saved_key=saved_key)
    return blocked_sentence(kind) if kind else ""


def fixture_only(mode: str) -> str:
    """The refusal `apply_run.py drain` and `one` print for a test judge before
    their Jev gate (cycle 16), `FIXTURE_ONLY`; "" for the live judge. `mode` is
    a mode `apply_mode` resolved."""
    return FIXTURE_ONLY if mode in TEST_MODES else ""


def start_blocked(*, config: Mapping[str, Any] | None = None,
                  env: Mapping[str, str] | None = None, mode: str | None = None,
                  saved_key: bool = False) -> str:
    """Why the drain the Auto-apply panel's Start launches would stop before it
    claims a job, in the sentence it prints; "" when it would run. It asks in
    the drain's order: a test judge's refusal (`fixture_only`), then the Jev
    gate (`apply_blocked`, with the same arguments). Test my answers and
    `probe --judge` ask `apply_blocked` alone: they are probes, and the test
    judges run there."""
    cfg = _config() if config is None else config
    resolved = apply_mode(mode, config=cfg)
    return fixture_only(resolved) or apply_blocked(config=cfg, env=env, mode=resolved,
                                                   saved_key=saved_key)


def client(area: str) -> Any:
    """The judge for `area`, or None when Jev is off for it or the judge cannot
    be built. Scoring and the tailor get a guarded TypeSafe judge that retries
    briefly (`jev.QUICK_RETRY_DELAYS_S`): their callers fall back to their LLM
    path, so an outage costs them seconds. Apply and the difficulty check get
    the auto-apply mode's judge (`jev.get`) with the run's retries
    (`jev.RETRY_DELAYS_S`), since auto-apply has no fallback. A build failure
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
