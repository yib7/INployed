"""Cycle 40 Phase 3e: the auto-apply structured details panel + header chrome."""
import apply_queue
from qt import apply_queue_panel as aqp
from qt.apply_queue_panel import ApplyQueuePanel


def _qfile(tmp_path):
    return tmp_path / "queue.json"


def _panel(qtbot, qfile, **kw):
    p = ApplyQueuePanel(queue_path=qfile, **kw)
    qtbot.addWidget(p)
    return p


def _entry(jid="1", status="needs_human", **over):
    e = apply_queue.new_entry(jid, company="Acme", title="Analyst",
                              apply_url=f"https://x/{jid}")
    e["status"] = status
    e.update(over)
    return e


def test_details_panel_structured_and_plain_text(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry(
        "1", company="Acme", title="Analyst", apply_url="https://x/1"), path=qfile)
    apply_queue.add_missing("1", "Salary expectation?", path=qfile)
    apply_queue.finish("1", "needs_human", notes="paused for one answer",
                       path=qfile)
    p = _panel(qtbot, qfile)
    p.table.selectRow(0)
    # structured pieces
    assert "Acme" in p.details.title_label.text()
    assert p.details.status_pill.text() == "Needs review"
    assert not p.details.callout.isHidden()             # warning callout shown
    assert "Salary expectation?" in p.details.callout_label.text()
    # plain-text mirror keeps the old pane's composition (test contract)
    text = p.details.toPlainText()
    assert "Salary expectation?" in text and "needs_human" in text


def test_details_panel_empty_state(qtbot, tmp_path):
    p = _panel(qtbot, _qfile(tmp_path))
    assert p.details.toPlainText() == ""
    assert p.details._content.isHidden()
    assert not p.details.empty_label.isHidden()


def test_answer_now_fires_injected_callback(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(_entry(), path=qfile)
    apply_queue.add_missing("1", "Years of Kubernetes?", path=qfile)
    fired = []
    p = _panel(qtbot, qfile, on_answer_now=lambda: fired.append(True))
    p.table.selectRow(0)
    p.details.answer_now_btn.click()
    assert fired == [True]


def test_status_chips_show_counts(qtbot, tmp_path):
    qfile = _qfile(tmp_path)
    apply_queue.enqueue(_entry("1", "queued"), path=qfile)
    apply_queue.enqueue(_entry("2", "queued"), path=qfile)
    apply_queue.enqueue(_entry("3", "needs_human"), path=qfile)
    p = _panel(qtbot, qfile)
    assert p.status_chips.chip("queued").count() == 2
    assert p.status_chips.chip("needs_human").count() == 1
    assert not p.status_chips.chip("queued").isCheckable()  # informational only
    # the caption's text contract is unchanged
    assert "queued: 2" in p.counts_label.text()
    assert "total: 3" in p.counts_label.text()


def test_pw_pill_mirrors_password_state(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(aqp, "_default_password_exists", lambda: False)
    p = _panel(qtbot, _qfile(tmp_path))
    assert p.pw_pill.text() == "NOT SET"
    assert p.pw_label.text() == "Master password: NOT SET"   # contract intact
    monkeypatch.setattr(aqp, "_default_password_exists", lambda: True)
    p.refresh_password_state()
    assert p.pw_pill.text() == "SET"
    assert p.pw_label.text() == "Master password: SET"
