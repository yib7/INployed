"""Cycle 40 Phase 3a: the chrome widgets — Pill / Chip / ChipBar / IdentityStrip."""
from qt.chrome import Chip, ChipBar, IdentityStrip, Pill


def test_pill_text_and_family(qtbot):
    p = Pill("Applied", "accent")
    qtbot.addWidget(p)
    assert p.text() == "Applied"
    hint = p.sizeHint()
    assert hint.width() > 0 and hint.height() > 0
    p.set_family("success")          # no crash; repaint path only
    p.setText("Offer")
    assert p.sizeHint().width() > 0


def test_chip_count_suffix_and_toggle(qtbot):
    c = Chip("Applied", dot="#4c8dff")
    qtbot.addWidget(c)
    assert c.isCheckable()
    assert c.count() is None
    w0 = c.sizeHint().width()
    c.set_count(12)
    assert c.count() == 12
    assert c.sizeHint().width() > w0     # the count suffix widens the chip
    c.setChecked(True)
    assert c.isChecked()


def test_chipbar_exclusive_selection_fires_on_change(qtbot):
    fired = []
    bar = ChipBar([("all", "All", None), ("applied", "Applied", "#4c8dff"),
                   ("offer", "Offer", "#3fb950")], on_change=fired.append)
    qtbot.addWidget(bar)
    bar.chip("all").setChecked(True)
    assert bar.checked_key() == "all" and fired == ["all"]
    bar.chip("applied").click()
    assert bar.checked_key() == "applied"        # exclusive: All unchecked
    assert not bar.chip("all").isChecked()
    assert fired == ["all", "applied"]


def test_chipbar_set_checked_is_silent(qtbot):
    fired = []
    bar = ChipBar([("a", "A", None), ("b", "B", None)], on_change=fired.append)
    qtbot.addWidget(bar)
    bar.set_checked("b")                          # mirrors external state
    assert bar.checked_key() == "b"
    assert fired == []                            # no on_change fired


def test_chipbar_counts(qtbot):
    bar = ChipBar([("a", "A", None), ("b", "B", None)])
    qtbot.addWidget(bar)
    bar.set_counts({"a": 3})
    assert bar.chip("a").count() == 3
    assert bar.chip("b").count() is None          # untouched keys stay unset


def test_identity_strip_counts_and_freshness(qtbot):
    strip = IdentityStrip()
    qtbot.addWidget(strip)
    strip.set_counts(27, 22, 8)
    assert strip.jobs_badge.value() == 27
    assert "27" in strip.jobs_badge.text() and "jobs" in strip.jobs_badge.text()
    assert strip.unseen_badge.value() == 22
    assert strip.tracked_badge.value() == 8
    strip.set_freshness("fresh", "Fresh — last run 2h ago")
    assert strip.freshness.text() == "Fresh — last run 2h ago"
    strip.set_freshness("stale", "Stale — last run 3d ago")
    assert "Stale" in strip.freshness.text()
    assert "INployed".replace("IN", "") in strip.wordmark.text()  # wordmark markup
