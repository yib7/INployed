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

import shlex
import subprocess
from dataclasses import dataclass

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


def changed_vm_files(before: dict, after: dict) -> set[str]:
    """Remote filenames whose owning settings changed between two settings dicts.
    Only settings backed by a VM file (search/scoring targets) count — local-only
    settings (config target) never trigger a VM push."""
    changed: set[str] = set()
    for f in settings.SETTINGS_SCHEMA:
        remote = TARGET_REMOTE_FILE.get(f.target)
        if remote and before.get(f.key) != after.get(f.key):
            changed.add(remote)
    return changed


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a gcloud argv and capture output. Only ever called from an explicit
    user click in the dashboard — never during the build or tests (mocked)."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)
