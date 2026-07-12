"""Token-driven dark theme for the Qt dashboard (restyle cycle 40).

Single source of truth for the design tokens (surfaces, borders, text tiers,
semantic color families, type scale, metric sizes) plus a Fusion-based dark
QPalette and one static QSS stylesheet. `apply_theme(app)` is called once at
startup; `set_scale(app, s)` re-scales the live UI off one factor.

Design rules baked in here:
- The stylesheet is scale-INDEPENDENT (zero `font-size`): type sizes are
  multipliers of the live base font, applied per type role (`font_for`).
- QSS carries font *weights* via property selectors; `font_for` only sets a
  weight for the title/section roles, so the QSS merge behavior is stable.
- QSS rejects float alpha, so `rgba()` emits integer 0-255 alpha; painters use
  `qcolor()` (QColor.setAlphaF) instead.
- Row tints for the model's BackgroundRole are pre-composed over PANEL with
  `blend()` so the legacy `ROW_*` hex constants (legend swatches, tests) keep
  working unchanged.
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
MIN_SCALE = 0.75
MAX_SCALE = 1.5

# --- surfaces ----------------------------------------------------------------
WINDOW = "#0d1117"    # window / tab-bar bg / input wells (deepest)
PANEL = "#161b22"     # tables, cards, status bar
RAISED = "#1c2330"    # headers, secondary buttons, chips
FLOATING = "#232b38"  # hover, menus, popups (the only shadowed layer)

# Back-compat aliases (existing import sites).
BG = WINDOW
SURFACE = PANEL
SURFACE2 = RAISED
ELEV = FLOATING

# --- borders -----------------------------------------------------------------
BORDER = "#30363d"         # hairline
BORDER_SOFT = "#21262d"    # dividers inside cards
BORDER_STRONG = "#3f4a5a"  # hovered controls

# --- text tiers ----------------------------------------------------------------
TEXT = "#e6edf3"           # primary
TEXT_SECONDARY = "#c8d1da"
MUTED = "#8b949e"
FAINT = "#6e7681"
TEXT_DISABLED = "#484f58"

# --- semantic families ---------------------------------------------------------
# Each family: base / hover / row-tint alpha / selected-row alpha / pill alphas.
# `tint_base` is the color the row tint + pill are derived from (neutral uses the
# faint gray rgb(110,118,129) rather than its base, per the token sheet).
SEMANTICS: dict[str, dict] = {
    "accent": {"base": "#4c8dff", "hover": "#6ba3ff", "tint_a": 0.10,
               "sel_a": 0.20, "pill_a": 0.16, "pill_fg": "#79b0ff"},
    "success": {"base": "#3fb950", "hover": "#56d364", "tint_a": 0.12,
                "sel_a": 0.22, "pill_a": 0.16, "pill_fg": "#56d364"},
    "warning": {"base": "#d29922", "hover": "#e3b341", "tint_a": 0.10,
                "sel_a": 0.20, "pill_a": 0.16, "pill_fg": "#e3b341"},
    "danger": {"base": "#f85149", "hover": "#ff7b72", "tint_a": 0.10,
               "sel_a": 0.20, "pill_a": 0.16, "pill_fg": "#ff7b72"},
    "followup": {"base": "#db61a2", "hover": "#e58bbd", "tint_a": 0.12,
                 "sel_a": 0.22, "pill_a": 0.16, "pill_fg": "#e58bbd"},
    "followup_sent": {"base": "#a371f7", "hover": "#c297ff", "tint_a": 0.10,
                      "sel_a": 0.20, "pill_a": 0.16, "pill_fg": "#c297ff"},
    "neutral": {"base": "#8b949e", "hover": "#adbac7", "tint_base": "#6e7681",
                "tint_a": 0.08, "sel_a": 0.14, "pill_a": 0.16, "pill_fg": "#adbac7"},
}

# Row tags (jobs_model._row_tag values) -> semantic family keys.
TAG_SEMANTIC = {
    "has_resume": "accent",
    "applied": "accent",
    "rejected": "danger",
    "tailor_failed": "danger",
    "apply": "success",
    "offer": "success",
    "consider": "warning",
    "interviewing": "warning",
    "followup": "followup",
    "pending": "followup_sent",
    "skip": "neutral",
}

# Score badges: score -> (color, bg alpha). 22x20, radius 5, mono bold.
SCORE_BADGES = {
    5: ("#3fb950", 0.16),
    4: ("#9ccc3d", 0.14),
    3: ("#d29922", 0.14),
    2: ("#e8703a", 0.14),
    1: ("#f85149", 0.14),
}

# --- metrics -------------------------------------------------------------------
RADII = {"checkbox": 4, "badge": 5, "control": 6, "card": 8}   # pill = height/2
SIZES = {"row": 34, "header_row": 32, "control": 30, "compact": 28}  # @100%
SPACING = (4, 8, 12, 16, 20, 24)

# Type roles: multiplier of the live base size (never absolute px).
TYPE_SCALE = {
    "title": 1.40,     # w700 (font_for)
    "section": 1.15,   # w600 (font_for)
    "body": 1.00,
    "control": 0.93,   # buttons / cells / tabs / inputs (labels w600 via QSS)
    "caption": 0.85,   # hints; table headers w600 via QSS
    "mono": 0.93,      # Consolas — numerals / dates / paths
}


# --- color helpers ---------------------------------------------------------------
def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgba(hex_color: str, a: float) -> str:
    """QSS rgba() string with an INTEGER alpha (QSS rejects float alpha).

    `a` in [0, 1] is scaled to 0-255 (0.10 -> 26); values > 1 are taken as an
    already-integer alpha."""
    r, g, b = _rgb(hex_color)
    alpha = int(round(a * 255)) if a <= 1 else int(a)
    return f"rgba({r},{g},{b},{alpha})"


def qcolor(hex_color: str, a: float | None = None) -> QtGui.QColor:
    """QColor for painters; `a` (0-1) applied via setAlphaF."""
    c = QtGui.QColor(hex_color)
    if a is not None:
        c.setAlphaF(a)
    return c


def blend(fg_hex: str, a: float, bg: str = PANEL) -> str:
    """Pre-compose `fg_hex` at alpha `a` over the opaque `bg` -> flat hex."""
    fr, fg_, fb = _rgb(fg_hex)
    br, bg_, bb = _rgb(bg)
    return "#{:02x}{:02x}{:02x}".format(
        round(fr * a + br * (1 - a)),
        round(fg_ * a + bg_ * (1 - a)),
        round(fb * a + bb * (1 - a)),
    )


def _tint(name: str, selected: bool = False) -> str:
    """Flat hex of a semantic family's row tint (pre-composed over PANEL)."""
    s = SEMANTICS[name]
    return blend(s.get("tint_base", s["base"]), s["sel_a" if selected else "tint_a"])


