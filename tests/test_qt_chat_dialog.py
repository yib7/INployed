"""SP4: the per-job "Ask AI" chat dialog and its two entry points.

Headless (QT_QPA_PLATFORM=offscreen, set in conftest) and thread-free: every test
that sends a turn swaps `workers.run_async` for the synchronous stand-in the rest
of the Qt suite uses, and `resume_tailor.chat.ask` is monkeypatched, so no real
LLM call and no real QThread is ever created.

The load-bearing test here is the lifetime one. This dialog is non-modal and the
main window holds a reference to it, which is exactly the widget-leak shape
`conftest._drain_qt_widgets` exists to catch: an accumulating pile of
closed-but-alive widgets makes `theme.apply_theme`'s `app.allWidgets()` sweep grow
without bound until a test effectively hangs. Parenting to the main window plus
`deleteLater()` on close is what keeps it bounded — see
`test_dialog_is_parented_and_destroyed_on_close`.
"""
import pandas as pd
import pytest
import shiboken6
from PySide6 import QtCore, QtGui, QtWidgets
from unittest.mock import MagicMock

from qt import chat_dialog as cd
from qt import main_window as mw
from qt.chat_dialog import JobChatDialog
from qt.jobs_tab import JobsTab
from resume_tailor import chat

JOB = {"job_posting_id": "1", "company_name": "Acme", "job_title": "Data Analyst",
       "job_description_formatted": "Dashboards in pandas.", "url": "https://x/1"}

COLS = [("score", 50), ("job_title", 240), ("company_name", 170), ("url", 220)]


def _df():
    return pd.DataFrame([
        {"job_posting_id": "1", "score": "5", "job_title": "Data Analyst",
         "company_name": "Acme", "url": "https://x/1", "is_seen": "no",
         "extracted_date": "2026-06-20"},
        {"job_posting_id": "2", "score": "4", "job_title": "ML Engineer",
         "company_name": "Globex", "url": "https://x/2", "is_seen": "no",
         "extracted_date": "2026-06-21"},
    ])


@pytest.fixture(autouse=True)
def _no_auth_env(monkeypatch):
    """`_apply_auth_env` writes a PROCESS-wide env var; keep it out of the session
    (the seeding test shadows this with an instance attribute of its own)."""
    monkeypatch.setattr(mw.MainWindow, "_apply_auth_env", lambda self: None)


def _sync_workers(monkeypatch):
    """`run_async` that runs the callable inline and marshals like the real one."""
    def run_async(owner, fn, on_done=None, on_error=None):
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - mirrors Worker.run
            if on_error is not None:
                on_error(exc)
        else:
            if on_done is not None:
                on_done(result)
        return None

    monkeypatch.setattr(cd.workers, "run_async", run_async)


def _stub_chat(monkeypatch, answer="Lead with the pipeline bullet.", context="CTX"):
    seen = {}

    def fake_ask(ctx, history, question):
        seen.setdefault("calls", []).append((ctx, list(history), question))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(chat, "context_for_job", lambda job: context)
    monkeypatch.setattr(chat, "ask", fake_ask)
    return seen


def _dialog(qtbot, job=None):
    """A dialog parented to a live host, the way the main window parents it.

    Only the host is registered with qtbot: the dialog deletes itself on close,
    and a registered widget whose C++ half is gone breaks pytest-qt's teardown.
    """
    host = QtWidgets.QWidget()
    qtbot.addWidget(host)
    return host, JobChatDialog(job or JOB, parent=host)


def _ask(dlg, question):
    dlg.input.setPlainText(question)
    dlg.send()


# ── construction ──────────────────────────────────────────────────────────────
def test_dialog_builds_with_a_transcript_an_input_and_the_two_buttons(qtbot):
    _host, dlg = _dialog(qtbot)
    assert isinstance(dlg.transcript, QtWidgets.QTextBrowser)
    assert dlg.transcript.isReadOnly()
    assert isinstance(dlg.input, QtWidgets.QPlainTextEdit)
    assert "Send" in dlg.send_btn.text()
    assert "Copy" in dlg.copy_btn.text()


def test_dialog_is_non_modal_and_roomy(qtbot):
    _host, dlg = _dialog(qtbot)
    assert dlg.isModal() is False          # the user keeps working in the dashboard
    assert (dlg.width(), dlg.height()) == (720, 800)


def test_dialog_title_names_the_job(qtbot):
    _host, dlg = _dialog(qtbot)
    assert "Data Analyst" in dlg.windowTitle() and "Acme" in dlg.windowTitle()


