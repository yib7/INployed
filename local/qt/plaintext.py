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


class ReaderBrowser(QtWidgets.QTextBrowser):
    """A read-only markdown viewer that follows no link and loads no resource.

    The apply sheet and the Ask AI transcript render scraped and model text.
    `MarkdownNoHTML` stops an `<img>` tag, but markdown's own `![](url)` still
    asks `loadResource` for the URL, and on Windows a `file://host/share/x.png`
    is a UNC path: opening it connects to that host over SMB with the user's
    Windows sign-in. Nothing these viewers show needs an image, so every
    resource is refused. The refusal is an empty value, never None: on None
    the document falls back to reading a local or UNC file itself."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setOpenLinks(False)          # reader only: a link click goes nowhere
        self.setOpenExternalLinks(False)

    def loadResource(self, kind, name):  # noqa: N802 (Qt override)
        if kind == int(QtGui.QTextDocument.ResourceType.ImageResource):
            return QtGui.QImage()
        return QtCore.QByteArray()
