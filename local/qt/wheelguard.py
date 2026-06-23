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


class WheelGuard(QtCore.QObject):
    """Application event filter that neutralizes wheel-over on unfocused controls."""

    def eventFilter(self, obj, event):  # noqa: N802 - Qt override name
        if (event.type() == QtCore.QEvent.Type.Wheel
                and isinstance(obj, _GUARDED) and not obj.hasFocus()):
            sa = _scroll_ancestor(obj)
            if sa is not None:
                # Let the wheel scroll the surrounding page instead of the control.
                QtWidgets.QApplication.sendEvent(sa.viewport(), event)
            return True  # never let an unfocused control eat the wheel
        return False


def install(app: QtWidgets.QApplication) -> WheelGuard:
    """Install one app-wide wheel guard and keep a reference alive on ``app``."""
    guard = WheelGuard(app)
    app.installEventFilter(guard)
    app._wheel_guard = guard  # keep it from being garbage-collected
    return guard
