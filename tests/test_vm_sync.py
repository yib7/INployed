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
    for good in ("11111111-2222-3333-4444-555555555555", "AQ.abc_123", "k1,k2,k3"):
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
                         capture_output=True, text=True, timeout=60)
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
        capture_output=True, text=True, timeout=60).stdout.strip()
    assert out == "second-value-222|gd_keepme"


def test_an_indented_inline_export_is_neutralised_not_left_to_win(tmp_path):
    """The sed used to be anchored at column 0, so `  export NAME=old` survived.

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
        capture_output=True, text=True, timeout=60).stdout.strip()
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
    """The name check used to live in set_secret_cmd, which runs only AFTER the
    scp: a rejected name still put the plaintext credential on the VM, with no
    EXIT trap ever armed to remove it."""
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
