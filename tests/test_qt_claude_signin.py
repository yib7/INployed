"""The Sign in to Claude button on a tailor failure from a signed-out CLI.

Nothing here starts a real console: `subprocess.Popen` and the dialog's
`exec` are faked.
"""
import base64
from unittest.mock import MagicMock

from PySide6 import QtWidgets

from qt import claude_signin
from qt.main_window import MainWindow


def _decode(argv):
    return base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")


def test_signin_argv_opens_a_console_running_claude_auth_login(monkeypatch):
    monkeypatch.setattr(claude_signin.shutil, "which", lambda name: None)
    argv = claude_signin.signin_argv()
    assert argv[0] == "powershell" and "-NoExit" in argv
    assert _decode(argv) == "claude auth login"


def test_the_console_runs_the_cli_by_its_path_not_the_ps1_shim(monkeypatch):
    """A bare `claude` in PowerShell resolves to npm's claude.ps1, which the
    default execution policy refuses; the path `which` finds is claude.cmd."""
    path = r"C:\Users\o'brien\AppData\Roaming\npm\claude.CMD"
    monkeypatch.setattr(claude_signin.shutil, "which", lambda name: path)
    assert _decode(claude_signin.signin_argv()) == (
        "& 'C:\\Users\\o''brien\\AppData\\Roaming\\npm\\claude.CMD' auth login")


def test_open_signin_starts_a_new_console(monkeypatch):
    monkeypatch.setattr(claude_signin.shutil, "which", lambda name: None)
    seen = {}
    monkeypatch.setattr(claude_signin.subprocess, "Popen",
                        lambda argv, **k: seen.update(argv=argv, **k))
    assert claude_signin.open_signin() is True
    assert _decode(seen["argv"]) == "claude auth login"
    assert seen["creationflags"] == getattr(claude_signin.subprocess, "CREATE_NEW_CONSOLE", 0)


def test_open_signin_reports_a_console_that_would_not_start(monkeypatch):
    def boom(argv, **k):
        raise OSError("no powershell")

    monkeypatch.setattr(claude_signin.subprocess, "Popen", boom)
    assert claude_signin.open_signin() is False


def _press(monkeypatch, label):
    def fake_exec(box):
        [b for b in box.buttons() if b.text() == label][0].click()
        return 0

    monkeypatch.setattr(QtWidgets.QMessageBox, "exec", fake_exec)


def test_the_button_opens_the_sign_in_console(qtbot, monkeypatch):
    opened = []
    monkeypatch.setattr(claude_signin, "open_signin", lambda: opened.append(1) or True)
    _press(monkeypatch, claude_signin.SIGNIN_BUTTON)
    assert claude_signin.warn_with_signin(None, "Tailor resume", "signed out") is True
    assert opened == [1]


def test_ok_closes_the_dialog_without_a_console(qtbot, monkeypatch):
    opened = []
    monkeypatch.setattr(claude_signin, "open_signin", lambda: opened.append(1) or True)
    _press(monkeypatch, "OK")
    assert claude_signin.warn_with_signin(None, "Tailor resume", "signed out") is False
    assert opened == []


def _win(qtbot):
    reg = MagicMock()
    reg.resume_paths.return_value = {}
    reg.status_rows.return_value = []
    w = MainWindow(csv_paths=[], registry=reg)
    qtbot.addWidget(w)
    w.reload_data_async = lambda: None
    return w


def test_a_signed_out_tailor_failure_offers_the_sign_in_button(qtbot, monkeypatch):
    w = _win(qtbot)
    offered = {}
    monkeypatch.setattr(claude_signin, "warn_with_signin",
                        lambda parent, title, text: offered.setdefault("text", text))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: offered.setdefault("plain", a[2])))
    w._finish_tailor([{"id": "2", "label": "Dev @ B", "dir": None, "kind": "signed_out",
                       "error": "The `claude` CLI is signed out."}])
    assert "plain" not in offered
    assert claude_signin.SIGNIN_BUTTON in offered["text"]
    assert "claude auth login" in offered["text"]


def test_other_tailor_failures_keep_the_plain_dialog(qtbot, monkeypatch):
    w = _win(qtbot)
    shown = {}
    monkeypatch.setattr(claude_signin, "warn_with_signin",
                        lambda *a: shown.setdefault("signin", 1))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.setdefault("plain", a[2])))
    w._finish_tailor([{"id": "2", "label": "Dev @ B", "dir": None, "error": "429 quota"}])
    assert "signin" not in shown and "429 quota" in shown["plain"]


def test_a_signed_out_batch_error_offers_the_sign_in_button(qtbot, monkeypatch):
    w = _win(qtbot)
    offered = {}
    monkeypatch.setattr(claude_signin, "warn_with_signin",
                        lambda parent, title, text: offered.setdefault("text", text))
    exc = RuntimeError("The `claude` CLI is signed out.")
    exc.kind = "signed_out"
    w._finish_tailor_error(exc)
    assert claude_signin.SIGNIN_BUTTON in offered["text"]


def test_the_tailor_result_carries_the_failure_kind(qtbot, monkeypatch):
    w = _win(qtbot)

    def fake_tailor(job, **k):
        exc = RuntimeError("The `claude` CLI is signed out.")
        exc.kind = "signed_out"
        raise exc

    monkeypatch.setattr("resume_tailor.tailor", fake_tailor, raising=False)
    [result] = w._tailor_work([{"job_posting_id": "1", "company_name": "C", "job_title": "T"}],
                              {"cover_letter": False, "ats_report": False,
                               "prep_sheet": False, "tone": "professional"})
    assert result["kind"] == "signed_out" and result["dir"] is None


def test_a_signed_out_cover_letter_offers_the_sign_in_button(qtbot, monkeypatch):
    w = _win(qtbot)
    offered = {}
    monkeypatch.setattr(claude_signin, "warn_with_signin",
                        lambda parent, title, text: offered.update(title=title, text=text))
    exc = RuntimeError("The `claude` CLI is signed out.")
    exc.kind = "signed_out"
    w._finish_cover_error(exc)
    assert offered["title"] == "Cover letter"
    assert claude_signin.SIGNIN_BUTTON in offered["text"]
