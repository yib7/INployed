"""The job detail card (cycle 40, Phase 3b) — replaces the plain ScorePreview.

`JobDetailCard` is a `QFrame[card="true"]` that renders one selected job:
title + meta line, the action row (Open posting / Tailor / Apply — the main
window aliases `btn_tailor`/`btn_apply` to this card's buttons so every
existing enable/repolish path keeps its object identities), a chips row
(score pill, deep mini-bar chip, applicants / salary / posted), the REASON
lede, STRENGTHS/GAPS columns, and a collapsed "Show description" tertiary
toggle over the muted JD snippet (locked user decision: the snippet stays,
hidden by default).

On the Tracker tab the card switches to its tracker variant: status +
follow-up pills, a days-since chip, a synthesized NEXT STEP line, and
Open résumé PDF / Mark followed up actions (Tailor/Apply hide but keep
existing).

Data comes from `jobsdata.job_detail_fields(row, snapshot)`;
`toPlainText()` exposes the rendered content as plain text (test-coupled).
"""
from __future__ import annotations

import html
from typing import Callable

from PySide6 import QtCore, QtWidgets

from qt import theme
from qt.chrome import Pill

_EMPTY_TEXT = ("Select a job to see its score breakdown, strengths, gaps, "
               "and description.")

# Score pill color per score value (same palette as the table badges).
_SCORE_PILL = {s: c for s, (c, _a) in theme.SCORE_BADGES.items()}


