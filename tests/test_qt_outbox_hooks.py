"""The dashboard's outbox hooks: a successful scrape/manual add queues + pushes rows.

Follows tests/test_qt_actions.py conventions: offscreen Qt via conftest, no real
subprocess/gcloud — everything at the outbox/vm_sync seam is monkeypatched.
"""
import io
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import outbox  # noqa: E402
import vm_sync  # noqa: E402
from qt.main_window import MainWindow  # noqa: E402


@pytest.fixture
def win(qtbot):
    w = MainWindow([])
    qtbot.addWidget(w)
    return w


def _unconfigured(*a, **k):
    return vm_sync.VMTarget(instance="", zone="", user="")


def test_push_outbox_to_vm_writes_rows_and_pushes(monkeypatch, win):
    calls = {}
    monkeypatch.setattr(outbox, "new_run_ids", lambda before: ["11", "22"])
    monkeypatch.setattr(outbox, "write_rows_outbox",
                        lambda ids: calls.setdefault("rows", list(ids)))
    monkeypatch.setattr(outbox, "write_stats_outbox",
                        lambda: calls.setdefault("stats", True))
    monkeypatch.setattr(outbox, "push_outbox",
                        lambda target, log=None: calls.setdefault("push", True) or (1, 0))
    monkeypatch.setattr(vm_sync.VMTarget, "from_env", classmethod(_unconfigured))
    log = io.StringIO()
    win._push_outbox_to_vm(log, before={})
    assert calls == {"rows": ["11", "22"], "stats": True, "push": True}


def test_push_outbox_to_vm_pushes_even_with_no_new_ids(monkeypatch, win):
    calls = {}
    monkeypatch.setattr(outbox, "new_run_ids", lambda before: [])
    monkeypatch.setattr(outbox, "write_rows_outbox",
                        lambda ids: pytest.fail("no rows file for an empty run"))
    monkeypatch.setattr(outbox, "write_stats_outbox", lambda: None)
    monkeypatch.setattr(outbox, "push_outbox",
                        lambda target, log=None: calls.setdefault("push", True) or (0, 2))
    monkeypatch.setattr(vm_sync.VMTarget, "from_env", classmethod(_unconfigured))
    win._push_outbox_to_vm(io.StringIO(), before={})
    assert calls == {"push": True}  # queued files still retry


def test_push_outbox_to_vm_swallows_errors(monkeypatch, win):
    monkeypatch.setattr(outbox, "new_run_ids",
                        lambda before: (_ for _ in ()).throw(RuntimeError("boom")))
    win._push_outbox_to_vm(io.StringIO(), before={})  # must not raise


def test_scrape_work_invokes_outbox_hook_after_success(monkeypatch, tmp_path, win):
    order = []
    monkeypatch.setattr(MainWindow, "scraper_cmd",
                        staticmethod(lambda bounded: [sys.executable, "-c", "pass"]))
    monkeypatch.setattr(MainWindow, "scorer_cmd",
                        staticmethod(lambda: [sys.executable, "-c", "pass"]))
    monkeypatch.setattr(MainWindow, "_scrape_log_path",
                        staticmethod(lambda: tmp_path / "scrape.log"))
    monkeypatch.setattr(MainWindow, "_outbox_snapshot", staticmethod(lambda: {"s": 1}))
    monkeypatch.setattr(MainWindow, "_push_seen_ids_to_vm",
                        staticmethod(lambda log: order.append("seen")))
    monkeypatch.setattr(MainWindow, "_push_outbox_to_vm",
                        staticmethod(lambda log, before: order.append(("outbox", before))))
    assert win._scrape_work(True) is True
    assert order == ["seen", ("outbox", {"s": 1})]


def test_scrape_work_skips_outbox_hook_on_failure(monkeypatch, tmp_path, win):
    called = []
    monkeypatch.setattr(MainWindow, "scraper_cmd",
                        staticmethod(lambda bounded: [sys.executable, "-c",
                                                      "import sys; sys.exit(3)"]))
    monkeypatch.setattr(MainWindow, "_scrape_log_path",
                        staticmethod(lambda: tmp_path / "scrape.log"))
    monkeypatch.setattr(MainWindow, "_outbox_snapshot", staticmethod(lambda: {}))
    monkeypatch.setattr(MainWindow, "_push_outbox_to_vm",
                        staticmethod(lambda log, before: called.append(1)))
    with pytest.raises(RuntimeError):
        win._scrape_work(True)
    assert called == []
