"""The 'Push resume.md to VM' button is enabled only when VM features are on AND
a VM is configured — otherwise greyed out (Cycle 6 SP6).

`_refresh_resume_md_push_state` is exercised on a tiny stand-in `self` so no full
dashboard or Tk window is needed; gcloud is never touched.
"""
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

pytest.importorskip("tkinter")

import ui  # noqa: E402
import vm_sync  # noqa: E402


class _FakeBtn:
    def __init__(self):
        self.state = None

    def configure(self, state):
        self.state = state


def _run(monkeypatch, *, vm_enabled, configured):
    monkeypatch.setattr(ui.settings, "load", lambda *a, **k: {"vm_enabled": vm_enabled})
    target = (vm_sync.VMTarget(instance="vm", zone="z", user="u") if configured
              else vm_sync.VMTarget())
    monkeypatch.setattr(vm_sync.VMTarget, "from_env",
                        classmethod(lambda cls, targets=None: target))
    btn = _FakeBtn()
    ui.App._refresh_resume_md_push_state(types.SimpleNamespace(btn_push_resume_md=btn))
    return btn.state


def test_push_disabled_when_vm_off(monkeypatch):
    assert _run(monkeypatch, vm_enabled=False, configured=True) == "disabled"


def test_push_disabled_when_vm_on_but_unconfigured(monkeypatch):
    assert _run(monkeypatch, vm_enabled=True, configured=False) == "disabled"


def test_push_enabled_when_vm_on_and_configured(monkeypatch):
    assert _run(monkeypatch, vm_enabled=True, configured=True) == "normal"
