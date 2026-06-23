"""gcloud-based VM sync core (local/vm_sync.py).

Pure argv builders + change detection. No real gcloud ever runs (the runner is
mocked); no secret is read — only non-secret connection identifiers.
"""
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import vm_sync  # noqa: E402


def _target():
    return vm_sync.VMTarget(gcloud="gcloud", instance="scraper-vm", zone="us-east1-c",
                            project="proj-123", user="yib", remote_dir="~")


def test_from_mapping_and_defaults():
    t = vm_sync.VMTarget.from_mapping({"VM_INSTANCE": "scraper-vm", "VM_ZONE": "us-east1-c",
                                       "VM_USER": "yib"})
    assert t.instance == "scraper-vm" and t.zone == "us-east1-c" and t.user == "yib"
    assert t.gcloud == "gcloud" and t.remote_dir == "~"  # defaults


def test_configured_requires_instance_zone_user():
    assert _target().configured() is True
    assert vm_sync.VMTarget(instance="", zone="", user="").configured() is False


def test_build_ssh_cmd():
    cmd = _target().build_ssh_cmd("echo hi")
    assert cmd[:3] == ["gcloud", "compute", "ssh"]
    assert "yib@scraper-vm" in cmd
    assert "--zone=us-east1-c" in cmd
    assert "--project=proj-123" in cmd
    assert "--command=echo hi" in cmd


def test_build_scp_cmd_dest_path():
    cmd = _target().build_scp_cmd("/local/search_config.json", "search_config.json")
    assert cmd[:3] == ["gcloud", "compute", "scp"]
    assert "/local/search_config.json" in cmd
    assert "yib@scraper-vm:~/search_config.json" in cmd


def test_set_pause_and_resume_and_crontab_are_ssh():
    t = _target()
    assert "pause_until" in t.set_pause_cmd("2026-07-01 09:00")[-1]
    assert "rm -f ~/pause_until" in t.resume_cmd()[-1]
    assert "crontab -" in t.install_crontab_cmd("0 10 * * * ~/run_scraper.sh")[-1]


def test_changed_vm_files_flags_scoring_not_local_config():
    before = {"stage2_threshold": 4, "keywords": ['"a"'], "min_score": 4}
    after = {"stage2_threshold": 5, "keywords": ['"a"'], "min_score": 3}
    changed = vm_sync.changed_vm_files(before, after)
    assert changed == {"scoring_config.json"}  # min_score is local-only; keywords unchanged


def test_changed_vm_files_flags_search_on_keyword_change():
    before = {"keywords": ['"a"']}
    after = {"keywords": ['"a"', '"b"']}
    assert vm_sync.changed_vm_files(before, after) == {"search_config.json"}


def test_run_cmd_invokes_subprocess(monkeypatch):
    seen = {}

    def _run(cmd, **k):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(vm_sync.subprocess, "run", _run)
    res = vm_sync.run_cmd(["gcloud", "compute", "ssh"])
    assert seen["cmd"] == ["gcloud", "compute", "ssh"]
    assert res.returncode == 0


def test_changed_vm_files_ignores_multichoice_reorder():
    before = {"remote_types": ["Hybrid", "On-site"]}
    after = {"remote_types": ["On-site", "Hybrid"]}
    assert vm_sync.changed_vm_files(before, after) == set()


def test_changed_vm_files_ignores_keyword_whitespace_only():
    before = {"keywords": ['"a"', '"b"']}
    after = {"keywords": [' "a" ', '"b"  ']}
    assert vm_sync.changed_vm_files(before, after) == set()


def test_changed_vm_files_flags_real_remote_type_change():
    before = {"remote_types": ["Hybrid"]}
    after = {"remote_types": ["Hybrid", "Remote"]}
    assert vm_sync.changed_vm_files(before, after) == {"search_config.json"}