def test_transcript_does_not_follow_links(qtbot):
    """The JD and the apply sheet can carry URLs; this is a reader, not a browser."""
    _host, dlg = _dialog(qtbot)
    assert dlg.transcript.openLinks() is False
    assert dlg.transcript.openExternalLinks() is False


def test_ctrl_enter_sends(qtbot):
    _host, dlg = _dialog(qtbot)
    seqs = {sc.key().toString() for sc in dlg.findChildren(QtGui.QShortcut)}
    assert "Ctrl+Return" in seqs or "Ctrl+Enter" in seqs


# ── sending a turn ────────────────────────────────────────────────────────────
def test_send_runs_the_worker_and_appends_the_exchange(qtbot, monkeypatch):
    _sync_workers(monkeypatch)
    _stub_chat(monkeypatch, answer="Lead with the pipeline bullet.")
    _host, dlg = _dialog(qtbot)
    _ask(dlg, "How do I answer 'why this role'?")
    text = dlg.transcript.toPlainText()
    assert "How do I answer 'why this role'?" in text
    assert "Lead with the pipeline bullet." in text


def test_send_goes_through_run_async_not_the_ui_thread(qtbot, monkeypatch):
    """A chat turn is a network round-trip; running it inline would freeze the app."""
    launched = []
    monkeypatch.setattr(cd.workers, "run_async",
                        lambda owner, fn, on_done=None, on_error=None: launched.append(fn))
    _stub_chat(monkeypatch)
    _host, dlg = _dialog(qtbot)
    _ask(dlg, "anything?")
    assert len(launched) == 1


def test_send_clears_the_input_box(qtbot, monkeypatch):
    _sync_workers(monkeypatch)
    _stub_chat(monkeypatch)
    _host, dlg = _dialog(qtbot)
    _ask(dlg, "a question")
    assert dlg.input.toPlainText() == ""


def test_blank_question_is_a_noop(qtbot, monkeypatch):
    launched = []
    monkeypatch.setattr(cd.workers, "run_async",
                        lambda *a, **k: launched.append(1))
    _stub_chat(monkeypatch)
    _host, dlg = _dialog(qtbot)
    _ask(dlg, "   \n  ")
    assert launched == []


def test_send_is_busy_while_the_turn_is_in_flight(qtbot, monkeypatch):
    state = {}
    monkeypatch.setattr(cd.workers, "run_async",
                        lambda owner, fn, on_done=None, on_error=None:
                        state.setdefault("enabled_during", owner.send_btn.isEnabled()))
    _stub_chat(monkeypatch)
    _host, dlg = _dialog(qtbot)
    _ask(dlg, "a question")
    assert state["enabled_during"] is False       # no double-send while thinking
    assert dlg.send_btn.isEnabled() is False


def test_send_re_enables_after_the_answer(qtbot, monkeypatch):
    _sync_workers(monkeypatch)
    _stub_chat(monkeypatch)
    _host, dlg = _dialog(qtbot)
    _ask(dlg, "a question")
    assert dlg.send_btn.isEnabled() is True


def test_second_send_while_busy_is_ignored(qtbot, monkeypatch):
    launched = []
    monkeypatch.setattr(cd.workers, "run_async",
                        lambda owner, fn, on_done=None, on_error=None: launched.append(fn))
    _stub_chat(monkeypatch)
    _host, dlg = _dialog(qtbot)
    _ask(dlg, "first")
    _ask(dlg, "second")
    assert len(launched) == 1


# ── the conversation ──────────────────────────────────────────────────────────
def test_history_accumulates_and_is_replayed_to_ask(qtbot, monkeypatch):
    _sync_workers(monkeypatch)
    seen = _stub_chat(monkeypatch, answer="an answer")
    _host, dlg = _dialog(qtbot)
    _ask(dlg, "first")
    _ask(dlg, "second")
    first_call, second_call = seen["calls"]
    assert first_call[1] == []                             # no history on turn one
    assert second_call[1] == [("first", "an answer")]      # turn one replayed
    assert second_call[2] == "second"


def test_context_is_built_once_and_reused_across_turns(qtbot, monkeypatch):
    """The context is the cacheable half — rebuilding it would re-scan the output
    root on every single turn and defeat the whole point of the split."""
    _sync_workers(monkeypatch)
    builds = []
    monkeypatch.setattr(chat, "context_for_job",
                        lambda job: builds.append(job) or "CTX")
    monkeypatch.setattr(chat, "ask", lambda ctx, hist, q: "an answer")
    _host, dlg = _dialog(qtbot)
    _ask(dlg, "first")
    _ask(dlg, "second")
    assert len(builds) == 1


