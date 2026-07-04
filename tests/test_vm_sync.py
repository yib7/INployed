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
    # remote_dir "~" must NOT be sent literally: Windows pscp can't open a
    # "~/..." path, so a home-dir push uses a bare relative dest.
    cmd = _target().build_scp_cmd("/local/search_config.json", "search_config.json")
    assert cmd[:3] == ["gcloud", "compute", "scp"]
    assert "/local/search_config.json" in cmd
    assert "yib@scraper-vm:search_config.json" in cmd
    assert "yib@scraper-vm:~/search_config.json" not in cmd


def test_build_scp_cmd_custom_dir_keeps_prefix():
    t = vm_sync.VMTarget(instance="scraper-vm", zone="z", user="yib", remote_dir="/opt/scraper/")
    cmd = t.build_scp_cmd("/local/x.json", "x.json")
    assert "yib@scraper-vm:/opt/scraper/x.json" in cmd


def test_set_pause_and_resume_and_crontab_are_ssh():
    t = _target()
    assert "pause_until" in t.set_pause_cmd("2026-07-01 09:00")[-1]
    assert "rm -f ~/pause_until" in t.resume_cmd()[-1]
    assert "crontab -" in t.install_crontab_cmd("0 10 * * * ~/run_scraper.sh")[-1]


def test_push_exclude_ids_cmd_targets_remote_file():
    # The seen-id file lands at the VM home (relative dest for "~", same as configs).
    cmd = _target().push_exclude_ids_cmd("/local/external_exclude_ids.json")
    assert cmd[:3] == ["gcloud", "compute", "scp"]
    assert "/local/external_exclude_ids.json" in cmd
    assert "yib@scraper-vm:external_exclude_ids.json" in cmd
    assert "yib@scraper-vm:~/external_exclude_ids.json" not in cmd


def test_sync_exclude_ids_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(vm_sync, "run_cmd", lambda cmd: (_ for _ in ()).throw(
        AssertionError("run_cmd must not be called when the VM is unconfigured")))
    unconfigured = vm_sync.VMTarget(instance="", zone="", user="")
    assert vm_sync.sync_exclude_ids_to_vm(unconfigured, "/local/x.json") is None


def test_sync_exclude_ids_runs_scp_when_configured(monkeypatch):
    seen = {}

    def _run(cmd):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vm_sync, "run_cmd", _run)
    res = vm_sync.sync_exclude_ids_to_vm(_target(), "/local/external_exclude_ids.json")
    assert res.returncode == 0
    assert seen["cmd"] == _target().push_exclude_ids_cmd("/local/external_exclude_ids.json")


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
    # run_cmd launches via launch_argv (which on Windows bypasses the gcloud.cmd
    # batch wrapper); on a box without gcloud it's the same argv.
    assert seen["cmd"] == vm_sync.launch_argv(["gcloud", "compute", "ssh"])
    assert res.returncode == 0


def _fake_sdk(tmp_path, with_gpy=True, with_bundled=True):
    sdk = tmp_path / "google-cloud-sdk"
    (sdk / "bin").mkdir(parents=True)
    (sdk / "lib").mkdir(parents=True)
    cmd = sdk / "bin" / "gcloud.cmd"
    cmd.write_text("@echo off\n", encoding="utf-8")
    gpy = sdk / "lib" / "gcloud.py"
    if with_gpy:
        gpy.write_text("# gcloud entrypoint\n", encoding="utf-8")
    bundled = sdk / "platform" / "bundledpython" / "python.exe"
    if with_bundled:
        bundled.parent.mkdir(parents=True)
        bundled.write_text("", encoding="utf-8")
    return cmd, gpy, bundled


def test_bypass_argv_runs_gcloud_py_with_bundled_python(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOUDSDK_PYTHON", raising=False)
    cmd, gpy, bundled = _fake_sdk(tmp_path)
    argv = vm_sync._bypass_argv(str(cmd), ["compute", "ssh", "--command=a && b"])
    assert argv == [str(bundled), "-S", str(gpy), "compute", "ssh", "--command=a && b"]


def test_bypass_argv_honours_cloudsdk_python_env(tmp_path, monkeypatch):
    cmd, gpy, _ = _fake_sdk(tmp_path, with_bundled=False)
    monkeypatch.setenv("CLOUDSDK_PYTHON", "C:/py/python.exe")
    argv = vm_sync._bypass_argv(str(cmd), ["version"])
    assert argv[0] == "C:/py/python.exe"
    assert argv[1:] == ["-S", str(gpy), "version"]


def test_bypass_argv_none_when_no_entrypoint(tmp_path):
    cmd, _, _ = _fake_sdk(tmp_path, with_gpy=False)
    assert vm_sync._bypass_argv(str(cmd), ["version"]) is None


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


def test_push_outbox_file_cmd_targets_incoming():
    cmd = _target().push_outbox_file_cmd("/local/outbox/local_rows_x.csv.gz")
    assert cmd[:3] == ["gcloud", "compute", "scp"]
    assert "/local/outbox/local_rows_x.csv.gz" in cmd
    assert "yib@scraper-vm:incoming/local_rows_x.csv.gz" in cmd
    assert "yib@scraper-vm:~/incoming/local_rows_x.csv.gz" not in cmd