# --- legacy palette constants (re-pointed at the tokens) -------------------------
ACCENT = SEMANTICS["accent"]["base"]
ACCENT_HOVER = SEMANTICS["accent"]["hover"]     # #6ba3ff
ACCENT_DEEP = "#3d7be6"                          # primary button pressed
ACCENT_INK = "#0b1220"                           # legacy: dark text on accent fill
GOOD = SEMANTICS["success"]["base"]
GOOD_HOVER = SEMANTICS["success"]["hover"]      # #56d364
AMBER = SEMANTICS["warning"]["base"]
DANGER = SEMANTICS["danger"]["base"]
SEL = ACCENT
SEL_TEXT = "#ffffff"
BTN = RAISED
BTN_HOVER = FLOATING

# Per-row tints for the jobs tables (JobsTableModel BackgroundRole fallback,
# legend swatches, tests). Flat hex, pre-composed over PANEL at the semantic
# family's row-tint alpha.
ROW_HAS_RESUME = _tint("accent")        # tailored resume ready / applied (blue)
ROW_REJECTED = _tint("danger")          # rejected (red)
ROW_TAILOR_FAILED = ROW_REJECTED        # failed tailor run — needs a re-run (red)
ROW_APPLY = _tint("success")            # reco "apply" / offer (green)
ROW_CONSIDER = _tint("warning")         # reco "consider" / interviewing (yellow)
ROW_FOLLOWUP = _tint("followup")        # follow-up due (pink)
ROW_PENDING = _tint("followup_sent")    # follow-up sent, awaiting reply (purple)
ROW_SKIP = _tint("neutral")             # reco "skip" — "Don't consider" (gray)

