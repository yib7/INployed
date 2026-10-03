"""INPLOYED_NO_DOTENV=1 has to actually stop the pipeline scripts loading `.env`.

Both scripts bill on a successful run (Bright Data, then LLM credits), and both
call load_dotenv() at import scope. That combination means clearing a credential
in the environment does not disarm them: the file puts the real key back before
main() checks anything, so a "what happens with no token" probe places a live
paid request. The opt-out is the only safe way to exercise those paths, so it
gets a test.

Each test builds its own `.env` in a temp DATA_ROOT, so the guard is covered on
every machine and the developer's real `.env` is never read. Nothing here prints a
value; the assertions are on presence only.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

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
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=120)
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