class _DeepChip(QtWidgets.QWidget):
    """The outline "Deep <mini-bar> N" chip: 56x6 track, fill = value/10."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._value: float | None = None
        self._fill = theme.SEMANTICS["neutral"]["base"]
        theme.set_type_role(self, "caption")
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed,
                           QtWidgets.QSizePolicy.Policy.Fixed)

    def set_value(self, value: float | None, fill_hex: str | None = None) -> None:
        self._value = value
        self._fill = fill_hex or theme.SEMANTICS["neutral"]["base"]
        self.updateGeometry()
        self.update()

    def _mono(self):
        from PySide6 import QtGui
        f = QtGui.QFont(self.font())
        f.setFamilies(["Consolas", "Cascadia Mono"])
        return f

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802 (Qt naming)
        from PySide6 import QtGui
        s = theme._current_scale
        fm = QtGui.QFontMetrics(self.font())
        label = "" if self._value is None else f"{self._value:g}"
        w = (fm.horizontalAdvance("Deep") + round(56 * s) + round(16 * s)
             + QtGui.QFontMetrics(self._mono()).horizontalAdvance(label)
             + round(24 * s))
        return QtCore.QSize(w, fm.height() + round(6 * s))

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 (Qt naming)
        return self.sizeHint()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        from PySide6 import QtGui
        s = theme._current_scale
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        r = rect.height() / 2
        p.setPen(QtGui.QPen(theme.qcolor(theme.BORDER), 1))
        p.setBrush(theme.qcolor(theme.PANEL))
        p.drawRoundedRect(rect, r, r)
        x = rect.left() + round(10 * s)
        p.setPen(theme.qcolor(theme.MUTED))
        p.setFont(self.font())
        fm = QtGui.QFontMetrics(self.font())
        p.drawText(QtCore.QRectF(x, rect.top(), rect.width(), rect.height()),
                   QtCore.Qt.AlignmentFlag.AlignVCenter
                   | QtCore.Qt.AlignmentFlag.AlignLeft, "Deep")
        x += fm.horizontalAdvance("Deep") + round(8 * s)
        bw, bh = round(56 * s), max(2, round(6 * s))
        track = QtCore.QRectF(x, rect.center().y() - bh / 2, bw, bh)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor("#ffffff", 0.08))
        p.drawRoundedRect(track, 3 * s, 3 * s)
        if self._value is not None:
            frac = max(0.0, min(1.0, self._value / 10.0))
            if frac > 0:
                p.setBrush(theme.qcolor(self._fill))
                p.drawRoundedRect(QtCore.QRectF(track.left(), track.top(),
                                                track.width() * frac, bh),
                                  3 * s, 3 * s)
        x += bw + round(8 * s)
        p.setPen(theme.qcolor(theme.TEXT))
        p.setFont(self._mono())
        label = "—" if self._value is None else f"{self._value:g}"
        p.drawText(QtCore.QRectF(x, rect.top(), rect.right() - x, rect.height()),
                   QtCore.Qt.AlignmentFlag.AlignVCenter
                   | QtCore.Qt.AlignmentFlag.AlignLeft, label)
        p.end()


def _clear_layout(lay) -> None:
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            # Unparent BEFORE deleteLater: deferred deletes only run once the
            # event loop spins, and an orphan still parented to the card would
            # repaint at a stale/default geometry over the new content.
            w.hide()
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())


class JobDetailCard(QtWidgets.QFrame):
    def __init__(self, *, on_open: Callable[[str], None] | None = None,
                 on_tailor: Callable[[], None] | None = None,
                 on_apply: Callable[[], None] | None = None,
                 on_open_resume: Callable[[], None] | None = None,
                 on_followed_up: Callable[[], None] | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self._on_open = on_open or (lambda _jid: None)
        self._jid = ""
        self._plain = ""
        self.setMinimumHeight(140)
        self._build(on_tailor, on_apply, on_open_resume, on_followed_up)
        self.set_empty()

    # ---- construction --------------------------------------------------------

    def _build(self, on_tailor, on_apply, on_open_resume, on_followed_up) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        # Empty state (swapped with the content widget).
        self._empty = QtWidgets.QLabel(_EMPTY_TEXT)
        self._empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet(f"color: {theme.FAINT};")
        outer.addWidget(self._empty, 1)

        self._content = QtWidgets.QWidget()
        outer.addWidget(self._content, 1)
        v = QtWidgets.QVBoxLayout(self._content)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # Header: title + meta | action buttons.
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(12)
        title_col = QtWidgets.QVBoxLayout()
        title_col.setSpacing(1)
        self.title_label = QtWidgets.QLabel("")
        theme.set_type_role(self.title_label, "title")
        title_col.addWidget(self.title_label)
        self.meta_label = QtWidgets.QLabel("")
        self.meta_label.setProperty("muted", True)
        title_col.addWidget(self.meta_label)
        head.addLayout(title_col, 1)

        self.open_btn = QtWidgets.QPushButton("Open posting ↗")
        self.open_btn.setProperty("tier", "link")
        self.open_btn.clicked.connect(lambda: self._on_open(self._jid))
        head.addWidget(self.open_btn, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        # Tracker-variant actions (hidden outside the Tracker tab).
        self.resume_btn = QtWidgets.QPushButton("Open résumé PDF")
        self.resume_btn.clicked.connect(on_open_resume or (lambda: None))
        head.addWidget(self.resume_btn, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        self.followup_btn = QtWidgets.QPushButton("Mark followed up")
        self.followup_btn.setProperty("accent", True)
        self.followup_btn.clicked.connect(on_followed_up or (lambda: None))
        head.addWidget(self.followup_btn, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        # Discovery actions — the main window aliases btn_tailor/btn_apply to
        # THESE buttons (object identity is test- and repolish-coupled).
        self.tailor_btn = QtWidgets.QPushButton("Tailor résumé")
        self.tailor_btn.clicked.connect(on_tailor or (lambda: None))
        head.addWidget(self.tailor_btn, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        self.apply_btn = QtWidgets.QPushButton("Apply")
        self.apply_btn.setProperty("accent", True)
        self.apply_btn.clicked.connect(on_apply or (lambda: None))
        head.addWidget(self.apply_btn, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        v.addLayout(head)

        # Chips row (rebuilt per job).
        self._chips = QtWidgets.QHBoxLayout()
        self._chips.setSpacing(8)
        v.addLayout(self._chips)

        # REASON / NEXT STEP lede.
        self.reason_label = QtWidgets.QLabel("")
        self.reason_label.setWordWrap(True)
        self.reason_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        v.addWidget(self.reason_label)

        # STRENGTHS / GAPS columns.
        cols = QtWidgets.QHBoxLayout()
        cols.setSpacing(24)
        self._strengths_col = QtWidgets.QVBoxLayout()
        self._strengths_col.setSpacing(3)
        self._gaps_col = QtWidgets.QVBoxLayout()
        self._gaps_col.setSpacing(3)
        cols.addLayout(self._strengths_col, 1)
        cols.addLayout(self._gaps_col, 1)
        v.addLayout(cols)
        v.addStretch(1)

        # Collapsed JD snippet behind a tertiary toggle (locked user decision).
        self.desc_toggle = QtWidgets.QPushButton("Show description")
        self.desc_toggle.setProperty("tier", "tertiary")
        self.desc_toggle.setCheckable(True)
        self.desc_toggle.toggled.connect(self._on_desc_toggled)
        v.addWidget(self.desc_toggle, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        self.desc_label = QtWidgets.QLabel("")
        self.desc_label.setWordWrap(True)
        self.desc_label.setProperty("muted", True)
        self.desc_label.setVisible(False)
        v.addWidget(self.desc_label)

    def _on_desc_toggled(self, on: bool) -> None:
        self.desc_label.setVisible(on and bool(self.desc_label.text()))
        self.desc_toggle.setText("Hide description" if on else "Show description")

    # ---- content -------------------------------------------------------------

    def set_empty(self) -> None:
        self._jid = ""
        self._plain = ""
        self._content.setVisible(False)
        self._empty.setVisible(True)

    def set_fields(self, fields: dict, *, jid: str = "",
                   tracker: dict | None = None) -> None:
        """Render one job. `fields` comes from jobsdata.job_detail_fields;
        `tracker` (status/days/follow_up/next_step) switches the card to its
        Tracker variant."""
        if not fields:
            self.set_empty()
            return
        self._jid = jid
        self._empty.setVisible(False)
        self._content.setVisible(True)
        is_tracker = tracker is not None
        plain: list[str] = []

        title = fields.get("title", "?")
        company = fields.get("company", "?")
        self.title_label.setText(title)
        meta = [m for m in (company, fields.get("location", "")) if m]
        if is_tracker and tracker.get("applied_date"):
            meta.append(f"applied {tracker['applied_date']}")
        if fields.get("snapshot_only"):
            meta.append(fields.get("note", ""))
        self.meta_label.setText(" · ".join(m for m in meta if m))
        plain.append(f"{title} — {company}")
        if self.meta_label.text():
            plain.append(self.meta_label.text())

        # Action-row variant.
        self.tailor_btn.setVisible(not is_tracker)
        self.apply_btn.setVisible(not is_tracker)
        self.resume_btn.setVisible(is_tracker)
        self.followup_btn.setVisible(is_tracker)
        self.open_btn.setEnabled(bool(fields.get("url")))

        # Chips row.
        _clear_layout(self._chips)
        if is_tracker:
            status = str(tracker.get("status") or "").strip()
            if status:
                sem = theme.TAG_SEMANTIC.get(status, "neutral")
                self._chips.addWidget(Pill(status.capitalize(), sem))
                plain.append(f"status: {status}")
            follow = str(tracker.get("follow_up") or "")
            if follow.upper() == "DUE":
                self._chips.addWidget(Pill("● Follow-up due now", "followup"))
                plain.append("follow-up due")
            elif follow == "done":
                self._chips.addWidget(Pill("Follow-up sent", "followup_sent"))
                plain.append("follow-up sent")
            days = str(tracker.get("days") or "")
            if days:
                self._chips.addWidget(self._meta_pill(days, "days since applying"))
                plain.append(f"{days} days since applying")
        score = fields.get("score", "")
        try:
            score_n = int(float(score))
        except (TypeError, ValueError):
            score_n = None
        if score_n in _SCORE_PILL:
            pill = Pill(f"Score {score_n} / 5",
                        colors=(_SCORE_PILL[score_n], 0.16))
            self._chips.addWidget(pill)
            plain.append(f"score: {score_n}")
        deep = fields.get("deep_score", "")
        try:
            deep_n = float(deep)
        except (TypeError, ValueError):
            deep_n = None
        if deep_n is not None:
            chip = _DeepChip()
            fill = _SCORE_PILL.get(score_n) if score_n in _SCORE_PILL else None
            chip.set_value(deep_n, fill)
            self._chips.addWidget(chip)
            plain.append(f"deep: {deep}")
        if not is_tracker:
            if fields.get("applicants"):
                self._chips.addWidget(
                    self._meta_pill(fields["applicants"], "applicants"))
                plain.append(f"applicants: {fields['applicants']}")
            if fields.get("salary"):
                self._chips.addWidget(self._meta_pill(fields["salary"], ""))
                plain.append(f"salary: {fields['salary']}")
            if fields.get("posted"):
                self._chips.addWidget(
                    self._meta_pill(fields["posted"], "posted", value_last=False))
                plain.append(f"posted: {fields['posted']}")
        self._chips.addStretch(1)

        # Lede: REASON (discovery) / NEXT STEP (tracker).
        if is_tracker:
            lede_tag, lede = "NEXT STEP", str(tracker.get("next_step") or "")
        else:
            lede_tag, lede = "REASON", fields.get("reason", "")
        if lede:
            self.reason_label.setText(
                f'<span style="color:{theme.MUTED};font-weight:600;'
                f'letter-spacing:0.4px">{lede_tag}</span>&nbsp;&nbsp;'
                f'<span style="color:{theme.TEXT_SECONDARY}">'
                f'{html.escape(lede)}</span>')
            plain.append(f"{lede_tag}: {lede}")
        self.reason_label.setVisible(bool(lede))

        # Strengths / gaps.
        self._fill_bullets(self._strengths_col, "STRENGTHS",
                           fields.get("strengths") or [], "success", "+")
        self._fill_bullets(self._gaps_col, "GAPS",
                           fields.get("gaps") or [], "danger", "−")
        plain.extend(fields.get("strengths") or [])
        plain.extend(fields.get("gaps") or [])

        # JD snippet: reset to collapsed on every new job.
        jd = "" if is_tracker else fields.get("jd", "")
        self.desc_label.setText(jd)
        self.desc_toggle.setVisible(bool(jd))
        self.desc_toggle.setChecked(False)
        self.desc_label.setVisible(False)
        if jd:
            plain.append(jd)
        if fields.get("url"):
            plain.append(fields["url"])
        self._plain = "\n".join(p for p in plain if p)

    @staticmethod
    def _meta_pill(value: str, label: str, value_last: bool = True) -> Pill:
        text = f"{value} {label}".strip() if value_last else f"{label} {value}".strip()
        return Pill(text, outline=True)

    def _fill_bullets(self, col, heading: str, items: list[str],
                      family: str, mark: str) -> None:
        _clear_layout(col)
        if not items:
            return
        fam = theme.SEMANTICS[family]
        head = QtWidgets.QLabel(
            f'<span style="color:{fam["pill_fg"]};font-weight:600;'
            f'letter-spacing:0.4px">{heading}</span>')
        theme.set_type_role(head, "caption")
        col.addWidget(head)
        for item in items:
            lab = QtWidgets.QLabel(
                f'<span style="color:{fam["pill_fg"]};font-weight:700">{mark}</span>'
                f'&nbsp;&nbsp;<span style="color:{theme.TEXT_SECONDARY}">'
                f'{html.escape(item)}</span>')
            lab.setWordWrap(True)
            col.addWidget(lab)
        col.addStretch(1)

    # ---- text mirror (test-coupled) --------------------------------------------

    def toPlainText(self) -> str:  # noqa: N802 (mirrors the old QTextBrowser API)
        return self._plain
