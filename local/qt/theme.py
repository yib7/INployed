"""Modern dark theme for the Qt dashboard.

A single source of truth for the palette plus a Fusion-based dark QPalette and a
QSS stylesheet (rounded controls, accent buttons, slim scrollbars, a clean table
header). `apply_theme(app)` is called once at startup. The row-tint QColors are
exported for the jobs model's BackgroundRole (SP3).
"""
from __future__ import annotations

from PySide6 import QtGui, QtWidgets

# --- interface scaling -----------------------------------------------------
# One factor drives the whole UI: the application font's point size. Qt sizes
# widgets to their font, so scaling the font scales the dashboard. The stylesheet
# is deliberately scale-INDEPENDENT (headings scale via the font, not a pinned px)
# so a scale change never re-applies it — re-polishing every widget was the lag.
# The bottom scale bar passes a scale in [MIN_SCALE, MAX_SCALE].
BASE_FONT_PT = 10.0
MIN_SCALE = 0.5
MAX_SCALE = 2.0

# --- palette ---------------------------------------------------------------
BG = "#0d1117"        # window background (deepest)
SURFACE = "#161b22"   # base: panels, inputs, table body
SURFACE2 = "#1c2330"  # alternating row / slightly raised
ELEV = "#232b38"      # raised band: table header, hovered controls
BORDER = "#30363d"    # hairline borders + separators
TEXT = "#e6edf3"      # primary text
MUTED = "#8b949e"     # secondary text
FAINT = "#6e7681"     # tertiary (placeholders, scrollbar arrows)
ACCENT = "#4c8dff"    # primary accent (modern blue)
ACCENT_HOVER = "#6ba1ff"
ACCENT_DEEP = "#3a7bed"
ACCENT_INK = "#0b1220"  # text on an accent fill
GOOD = "#3fb950"      # apply / offer
GOOD_HOVER = "#56c468"  # apply button hover (ready-state)
AMBER = "#d29922"     # consider / interviewing
DANGER = "#f85149"    # gaps / rejected / errors
SEL = "#1f6feb"       # selected row
SEL_TEXT = "#ffffff"
BTN = "#21262d"       # secondary button
BTN_HOVER = "#2d3340"

# Per-row tints for the jobs tables (used by JobsTableModel BackgroundRole).
# The meaning of a tint is now tab-specific (see jobs_model._row_tag): on the
# High Score tab they key the recommendation + tailored-resume; on the Tracker
# they key the application status + follow-up state.
ROW_HAS_RESUME = "#10243a"   # tailored resume ready / applied (blue)
ROW_REJECTED = "#3a1418"     # rejected (red)
ROW_APPLY = "#0f271b"        # reco "apply" / offer (green)
ROW_CONSIDER = "#2a2412"     # reco "consider" / interviewing (yellow)
ROW_FOLLOWUP = "#3a2410"     # follow-up due (orange)
ROW_PENDING = "#33162b"      # follow-up sent, awaiting reply (pink)

_ROW_TINTS = {
    "has_resume": ROW_HAS_RESUME,
    "applied": ROW_HAS_RESUME,
    "rejected": ROW_REJECTED,
    "apply": ROW_APPLY,
    "offer": ROW_APPLY,
    "consider": ROW_CONSIDER,
    "interviewing": ROW_CONSIDER,
    "followup": ROW_FOLLOWUP,
    "pending": ROW_PENDING,
}


def row_color(name: str) -> QtGui.QColor:
    """QColor for a named row tint, or an invalid QColor (no tint) if unknown."""
    return QtGui.QColor(_ROW_TINTS.get(name, ""))


def _dark_palette() -> QtGui.QPalette:
    p = QtGui.QPalette()
    C = QtGui.QColor
    Role = QtGui.QPalette.ColorRole
    p.setColor(Role.Window, C(BG))
    p.setColor(Role.WindowText, C(TEXT))
    p.setColor(Role.Base, C(SURFACE))
    p.setColor(Role.AlternateBase, C(SURFACE2))
    p.setColor(Role.Text, C(TEXT))
    p.setColor(Role.Button, C(BTN))
    p.setColor(Role.ButtonText, C(TEXT))
    p.setColor(Role.BrightText, C("#ffffff"))
    p.setColor(Role.ToolTipBase, C(SURFACE2))
    p.setColor(Role.ToolTipText, C(TEXT))
    p.setColor(Role.PlaceholderText, C(FAINT))
    p.setColor(Role.Highlight, C(SEL))
    p.setColor(Role.HighlightedText, C(SEL_TEXT))
    p.setColor(Role.Link, C(ACCENT))
    disabled = QtGui.QPalette.ColorGroup.Disabled
    p.setColor(disabled, Role.Text, C(FAINT))
    p.setColor(disabled, Role.ButtonText, C(FAINT))
    p.setColor(disabled, Role.WindowText, C(FAINT))
    return p


