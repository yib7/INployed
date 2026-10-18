"""Markdown viewers never fetch an image a document names.

The apply sheet and the Ask AI transcript render markdown that carries scraped
text (the job title is the sheet's heading) and model text over a scraped
posting. `MarkdownNoHTML` stops an `<img>` tag, but markdown's own `![](url)`
still makes `QTextBrowser.loadResource` open the URL, and on Windows a
`file://host/share/x.png` is a UNC path: opening it connects to that host over
SMB with the user's Windows sign-in. Both viewers refuse every resource.
"""
from PySide6 import QtCore, QtGui, QtWidgets

from qt.apply_panel import SheetViewer
from qt.plaintext import ReaderBrowser

IMAGE = int(QtGui.QTextDocument.ResourceType.ImageResource)


def _png(tmp_path):
    path = tmp_path / "x.png"
    img = QtGui.QImage(4, 4, QtGui.QImage.Format.Format_RGB32)
    img.fill(QtGui.QColor("red"))
    assert img.save(str(path))
    return QtCore.QUrl.fromLocalFile(str(path))


def _rendered(browser, qtbot, url):
    """`browser` showing a markdown image of `url`: True when it loaded the
    image's pixels (a refused one is an empty image or empty bytes)."""
    qtbot.addWidget(browser)
    browser.resize(400, 300)
    browser.setMarkdown(f"# Data Analyst ![]({url.toString()})\n\ntext\n")
    browser.show()
    browser.document().size()           # lays the document out, which asks for the image
    QtWidgets.QApplication.processEvents()
    got = browser.document().resource(IMAGE, url)
    if isinstance(got, (QtGui.QImage, QtGui.QPixmap)):
        return not got.isNull()
    return bool(got)


def test_a_plain_text_browser_does_load_the_image(qtbot, tmp_path):
    """The control: what the base class does with the same document."""
    browser = QtWidgets.QTextBrowser()
    assert _rendered(browser, qtbot, _png(tmp_path)) is True


def test_the_reader_browser_loads_no_resource(qtbot, tmp_path):
    browser = ReaderBrowser()
    assert browser.openLinks() is False and browser.openExternalLinks() is False
    for url in ("file://attacker.example/s/x.png", "file:///C:/Windows/win.ini",
                "https://attacker.example/p.png", "x.png"):
        assert browser.loadResource(IMAGE, QtCore.QUrl(url)).isNull()
    assert _rendered(browser, qtbot, _png(tmp_path)) is False


def test_the_apply_sheet_viewer_loads_no_resource(qtbot, tmp_path):
    viewer = SheetViewer()
    assert isinstance(viewer, ReaderBrowser)
    assert _rendered(viewer, qtbot, _png(tmp_path)) is False
    assert "Data Analyst" in viewer.toPlainText()


def test_the_chat_transcript_is_a_reader_browser(qtbot):
    from qt.chat_dialog import JobChatDialog
    host = QtWidgets.QWidget()
    qtbot.addWidget(host)
    dlg = JobChatDialog({"job_posting_id": "1", "company_name": "Acme",
                         "job_title": "Data Analyst"}, parent=host)
    assert isinstance(dlg.transcript, ReaderBrowser)
