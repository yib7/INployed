"""INPLOYED_NO_DOTENV=1 has to actually stop the pipeline scripts loading `.env`.

Both scripts bill on a successful run (Bright Data, then LLM credits), and both
call load_dotenv() at import scope. That combination means clearing a credential
in the environment does not disarm them: the file puts the real key back before
main() checks anything, so a "what happens with no token" probe places a live
paid request. The opt-out is the only safe way to exercise those paths, so it
gets a test.

Skips when there is no local `.env`, which is the CI case. Nothing here prints a
value; the assertions are on presence only.
"""
import os
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
