"""Text that shows as written: no HTML rendering of scraped or page text.

Qt guesses a string's format by default (`Qt::mightBeRichText`), so a job
title, a parked question or an error that happens to start like a tag renders
as HTML, and an `<img src=...>` there loads a file with no click. Every label
in the dashboard is a `Label` (plain text from birth); a label that shows
markup we compose sets RichText itself and escapes what it inserts. Message
box and tooltip text goes through `literal()`.
"""
from __future__ import annotations

import html

from PySide6 import QtCore, QtGui, QtWidgets

PLAIN = QtCore.Qt.TextFormat.PlainText
RICH = QtCore.Qt.TextFormat.RichText


class Label(QtWidgets.QLabel):
    """A QLabel that shows its text as written."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setTextFormat(PLAIN)


def literal(text) -> str:
    """`text` for a message box or a tooltip, both of which guess the format:
    unchanged when Qt would show it as plain text, else escaped inside a rich
    text block that keeps its line breaks."""
    text = "" if text is None else str(text)
    if not QtGui.Qt.mightBeRichText(text):
        return text
    return ('<qt><p style="white-space:pre-wrap">'
            + html.escape(text, quote=False) + "</p></qt>")