def test_context_is_built_off_the_ui_thread(qtbot, monkeypatch):
    """`context_for_job` scans the output root — a Drive-backed folder would
    freeze the window if it ran during the click."""
    built = []
    monkeypatch.setattr(chat, "context_for_job", lambda job: built.append(1) or "CTX")
    monkeypatch.setattr(chat, "ask", lambda ctx, hist, q: "an answer")
    monkeypatch.setattr(cd.workers, "run_async", lambda *a, **k: None)  # never runs fn
    _host, dlg = _dialog(qtbot)
    _ask(dlg, "a question")
    assert built == []


def test_copy_answer_puts_the_last_answer_on_the_clipboard(qtbot, monkeypatch):
    _sync_workers(monkeypatch)
    _stub_chat(monkeypatch, answer="Lead with the pipeline bullet.")
    _host, dlg = _dialog(qtbot)
    QtWidgets.QApplication.clipboard().setText("")
    _ask(dlg, "a question")
    dlg.copy_btn.click()
    assert QtWidgets.QApplication.clipboard().text() == "Lead with the pipeline bullet."


def test_failure_surfaces_in_the_transcript_and_re_enables_send(qtbot, monkeypatch):
    _sync_workers(monkeypatch)
    _stub_chat(monkeypatch, answer=RuntimeError("no credentials configured"))
    _host, dlg = _dialog(qtbot)
    _ask(dlg, "a question")
    assert "no credentials configured" in dlg.transcript.toPlainText()
    assert dlg.send_btn.isEnabled() is True
    assert dlg.history() == []          # a failed turn is not remembered as an answer


# ── Qt object lifetime (the CI-hanging one) ───────────────────────────────────
def test_dialog_is_parented_and_destroyed_on_close(qtbot):
    host = QtWidgets.QWidget()
    qtbot.addWidget(host)
    dlg = JobChatDialog(JOB, parent=host)
    assert dlg.parent() is host             # never a stray top-level widget
    dlg.close()
    QtWidgets.QApplication.instance().sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(dlg)       # deleteLater actually ran


def test_dialog_is_destroyed_on_reject_too(qtbot):
    """Esc goes through reject(), which never raises a close event."""
    host = QtWidgets.QWidget()
    qtbot.addWidget(host)
    dlg = JobChatDialog(JOB, parent=host)
    dlg.reject()
    QtWidgets.QApplication.instance().sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(dlg)


def test_close_fires_the_on_closed_callback_once(qtbot):
    host = QtWidgets.QWidget()
    qtbot.addWidget(host)
    closed = []
    dlg = JobChatDialog(JOB, parent=host, on_closed=lambda: closed.append(1))
    dlg.close()
    dlg.close()
    assert closed == [1]


# ── entry point 1: the jobs-table right-click menu ────────────────────────────
def _select_rows(tab, *rows):
    sm = tab.table.selectionModel()
    sm.clearSelection()
    flags = (QtCore.QItemSelectionModel.SelectionFlag.Select
             | QtCore.QItemSelectionModel.SelectionFlag.Rows)
    for r in rows:
        sm.select(tab.proxy.index(r, 0), flags)


def _menu_texts(monkeypatch, tab, choose=None):
    seen = {}

    class FakeMenu(QtWidgets.QMenu):
        def exec(self, *a, **k):
            seen["texts"] = [act.text() for act in self.actions()]
            if choose is not None:
                for act in self.actions():
                    if act.text() == choose:
                        return act
            return None

    monkeypatch.setattr(QtWidgets, "QMenu", FakeMenu)
    tab._context_menu(QtCore.QPoint(2, 2))
    return seen.get("texts", [])


ASK_AI_ITEM = "Ask AI about this job"


def test_context_menu_offers_ask_ai_when_wired(qtbot, monkeypatch):
    fired = []
    tab = JobsTab("all", COLS, on_ask_ai=fired.append)
    qtbot.addWidget(tab)
    tab.set_source_df(_df())
    _select_rows(tab, 0)
    expected = tab.selected_ids()[0]
    texts = _menu_texts(monkeypatch, tab, choose=ASK_AI_ITEM)
    assert ASK_AI_ITEM in texts
    assert fired == [expected]


def test_context_menu_has_no_ask_ai_item_when_unwired(qtbot, monkeypatch):
    tab = JobsTab("all", COLS)     # no on_ask_ai injected
    qtbot.addWidget(tab)
    tab.set_source_df(_df())
    _select_rows(tab, 0)
    texts = _menu_texts(monkeypatch, tab)
    assert texts and ASK_AI_ITEM not in texts


def test_context_menu_hides_ask_ai_on_a_multi_selection(qtbot, monkeypatch):
    """One chat is about one job; a two-row selection has no single subject."""
    tab = JobsTab("all", COLS, on_ask_ai=lambda jid: None)
    qtbot.addWidget(tab)
    tab.set_source_df(_df())
    _select_rows(tab, 0, 1)
    texts = _menu_texts(monkeypatch, tab)
    assert texts and ASK_AI_ITEM not in texts


