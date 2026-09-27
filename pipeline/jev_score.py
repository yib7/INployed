"""Jev scoring for score_jobs.py (cycle 19: SC-1 to SC-3, JS-4).

Jev (TypeSafe System One) answers typed questions about a state and cannot
write text, so the split is: Jev decides, code composes. `score_jobs.py` calls
`stage1` and `stage2` once per job per stage when `use_jev` says so; each asks
one request about the state `{candidate, resume, job}` and composes the
columns the LLM path writes today. `None` from either means "use the LLM path"
for that job.

The scorer runs as its own process and on the VM, so it decides Jev use by
itself (JS-4): `SCORE_USE_JEV` in the environment, else the dashboard's
`local/config.json` beside the repo when that file exists (`jev_enabled` and
`jev_scoring`, read the way `local/jev_switch.py` reads them), else off. The VM
has neither, so it stays on Gemini by construction.

Importing this module imports neither the TypeSafe SDK nor `local/jev.py`:
`_jev_module()` loads `jev` on first use through a sys.path hop to `local/`.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
# The repo keeps this file in pipeline/, with the dashboard's local/ beside it.
# The VM copies the pipeline scripts flat into ~/, where neither exists.
LOCAL_DIR: Path | None = _HERE.parent / "local" if _HERE.name == "pipeline" else None
CONFIG_PATH: Path | None = LOCAL_DIR / "config.json" if LOCAL_DIR is not None else None

ENV_SWITCH = "SCORE_USE_JEV"
MASTER_KEY = "jev_enabled"
AREA_KEY = "jev_scoring"
KEY_ENV = "TYPESAFE_API_KEY"
SDK_MODULE = "typesafe_sdk"
_TRUE = ("1", "true", "yes", "on")

# The reasons `jev_switch.jev_why_off("scoring")` gives, word for word, so the
# dashboard and the scorer name a switched-off Jev the same way.
REASON_SWITCH = "Jev is switched off in Settings"
REASON_AREA = "Jev is switched off for scoring in Settings"
REASON_KEY = "no TypeSafe API key"
REASON_SDK = "typesafe-sdk is not installed"
REASON_MODULE = "local/jev.py could not be imported"
REASON_NO_CONFIG = f"no {ENV_SWITCH} and no dashboard config beside the scorer"


# --- JS-4: does this run use Jev? -------------------------------------------------

def _read_config(path: Path) -> dict[str, Any]:
    """The dashboard config as a dict; {} when it is unreadable or holds no JSON
    object, so every switch reads its default (`jsonutil.read_json_dict`)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _sdk_installed() -> bool:
    """Is `typesafe_sdk` importable? A find_spec probe, so nothing is imported."""
    try:
        return importlib.util.find_spec(SDK_MODULE) is not None
    except (ImportError, ValueError):
        return False


def _jev_module() -> Any:
    """`local/jev.py`, imported on first use. local/ goes on the end of sys.path
    so nothing in it can shadow a pipeline module (llm.py's hop, reversed)."""
    if "jev" in sys.modules:
        return sys.modules["jev"]
    if LOCAL_DIR is not None and str(LOCAL_DIR) not in sys.path:
        sys.path.append(str(LOCAL_DIR))
    import jev
    return jev


def use_jev(env: Any = None) -> tuple[bool, str]:
    """(True, where the switch came from) when this run scores with Jev, else
    (False, the reason). Order (JS-4): `SCORE_USE_JEV`, else the dashboard
    config when it exists (`jev_enabled`, `jev_scoring`, both default on),
    else off. When the switch is on but the key, the SDK or `local/jev.py` is
    missing, one warning is printed and the answer is off: the run falls back
    to the LLM path and never exits over it. The key is checked for presence
    only and never printed."""
    env = os.environ if env is None else env
    raw = str(env.get(ENV_SWITCH) or "").strip().lower()
    if raw:
        if raw not in _TRUE:
            return False, f"{ENV_SWITCH}={raw} turns it off"
        source = f"{ENV_SWITCH}={raw}"
    elif CONFIG_PATH is not None and CONFIG_PATH.is_file():
        cfg = _read_config(CONFIG_PATH)
        if cfg.get(MASTER_KEY, True) is False:
            return False, REASON_SWITCH
        if cfg.get(AREA_KEY, True) is False:
            return False, REASON_AREA
        source = "Settings"
    else:
        return False, REASON_NO_CONFIG
    if not str(env.get(KEY_ENV) or "").strip():
        reason = REASON_KEY
    elif not _sdk_installed():
        reason = REASON_SDK
    else:
        try:
            _jev_module()
        except Exception:       # noqa: BLE001  (any import failure reads as Jev off)
            reason = REASON_MODULE
        else:
            return True, source
    print(f"WARNING: Jev scoring is on ({source}) but {reason}; scoring on the LLM path.")
    return False, reason


def make_judge() -> Any:
    """The run's judge: `jev.Guarded(jev.TypeSafeJev())`, whose breaker opens
    after an outage so the rest of the run takes the LLM path (SC-5). None,
    with one warning naming the error's class only, when it cannot be built."""
    try:
        jev = _jev_module()
        return jev.Guarded(jev.TypeSafeJev())
    except Exception as e:      # noqa: BLE001  (None keeps the run on the LLM path)
        print(f"WARNING: the Jev judge could not be built ({type(e).__name__}); "
              "scoring on the LLM path.")
        return None


def usage() -> dict[str, Any]:
    """`jev.usage()`: the live requests this process made and what they cost.
    Zeros when `local/jev.py` cannot be loaded."""
    try:
        return dict(_jev_module().usage())
    except Exception:           # noqa: BLE001  (no jev module means no Jev spend)
        return {"requests": 0, "input_tokens": 0, "usd": 0.0}