_ROW_TINTS = {
    "has_resume": ROW_HAS_RESUME,
    "applied": ROW_HAS_RESUME,
    "rejected": ROW_REJECTED,
    "tailor_failed": ROW_TAILOR_FAILED,
    "apply": ROW_APPLY,
    "offer": ROW_APPLY,
    "consider": ROW_CONSIDER,
    "interviewing": ROW_CONSIDER,
    "followup": ROW_FOLLOWUP,
    "pending": ROW_PENDING,
    "skip": ROW_SKIP,
}


def row_color(name: str) -> QtGui.QColor:
    """QColor for a named row tint, or an invalid QColor (no tint) if unknown."""
    return QtGui.QColor(_ROW_TINTS.get(name, ""))


def _dark_palette() -> QtGui.QPalette:
    p = QtGui.QPalette()
    C = QtGui.QColor
    Role = QtGui.QPalette.ColorRole
    p.setColor(Role.Window, C(WINDOW))
    p.setColor(Role.WindowText, C(TEXT))
    p.setColor(Role.Base, C(PANEL))
    p.setColor(Role.AlternateBase, C(PANEL))    # zebra striping off
    p.setColor(Role.Text, C(TEXT))
    p.setColor(Role.Button, C(RAISED))
    p.setColor(Role.ButtonText, C(TEXT))
    p.setColor(Role.BrightText, C("#ffffff"))
    p.setColor(Role.ToolTipBase, C(FLOATING))
    p.setColor(Role.ToolTipText, C(TEXT))
    p.setColor(Role.PlaceholderText, C(FAINT))
    p.setColor(Role.Highlight, C(ACCENT))
    p.setColor(Role.HighlightedText, C("#ffffff"))
    p.setColor(Role.Link, C(ACCENT))
    disabled = QtGui.QPalette.ColorGroup.Disabled
    p.setColor(disabled, Role.Text, C(TEXT_DISABLED))
    p.setColor(disabled, Role.ButtonText, C(TEXT_DISABLED))
    p.setColor(disabled, Role.WindowText, C(TEXT_DISABLED))
    return p


