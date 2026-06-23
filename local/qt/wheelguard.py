"""App-wide wheel guard: stop the mouse wheel from silently editing a control.

Qt's ``QComboBox``, ``QAbstractSpinBox`` (spin boxes / ``QDateEdit``) and
``QSlider`` consume wheel events to change their *value* even when they don't
have keyboard focus. Inside a scroll area that means scrolling the page while the
cursor happens to pass over one of these controls silently edits it — e.g. flips
the Gemini-model dropdown to a different model, or nudges a spend-guard slider.
The old Tk ``ttk`` widgets never did this, so the port regressed it.

Installed once on the ``QApplication`` (see ``install``), this guard swallows
wheel events on those controls when they are *not* focused and forwards the wheel
to the nearest scroll area, so the page scrolls instead of the control changing.
To change a value on purpose you click the control first (giving it focus), or
use its dropdown / arrows.

An *editable* ``QComboBox`` (the Gemini model selectors) delivers the wheel to
its inner ``QLineEdit``, not the combo itself, and an application event filter
only sees the original receiver — never the parents the wheel later propagates
to. So the guard walks up from whatever widget got the wheel to find the guarded
control above it, otherwise editable combos slip through and scroll-edit silently.
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

# Controls whose value changes on a stray wheel-over.
_GUARDED = (QtWidgets.QComboBox, QtWidgets.QAbstractSpinBox, QtWidgets.QSlider)


def _scroll_ancestor(w: QtWidgets.QWidget) -> QtWidgets.QAbstractScrollArea | None:
    """The nearest scrollable ancestor of ``w`` (so the page can scroll instead)."""
    p = w.parentWidget()
    while p is not None:
        if isinstance(p, QtWidgets.QAbstractScrollArea):
            return p
        p = p.parentWidget()
    return None


def _guarded_control(w: QtWidgets.QWidget) -> QtWidgets.QWidget | None:
    """The guarded control at or above ``w`` — handles an editable combo / spin box
    whose inner line-edit is the actual wheel receiver."""
    while w is not None:
        if isinstance(w, _GUARDED):
            return w
        w = w.parentWidget()
    return None


def _has_focus(ctrl: QtWidgets.QWidget) -> bool:
    """True if the control (or its inner editor, e.g. an editable combo's line edit)
    holds keyboard focus — i.e. the user is deliberately interacting with it."""
    if ctrl.hasFocus():
        return True
    focused = QtWidgets.QApplication.focusWidget()
    return focused is not None and ctrl.isAncestorOf(focused)


class WheelGuard(QtCore.QObject):
    """Application event filter that neutralizes wheel-over on unfocused controls."""

    def eventFilter(self, obj, event):  # noqa: N802 - Qt override name
        if event.type() != QtCore.QEvent.Type.Wheel or not isinstance(obj, QtWidgets.QWidget):
            return False
        ctrl = _guarded_control(obj)
        if ctrl is None or _has_focus(ctrl):
            return False  # not a guarded control, or focused -> deliberate, allow it
        # An open dropdown must keep the wheel so the popup list scrolls.
        if isinstance(ctrl, QtWidgets.QComboBox) and ctrl.view().isVisible():
            return False
        sa = _scroll_ancestor(ctrl)
        if sa is not None:
            # Let the wheel scroll the surrounding page instead of the control.
            QtWidgets.QApplication.sendEvent(sa.viewport(), event)
        return True  # never let an unfocused control eat the wheel


def install(app: QtWidgets.QApplication) -> WheelGuard:
    """Install one app-wide wheel guard and keep a reference alive on ``app``."""
    guard = WheelGuard(app)
    app.installEventFilter(guard)
    app._wheel_guard = guard  # keep it from being garbage-collected
    return guard
