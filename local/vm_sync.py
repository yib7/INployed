"""Push config / schedule / pause to the cloud scraper VM via the user's gcloud.

Design constraints:
  * NO secrets are STORED or READ here — VM access uses the user's existing
    `gcloud` login, and only NON-secret connection identifiers (instance/zone/
    project/user/remote dir/gcloud path) are read, from the git-ignored .env.
    `set_vm_secret` is the one exception: it carries a value the user just typed
    straight through to the VM, never persisting it locally. It rides an scp of a
    mode-600 temp file that is deleted in a `finally`, because neither obvious
    channel is safe here -- see `stage_secret_cmd` for the measurements.
  * Pure argv builders + a thin `run_cmd` runner. The build/tests never execute a
    real gcloud command; the dashboard runs them only on an explicit user click.

`gcloud compute ssh/scp` is the transport, so the user authenticates once with
`gcloud auth login` and nothing here ever sees a VM password or SSH key. The one
credential that does pass through, in `set_vm_secret`, is a pipeline API key the
user pastes in to send onward, not anything used to reach the VM.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import settings
from vm_schedule import SCHEDULE_BEGIN, SCHEDULE_END

# Settings whose backing file lives on the VM (the scraper reads them there).
# Maps a Field.target -> the remote filename to push.
TARGET_REMOTE_FILE = {
    "search": "search_config.json",
    "scoring": "scoring_config.json",
}

# Seen/exclude id set pushed to the VM after a manual local scrape so its next
# scheduled run skips the ids this machine just collected. The VM's scraper unions
# this file into load_exclude_ids() (same dir convention as the config files above).
EXCLUDE_REMOTE_FILE = "external_exclude_ids.json"

# Remote spool dir for outbox files pushed from local machines; merge_incoming.py
# (run by run_scraper.sh before each scrape) drains it into the VM master.
INCOMING_REMOTE_DIR = "incoming"

# --- managed VM credentials -------------------------------------------------
# The VM's cron runs with a bare environment, so run_scraper.sh has to export the
# API credentials itself. They used to be pasted inline in that script, which made
# rotating a dead token an ssh-and-sed chore. These helpers move them into a
# chmod-600 ~/scraper_secrets.env that the script sources, so a rotation is a
# dashboard action instead.
SECRETS_REMOTE_FILE = "scraper_secrets.env"
# Short-lived upload slot for one credential; the install script deletes it.
SECRET_STAGE_REMOTE_FILE = ".inployed_secret_in"
SECRETS_BEGIN = ">>> INPLOYED SECRETS BEGIN <<<"
SECRETS_END = ">>> INPLOYED SECRETS END <<<"

# Only these may be set remotely — the name is interpolated into a shell script,
# so an unbounded name would be an injection point.
MANAGED_SECRETS = {
    "BRIGHT_DATA_API_TOKEN": "Bright Data token",
    "GEMINI_API_KEYS": "Gemini API keys",
}

# The secrets file is *sourced* by bash, so a value carrying $, a backtick, a
# quote or whitespace would be interpolated or word-split at source time. Every
# credential this pipeline actually uses (Bright Data UUIDs, "AQ."-prefixed Gemini
# keys, comma-joined key pools) fits this set, so validate and reject rather than
# try to escape a value into a shell-safe form.
_SAFE_SECRET = re.compile(r"[A-Za-z0-9._:,/+=-]+")


def valid_secret_value(value: str) -> bool:
    """True if `value` can live in a sourced shell file without interpolating."""
    return bool(_SAFE_SECRET.fullmatch((value or "").strip()))

# VM connection identifiers (all NON-secret), read from the .env via settings,
# with the fallback each one gets when it is unset. VMTarget.from_mapping reads
# its defaults from here rather than repeating them, so VM_KEYS cannot drift out
# of sync with the fields it is supposed to name (test_settings cross-checks the
# Settings schema against it).
VM_ENV_DEFAULTS = {
    "VM_GCLOUD_PATH": "gcloud",
    "VM_INSTANCE": "",
    "VM_ZONE": "",
    "VM_PROJECT": "",
    "VM_USER": "",
    "VM_REMOTE_DIR": "~",
}
VM_KEYS = tuple(VM_ENV_DEFAULTS)


def merge_crontab(existing: str, managed_block: str) -> str:
    """Combine an existing VM crontab with a freshly-generated managed block.

    Strips any prior SCHEDULE_BEGIN..END block from `existing` (so re-applying a
    schedule never stacks duplicate blocks) and appends `managed_block`. Every
    line OUTSIDE the markers is preserved verbatim — notably the user-added
    HEALTHCHECKS_URL= and GOOGLE_CLOUD_PROJECT= env lines run_scraper.sh reads,
    which a whole-crontab replace used to wipe on every schedule push.

    Pure text, so the round-trip is unit-testable without a live VM.
    `install_crontab_cmd` runs the equivalent (sed strip + append) on the VM."""
    kept: list[str] = []
    in_block = False
    for line in existing.splitlines():
        if line.strip() == SCHEDULE_BEGIN:
            in_block = True
            continue
        if in_block:
            if line.strip() == SCHEDULE_END:
                in_block = False
            continue
        kept.append(line)
    while kept and not kept[-1].strip():  # tidy trailing blanks before the block
        kept.pop()
    block = managed_block.strip("\n")
    return ("\n".join(kept) + "\n" + block + "\n") if kept else block + "\n"


@dataclass(frozen=True)
class VMTarget:
    gcloud: str = "gcloud"
    instance: str = ""
    zone: str = ""
    project: str = ""
    user: str = ""
    remote_dir: str = "~"

    @classmethod
    def from_mapping(cls, values: dict) -> "VMTarget":
        def g(key):
            v = str(values.get(key, "") or "").strip()
            return v or VM_ENV_DEFAULTS[key]
        return cls(
            gcloud=g("VM_GCLOUD_PATH"),
            instance=g("VM_INSTANCE"),
            zone=g("VM_ZONE"),
            project=g("VM_PROJECT"),
            user=g("VM_USER"),
            remote_dir=g("VM_REMOTE_DIR"),
        )

    @classmethod
    def from_env(cls, targets: dict | None = None) -> "VMTarget":
        """Build from the saved settings (.env). Freshly-saved identifiers work
        without a restart because settings.load reads the file, not os.environ."""
        return cls.from_mapping(settings.load(targets))

    def configured(self) -> bool:
        return bool(self.instance and self.zone and self.user)

    def _host(self) -> str:
        return f"{self.user}@{self.instance}"

    def _common_flags(self) -> list[str]:
        flags = [f"--zone={self.zone}"]
        if self.project:
            flags.append(f"--project={self.project}")
        return flags

    def build_ssh_cmd(self, remote_command: str) -> list[str]:
        return [self.gcloud, "compute", "ssh", self._host(),
                *self._common_flags(), f"--command={remote_command}"]

    def build_scp_cmd(self, local_path: str, remote_rel: str) -> list[str]:
        # A bare *relative* remote path resolves against the SSH login's home dir
        # on both OpenSSH scp and Windows pscp. An explicit "~" does NOT: pscp
        # (which gcloud uses on Windows) tries to open a literal "~/..." path and
        # fails with "unable to open ~/<file>", silently breaking the push. So
        # "~" / "." / "" all mean "the home dir" and emit a relative dest; only a
        # real directory keeps its prefix.
        base = self.remote_dir.rstrip("/")
        dest = (f"{self._host()}:{remote_rel}" if base in ("", "~", ".")
                else f"{self._host()}:{base}/{remote_rel}")
        return [self.gcloud, "compute", "scp", str(local_path), dest,
                *self._common_flags()]

    # --- higher-level operations (still pure: they return argv) ---------------

    def set_pause_cmd(self, value: str) -> list[str]:
        """ssh argv that writes ~/pause_until and echoes it back for confirmation."""
        q = shlex.quote(value)
        return self.build_ssh_cmd(
            f"printf '%s\\n' {q} > ~/pause_until && echo PAUSE_SET: $(cat ~/pause_until)")

    def resume_cmd(self) -> list[str]:
        return self.build_ssh_cmd("rm -f ~/pause_until && echo RESUMED")

    def install_crontab_cmd(self, crontab_text: str) -> list[str]:
        """ssh argv that MERGES the managed schedule block into the VM crontab
        instead of replacing the whole thing. Fetches the current crontab
        (tolerating "no crontab yet"), strips any prior SCHEDULE_BEGIN..END
        block, and appends the freshly-generated one, so user-added env lines
        outside the markers (HEALTHCHECKS_URL=, GOOGLE_CLOUD_PROJECT=) survive.

        The strip+append runs on the VM (sed over the piped `crontab -l`) because
        the current crontab lives there; `merge_crontab` below is the same
        contract in pure Python, unit-tested without a live VM."""
        q = shlex.quote(crontab_text.strip("\n") + "\n")
        strip = f"sed '/{SCHEDULE_BEGIN}/,/{SCHEDULE_END}/d'"
        return self.build_ssh_cmd(
            f"( crontab -l 2>/dev/null | {strip}; printf '%s' {q} ) | crontab - "
            "&& echo CRONTAB_INSTALLED && crontab -l")

    def push_exclude_ids_cmd(self, local_path: str) -> list[str]:
        """scp argv that uploads the seen/exclude id file to the VM, where the
        scraper unions it into load_exclude_ids()."""
        return self.build_scp_cmd(local_path, EXCLUDE_REMOTE_FILE)

    def _require_home_remote_dir(self) -> None:
        """Refuse a credential install when VM_REMOTE_DIR points somewhere else.

        build_scp_cmd honours remote_dir, but the install script below hardcodes
        $HOME. Set VM_REMOTE_DIR=/opt/scraper and the staged file lands in
        /opt/scraper while the script looks in $HOME: every attempt fails with
        NO_STAGED_VALUE, and every attempt leaves a plaintext credential behind
        where the EXIT trap will not reach it.

        Refusing beats teaching the script about remote_dir. That value comes from
        the user's .env and would have to be interpolated into shell text here,
        which is a new injection surface for a setting nothing else about this
        feature needs. Every other VM push keeps remote_dir in argv, where it is
        data. So: fail early, and name the setting.
        """
        if self.remote_dir.rstrip("/") not in ("", "~", "."):
            raise ValueError(
                f"Installing a credential needs VM_REMOTE_DIR to be the VM's home "
                f"directory, but it is {self.remote_dir!r}. Clear VM_REMOTE_DIR (or "
                f"set it to '~') to use this, or set the value on the VM by hand.")

    def stage_secret_cmd(self, local_path: str) -> list[str]:
        """scp argv that uploads the one-line secret file `set_secret_cmd` consumes.

        Why a staged file and not the obvious channels — both were measured
        against this exact VM on 2026-08-27, and both leak or fail:

          * argv is OUT: gcloud writes the whole `--command` string, verbatim, into
            its own plaintext debug log under ~/AppData/Roaming/gcloud/logs/. A
            token in the argv would persist there indefinitely.
          * stdin is OUT: on Windows gcloud shells out to PuTTY's plink.exe with
            `-legacy-stdio-prompts`, which consumes stdin for its own prompts. A
            probe piping "probe-value" reached the remote end as the single byte
            "y" — the data never arrives.

        scp logs the file's PATH but never its CONTENT, so this is the one channel
        that carries a credential without recording it.
        """
        self._require_home_remote_dir()
        return self.build_scp_cmd(local_path, SECRET_STAGE_REMOTE_FILE)

    def clear_staged_secret_cmd(self) -> list[str]:
        """ssh argv that deletes the staged credential, for when the installer
        never ran to fire its own EXIT trap. See set_vm_secret's except branch."""
        return self.build_ssh_cmd(f'rm -f "$HOME/{SECRET_STAGE_REMOTE_FILE}"')

    def set_secret_cmd(self, name: str) -> list[str]:
        """ssh argv that installs the staged credential into ~/scraper_secrets.env.

        The value is deliberately NOT in this argv (see `stage_secret_cmd`); it is
        read from the file scp put there, which this script always deletes via an
        EXIT trap — including on failure.

        The script is idempotent and self-healing. It creates the chmod-600
        secrets file, replaces just this one variable inside it, makes
        run_scraper.sh source that file (once, via a marker block inserted right
        after the shebang so it runs before anything needs the value), and
        comments out any older inline assignment further down. That last step is
        load-bearing: an inline export sits AFTER the source line and would
        otherwise win, leaving the dead credential in force and making the fix
        look like it silently did nothing.

        It then PROVES the result instead of announcing it. `echo SECRET_SET`
        used to be unconditional, so two shapes reported success while changing
        nothing the cron run would see: a missing run_scraper.sh (the whole edit
        block was skipped) and an indented `  export NAME=` (the old sed anchored
        at column 0, so the dead value still won at source time). Both now fail
        with a named code. The check is textual on purpose — verifying by
        SOURCING run_scraper.sh would execute it, and that script starts a billed
        scrape.

        run_scraper.sh is backed up once, to a fixed name, and restored if the
        result fails either check, so a bad edit can never leave the VM with a
        broken cron script. A script that was ALREADY unparseable before we
        touched it reports SCRIPT_ALREADY_BROKEN rather than blaming this edit.

        Raw f-string on purpose: the sed newline escapes below must reach sed as
        two characters, not as real line breaks.
        """
        if name not in MANAGED_SECRETS:
            raise ValueError(f"{name!r} is not a managed VM secret; expected one "
                             f"of {sorted(MANAGED_SECRETS)}")
        self._require_home_remote_dir()
        src = f'[ -f "$HOME/{SECRETS_REMOTE_FILE}" ] && . "$HOME/{SECRETS_REMOTE_FILE}"'
        # Matches `export NAME=`, `NAME=`, indented or not, with any run of spaces
        # after `export`. The old pattern was `^export NAME=` and missed all three
        # variations, each of which leaves the dead credential winning.
        inline = rf"^([[:space:]]*)(export[[:space:]]+)?{name}="
        return self.build_ssh_cmd(rf"""set -e
IN="$HOME/{SECRET_STAGE_REMOTE_FILE}"
trap 'rm -f "$IN"' EXIT
[ -s "$IN" ] || {{ echo NO_STAGED_VALUE; exit 1; }}
V="$(head -n 1 "$IN")"
[ -n "$V" ] || {{ echo EMPTY_VALUE; exit 1; }}
umask 077
F="$HOME/{SECRETS_REMOTE_FILE}"
touch "$F"; chmod 600 "$F"
T="$(mktemp "$F.XXXXXX")"
grep -v '^export {name}=' "$F" > "$T" || [ $? -eq 1 ]
printf 'export {name}="%s"\n' "$V" >> "$T"
mv "$T" "$F"; chmod 600 "$F"
S="$HOME/run_scraper.sh"
[ -s "$S" ] || {{ echo NO_RUN_SCRIPT; exit 1; }}
B="$S.inployed.bak"
[ -f "$B" ] || {{ cp "$S" "$B"; chmod 600 "$B"; }}
PRE=0; bash -n "$S" 2>/dev/null || PRE=1
grep -q '{SECRETS_BEGIN}' "$S" || sed -i '1a # {SECRETS_BEGIN}\n{src}\n# {SECRETS_END}' "$S"
sed -i -E 's|{inline}|\1# moved into {SECRETS_REMOTE_FILE} by INployed: \2{name}=|' "$S"
if ! bash -n "$S" 2>/dev/null; then
  cp "$B" "$S"
  if [ "$PRE" = 1 ]; then echo SCRIPT_ALREADY_BROKEN; else echo SYNTAX_FAIL_REVERTED; fi
  exit 1
fi
if grep -Eq '{inline}' "$S"; then
  cp "$B" "$S"; echo INLINE_ASSIGNMENT_REMAINS; exit 1
fi
grep -q '{SECRETS_BEGIN}' "$S" || {{ cp "$B" "$S"; echo SOURCE_LINE_MISSING; exit 1; }}
echo SECRET_SET: {name}
""")

    def push_outbox_file_cmd(self, local_path: str) -> list[str]:
        """scp argv that spools one local outbox file into the VM's ~/incoming/,
        where merge_incoming.py folds it into the master before the next scrape."""
        return self.build_scp_cmd(local_path,
                                  f"{INCOMING_REMOTE_DIR}/{Path(local_path).name}")