def _qss() -> str:
    # One static f-string; NO `font-size` anywhere (type sizes ride the app/role
    # fonts so `ui_scale_pct` keeps working without a re-polish). Weights live
    # here on property selectors.
    return f"""
    QWidget {{ background: {WINDOW}; color: {TEXT}; }}
    QToolTip {{ background: {FLOATING}; color: {TEXT}; border: 1px solid {BORDER};
               padding: 4px 6px; }}

    QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 8px; top: -1px;
                       background: {WINDOW}; }}
    QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 9px 18px;
                   margin-right: 2px; border: 0; }}
    QTabBar::tab:selected {{ color: {TEXT}; font-weight: 600;
                   border-bottom: 2px solid {ACCENT}; }}
    QTabBar::tab:hover {{ color: {TEXT}; }}

    /* Buttons — default tier is Secondary (raised surface, hairline border). */
    QPushButton {{ background: {RAISED}; color: {TEXT}; border: 1px solid {BORDER};
                  border-radius: {RADII["control"]}px; padding: 6px 12px;
                  font-weight: 600; }}
    QPushButton:hover {{ background: {FLOATING}; border-color: {BORDER_STRONG}; }}
    QPushButton:pressed {{ background: {PANEL}; border-color: {BORDER_STRONG}; }}
    QPushButton:disabled {{ background: {PANEL}; color: {TEXT_DISABLED};
                  border-color: {BORDER_SOFT}; }}

    QPushButton[accent="true"] {{ background: {ACCENT}; color: #ffffff;
                  border: 1px solid {ACCENT}; }}
    QPushButton[accent="true"]:hover {{ background: {ACCENT_HOVER};
                  border-color: {ACCENT_HOVER}; }}
    QPushButton[accent="true"]:pressed {{ background: {ACCENT_DEEP};
                  border-color: {ACCENT_DEEP}; }}
    QPushButton[accent="true"]:disabled {{ background: {rgba(ACCENT, 0.25)};
                  color: {rgba("#ffffff", 0.4)}; border-color: transparent; }}

    QPushButton[tier="tertiary"] {{ background: transparent; color: {MUTED};
                  border: 1px solid transparent; }}
    QPushButton[tier="tertiary"]:hover {{ background: {RAISED}; color: {TEXT};
                  border-color: transparent; }}
    QPushButton[tier="tertiary"]:pressed {{ background: {PANEL}; }}
    QPushButton[tier="tertiary"]:disabled {{ background: transparent;
                  color: {TEXT_DISABLED}; border-color: transparent; }}

    QPushButton[tier="destructive"] {{ background: transparent; color: {MUTED};
                  border: 1px solid transparent; }}
    QPushButton[tier="destructive"]:hover {{ background: {rgba(DANGER, 0.12)};
                  color: {SEMANTICS["danger"]["hover"]};
                  border-color: {rgba(DANGER, 0.5)}; }}
    QPushButton[tier="destructive"]:pressed {{ background: {rgba(DANGER, 0.2)};
                  color: {DANGER}; }}
    QPushButton[tier="destructive"]:disabled {{ background: transparent;
                  color: {TEXT_DISABLED}; border-color: transparent; }}

    /* Accent text button ("Open posting ↗" on the detail card). */
    QPushButton[tier="link"] {{ background: transparent; color: {ACCENT};
                  border: 1px solid transparent; }}
    QPushButton[tier="link"]:hover {{ background: {rgba(ACCENT, 0.10)}; }}
    QPushButton[tier="link"]:pressed {{ background: {rgba(ACCENT, 0.16)}; }}
    QPushButton[tier="link"]:disabled {{ background: transparent;
                  color: {TEXT_DISABLED}; }}

    QPushButton[applyReady="true"] {{ background: {GOOD}; color: #ffffff;
                  border: 1px solid {GOOD}; }}
    QPushButton[applyReady="true"]:hover {{ background: {GOOD_HOVER};
                  border-color: {GOOD_HOVER}; }}

    /* Inputs — wells sit on the deepest surface; focus = 1px accent, no glow. */
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit, QDateEdit {{
        background: {WINDOW}; color: {TEXT}; border: 1px solid {BORDER};
        border-radius: {RADII["control"]}px; padding: 6px 8px;
        selection-background-color: {SEL}; selection-color: {SEL_TEXT}; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
    QPlainTextEdit:focus, QTextEdit:focus, QDateEdit:focus {{ border-color: {ACCENT}; }}
    QLineEdit[error="true"], QPlainTextEdit[error="true"], QTextEdit[error="true"] {{
        border-color: {DANGER}; }}
    QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
        border: 0; width: 22px; }}
    QComboBox::down-arrow {{ width: 0; height: 0; margin-right: 8px;
        border-left: 5px solid transparent; border-right: 5px solid transparent;
        border-top: 6px solid {MUTED}; }}
    QComboBox::down-arrow:hover {{ border-top-color: {TEXT}; }}
    QComboBox::down-arrow:on {{ border-top: 0;
        border-bottom: 6px solid {TEXT}; }}
    QComboBox QAbstractItemView {{ background: {FLOATING}; color: {TEXT};
        border: 1px solid {BORDER}; selection-background-color: {rgba(ACCENT, 0.16)};
        selection-color: {TEXT}; outline: 0; }}

    /* Tables — the delegate owns cell painting (selection/tints/pills). */
    QTableView {{ background: {PANEL}; alternate-background-color: {PANEL};
        gridline-color: {BORDER_SOFT}; border: 1px solid {BORDER};
        border-radius: 8px; outline: 0; }}
    QHeaderView::section {{ background: {RAISED}; color: {MUTED}; padding: 7px 9px;
        border: 0; border-right: 1px solid {BORDER_SOFT};
        border-bottom: 1px solid {BORDER}; font-weight: 600; }}
    QHeaderView::section:hover {{ color: {TEXT}; }}
    QTableCornerButton::section {{ background: {RAISED}; border: 0; }}

    QCheckBox {{ spacing: 7px; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {BORDER};
        border-radius: {RADII["checkbox"]}px; background: {WINDOW}; }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

    QGroupBox {{ border: 1px solid {BORDER}; border-radius: 8px; margin-top: 10px;
        padding: 10px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
        color: {MUTED}; }}

    QLabel[muted="true"] {{ color: {MUTED}; }}
    QLabel[secondary="true"] {{ color: {TEXT_SECONDARY}; }}
    QLabel[heading="true"] {{ color: {TEXT}; font-weight: 600; }}
    QLabel[warn="true"] {{ color: {AMBER}; }}
    QToolButton[sectionHeader="true"] {{ color: {TEXT}; font-weight: 600; border: 0;
        padding: 6px 2px; text-align: left; }}
    QToolButton[sectionHeader="true"]:hover {{ color: {ACCENT}; }}

    /* Containers: cards, warning callouts, chip/identity strips (Phase 3). */
    QFrame[card="true"] {{ background: {PANEL}; border: 1px solid {BORDER};
        border-radius: {RADII["card"]}px; }}
    QFrame[callout="warning"] {{ background: {rgba(AMBER, 0.08)};
        border: 1px solid {rgba(AMBER, 0.45)};
        border-radius: {RADII["card"]}px; }}
    QFrame[strip="true"] {{ background: {WINDOW}; border: 0;
        border-bottom: 1px solid {BORDER}; }}
    QWidget[chipbar="true"] {{ background: transparent; }}
    /* The global `QWidget {{ background: WINDOW }}` rule would paint WINDOW
       patches over PANEL cards — plain containers/labels inside a card (or a
       warning callout) go transparent instead. Inputs/buttons keep their own
       surfaces (their type rules stay untouched). */
    QFrame[card="true"] QLabel, QFrame[card="true"] QCheckBox,
    QFrame[card="true"] QSlider, QFrame[card="true"] .QWidget,
    QFrame[callout="warning"] QLabel, QFrame[callout="warning"] .QWidget {{
        background: transparent; }}
    QFrame[divider="true"] {{ background: {BORDER_SOFT}; border: 0; }}
    QLabel[storageTag="true"] {{ color: {FAINT}; border: 1px solid {BORDER};
        border-radius: 4px; padding: 0px 5px; background: transparent; }}
    QLabel[countBadge="true"] {{ background: {PANEL}; border: 1px solid {BORDER};
        border-radius: {RADII["control"]}px; padding: 2px 9px; }}

    QSplitter::handle {{ background: {BORDER}; }}
    QSplitter::handle:horizontal {{ width: 4px; }}
    QSplitter::handle:vertical {{ height: 4px; }}

    QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {FLOATING}; border-radius: 6px;
        min-height: 28px; }}
    QScrollBar::handle:vertical:hover {{ background: {BORDER_STRONG}; }}
    QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 0; }}
    QScrollBar::handle:horizontal {{ background: {FLOATING}; border-radius: 6px;
        min-width: 28px; }}
    QScrollBar::handle:horizontal:hover {{ background: {BORDER_STRONG}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QMenu {{ background: {FLOATING}; color: {TEXT}; border: 1px solid {BORDER}; }}
    QMenu::item {{ padding: 6px 22px; }}
    QMenu::item:selected {{ background: {rgba(ACCENT, 0.16)}; color: {TEXT}; }}
    QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
    """


