"""gcloud-based VM sync core (local/vm_sync.py).

Pure argv builders. No real gcloud ever runs (the runner is
mocked); no secret is read — only non-secret connection identifiers.
"""
import os
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


def test_merge_crontab_preserves_env_lines_and_replaces_block():
    # A push must keep every line OUTSIDE the managed markers (the healthcheck
    # and project env lines the docs tell users to add) and swap only the block.
    import vm_schedule
    existing = (
        "HEALTHCHECKS_URL=https://hc-ping.com/abc\n"
        "GOOGLE_CLOUD_PROJECT=example-gcp-project\n"
        f"{vm_schedule.SCHEDULE_BEGIN}\n"
        "0 8 * * * ~/run_scraper.sh\n"
        f"{vm_schedule.SCHEDULE_END}\n"
    )
    combined = vm_sync.merge_crontab(existing, vm_schedule.build_crontab(["19:00"]))
    assert "HEALTHCHECKS_URL=https://hc-ping.com/abc" in combined
    assert "GOOGLE_CLOUD_PROJECT=example-gcp-project" in combined
    assert "0 8 * * *" not in combined            # old schedule gone
    assert "0 19 * * *" in combined               # new schedule in
    assert combined.count(vm_schedule.SCHEDULE_BEGIN) == 1  # no duplicate blocks
    assert combined.count(vm_schedule.SCHEDULE_END) == 1


def test_merge_crontab_appends_when_no_prior_block():
    import vm_schedule
    existing = "HEALTHCHECKS_URL=https://hc-ping.com/abc\n"
    combined = vm_sync.merge_crontab(existing, vm_schedule.build_crontab(["10:00"]))
    assert "HEALTHCHECKS_URL=https://hc-ping.com/abc" in combined
    assert combined.count(vm_schedule.SCHEDULE_BEGIN) == 1
    assert "0 10 * * *" in combined


def test_install_crontab_cmd_merges_not_replaces():
    # The remote command fetches the existing crontab and strips only the managed
    # block instead of blindly overwriting the whole crontab.
    remote = _target().install_crontab_cmd("0 10 * * * ~/run_scraper.sh")[-1]
    assert "crontab -l" in remote                       # fetch current
    assert "INPLOYED-SCHEDULE-BEGIN" in remote          # target the managed block
    assert "INPLOYED-SCHEDULE-END" in remote
    assert "crontab -" in remote                        # still installs


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


# Captured at import time — conftest's hermetic autouse fixture replaces
# vm_sync.run_cmd with a blocked stub for every test, so the one test OF
# run_cmd itself must hold the real function (module import runs first).
_REAL_RUN_CMD = vm_sync.run_cmd


def test_run_cmd_invokes_subprocess(monkeypatch):
    seen = {}

    def _run(cmd, **k):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(vm_sync.subprocess, "run", _run)
    res = _REAL_RUN_CMD(["gcloud", "compute", "ssh"])
    # run_cmd launches via launch_argv (which on Windows bypasses the gcloud.cmd
    # batch wrapper); on a box without gcloud it's the same argv.
    assert seen["cmd"] == vm_sync.launch_argv(["gcloud", "compute", "ssh"])
    assert res.returncode == 0


def test_run_cmd_decodes_gcloud_output_as_utf8(monkeypatch):
    """3.4: text=True without an explicit encoding decodes with the OS default,
    so a gcloud error body carrying non-ASCII would raise UnicodeDecodeError and
    surface as a failed sync with no message."""
    seen = {}

    def _run(cmd, **k):
        seen.update(k)
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(vm_sync.subprocess, "run", _run)
    _REAL_RUN_CMD(["gcloud", "version"])
    assert seen.get("text") is True
    assert seen.get("encoding") == "utf-8"
    assert seen.get("errors") == "replace"


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


def test_push_outbox_file_cmd_targets_incoming():
    cmd = _target().push_outbox_file_cmd("/local/outbox/local_rows_x.csv.gz")
    assert cmd[:3] == ["gcloud", "compute", "scp"]
    assert "/local/outbox/local_rows_x.csv.gz" in cmd
    assert "yib@scraper-vm:incoming/local_rows_x.csv.gz" in cmd
    assert "yib@scraper-vm:~/incoming/local_rows_x.csv.gz" not in cmd


# --- managed VM secrets (set the Bright Data / Gemini credentials from the GUI) ---
#
# The whole point of these is that the SECRET VALUE NEVER TOUCHES THE ARGV: it
# travels over the ssh stdin pipe, so it can't leak into the local process list,
# a crash traceback, or gcloud's own logging.

def test_managed_secret_names_are_the_two_the_vm_actually_reads():
    assert set(vm_sync.MANAGED_SECRETS) == {"BRIGHT_DATA_API_TOKEN", "GEMINI_API_KEYS"}


