"""Push config / schedule / pause to the cloud scraper VM via the user's gcloud.

Design constraints (see .autopilot/AUTONOMY.md):
  * NO secrets stored or read here — VM access uses the user's existing `gcloud`
    login. Only NON-secret connection identifiers (instance/zone/project/user/
    remote dir/gcloud path) are read, from the git-ignored .env via settings.
  * Pure argv builders + a thin `run_cmd` runner. The build/tests never execute a
    real gcloud command; the dashboard runs them only on an explicit user click.

`gcloud compute ssh/scp` is the transport (matches docs/HANDOFF.md), so the user
authenticates once with `gcloud auth login` and nothing here ever sees a password
or key.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import settings

# Settings whose backing file lives on the VM (the scraper reads them there).
# Maps a Field.target -> the remote filename to push.
TARGET_REMOTE_FILE = {
    "search": "search_config.json",
    "scoring": "scoring_config.json",
}

# VM connection identifiers (all NON-secret), read from the .env via settings.
VM_KEYS = ("VM_GCLOUD_PATH", "VM_INSTANCE", "VM_ZONE", "VM_PROJECT", "VM_USER",
           "VM_REMOTE_DIR")


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
        def g(key, default):
            v = str(values.get(key, "") or "").strip()
            return v or default
        return cls(
            gcloud=g("VM_GCLOUD_PATH", "gcloud"),
            instance=g("VM_INSTANCE", ""),
            zone=g("VM_ZONE", ""),
            project=g("VM_PROJECT", ""),
            user=g("VM_USER", ""),
            remote_dir=g("VM_REMOTE_DIR", "~"),
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
        dest = f"{self._host()}:{self.remote_dir.rstrip('/')}/{remote_rel}"
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
        """ssh argv that replaces the VM crontab with `crontab_text`."""
        q = shlex.quote(crontab_text + "\n")
        return self.build_ssh_cmd(
            f"printf '%s' {q} | crontab - && echo CRONTAB_INSTALLED && crontab -l")


def _norm_value(field, value):
    """Normalize a value for change detection so semantically-equal saves don't
    falsely flag a push: multichoice is order-insensitive (a set), and list items
    are whitespace-insensitive (stripped). Everything else compares as-is."""
    if field.type == "multichoice":
        return frozenset(value) if isinstance(value, list) else value
    if field.type == "list":
        return tuple(str(v).strip() for v in value) if isinstance(value, list) else value
    return value


def changed_vm_files(before: dict, after: dict) -> set[str]:
    """Remote filenames whose owning settings *meaningfully* changed between two
    settings dicts. Only settings backed by a VM file (search/scoring targets)
    count — local-only settings (config target) never trigger a VM push — and the
    comparison is value-semantic (see `_norm_value`), so re-saving the same values
    (e.g. re-picking the same model, or a reordered multichoice) does not flag."""
    changed: set[str] = set()
    for f in settings.SETTINGS_SCHEMA:
        remote = TARGET_REMOTE_FILE.get(f.target)
        if remote and _norm_value(f, before.get(f.key)) != _norm_value(f, after.get(f.key)):
            changed.add(remote)
    return changed


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
    return subprocess.run(launch_argv(cmd), capture_output=True, text=True, timeout=300)