# --- type-role system --------------------------------------------------------
_current_scale = 1.0

# Widget classes whose default role is "control" (0.93 x base). Registered as
# per-class application fonts too, so widgets created AFTER a rescale (dialogs,
# popups) resolve the right size with no explicit setFont.
_CONTROL_CLASSES = (
    "QPushButton", "QToolButton", "QLineEdit", "QComboBox",
    "QAbstractSpinBox", "QCheckBox", "QTabBar", "QAbstractItemView",
)


def font_for(role: str, scale: float | None = None) -> QtGui.QFont:
    """QFont for a type role at the given (default: current) scale.

    Only title/section carry a weight here — every other weight comes from the
    QSS property selectors, so the stylesheet merge behavior stays as documented."""
    if scale is None:
        scale = _current_scale
    font = QtGui.QFont("Segoe UI")
    if role == "mono":
        font.setFamilies(["Consolas", "Cascadia Mono"])
    font.setPointSizeF(BASE_FONT_PT * scale * TYPE_SCALE.get(role, 1.0))
    if role == "title":
        font.setWeight(QtGui.QFont.Weight.Bold)        # 700
    elif role == "section":
        font.setWeight(QtGui.QFont.Weight.DemiBold)    # 600
    return font


def set_type_role(widget: QtWidgets.QWidget, role: str) -> None:
    """Tag `widget` with an explicit type role and apply the current font now.
    The tag survives rescales (set_scale re-derives the font from the role)."""
    widget.setProperty("typeRole", role)
    widget.setFont(font_for(role))