def _bypass_argv(resolved: str, rest: list[str]) -> list[str] | None:
    """Given a resolved gcloud `.cmd`/`.bat` wrapper path, return an argv that runs
    gcloud's Python entrypoint (`<sdk>/lib/gcloud.py`) directly.

    Why: on Windows the SDK ships `gcloud.cmd`, a batch wrapper that forwards args
    with `%*`. cmd.exe re-parses `%*`, so any argument carrying shell metacharacters
    — our `--command="… && …"` / `printf '%s\\n' … > ~/pause_until` — gets truncated
    at the first `%`/`&&`/`>`. Calling the python entrypoint with no shell passes
    every argument through verbatim. Returns None if the SDK layout isn't found."""
    root = Path(resolved).resolve().parent.parent  # <sdk>/bin/gcloud.cmd -> <sdk>
    gpy = root / "lib" / "gcloud.py"
    if not gpy.exists():
        return None
    bundled = root / "platform" / "bundledpython" / "python.exe"
    py = (os.environ.get("CLOUDSDK_PYTHON")
          or (str(bundled) if bundled.exists() else shutil.which("python") or sys.executable))
    return [py, "-S", str(gpy), *rest]


def launch_argv(cmd: list[str]) -> list[str]:
    """Resolve a gcloud argv into something `subprocess` can launch reliably. On
    Windows, a bare `gcloud` is the `gcloud.cmd` batch wrapper (subprocess can't
    find it -> WinError 2, and its `%*` mangles shell-metachar args), so we bypass
    it via gcloud's python entrypoint. Elsewhere (and for a real executable) the
    argv is returned unchanged save for resolving the program to a full path."""
    cmd = list(cmd)
    if os.name != "nt" or not cmd:
        return cmd
    resolved = shutil.which(cmd[0]) or cmd[0]
    if resolved.lower().endswith((".cmd", ".bat")):
        bypass = _bypass_argv(resolved, cmd[1:])
        if bypass is not None:
            return bypass
    return [resolved, *cmd[1:]]


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a gcloud argv and capture output. Only ever called from an explicit
    user click in the dashboard — never during the build or tests (mocked).
    `launch_argv` makes the bare `gcloud` name launch on Windows."""
    # Explicit utf-8, not the OS default text=True would use: gcloud echoes
    # instance names, paths and error bodies that carry non-ASCII, and a
    # UnicodeDecodeError here would surface as a failed sync with no message.
    # No stdin channel on purpose: on Windows gcloud shells out to plink.exe,
    # which consumes stdin for its own prompts -- piped data reaches the remote
    # command mangled (measured: "probe-value" arrived as the single byte "y").
    return subprocess.run(launch_argv(cmd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)


def sync_exclude_ids_to_vm(target: VMTarget, local_path) -> subprocess.CompletedProcess | None:
    """Push the seen/exclude id set to the VM so its next scheduled scrape skips the
    ids this machine just collected. Returns None when the VM isn't configured. The
    caller treats this as best-effort — a sync failure must never block a local scrape."""
    if not target.configured():
        return None
    return run_cmd(target.push_exclude_ids_cmd(str(local_path)))


def set_vm_secret(target: VMTarget, name: str, value: str):
    """Store one managed credential on the VM: stage it, install it, clean up.

    Returns the install step's CompletedProcess, or None when the VM isn't
    configured. Raises ValueError for an unmanaged name, a non-home VM_REMOTE_DIR,
    or a value that couldn't live safely in a sourced shell file.

    EVERY rejection happens before the upload. The name check used to live in
    set_secret_cmd, which runs only AFTER the scp has already put the plaintext
    value on the VM, so a rejected name left the credential sitting there with no
    EXIT trap ever armed to remove it. Same for the remote-dir check.

    If the install step fails to run at all (gcloud session expired, network
    drop, the 300s timeout in run_cmd), the staged file is removed with a second
    ssh rather than left for a trap that never fired.

    The value touches this machine's disk only as a file inside a private temp
    dir, deleted in the `finally` no matter what — the least-bad channel
    available (see `stage_secret_cmd` for why argv and stdin are both unusable
    through gcloud). The 0600 mode passed to os.open is a POSIX no-op on Windows,
    where CPython honours only the read-only bit; the real protection there is the
    per-user ACL on %TEMP%. Both are stated because one of them is doing the work
    on the only platform this dashboard runs on.
    """
    if not target.configured():
        return None
    if name not in MANAGED_SECRETS:
        raise ValueError(f"{name!r} is not a managed VM secret; expected one "
                         f"of {sorted(MANAGED_SECRETS)}")
    target._require_home_remote_dir()
    value = (value or "").strip()
    if not valid_secret_value(value):
        raise ValueError(
            "That value has characters the VM secrets file cannot hold safely. "
            "Allowed: letters, digits and . _ - : , / + = (no spaces or quotes).")
    tmpdir = tempfile.mkdtemp(prefix="inployed-secret-")
    local = Path(tmpdir) / "value.txt"
    try:
        fd = os.open(local, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(value + "\n")
        staged = run_cmd(target.stage_secret_cmd(str(local)))
        if staged.returncode != 0:
            return staged
        try:
            return run_cmd(target.set_secret_cmd(name))
        except BaseException:
            # The installer never ran, so its EXIT trap never armed. Best effort,
            # and deliberately silent on failure: we are already unwinding, and a
            # cleanup error must not replace the real one.
            try:
                run_cmd(target.clear_staged_secret_cmd())
            except Exception:  # noqa: BLE001
                pass
            raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if local.exists():
            # rmtree swallowed something (a handle still open). Overwrite before
            # giving up, so a readable plaintext credential is not what is left.
            try:
                local.write_text("x" * len(value), encoding="utf-8")
                local.unlink()
                Path(tmpdir).rmdir()
            except OSError:
                print(f"WARNING: could not remove the staged credential at {local}; "
                      f"delete it by hand.")