def _qss() -> str:
    return f"""
    QWidget {{ background: {BG}; color: {TEXT}; }}
    QToolTip {{ background: {SURFACE2}; color: {TEXT}; border: 1px solid {BORDER};
               padding: 4px 6px; }}

    QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 8px; top: -1px;
                       background: {SURFACE}; }}
    QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 9px 18px;
                   margin-right: 2px; border: 0; font-weight: 600; }}
    QTabBar::tab:selected {{ color: {ACCENT}; border-bottom: 2px solid {ACCENT}; }}
    QTabBar::tab:hover {{ color: {TEXT}; }}

    QPushButton {{ background: {BTN}; color: {TEXT}; border: 1px solid {BORDER};
                  border-radius: 7px; padding: 7px 14px; }}
    QPushButton:hover {{ background: {BTN_HOVER}; border-color: {FAINT}; }}
    QPushButton:pressed {{ background: {ELEV}; }}
    QPushButton:disabled {{ color: {FAINT}; border-color: {BORDER}; }}
    QPushButton[accent="true"] {{ background: {ACCENT}; color: {ACCENT_INK};
                  border: 0; font-weight: 600; }}
    QPushButton[accent="true"]:hover {{ background: {ACCENT_HOVER}; }}
    QPushButton[accent="true"]:pressed {{ background: {ACCENT_DEEP}; }}
    QPushButton[applyReady="true"] {{ background: {GOOD}; color: {ACCENT_INK};
                  border: 0; font-weight: 600; }}
    QPushButton[applyReady="true"]:hover {{ background: {GOOD_HOVER}; }}

    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit, QDateEdit {{
        background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER};
        border-radius: 7px; padding: 6px 8px; selection-background-color: {SEL};
        selection-color: {SEL_TEXT}; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
    QPlainTextEdit:focus, QTextEdit:focus, QDateEdit:focus {{ border-color: {ACCENT}; }}
    QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
        border: 0; width: 22px; }}
    QComboBox::down-arrow {{ width: 0; height: 0; margin-right: 8px;
        border-left: 5px solid transparent; border-right: 5px solid transparent;
        border-top: 6px solid {MUTED}; }}
    QComboBox::down-arrow:hover {{ border-top-color: {TEXT}; }}
    QComboBox::down-arrow:on {{ border-top: 0;
        border-bottom: 6px solid {TEXT}; }}
    QComboBox QAbstractItemView {{ background: {SURFACE2}; color: {TEXT};
        border: 1px solid {BORDER}; selection-background-color: {SEL};
        selection-color: {SEL_TEXT}; outline: 0; }}

    QTableView {{ background: {SURFACE}; alternate-background-color: {SURFACE2};
        gridline-color: {BORDER}; border: 1px solid {BORDER}; border-radius: 8px;
        selection-background-color: {SEL}; selection-color: {SEL_TEXT};
        outline: 0; }}
    QTableView::item {{ padding: 4px 6px; }}
    QHeaderView::section {{ background: {ELEV}; color: {ACCENT}; padding: 7px 9px;
        border: 0; border-right: 1px solid {BORDER}; border-bottom: 1px solid {BORDER};
        font-weight: 600; }}
    QHeaderView::section:hover {{ color: {ACCENT_HOVER}; }}
    QTableCornerButton::section {{ background: {ELEV}; border: 0; }}

    QCheckBox {{ spacing: 7px; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {BORDER};
        border-radius: 4px; background: {SURFACE}; }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

    QGroupBox {{ border: 1px solid {BORDER}; border-radius: 8px; margin-top: 10px;
        padding: 10px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
        color: {MUTED}; }}

    QLabel[muted="true"] {{ color: {MUTED}; }}
    QLabel[heading="true"] {{ color: {TEXT}; font-weight: 600; }}
    QToolButton[sectionHeader="true"] {{ color: {TEXT}; font-weight: 600; border: 0;
        padding: 6px 2px; text-align: left; }}
    QToolButton[sectionHeader="true"]:hover {{ color: {ACCENT}; }}

    QSplitter::handle {{ background: {BORDER}; }}
    QSplitter::handle:horizontal {{ width: 4px; }}
    QSplitter::handle:vertical {{ height: 4px; }}

    QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {BTN_HOVER}; border-radius: 6px;
        min-height: 28px; }}
    QScrollBar::handle:vertical:hover {{ background: {FAINT}; }}
    QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 0; }}
    QScrollBar::handle:horizontal {{ background: {BTN_HOVER}; border-radius: 6px;
        min-width: 28px; }}
    QScrollBar::handle:horizontal:hover {{ background: {FAINT}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QMenu {{ background: {SURFACE2}; color: {TEXT}; border: 1px solid {BORDER}; }}
    QMenu::item {{ padding: 6px 22px; }}
    QMenu::item:selected {{ background: {SEL}; color: {SEL_TEXT}; }}
    QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
    """


def _clamp_scale(scale: float) -> float:
    try:
        s = float(scale)
    except (TypeError, ValueError):
        return 1.0
    return max(MIN_SCALE, min(MAX_SCALE, s))


def apply_theme(app: QtWidgets.QApplication, scale: float = 1.0) -> None:
    """Apply the Fusion base style, the dark palette, and the (static) QSS polish,
    then set the interface scale. The stylesheet is applied once here, never again
    on a scale change."""
    app.setStyle("Fusion")
    app.setPalette(_dark_palette())
    app.setStyleSheet(_qss())
    set_scale(app, scale)


def set_scale(app: QtWidgets.QApplication, scale: float) -> None:
    """Re-scale the whole UI by setting ONLY the application font's point size
    (`10*scale`). Cheap and safe to call live — it does NOT re-apply the global
    stylesheet (that re-polish was the lag). `scale` is clamped to [MIN_SCALE, MAX_SCALE]."""
    scale = _clamp_scale(scale)
    font = QtGui.QFont("Segoe UI")
    font.setPointSizeF(BASE_FONT_PT * scale)
    app.setFont(font)