def _role_for(widget: QtWidgets.QWidget) -> str:
    """Resolve a widget's type role: explicit `typeRole` property > `muted`
    property (captions) > class map > body. Plain widgets default to `body`
    (exactly BASE_FONT_PT x scale — test-pinned)."""
    role = widget.property("typeRole")
    if role:
        return str(role)
    if widget.property("muted"):
        return "caption"
    if isinstance(widget, QtWidgets.QHeaderView):   # before QAbstractItemView
        return "caption"
    if isinstance(widget, (
            QtWidgets.QPushButton, QtWidgets.QToolButton, QtWidgets.QLineEdit,
            QtWidgets.QComboBox, QtWidgets.QAbstractSpinBox, QtWidgets.QCheckBox,
            QtWidgets.QTabBar, QtWidgets.QAbstractItemView)):
        return "control"
    return "body"


def _apply_table_metrics(view: QtWidgets.QTableView, scale: float) -> None:
    """Fixed row heights + header height from the SIZES tokens x scale.
    Views tagged `rowSize="compact"` get the compact row height."""
    row_key = "compact" if view.property("rowSize") == "compact" else "row"
    vh = view.verticalHeader()
    vh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Fixed)
    vh.setDefaultSectionSize(round(SIZES[row_key] * scale))
    view.horizontalHeader().setFixedHeight(round(SIZES["header_row"] * scale))


def register_table(view: QtWidgets.QTableView) -> None:
    """Apply the current-scale table metrics at construction time (set_scale
    keeps every live QTableView in sync afterwards)."""
    _apply_table_metrics(view, _current_scale)


def add_popup_shadow(widget: QtWidgets.QWidget) -> None:
    """Install the app's ONE drop shadow — floating popups/menus only (the
    FLOATING layer): offset (0, 8), blur 24, ink rgba(1,4,9,0.55). Each popup
    gets its own effect instance (a QGraphicsEffect can only serve one widget);
    the parameters here are the single source of truth."""
    effect = QtWidgets.QGraphicsDropShadowEffect(widget)
    effect.setOffset(0, 8)
    effect.setBlurRadius(24)
    effect.setColor(QtGui.QColor(1, 4, 9, round(0.55 * 255)))
    widget.setGraphicsEffect(effect)


def _clamp_scale(scale: float) -> float:
    try:
        s = float(scale)
    except (TypeError, ValueError):
        return 1.0
    return max(MIN_SCALE, min(MAX_SCALE, s))


def apply_theme(app: QtWidgets.QApplication, scale: float = 1.0) -> None:
    """Apply the Fusion base style + dark palette + the static QSS polish (once),
    then the interface scale."""
    app.setStyle("Fusion")
    app.setPalette(_dark_palette())
    app.setStyleSheet(_qss())
    set_scale(app, scale)


def set_scale(app: QtWidgets.QApplication, scale: float) -> None:
    """Re-scale the whole UI off one factor — the application font's point size
    (`BASE_FONT_PT * scale`), refracted through the type roles.

    A global stylesheet pins each widget's font at polish time, so `app.setFont()`
    alone doesn't reach widgets already on screen (the size used to change only after
    a restart). Earlier this was forced through by re-applying the stylesheet, but
    that synchronously re-polishes *every* widget (all hidden tabs included) — the
    lag. Instead we push the new role font straight onto each live widget: that only
    marks them dirty, so Qt defers the actual relayout to the visible ones and never
    re-runs the QSS cascade. Live AND cheap. Per-class application fonts cover
    widgets created *after* the rescale (dialogs, popups). The stylesheet is
    untouched (its heading/section font-weight rules still merge over the new
    fonts). `scale` is clamped to [MIN_SCALE, MAX_SCALE]."""
    global _current_scale
    scale = _clamp_scale(scale)
    _current_scale = scale
    app.setFont(font_for("body", scale))  # default for any widget created later
    control_font = font_for("control", scale)
    for cls in _CONTROL_CLASSES:
        app.setFont(control_font, cls)
    app.setFont(font_for("caption", scale), "QHeaderView")
    for w in app.allWidgets():
        w.setFont(font_for(_role_for(w), scale))  # override stylesheet-pinned fonts
        if isinstance(w, QtWidgets.QTableView):
            _apply_table_metrics(w, scale)
