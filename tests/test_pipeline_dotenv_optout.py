"""INPLOYED_NO_DOTENV=1 has to actually stop the pipeline scripts loading `.env`.

Both scripts bill on a successful run (Bright Data, then LLM credits), and both
call load_dotenv() at import scope. That combination means clearing a credential
in the environment does not disarm them: the file puts the real key back before
main() checks anything, so a "what happens with no token" probe places a live
paid request. The opt-out is the only safe way to exercise those paths, so it
gets a test.

The first group needs the developer's real `.env` and skips without one, which is
every CI run. The second group builds its own `.env` in a temp DATA_ROOT and so
runs everywhere -- added 2026-08-27, when the first group turned out to be the
only coverage this guard had. Nothing here prints a value; the assertions are on
presence only.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / ".env"

_SNIPPET = """
import importlib.util, os, sys
sys.argv = [{mod!r}]
spec = importlib.util.spec_from_file_location("probe", {path!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print("PRESENT" if os.environ.get({key!r}) else "ABSENT")
"""


def _first_key() -> str:
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name = line.split("=", 1)[0].strip()
            if name and name.isidentifier():
                return name
    return ""


@pytest.mark.skipif(not ENV_FILE.exists(), reason="no local .env to load")
@pytest.mark.parametrize("script", ["scraper.py", "score_jobs.py"])
@pytest.mark.parametrize(
    "flag,expected", [("1", "ABSENT"), ("", "PRESENT")], ids=["opt-out", "default"]
)
def test_dotenv_optout_controls_whether_env_file_loads(script, flag, expected):
    key = _first_key()
    if not key:
        pytest.skip(".env has no assignments to check")
    path = REPO / "pipeline" / script
    env = dict(os.environ)
    env.pop(key, None)
    # Importing scraper.py runs its sibling imports (run_labels) from pipeline/.
    env["PYTHONPATH"] = str(REPO / "pipeline")
    if flag:
        env["INPLOYED_NO_DOTENV"] = flag
    else:
        env.pop("INPLOYED_NO_DOTENV", None)
    code = _SNIPPET.format(mod=script, path=str(path), key=key)
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().endswith(expected)


# --- the same guard, without needing the author's .env -----------------------
#
# Everything above skips whenever there is no local `.env`, which is EVERY CI run
# and every fresh clone. That left the one mechanism making it safe to exercise a
# billed script's unhappy path with no coverage anywhere except one laptop. These
# build their own `.env` in a temp DATA_ROOT, so they run everywhere.

_SNIPPET_TMP = """
import importlib.util, os, sys
sys.argv = [{mod!r}]
spec = importlib.util.spec_from_file_location("probe", {path!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print("PRESENT" if os.environ.get("INPLOYED_PROBE_VALUE") else "ABSENT")
"""


def _probe_with_temp_env(tmp_path, script, flag):
    """Copy one pipeline script into a temp DATA_ROOT beside a synthetic `.env`,
    import it in a subprocess, and report whether the file's var arrived.

    A COPY, not the real path: the scripts derive DATA_ROOT from their own
    __file__, so this is the only way to point them at a `.env` that is not the
    developer's. Importing is safe -- both guard every billed call behind
    `main()`, which `__name__ == "probe"` never reaches.
    """
    root = tmp_path / "root"
    (root / "pipeline").mkdir(parents=True)
    shutil.copy(REPO / "pipeline" / script, root / "pipeline" / script)
    for sibling in ("run_labels.py", "keypool.py"):
        src = REPO / "pipeline" / sibling
        if src.exists():
            shutil.copy(src, root / "pipeline" / sibling)
    (root / ".env").write_text("INPLOYED_PROBE_VALUE=seen\n", encoding="utf-8")

    env = dict(os.environ)
    env.pop("INPLOYED_PROBE_VALUE", None)
    env["PYTHONPATH"] = str(root / "pipeline")
    if flag is None:
        env.pop("INPLOYED_NO_DOTENV", None)
    else:
        env["INPLOYED_NO_DOTENV"] = flag
    code = _SNIPPET_TMP.format(mod=script, path=str(root / "pipeline" / script))
    out = subprocess.run([sys.executable, "-c", code], env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("script", ["scraper.py", "score_jobs.py"])
def test_optout_stops_the_env_file_loading_without_a_real_env(tmp_path, script):
    assert _probe_with_temp_env(tmp_path, script, "1") == "ABSENT"


@pytest.mark.parametrize("script", ["scraper.py", "score_jobs.py"])
def test_without_the_optout_the_env_file_still_loads(tmp_path, script):
    assert _probe_with_temp_env(tmp_path, script, None) == "PRESENT"


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "True", "yes", "on", " 1 "])
def test_the_optout_accepts_the_spellings_a_human_types(tmp_path, flag):
    """This is typed by hand at a shell. An INPLOYED_NO_DOTENV=TRUE that silently
    re-arms a billed script is the worst possible way to be strict -- and "TRUE"
    and "yes" both did exactly that before 2026-08-27."""
    assert _probe_with_temp_env(tmp_path, "scraper.py", flag) == "ABSENT"


@pytest.mark.parametrize("flag", ["0", "", "no", "off", "false"])
def test_a_falsey_optout_does_not_disarm_anything(tmp_path, flag):
    """The widening must not go so far that a deliberate "off" reads as "on"."""
    assert _probe_with_temp_env(tmp_path, "scraper.py", flag) == "PRESENT"