def test_set_secret_cmd_is_ssh_and_names_the_variable():
    cmd = _target().set_secret_cmd("BRIGHT_DATA_API_TOKEN")
    assert cmd[:3] == ["gcloud", "compute", "ssh"]
    assert "BRIGHT_DATA_API_TOKEN" in cmd[-1]
    assert "scraper_secrets.env" in cmd[-1]


def test_set_secret_cmd_NEVER_carries_the_value():
    """The regression that matters. Two channels were ruled out by measurement:
    gcloud copies the whole --command into its plaintext debug log, and plink
    eats stdin. So the value must arrive as a staged file and nothing else."""
    cmd = _target().set_secret_cmd("BRIGHT_DATA_API_TOKEN")
    assert vm_sync.SECRET_STAGE_REMOTE_FILE in cmd[-1]   # read from the staged file
    assert "read -r V" not in cmd[-1]                    # never from stdin
    for part in cmd:
        assert "SUPERSECRET" not in part


def test_the_staged_secret_file_is_always_deleted_on_the_vm():
    remote = _target().set_secret_cmd("BRIGHT_DATA_API_TOKEN")[-1]
    assert "trap 'rm -f" in remote      # an EXIT trap, so a failure cleans up too


def test_stage_secret_cmd_is_an_scp_of_the_local_file():
    cmd = _target().stage_secret_cmd("/tmp/x/value.txt")
    assert cmd[:3] == ["gcloud", "compute", "scp"]
    assert "/tmp/x/value.txt" in cmd
    assert any(vm_sync.SECRET_STAGE_REMOTE_FILE in p for p in cmd)


def test_set_secret_cmd_rejects_an_unmanaged_variable_name():
    """No arbitrary env-var injection through the name."""
    import pytest
    with pytest.raises(ValueError):
        _target().set_secret_cmd("PATH; rm -rf ~")


def test_set_secret_cmd_chmods_the_file_and_syntax_checks_the_script():
    remote = _target().set_secret_cmd("GEMINI_API_KEYS")[-1]
    assert "chmod 600" in remote
    assert "bash -n" in remote            # never leave run_scraper.sh unparseable
    assert ".bak-" in remote               # and always keep a backup to revert to


def test_set_secret_cmd_neutralises_the_old_inline_export():
    """Otherwise the inline `export TOKEN=<dead>` later in run_scraper.sh would
    override the value we just sourced, and the fix would silently do nothing."""
    remote = _target().set_secret_cmd("BRIGHT_DATA_API_TOKEN")[-1]
    assert "^export BRIGHT_DATA_API_TOKEN=" in remote


def test_valid_secret_values_are_accepted():
    for good in ("11111111-2222-3333-4444-555555555555", "AQ.abc_123", "k1,k2,k3"):
        assert vm_sync.valid_secret_value(good) is True


def test_shell_unsafe_secret_values_are_rejected():
    """The file is sourced by bash, so a `$` or backtick in the value would be
    interpolated at source time. Reject rather than try to escape."""
    for bad in ("", "   ", "a b", "$(whoami)", "`id`", 'a"b', "a'b", "a\b", "a$b"):
        assert vm_sync.valid_secret_value(bad) is False


def test_set_vm_secret_stages_the_value_then_installs_it(monkeypatch):
    seen = []

    def fake_run(cmd):
        # capture the staged file's CONTENT at scp time; it is gone by the end
        if "scp" in cmd:
            src = [p for p in cmd if p.endswith("value.txt")][0]
            seen.append(("scp", src, open(src, encoding="utf-8").read()))
        else:
            seen.append(("ssh", None, None))
        return types.SimpleNamespace(returncode=0, stdout="SECRET_SET", stderr="")

    monkeypatch.setattr(vm_sync, "run_cmd", fake_run)
    vm_sync.set_vm_secret(_target(), "BRIGHT_DATA_API_TOKEN", "tok-123")

    assert [s[0] for s in seen] == ["scp", "ssh"]      # stage, then install
    assert seen[0][2] == "tok-123\n"                  # value rode the file
    assert not os.path.exists(seen[0][1])              # and the temp dir is gone


def test_set_vm_secret_deletes_the_local_temp_even_when_the_push_fails(monkeypatch):
    seen = {}

    def boom(cmd):
        seen["src"] = [p for p in cmd if p.endswith("value.txt")][0]
        raise RuntimeError("scp exploded")

    monkeypatch.setattr(vm_sync, "run_cmd", boom)
    try:
        vm_sync.set_vm_secret(_target(), "BRIGHT_DATA_API_TOKEN", "tok-123")
    except RuntimeError:
        pass
    assert not os.path.exists(seen["src"])   # no plaintext credential left behind


