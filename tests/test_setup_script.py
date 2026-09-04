"""End-to-end tests for scripts/setup.ps1 — the documented Setup command.

CI already proves the script *runs* on a clean runner and that the three files
appear (the `readme-setup` job's "the config files were written" step). That is
not the same as proving the app can read what it wrote, and the gap hid a real
bug: `Set-Content -Encoding UTF8` writes a BOM on PowerShell 5.1, json.loads
rejects a leading BOM, and jsonutil.read_json_dict swallows the rejection into
{} — so every value setup.ps1 put in local/config.json was silently discarded on
a fresh install and the dashboard ran on hardcoded defaults. Nothing errored,
nothing logged, and Test-Path was perfectly happy.

So these tests assert on the CONTENT the setup command produces, loaded through
the same readers the dashboard uses. Windows-only, because the script is.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import envfile  # noqa: E402
import jsonutil  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell") is None,
    reason="scripts/setup.ps1 is a Windows PowerShell script")


@pytest.fixture
def staged_repo(tmp_path):
    """A throwaway tree holding just what setup.ps1 reads, so the run is
    hermetic: -Root points here and the script writes nowhere else."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "local").mkdir()
    (tmp_path / "resume_tailor_files").mkdir()
    shutil.copy2(REPO / ".env.example", tmp_path / ".env.example")
    shutil.copy2(REPO / "scripts" / "setup.ps1", tmp_path / "scripts" / "setup.ps1")
    shutil.copy2(REPO / "resume_tailor_files" / "master_experience.example.yaml",
                 tmp_path / "resume_tailor_files" / "master_experience.example.yaml")
    return tmp_path


def run_setup(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Invoke setup.ps1 the way the README's Step 3 tells the reader to."""
    cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-NoProfile",
           "-File", str(root / "scripts" / "setup.ps1"), "-Root", str(root), *args]
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=90)


def test_setup_writes_a_config_the_dashboard_can_actually_read(staged_repo):
    """The regression this file exists for: the written config must survive
    read_json_dict, values intact, rather than decoding to {}."""
    res = run_setup(staged_repo, "-MinScore", "6", "-FollowupDays", "9")
    assert res.returncode == 0, res.stdout + res.stderr

    cfg_path = staged_repo / "local" / "config.json"
    assert cfg_path.exists()
    assert cfg_path.read_bytes()[:3] != b"\xef\xbb\xbf", "config.json got a BOM"

    cfg = jsonutil.read_json_dict(cfg_path)
    assert cfg, "read_json_dict decoded the fresh config.json as empty"
    assert cfg["min_score"] == 6
    assert cfg["followup_days"] == 9
    assert "gdrive_root" in cfg and "mtime_stable_seconds" in cfg
    # ...and through the settings loader, which has its own reader.
    assert json.loads(cfg_path.read_text(encoding="utf-8-sig"))["min_score"] == 6


def test_setup_seeds_env_without_mangling_it(staged_repo):
    """A fresh .env is a byte-for-byte copy of .env.example.

    `Get-Content` decodes a BOM-less UTF-8 file as the ANSI codepage, which
    turned every box-drawing rule in the template's comments into Windows-1252
    mojibake in the user's own .env. Comments only, so nothing broke — it just
    made the first file a new user opens look corrupt.
    """
    res = run_setup(staged_repo)
    assert res.returncode == 0, res.stdout + res.stderr

    env_path = staged_repo / ".env"
    assert env_path.read_bytes() == (staged_repo / ".env.example").read_bytes()
    assert env_path.read_bytes()[:3] != b"\xef\xbb\xbf", ".env got a BOM"


def test_setup_is_idempotent_and_heals_a_legacy_bom(staged_repo):
    """Re-running must keep existing values, and must clean up after the
    version of this script that wrote BOMs — otherwise the documented fix
    ("re-run it any time") does not actually fix an affected install."""
    (staged_repo / "local" / "config.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps(
            {"gdrive_root": "D:/Drive", "min_score": 7,
             "followup_days": 3, "mtime_stable_seconds": 30}).encode("utf-8"))
    (staged_repo / ".env").write_bytes(
        b"\xef\xbb\xbf" + b"BRIGHT_DATA_API_TOKEN=keep-me\n")

    res = run_setup(staged_repo)
    assert res.returncode == 0, res.stdout + res.stderr

    cfg = jsonutil.read_json_dict(staged_repo / "local" / "config.json")
    assert cfg["gdrive_root"] == "D:/Drive"
    assert cfg["min_score"] == 7 and cfg["followup_days"] == 3
    assert envfile.read(staged_repo / ".env")["BRIGHT_DATA_API_TOKEN"] == "keep-me"
    assert (staged_repo / ".env").read_bytes()[:3] != b"\xef\xbb\xbf"


def test_setup_leaves_an_existing_master_experience_alone(staged_repo):
    """The one file that must never be clobbered without -Force is the user's
    own experience data."""
    master = staged_repo / "resume_tailor_files" / "master_experience.yaml"
    master.write_text("mine: true\n", encoding="utf-8")
    res = run_setup(staged_repo)
    assert res.returncode == 0, res.stdout + res.stderr
    assert master.read_text(encoding="utf-8") == "mine: true\n"
