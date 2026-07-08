"""watcher.sync_back_to_vm — the self-healing local→VM data drain.

The watcher fires six times a day plus logon/unlock/resume, so hanging the
outbox drain + unsynced-rows sweep off it means locally collected rows reach
the VM master (and thus the Drive master) even when the dashboard never runs —
e.g. a snapshot recovered from the CLI. Everything at the outbox/vm_sync seam
is monkeypatched: no gcloud, no network, no real outbox.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import outbox  # noqa: E402
import vm_sync  # noqa: E402
import watcher  # noqa: E402


def _configured(*a, **k):
    return vm_sync.VMTarget(instance="i", zone="z", user="u")


def _unconfigured(*a, **k):
    return vm_sync.VMTarget(instance="", zone="", user="")


def test_sync_back_to_vm_pushes_when_work_exists(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(outbox, "unsynced_master_ids", lambda dm, **k: ["1"])
    monkeypatch.setattr(outbox, "pending_files", lambda **k: [])
    monkeypatch.setattr(vm_sync.VMTarget, "from_env", classmethod(_configured))

    def _sync_back(target, dm, **k):
        calls["drive"] = Path(dm)
        return (1, 1, 0)

    monkeypatch.setattr(outbox, "sync_back", _sync_back)
    watcher.sync_back_to_vm(tmp_path)
    assert calls["drive"] == tmp_path / "linkedin_jobs_master.csv.gz"


def test_sync_back_to_vm_drains_pending_even_without_unsynced_ids(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(outbox, "unsynced_master_ids", lambda dm, **k: [])
    monkeypatch.setattr(outbox, "pending_files",
                        lambda **k: [tmp_path / "local_rows_x.csv.gz"])
    monkeypatch.setattr(vm_sync.VMTarget, "from_env", classmethod(_configured))
    monkeypatch.setattr(outbox, "sync_back",
                        lambda target, dm, **k: called.append(1) or (0, 1, 0))
    watcher.sync_back_to_vm(tmp_path)
    assert called == [1]


def test_sync_back_to_vm_skips_when_nothing_to_do(monkeypatch, tmp_path):
    # Idle fires must not spawn gcloud (or even build a VMTarget).
    monkeypatch.setattr(outbox, "unsynced_master_ids", lambda dm, **k: [])
    monkeypatch.setattr(outbox, "pending_files", lambda **k: [])
    monkeypatch.setattr(vm_sync.VMTarget, "from_env", classmethod(
        lambda cls, targets=None: pytest.fail("no VMTarget on an idle fire")))
    monkeypatch.setattr(outbox, "sync_back",
                        lambda *a, **k: pytest.fail("nothing to sync"))
    watcher.sync_back_to_vm(tmp_path)


def test_sync_back_to_vm_skips_push_when_vm_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(outbox, "unsynced_master_ids", lambda dm, **k: ["1"])
    monkeypatch.setattr(outbox, "pending_files", lambda **k: [])
    monkeypatch.setattr(vm_sync.VMTarget, "from_env", classmethod(_unconfigured))
    monkeypatch.setattr(outbox, "sync_back",
                        lambda *a, **k: pytest.fail("no push without a VM"))
    watcher.sync_back_to_vm(tmp_path)


def test_sync_back_to_vm_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(outbox, "unsynced_master_ids",
                        lambda dm, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    watcher.sync_back_to_vm(tmp_path)  # must not raise — the watcher continues
