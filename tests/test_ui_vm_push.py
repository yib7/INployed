"""The post-save 'push to VM?' prompt must stay silent unless VM features are
enabled (the master toggle). It also already requires a real VM-relevant change
and a configured target — here we pin the toggle gate.

`_maybe_prompt_vm_push` is exercised directly on a tiny stand-in `self` so no full
dashboard (data files, Tk) is needed; gcloud is never touched.
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


def _configured_target():
    return vm_sync.VMTarget(instance="vm", zone="z", user="u")


def _patch(monkeypatch, seen):
    monkeypatch.setattr(vm_sync, "changed_vm_files", lambda b, a: {"scoring_config.json"})
    monkeypatch.setattr(vm_sync.VMTarget, "from_env",
                        classmethod(lambda cls, targets=None: _configured_target()))
    monkeypatch.setattr(ui.messagebox, "askyesno",
                        lambda *a, **k: (seen.append(1), False)[1])


def test_no_prompt_when_vm_disabled(monkeypatch):
    seen: list[int] = []
    _patch(monkeypatch, seen)
    fake = types.SimpleNamespace(root=None)
    ui.App._maybe_prompt_vm_push(fake, {}, {"vm_enabled": False})
    assert seen == []  # disabled -> never asks, even with a real change + configured VM


def test_prompts_when_vm_enabled(monkeypatch):
    seen: list[int] = []
    _patch(monkeypatch, seen)
    fake = types.SimpleNamespace(root=None)
    ui.App._maybe_prompt_vm_push(fake, {}, {"vm_enabled": True})
    assert seen == [1]  # enabled + configured + changed -> asks once
