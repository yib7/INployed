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
mode (`apply_mode`): the fake and replay judges need neither, so the suite
runs keyless. The master switch turns those modes off too.

`jev_why_off(area)` names the first check that fails, as the one-line reason
the UI and the logs show. `apply_blocked()` builds the sentence the Auto-apply
panel's Start button and `apply_run.py drain` both show, so the two cannot
drift. `client(area)` returns a `jev.Guarded` judge, or None when the area is
off or the judge cannot be built; a caller outside auto-apply keeps its LLM
path on None.

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


def apply_mode(*, config: Mapping[str, Any] | None = None,
               env: Mapping[str, str] | None = None) -> str:
    """The auto-apply judge mode: `AUTO_APPLY_JEV_MODE` in the environment, else
    the Auto-apply judge setting, else "typesafe", stripped and lower-cased the
    way `jev.get` reads it. The dashboard's Test my answers button and
    `client("apply")` / `client("difficulty")` read the mode here."""
    env = os.environ if env is None else env
    raw = str(env.get(jev.MODE_ENV) or "").strip()
    if not raw:
        cfg = _config() if config is None else config
        raw = str(cfg.get(MODE_KEY) or "").strip()
    return (raw or DEFAULT_MODE).lower()


def sdk_installed() -> bool:
    """Is `typesafe_sdk` importable? A find_spec probe, so nothing is imported."""
    try:
        return importlib.util.find_spec(SDK_MODULE) is not None
    except (ImportError, ValueError):
        return False


def _check(area: str, config: Mapping[str, Any] | None, env: Mapping[str, str] | None,
           *, mode: str | None = None, saved_key: bool = False) -> tuple[str, str]:
    """(kind, reason) of the first failing check, or ("", "") when Jev is on.
    `kind` is "switch", "key" or "sdk". An unknown area raises ValueError: a
    typo there would otherwise read as a silent answer forever."""
    if area not in AREAS:
        raise ValueError(f"unknown Jev area {area!r}; expected one of {', '.join(AREAS)}")
    cfg = _config() if config is None else config
    env = os.environ if env is None else env
    if cfg.get(MASTER_KEY, True) is False:
        return "switch", REASON_SWITCH
    area_key = AREA_KEYS.get(area)
    if area_key is not None and cfg.get(area_key, True) is False:
        return "switch", f"Jev is switched off for {_AREA_WORDS[area]} in Settings"
    if area in _MODE_AREAS:
        run_mode = (mode or apply_mode(config=cfg, env=env)).strip().lower()
        if run_mode in TEST_MODES:
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
    """Why an auto-apply run cannot start, as the sentence the panel's Start
    button and `apply_run.py drain` both show; "" when it can.

    `mode` is the judge mode the run will use (the drain passes its own, from
    the --jev flag or the setting); None reads `apply_mode()`. `saved_key`
    counts a key saved in Settings: the drain runs in a child process that
    loads `.env` itself, so such a key reaches it before the dashboard
    restarts."""
    kind, _reason = _check("apply", config, env, mode=mode, saved_key=saved_key)
    return blocked_sentence(kind) if kind else ""


def client(area: str) -> Any:
    """The judge for `area`, or None when Jev is off for it or the judge cannot
    be built. Scoring and the tailor get a guarded TypeSafe judge; apply and
    the difficulty check get the auto-apply mode's judge (`jev.get`), guarded
    the same way. A build failure is logged by its type only, since its message
    can carry request detail."""
    if area not in AREAS:
        raise ValueError(f"unknown Jev area {area!r}; expected one of {', '.join(AREAS)}")
    cfg = _config()
    if _check(area, cfg, None)[0]:
        return None
    try:
        if area in _MODE_AREAS:
            inner = jev.get(apply_mode(config=cfg))
        else:
            inner = jev.TypeSafeJev()
        return jev.Guarded(inner)
    except Exception as e:      # noqa: BLE001  (None keeps the caller on its own path)
        log.warning("jev_switch: the %s judge could not be built (%s)", area,
                    type(e).__name__)
        return None