# ── entry point 2: the Apply panel button ─────────────────────────────────────
def test_apply_panel_has_ask_ai_next_to_open_folder(qtbot):
    from qt.apply_panel import ApplyPanel
    p = ApplyPanel()
    qtbot.addWidget(p)
    texts = [b.text() for b in p.findChildren(QtWidgets.QPushButton)]
    assert "Ask AI" in texts
    assert abs(texts.index("Ask AI") - texts.index("Open folder")) == 1


def test_apply_panel_ask_ai_fires_the_injected_callback(qtbot):
    from qt.apply_panel import ApplyPanel
    fired = []
    p = ApplyPanel(on_ask_ai=lambda: fired.append(True))
    qtbot.addWidget(p)
    p.ask_ai_btn.click()
    assert fired == [True]


# ── main-window wiring ────────────────────────────────────────────────────────
def _fake_registry():
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    return reg


def _win(qtbot):
    w = mw.MainWindow(csv_paths=[], registry=_fake_registry())
    qtbot.addWidget(w)
    return w


def test_jobs_tabs_wire_the_ask_ai_callback(qtbot):
    w = _win(qtbot)
    for tab in (w.high_tab, w.all_tab, w.tracker_tab):
        assert tab._on_ask_ai == w._ask_ai_for


def test_ask_ai_for_opens_a_dialog_parented_to_the_window(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w, "_job_payload", lambda jid: dict(JOB))
    w._ask_ai_for("1")
    dlg = w._chat_dialogs["1"]
    assert isinstance(dlg, JobChatDialog)
    assert dlg.parent() is w                     # the anti-leak contract
    assert dlg.isVisible()


def test_ask_ai_for_reuses_the_dialog_already_open_for_that_job(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w, "_job_payload", lambda jid: dict(JOB))
    w._ask_ai_for("1")
    first = w._chat_dialogs["1"]
    w._ask_ai_for("1")
    assert w._chat_dialogs["1"] is first
    assert len(w.findChildren(JobChatDialog)) == 1


def test_closing_the_dialog_drops_the_main_window_reference(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w, "_job_payload", lambda jid: dict(JOB))
    w._ask_ai_for("1")
    w._chat_dialogs["1"].close()
    assert "1" not in w._chat_dialogs        # no stale wrapper over a deleted object


def test_ask_ai_for_falls_back_to_the_master_row(qtbot, monkeypatch):
    """A tracker-only row isn't in the loaded frames, but the master CSV has its JD."""
    w = _win(qtbot)
    monkeypatch.setattr(mw.jobsdata, "master_row", lambda jid: {
        "job_posting_id": jid, "company_name": "Globex", "job_title": "Engineer",
        "job_description_formatted": "x" * 200, "url": "http://x"})
    w._ask_ai_for("9")
    assert "Globex" in w._chat_dialogs["9"].windowTitle()


def test_ask_ai_for_opens_even_for_a_job_it_knows_nothing_about(qtbot, monkeypatch):
    """An untailored, unknown job still degrades to a usable chat — never an error."""
    w = _win(qtbot)
    monkeypatch.setattr(mw.jobsdata, "master_row", lambda jid: {})
    w._ask_ai_for("404")
    assert "404" in w._chat_dialogs


def test_ask_ai_for_seeds_the_engine_auth_env(qtbot, monkeypatch):
    """Same pre-flight the cover-letter path does: the chat makes a real LLM call."""
    w = _win(qtbot)
    monkeypatch.setattr(w, "_job_payload", lambda jid: dict(JOB))
    seeded = []
    monkeypatch.setattr(w, "_apply_auth_env", lambda: seeded.append(True))
    w._ask_ai_for("1")
    assert seeded == [True]


def test_apply_panel_ask_ai_opens_the_dialog_for_the_panel_job(qtbot, monkeypatch):
    w = _win(qtbot)
    monkeypatch.setattr(w, "_job_payload", lambda jid: dict(JOB))
    w._finish_apply_ok({"job": {"job_posting_id": "1", "company": "Acme",
                                "title": "Data Analyst"},
                        "apply_md": "# sheet", "resume_pdf": "", "generated_dir": ""})
    w.apply_panel.ask_ai_btn.click()
    assert isinstance(w._chat_dialogs.get("1"), JobChatDialog)


def test_apply_panel_ask_ai_without_a_job_is_a_noop(qtbot):
    w = _win(qtbot)
    w._ask_ai_from_panel()
    assert w._chat_dialogs == {}