def test_set_vm_secret_skips_the_install_when_staging_fails(monkeypatch):
    calls = []

    def fake_run(cmd):
        calls.append("scp" if "scp" in cmd else "ssh")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="permission denied")

    monkeypatch.setattr(vm_sync, "run_cmd", fake_run)
    res = vm_sync.set_vm_secret(_target(), "BRIGHT_DATA_API_TOKEN", "tok-123")
    assert calls == ["scp"]                 # never ran the installer on a failed upload
    assert res.returncode == 1


def test_set_vm_secret_refuses_an_unconfigured_target():
    assert vm_sync.set_vm_secret(vm_sync.VMTarget(), "BRIGHT_DATA_API_TOKEN", "x") is None


def test_the_remote_secret_script_actually_works_end_to_end(tmp_path):
    """Run the generated shell against a throwaway $HOME.

    The argv builders above are pure, so nothing else here proves the SCRIPT is
    correct -- and its failure mode is nasty (a mangled run_scraper.sh on the box
    that runs the cron). This executes it for real against a fake home holding a
    stand-in run_scraper.sh, then sources the result to prove the new value wins.
    """
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if not bash:
        import pytest
        pytest.skip("no bash available to run the remote script")

    home = tmp_path / "home"
    home.mkdir()
    (home / "run_scraper.sh").write_text(
        "#!/bin/bash\n"
        "export BRIGHT_DATA_API_TOKEN=deadtoken-old\n"
        "export BRIGHT_DATA_DATASET_ID=gd_keepme\n"
        "set -e\n", newline="\n")

    remote = _target().set_secret_cmd("BRIGHT_DATA_API_TOKEN")[-1][len("--command="):]
    script = tmp_path / "remote.sh"
    script.write_text(remote, newline="\n")
    env = {**os.environ, "HOME": str(home)}
    staged = home / vm_sync.SECRET_STAGE_REMOTE_FILE

    # Twice, to prove it is idempotent: a rotation must replace, never append.
    for value in ("first-value-111", "second-value-222"):
        staged.write_text(value + "\n", newline="\n")     # what scp would deliver
        res = subprocess.run([bash, str(script)], env=env, capture_output=True,
                             text=True, timeout=60)
        assert res.returncode == 0, res.stderr
        assert "SECRET_SET" in res.stdout
        assert not staged.exists(), "the staged credential must not survive the run"

    secrets = (home / "scraper_secrets.env").read_text()
    assert secrets.count("export BRIGHT_DATA_API_TOKEN=") == 1   # replaced, not appended
    assert "second-value-222" in secrets and "first-value-111" not in secrets

    script_text = (home / "run_scraper.sh").read_text()
    # the dead inline export must be neutralised, or it would override the source
    assert "\nexport BRIGHT_DATA_API_TOKEN=" not in script_text
    assert script_text.count("INPLOYED SECRETS BEGIN") == 1      # inserted once only
    assert "export BRIGHT_DATA_DATASET_ID=gd_keepme" in script_text   # untouched

    # and the whole thing still parses, then yields the NEW value when sourced
    assert subprocess.run([bash, "-n", str(home / "run_scraper.sh")]).returncode == 0
    out = subprocess.run(
        [bash, "-c", 'source "$HOME/run_scraper.sh" >/dev/null; '
                     'echo "$BRIGHT_DATA_API_TOKEN|$BRIGHT_DATA_DATASET_ID"'],
        env=env, capture_output=True, text=True, timeout=60).stdout.strip()
    assert out == "second-value-222|gd_keepme"


def test_a_failed_install_still_removes_the_staged_credential(tmp_path):
    """The EXIT trap, proven: an unreadable run_scraper.sh aborts the script, and
    the plaintext credential must not be left sitting in the VM's home dir."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if not bash:
        import pytest
        pytest.skip("no bash available to run the remote script")

    home = tmp_path / "home"
    home.mkdir()
    (home / "run_scraper.sh").write_text("#!/bin/bash\nif then fi\n", newline="\n")
    remote = _target().set_secret_cmd("BRIGHT_DATA_API_TOKEN")[-1][len("--command="):]
    script = tmp_path / "remote.sh"
    script.write_text(remote, newline="\n")
    staged = home / vm_sync.SECRET_STAGE_REMOTE_FILE
    staged.write_text("tok-abc\n", newline="\n")

    res = subprocess.run([bash, str(script)], env={**os.environ, "HOME": str(home)},
                         capture_output=True, text=True, timeout=60)
    assert res.returncode != 0
    assert "SYNTAX_FAIL_REVERTED" in res.stdout
    assert not staged.exists()
    # and the original script is back exactly as it was
    assert (home / "run_scraper.sh").read_text() == "#!/bin/bash\nif then fi\n"
