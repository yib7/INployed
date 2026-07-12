"""Row delegate for the job tables (restyle cycle 40, Phase 2).

`JobRowDelegate` paints EVERY cell itself and never calls `super().paint()`:
under Fusion + a stylesheet, `CE_ItemViewItem` re-enters the QSS path and
repaints the palette Highlight (solid accent) and alternate-base fills OVER
whatever the delegate drew — that was the selection bug (a selected green
"apply" row turned solid blue). Full custom paint means selection *adds* to
the category color instead of replacing it:

  PANEL fill -> category tint (row-tint alpha; selected alpha when selected)
  -> 1px BORDER_SOFT bottom separator -> selected rows get 1px top+bottom
  accent lines -> 3px category stripe on the first VISIBLE column -> content.

Content renderers key on the table's column ids (score badge, deep-score
mini-bar, recommendation/status pills, follow-up state, "Open ↗" links, mono
numerics/dates). Pill labels are paint-time only — DisplayRole always keeps
the raw text (test-coupled: sorting, details panes, and the apply-queue tests
all read the raw values).

The row's category comes from TAG_ROLE (UserRole + 2; SORT_ROLE is +1), which
both JobsTableModel and the apply-queue table provide; sort proxies forward
custom roles automatically. Fonts derive from `option.font` (family/weight
swaps only) so they track the live ui-scale with no re-polish.
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from qt import theme

# Paint-time row tag. jobs_model.SORT_ROLE is UserRole + 1.
TAG_ROLE = QtCore.Qt.ItemDataRole.UserRole + 2

# Apply-queue statuses -> semantic families (the queue table's TAG_ROLE carries
# the RAW status string, not a jobs-model row tag).
STATUS_TAGS = {
    "tailoring": "accent",
    "in_progress": "accent",
    "queued": "neutral",
    "ready_to_submit": "success",
    "submitted": "success",
    "needs_human": "warning",
    "failed": "danger",
}

# Humanized pill labels (paint-time only; DisplayRole stays the raw status).
STATUS_LABELS = {
    "needs_human": "Needs review",
    "in_progress": "In progress",
    "ready_to_submit": "Ready to submit",
}

# Recommendation pill labels; the row tag overrides the reco when a tailored
# resume exists / the last tailor run failed.
_RECO_LABELS = {"apply": "Apply", "consider": "Consider", "skip": "Don't consider"}
_RECO_FAMILY = {"apply": "success", "consider": "warning", "skip": "neutral"}
_TAG_PILLS = {"has_resume": ("Tailored", "accent"),
              "tailor_failed": ("Tailor failed", "danger")}

_NUMERIC_RIGHT = frozenset({"applicants", "days", "attempts", "missing"})
_DATE_COLS = frozenset({"extracted_date", "job_posted_date", "status_date",
                        "applied_date", "updated"})

_SELECTED_LINE = ("#4c8dff", 0.65)   # 1px top+bottom lines on selected rows
_TRACK = ("#ffffff", 0.08)           # deep-score mini-bar track
_PAD = 9                             # horizontal cell padding @100%


class JobRowDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, column_ids: list[str], *, kind: str = "jobs", parent=None):
        super().__init__(parent)
        self._column_ids = list(column_ids)
        self._kind = kind                      # "jobs" | "tracker" | "apply"
        self._fonts: dict[tuple, QtGui.QFont] = {}
        self._colors: dict[tuple, QtGui.QColor] = {}

    # ---- shared lookups -------------------------------------------------------

    def _color(self, hex_color: str, a: float | None = None) -> QtGui.QColor:
        key = (hex_color, a)
        c = self._colors.get(key)
        if c is None:
            c = self._colors[key] = theme.qcolor(hex_color, a)
        return c

    def _font(self, base: QtGui.QFont, kind: str) -> QtGui.QFont:
        """Variant of the option font (family/weight/size-ratio swaps only, so
        every variant keeps tracking the live scale)."""
        key = (kind, round(base.pointSizeF() * 100))
        f = self._fonts.get(key)
        if f is not None:
            return f
        f = QtGui.QFont(base)
        if kind in ("mono", "mono_bold"):
            f.setFamilies(["Consolas", "Cascadia Mono"])
        if kind in ("bold", "mono_bold"):
            f.setWeight(QtGui.QFont.Weight.Bold)
        elif kind == "w600":
            f.setWeight(QtGui.QFont.Weight.DemiBold)
        elif kind == "pill":
            f.setPointSizeF(base.pointSizeF() * 0.9)
            f.setWeight(QtGui.QFont.Weight.DemiBold)
        self._fonts[key] = f
        return f

    def _family(self, tag: str) -> dict | None:
        """The semantic family for a row tag (or None for an untagged row)."""
        if not tag:
            return None
        sem = STATUS_TAGS.get(tag) if self._kind == "apply" else theme.TAG_SEMANTIC.get(tag)
        return theme.SEMANTICS.get(sem) if sem else None

    @staticmethod
    def _first_visible_column(widget) -> int:
        """Logical index of the leftmost visible column (stripe target),
        resolved per paint so the Columns… dialog stays compatible."""
        header = getattr(widget, "horizontalHeader", lambda: None)()
        if header is None:
            return 0
        for visual in range(header.count()):
            logical = header.logicalIndex(visual)
            if not header.isSectionHidden(logical):
                return logical
        return 0

    # ---- QStyledItemDelegate --------------------------------------------------

    def sizeHint(self, option, index):  # noqa: N802 (Qt naming)
        hint = super().sizeHint(option, index)
        widget = option.widget
        key = ("compact" if widget is not None
               and widget.property("rowSize") == "compact" else "row")
        hint.setHeight(round(theme.SIZES[key] * theme._current_scale))
        return hint

    def paint(self, painter, option, index):  # noqa: N802 (Qt naming)
        # NEVER call super().paint(): Fusion+QSS CE_ItemViewItem re-enters the
        # stylesheet path and repaints Highlight over the category tint.
        painter.save()
        rect = option.rect
        selected = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QtWidgets.QStyle.StateFlag.State_MouseOver)
        tag = str(index.data(TAG_ROLE) or "")
        family = self._family(tag)

        # 1. Base fill + category tint (selection ADDS, never replaces).
        painter.fillRect(rect, self._color(theme.PANEL))
        if family is not None:
            tint = family.get("tint_base", family["base"])
            painter.fillRect(rect, self._color(
                tint, family["sel_a" if selected else "tint_a"]))
        elif selected:
            # Untagged rows: neutral accent-style selection treatment.
            acc = theme.SEMANTICS["accent"]
            painter.fillRect(rect, self._color(acc["base"], acc["sel_a"]))
        if hovered and not selected:
            painter.fillRect(rect, self._color("#ffffff", 0.03))

        # 2. 1px bottom separator (the grid is off).
        painter.fillRect(QtCore.QRect(rect.left(), rect.bottom(), rect.width(), 1),
                         self._color(theme.BORDER_SOFT))

        # 3. Selected rows: 1px top+bottom accent lines across every cell.
        if selected:
            line = self._color(*_SELECTED_LINE)
            painter.fillRect(QtCore.QRect(rect.left(), rect.top(), rect.width(), 1), line)
            painter.fillRect(QtCore.QRect(rect.left(), rect.bottom(), rect.width(), 1), line)

        # 4. 3px category stripe on the first VISIBLE column.
        if (family is not None
                and index.column() == self._first_visible_column(option.widget)):
            painter.fillRect(QtCore.QRect(rect.left(), rect.top(), 3, rect.height()),
                             self._color(family["base"]))

        # 5. Content.
        self._paint_content(painter, option, index, selected, family)
        painter.restore()

    # ---- content renderers ------------------------------------------------------

    def _paint_content(self, painter, option, index, selected, family):
        col = index.column()
        cid = self._column_ids[col] if col < len(self._column_ids) else ""
        text = str(index.data(QtCore.Qt.ItemDataRole.DisplayRole) or "")
        s = theme._current_scale
        rect = option.rect.adjusted(round(_PAD * s), 0, -round(_PAD * s), 0)
        font = option.font

        if cid == "score":
            self._paint_score_badge(painter, rect, text, font, s)
        elif cid == "deep_score":
            self._paint_deep_bar(painter, rect, text, font, s, family)
        elif cid == "recommendation":
            self._paint_reco_pill(painter, rect, text, font, s,
                                  str(index.data(TAG_ROLE) or ""))
        elif cid == "status":
            self._paint_status_pill(painter, rect, text, font, s)
        elif cid == "follow_up":
            self._paint_follow_up(painter, rect, text, font)
        elif cid == "url":
            if text.strip():
                self._draw_text(painter, rect, "Open ↗", font,
                                self._color(theme.ACCENT))
        elif cid in _NUMERIC_RIGHT:
            self._draw_text(painter, rect, text, self._font(font, "mono"),
                            self._color(theme.TEXT_SECONDARY),
                            align=QtCore.Qt.AlignmentFlag.AlignRight)
        elif cid in _DATE_COLS:
            self._draw_text(painter, rect, text, self._font(font, "mono"),
                            self._color(theme.MUTED))
        elif cid in ("company_name", "company"):
            self._draw_text(painter, rect, text, font,
                            self._color(theme.TEXT_SECONDARY))
        elif cid in ("job_title", "title"):
            f = self._font(font, "w600") if selected else font
            self._draw_text(painter, rect, text, f, self._color(theme.TEXT))
        else:
            self._draw_text(painter, rect, text, font, self._color(theme.TEXT))

    def _draw_text(self, painter, rect, text, font, color,
                   align=QtCore.Qt.AlignmentFlag.AlignLeft):
        if not text:
            return
        painter.setFont(font)
        painter.setPen(color)
        fm = QtGui.QFontMetrics(font)
        elided = fm.elidedText(text, QtCore.Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, align | QtCore.Qt.AlignmentFlag.AlignVCenter, elided)

    def _paint_score_badge(self, painter, rect, text, font, s):
        try:
            score = int(float(text))
        except (TypeError, ValueError):
            self._draw_text(painter, rect, text, self._font(font, "mono"),
                            self._color(theme.MUTED))
            return
        badge = theme.SCORE_BADGES.get(score)
        if badge is None:
            self._draw_text(painter, rect, text, self._font(font, "mono"),
                            self._color(theme.TEXT_SECONDARY))
            return
        color_hex, bg_a = badge
        w, h = round(22 * s), round(20 * s)
        box = QtCore.QRectF(rect.left(), rect.center().y() - h / 2 + 1, w, h)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(self._color(color_hex, bg_a))
        r = theme.RADII["badge"] * s
        painter.drawRoundedRect(box, r, r)
        painter.setPen(self._color(color_hex))
        painter.setFont(self._font(font, "mono_bold"))
        painter.drawText(box, QtCore.Qt.AlignmentFlag.AlignCenter, str(score))

    def _paint_deep_bar(self, painter, rect, text, font, s, family):
        try:
            value = float(text)
        except (TypeError, ValueError):
            self._draw_text(painter, rect, "—", self._font(font, "mono"),
                            self._color(theme.MUTED))
            return
        label = f"{value:g}"
        mono = self._font(font, "mono")
        num_w = QtGui.QFontMetrics(mono).horizontalAdvance(label)
        gap = round(6 * s)
        # The bar yields to the numeral in a narrow column (Deep is ~70px):
        # the value must never be clipped away by the track.
        bw = max(round(16 * s), min(round(56 * s), rect.width() - num_w - gap))
        bh = max(2, round(6 * s))
        r = 3 * s
        track = QtCore.QRectF(rect.left(), rect.center().y() - bh / 2 + 1, bw, bh)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(self._color(*_TRACK))
        painter.drawRoundedRect(track, r, r)
        frac = max(0.0, min(1.0, value / 10.0))
        if frac > 0:
            fill_hex = family["base"] if family else theme.SEMANTICS["neutral"]["base"]
            painter.setBrush(self._color(fill_hex))
            painter.drawRoundedRect(
                QtCore.QRectF(track.left(), track.top(), track.width() * frac, bh), r, r)
        num_rect = QtCore.QRect(rect.left() + bw + gap, rect.top(),
                                max(0, rect.width() - bw - gap), rect.height())
        self._draw_text(painter, num_rect, label, mono,
                        self._color(theme.TEXT_SECONDARY))

    def _paint_pill(self, painter, rect, label, font, s, family):
        pill_font = self._font(font, "pill")
        fm = QtGui.QFontMetrics(pill_font)
        h = min(rect.height() - round(6 * s), fm.height() + round(6 * s))
        w = min(rect.width(), fm.horizontalAdvance(label) + round(16 * s))
        box = QtCore.QRectF(rect.left(), rect.center().y() - h / 2 + 1, w, h)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        bg = family.get("tint_base", family["base"])
        painter.setBrush(self._color(bg, family["pill_a"]))
        painter.drawRoundedRect(box, h / 2, h / 2)
        painter.setPen(self._color(family["pill_fg"]))
        painter.setFont(pill_font)
        elided = fm.elidedText(label, QtCore.Qt.TextElideMode.ElideRight,
                               round(w - 12 * s))
        painter.drawText(box, QtCore.Qt.AlignmentFlag.AlignCenter, elided)
        return box

    def _paint_reco_pill(self, painter, rect, text, font, s, tag):
        # Row tag overrides the reco: Tailored / Tailor failed pills.
        override = _TAG_PILLS.get(tag)
        if override is not None:
            label, sem = override
        else:
            reco = text.strip().lower()
            if reco not in _RECO_LABELS:
                self._draw_text(painter, rect, text, font, self._color(theme.MUTED))
                return
            label, sem = _RECO_LABELS[reco], _RECO_FAMILY[reco]
        self._paint_pill(painter, rect, label, font, s, theme.SEMANTICS[sem])

    def _paint_status_pill(self, painter, rect, text, font, s):
        status = text.strip().lower()
        if not status:
            return
        if self._kind == "apply":
            sem = STATUS_TAGS.get(status, "neutral")
            label = STATUS_LABELS.get(status, status.capitalize())
            family = theme.SEMANTICS[sem]
            # Leading 6px status dot inside the pill's left edge.
            dot = round(6 * s)
            box = self._paint_pill(painter, rect.adjusted(dot + round(6 * s), 0, 0, 0),
                                   label, font, s, family)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(self._color(family["base"]))
            painter.drawEllipse(QtCore.QRectF(
                rect.left(), box.center().y() - dot / 2, dot, dot))
            return
        sem = theme.TAG_SEMANTIC.get(status, "neutral")
        self._paint_pill(painter, rect, status.capitalize(), font, s,
                         theme.SEMANTICS[sem])

    def _paint_follow_up(self, painter, rect, text, font):
        state = text.strip()
        if state.upper() == "DUE":
            fam = theme.SEMANTICS["followup"]
            self._draw_text(painter, rect, "● Due now", self._font(font, "w600"),
                            self._color(fam["pill_fg"]))
        elif state.lower() == "done":
            fam = theme.SEMANTICS["followup_sent"]
            self._draw_text(painter, rect, "Sent", font, self._color(fam["pill_fg"]))
        else:
            self._draw_text(painter, rect, "—", font, self._color(theme.MUTED))
