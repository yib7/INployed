"""Sign the `claude` CLI back in from the dashboard.

The tailor's Claude lane runs `claude -p`, and once the CLI's sign-in lapses
every call fails with kind "signed_out". The dialog for that failure carries a
Sign in to Claude button: it opens a new PowerShell console running
`claude auth login`, the CLI's own sign-in, which continues in the browser. No
credential passes through the dashboard.
"""
from __future__ import annotations

import base64
import subprocess

from PySide6 import QtWidgets

SIGNIN_COMMAND = "claude auth login"
SIGNIN_BUTTON = "Sign in to Claude"
SIGNIN_HINT = (
    f"Click {SIGNIN_BUTTON} to open a console that runs `{SIGNIN_COMMAND}`, finish "
    "the sign-in in the browser it opens, then tailor again. Without the button: "
    f"open a terminal and run `{SIGNIN_COMMAND}`."
)


def signin_argv() -> list[str]:
    """The argv that opens a new PowerShell console running SIGNIN_COMMAND.
    -NoExit keeps the console open so the user sees how the sign-in ended."""
    encoded = base64.b64encode(SIGNIN_COMMAND.encode("utf-16-le")).decode("ascii")
    return ["powershell", "-NoExit", "-EncodedCommand", encoded]


def open_signin() -> bool:
    """Start the sign-in console. False when it could not be started."""
    try:
        subprocess.Popen(signin_argv(),
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    except OSError:
        return False
    return True


def warn_with_signin(parent, title: str, text: str) -> bool:
    """A warning dialog with a Sign in to Claude button beside OK. True when the
    user pressed the button (the console is opened then)."""
    box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Icon.Warning, title, text,
                                QtWidgets.QMessageBox.StandardButton.Ok, parent)
    signin = box.addButton(SIGNIN_BUTTON, QtWidgets.QMessageBox.ButtonRole.ActionRole)
    box.exec()
    if box.clickedButton() is not signin:
        return False
    if not open_signin():
        QtWidgets.QMessageBox.warning(
            parent, title, f"Could not open a console. Open a terminal and run "
            f"`{SIGNIN_COMMAND}`.")
    return True
