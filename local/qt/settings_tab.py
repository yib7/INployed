"""Schema-driven settings form (Qt) — mounted as the dashboard's Settings tab.

Renders `settings.SETTINGS_SCHEMA` grouped by section: one labelled, explained
input per Field, the right widget per type (dropdown / editable dropdown / slider /
checkboxes / multiline list / path+Browse / credential (masked by default, with
a Hide toggle) / entry), a muted "(filename)" storage tag, a collapsible VM section
gated by a master checkbox, a "Show advanced settings (N hidden)" disclosure
that folds every `Field.advanced` row away, and Save / Revert changes / Restore
from archive / Restore defaults. Save validates via `settings.validate`/`settings.save`, reports
a changed-field summary, and never echoes a secret value into that summary.
"""
from __future__ import annotations

from typing import Callable

from PySide6 import QtCore, QtWidgets

import jobsdata
import settings
import settings_archive
import vm_sync
from qt import theme
from qt.widgets import CollapsibleSection

SECTION_HELP = {
    "Credentials": ("API keys and tokens, saved to your private .env file on this PC. Saved "
                    "values are masked by default — untick Hide to reveal one, edit it to "
                    "change it, or clear the box to remove it."),
    "Connection & paths": "Your cloud project, your name, and where files live on this PC.",
    "Engine": ("Which AI service (Gemini or Claude) tailors résumés, how the Gemini side "
               "bills (Cloud project vs API key), and which model each tailoring stage runs."),
    "Dashboard": "How the dashboard surfaces and tracks jobs.",
    "Scraper": "What job searches the discovery step runs (this drives its API spend).",
    "Scoring": ("Advanced — which models score jobs and the spend guards around them. The "
                "defaults are tuned; changing the model names can silently break scoring."),
    "Resume": "What the resume tailor generates, and how the cover letter reads.",
    "Auto-apply": ("The batch auto-apply queue (Auto-apply tab): how many jobs one 'Queue for "
                   "auto-apply' action may add, and which webmail inbox the agent may open for "
                   "account-verification emails. Applications are parked at their review page — "
                   "never submitted for you."),
    "Settings history": ("Every Save snapshots all your settings to a dated folder so you can "
                         "roll one back later with 'Restore from archive...' below. Snapshots "
                         "include your saved keys and live alongside your settings on this PC."),
    "VM (cloud scraper)": ("Connect to your cloud job-discovery VM (GCP) so you can push config, "
                           "schedule, and pause changes to it. Uses your existing `gcloud` login — "
                           "no SSH password or key is ever stored."),
}
SECTION_ORDER = ["Credentials", "Connection & paths", "Engine",
                 "Dashboard", "Scraper", "Scoring", "Resume", "Auto-apply",
                 "Settings history", "VM (cloud scraper)"]

# Friendlier section headers shown in the UI. The dict KEYS above stay the canonical
# section names (they must match settings.Field.section); this only changes the
# visible title so the dashboard reads as a "job discovery" tool.
SECTION_DISPLAY = {
    "Scraper": "Job discovery",
    "VM (cloud scraper)": "VM (cloud job discovery)",
}

# Short one-liners shown next to each section header — always visible, even when the
# section is collapsed, so a user knows what to expand without clicking through.
SECTION_TAGLINE = {
    "Credentials": "API keys & tokens — saved to your private .env file on this PC",
    "Connection & paths": "Project, your name, file locations",
    "Engine": "Tailor AI service, billing & per-stage models",
    "Dashboard": "How jobs are surfaced & tracked",
    "Scraper": "What job searches to run",
    "Scoring": "Models & spend guards (advanced)",
    "Resume": "Cover letter, ATS report & prep-sheet toggles",
    "Auto-apply": "Batch cap & inbox for the apply agent",
    "Settings history": "Snapshot & restore your settings",
    "VM (cloud scraper)": "Manage the cloud job-discovery VM",
}
COLLAPSIBLE_SECTIONS = {"VM (cloud scraper)": "vm_enabled"}


class _PopupOnClick(QtCore.QObject):
    """Open an editable combo's dropdown when its text field is clicked.

    An editable ``QComboBox`` shows its line edit, so a click lands on the text
    field and Qt does *not* open the popup — the field just looks like a plain
    text box. Installed on the combo's line edit, this opens the list on click so
    the model selectors behave like the dropdowns users expect; typing a custom
    id still works once the popup is dismissed.
    """

    def __init__(self, combo: QtWidgets.QComboBox):
        super().__init__(combo)
        self._combo = combo

    def eventFilter(self, obj, event):  # noqa: N802 - Qt override name
        if (event.type() == QtCore.QEvent.Type.MouseButtonPress
                and not self._combo.view().isVisible()):
            self._combo.showPopup()
            return True
        return False


def _ordered_sections() -> list[tuple[str, list[settings.Field]]]:
    by_section: dict[str, list[settings.Field]] = {}
    for f in settings.SETTINGS_SCHEMA:
        by_section.setdefault(f.section, []).append(f)
    ordered = [(s, by_section[s]) for s in SECTION_ORDER if s in by_section]
    ordered += [(s, fs) for s, fs in by_section.items() if s not in SECTION_ORDER]
    return ordered


class SettingsForm(QtWidgets.QWidget):
    def __init__(self, on_saved: Callable[[], None] | None = None, targets: dict | None = None,
                 vm_panel_factory: Callable[[QtWidgets.QWidget], QtWidgets.QWidget] | None = None,
                 collapsed_sections: list[str] | None = None,
                 save_collapsed: Callable[[list[str]], None] | None = None,
                 show_advanced: bool | None = None,
                 save_show_advanced: Callable[[bool], None] | None = None,
                 parent=None):
        super().__init__(parent)
        self.on_saved = on_saved
        self.targets = targets
        self._vm_factory = vm_panel_factory

        self._getters: dict[str, Callable[[], str]] = {}
        self._setters: dict[str, Callable[[object], None]] = {}
        # Address any field by key: its control, and every form row it occupies.
        # `_widgets` holds the control you'd read/write/style (the INNER one for a
        # composite cell — see `_make_widget`); `_rows` holds the (form, index)
        # pairs `_set_field_visible` flips, which for a normal field is BOTH its
        # label row and its muted help row.
        #
        # CONSTRAINT: the row indices are POSITIONAL and captured once, at build.
        # Never `insertRow`/`removeRow` into these forms afterwards — an insert
        # shifts every later row and silently re-points every stored index below
        # it at the wrong field, with no error (`setRowVisible` on an out-of-range
        # row neither raises nor warns). A row that only sometimes shows (an
        # inline validation message, say) must be BUILT here, hidden, and toggled
        # with `setRowVisible` — not inserted later.
        # `test_every_field_registers_the_widget_in_its_own_row` is the guard.
        self._widgets: dict[str, QtWidgets.QWidget] = {}
        self._rows: dict[str, list[tuple[QtWidgets.QFormLayout, int]]] = {}
        self._multi: dict[str, dict[str, QtWidgets.QCheckBox]] = {}
        self._lists: dict[str, QtWidgets.QPlainTextEdit] = {}
        self._secret_edits: dict[str, QtWidgets.QLineEdit] = {}
        self._secret_hides: dict[str, QtWidgets.QCheckBox] = {}
        self._collapse: dict[str, QtWidgets.QWidget] = {}  # gate-section -> gated sub-container (VM)
        self._gate_keys: dict[str, str] = {}  # gate_key -> section
        self._vm_panel: QtWidgets.QWidget | None = None  # the VM ops panel, if mounted

        # Collapsible-section state: which sections start folded, and where to persist it.
        self._section_widgets: dict[str, CollapsibleSection] = {}
        self._collapsed: set[str] = set(
            jobsdata.load_collapsed_sections() if collapsed_sections is None else collapsed_sections)
        self._save_collapsed = save_collapsed or jobsdata.save_collapsed_sections

        # Progressive disclosure: are the `advanced` fields folded away? Injected
        # the same way as the collapse state (and for the same reason — a test
        # that fell through to jobsdata would read the DEVELOPER's config.json and
        # its assertions would depend on what that person last ticked).
        self._show_advanced: bool = (
            jobsdata.load_show_advanced() if show_advanced is None else bool(show_advanced))
        self._save_show_advanced = save_show_advanced or jobsdata.save_show_advanced
        # self._advanced_check is created by `_build` -> `_add_advanced_toggle`,
        # before anything reads it (same as self.status, built by `_add_buttons`).

        self._build()

    # ---- construction --------------------------------------------------------

    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        # The form lives in a centered, max-width column of section cards
        # (restyle 3f) so a full-width window doesn't stretch every input.
        host = QtWidgets.QWidget()
        hl = QtWidgets.QHBoxLayout(host)
        hl.setContentsMargins(16, 12, 16, 12)
        column = QtWidgets.QWidget()
        column.setMaximumWidth(1160)
        self._body = QtWidgets.QVBoxLayout(column)
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(14)
        # Zero-stretch spacers: the column grows first (to its 1160 cap), THEN
        # the leftover splits equally between the two spacers — centered.
        hl.addStretch(0)
        hl.addWidget(column, 1)
        hl.addStretch(0)
        scroll.setWidget(host)

        stored = settings.load(self.targets)
        self._opening_values = dict(stored)

        self._add_advanced_toggle()

        for section, fields in _ordered_sections():
            sec = CollapsibleSection(
                SECTION_DISPLAY.get(section, section),
                subtitle=SECTION_TAGLINE.get(section, ""),
                collapsed=section in self._collapsed,
                on_toggled=lambda c, s=section: self._on_section_toggled(s, c))
            self._section_widgets[section] = sec
            self._body.addWidget(sec)
            if section in COLLAPSIBLE_SECTIONS:
                self._fill_gated_section(sec, section, fields, stored)
            else:
                self._fill_section(sec, section, fields, stored)

        # Second pass, AFTER every field exists: wire the `show_if` gates and do
        # the first visibility render. See `_connect_gate_signals` for why this
        # cannot happen inside the section loop above.
        self._connect_gate_signals()
        self._apply_field_visibility()

        self._add_buttons()
        self._body.addStretch(1)

    # ---- progressive disclosure (settings.Field.advanced) --------------------

    def _add_advanced_toggle(self) -> None:
        """The one checkbox that folds every `advanced` field away, at the top of
        the form (P7's search box goes above it).

        Built BEFORE the sections so `_apply_field_visibility` — which refreshes
        the label — can assume it exists, and connected AFTER `setChecked` so
        restoring the persisted state does not write it straight back out.
        """
        check = QtWidgets.QCheckBox()
        check.setChecked(self._show_advanced)
        check.toggled.connect(self._on_advanced_toggled)
        self._advanced_check = check
        self._body.addWidget(check)

    def _section_gate_open(self, f: settings.Field) -> bool:
        """Is the master checkbox of `f`'s section (if it has one) switched on?

        Read ONLY by the advanced count, never by `_field_visible`. The section
        gate already hides its fields by hiding their whole container, so
        repeating it per row would fight `_set_field_visible`'s property that
        showing a row inside a gated-off container does not force the container
        open — and would make this a second field-visibility path.
        """
        gate_key = COLLAPSIBLE_SECTIONS.get(f.section)
        return gate_key is None or bool(self._getters[gate_key]())

    def _advanced_hidden_count(self, gate_values: dict | None = None) -> int:
        """How many advanced settings that APPLY to this configuration the
        checkbox is withholding right now. Computed, never hardcoded.

        Two subtractions, one line between them:

        * A CONFIGURATION gate subtracts — both kinds. A `show_if` gate shut
          (the other provider's model pickers) and the VM section's master
          switch off both mean the same thing: that field does nothing for the
          way this user has things set up. Counting them would promise rows a
          tick cannot deliver, which is the whole reason this is not simply
          "how many advanced fields exist" (18 today, of which 5 belong to the
          provider that is not selected and 3 to a VM the user may not run).
        * A collapsed SECTION does not subtract. That is a fold the user set
          themselves, with a header on screen naming what is inside; the
          settings still apply and expanding is not a configuration change. It
          also cannot subtract: this repo's owner runs with 9 of the 10 sections
          folded, so counting that way would report ~0 and destroy the only
          signal — until search ships — that the hidden settings exist at all.

        Zero once the box is ticked, which is what drops the parenthetical from
        the label.
        """
        if self._show_advanced:
            return 0
        if gate_values is None:
            gate_values = self._gate_values()
        return sum(1 for f in settings.SETTINGS_SCHEMA
                   if f.advanced and settings.is_visible(f, gate_values)
                   and self._section_gate_open(f))

    def _refresh_advanced_label(self, *_, gate_values: dict | None = None) -> None:
        """Re-render the checkbox's count. Takes `*_` because it is also connected
        directly to the VM master switch, whose `toggled(bool)` moves the count
        without going through `_apply_field_visibility`."""
        hidden = self._advanced_hidden_count(gate_values)
        self._advanced_check.setText(
            f"Show advanced settings ({hidden} hidden)" if hidden
            else "Show advanced settings")

    def _on_advanced_toggled(self, checked: bool) -> None:
        self._show_advanced = bool(checked)
        try:
            self._save_show_advanced(self._show_advanced)
        except OSError:
            pass  # persisting the disclosure state must never break the form
        self._apply_field_visibility()

    def _on_section_toggled(self, section: str, collapsed: bool) -> None:
        if collapsed:
            self._collapsed.add(section)
        else:
            self._collapsed.discard(section)
        try:
            self._save_collapsed(sorted(self._collapsed))
        except OSError:
            pass  # persisting the fold state must never break the form

    def _fill_section(self, sec: CollapsibleSection, section, fields, stored):
        blurb = SECTION_HELP.get(section)
        if blurb:
            lab = QtWidgets.QLabel(blurb)
            lab.setProperty("muted", True)
            lab.setWordWrap(True)
            sec.add_widget(lab)
        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        sec.add_layout(form)
        for f in fields:
            self._add_field(form, f, stored.get(f.key, f.default))

    def _fill_gated_section(self, sec: CollapsibleSection, section, fields, stored):
        """A collapsible section whose body is ALSO gated by a master checkbox
        (the VM section): collapse hides the header's body; the checkbox hides the
        settings within. Both behaviours coexist."""
        gate_key = COLLAPSIBLE_SECTIONS[section]
        gate = next((f for f in fields if f.key == gate_key), None)
        self._gate_keys[gate_key] = section

        check = QtWidgets.QCheckBox(gate.label if gate else "Enable")
        check.setChecked(bool(stored.get(gate_key, getattr(gate, "default", False))))
        check.toggled.connect(self._apply_section_visibility)
        # This switch decides whether its section's advanced fields apply at all,
        # so it moves the disclosure count. Connected HERE rather than from
        # `_apply_section_visibility`, which also runs mid-`_build` — the label
        # must not depend on SECTION_ORDER happening to render this section after
        # every gate widget `_gate_values` reads. (setChecked is above the
        # connects, so neither fires during construction.)
        check.toggled.connect(self._refresh_advanced_label)
        sec.add_widget(check)
        self._getters[gate_key] = lambda c=check: c.isChecked()
        self._setters[gate_key] = lambda v, c=check: c.setChecked(bool(v))
        # The gate is a section-body widget, not a QFormLayout row, so it is the one
        # schema key with an EMPTY row list (registered so lookups never KeyError).
        # That makes `_set_field_visible(gate_key, ...)` a deliberate no-op, which is
        # the behaviour we want: `_apply_section_visibility` keys off the checkbox's
        # STATE, so hiding the gate would strand its section's fields ungovernable.
        self._widgets[gate_key] = check
        self._rows.setdefault(gate_key, [])

        container = QtWidgets.QWidget()
        cbox = QtWidgets.QVBoxLayout(container)
        cbox.setContentsMargins(0, 0, 0, 0)
        sec.add_widget(container)
        self._collapse[section] = container

        blurb = SECTION_HELP.get(section)
        if blurb:
            lab = QtWidgets.QLabel(blurb)
            lab.setProperty("muted", True)
            lab.setWordWrap(True)
            cbox.addWidget(lab)
        form = QtWidgets.QFormLayout()
        cbox.addLayout(form)
        for f in fields:
            if f.key == gate_key:
                continue
            self._add_field(form, f, stored.get(f.key, f.default))
        if self._vm_factory is not None:
            extra = self._vm_factory(container)
            if extra is not None:
                cbox.addWidget(extra)
                self._vm_panel = extra  # so Revert changes can reset it too
        self._apply_section_visibility()

    def _apply_section_visibility(self, *_):
        for gate_key, section in self._gate_keys.items():
            container = self._collapse.get(section)
            if container is not None:
                container.setVisible(bool(self._getters[gate_key]()))

    def _set_field_visible(self, key: str, visible: bool) -> None:
        """Show or hide every form row ONE field occupies — its label+chip row AND
        its help row, so a hidden control never leaves its explanation behind.

        Composes with the two coarser visibility mechanisms rather than fighting
        them: a row hidden here stays hidden through a VM gate off/on cycle and a
        section collapse/expand, and showing a row inside a gated-off container
        does not force the container open. `KeyError` on an unregistered key is
        deliberate — every schema key IS registered, so a miss is a typo or a key
        that was deleted from the schema, not a field that happens to be hidden.
        `vm_enabled` registers zero rows, which makes it a legitimate no-op.
        """
        for form, row in self._rows[key]:
            form.setRowVisible(row, visible)

    # ---- conditional visibility (settings.Field.show_if) ---------------------

    def _gate_keys_in_use(self) -> list[str]:
        """Every field named as another field's `show_if` gate, deduped, in schema
        order.

        This is also the full set of keys `settings.is_visible` can consult: a
        gate that is itself gated (`gemini_auth`) appears here because something
        gates on IT, and its own gate (`tailor_provider`) appears for the same
        reason — so a chain of any depth is covered without walking it.
        """
        keys: list[str] = []
        for f in settings.SETTINGS_SCHEMA:
            if f.show_if is not None and f.show_if[0] not in keys:
                keys.append(f.show_if[0])
        return keys

    def _gate_values(self) -> dict[str, object]:
        """Current values of the gate fields, read LIVE from their widgets.

        Not from `settings.load()`: visibility has to follow the form as the user
        edits it, before anything is saved. Only gates are read —
        `settings.is_visible` falls back to a Field's own default for any key
        absent from this mapping.
        """
        return {key: self._getters[key]() for key in self._gate_keys_in_use()}

    def _connect_gate_signals(self) -> None:
        """Connect every gate's change signal, in ONE pass at the end of `_build`.

        Deferring this is mandatory, not stylistic: two gates render AFTER what
        they gate. `provider` follows `stage1_model` in the Scoring section, and
        `gemini_auth` lives in Engine while it gates `RESUME_TAILOR_GEMINI_API_KEY`
        in Credentials — which `SECTION_ORDER` renders FIRST. Connecting as each
        dependent is built would leave both of those gates inert.

        An unsupported gate widget raises rather than silently never firing (the
        same posture as `_set_field_visible`'s KeyError). Only a bounded-value
        control can be a gate — `show_if` compares against `Field.choices` — so
        QComboBox and QCheckBox are the whole supported set, and both are
        guaranteed to have a `_getters` entry for `_gate_values`.

        The QCheckBox arm is reachable only if you give the bool gate an explicit
        `choices=("True", "False")`: a bool Field defaults to `choices=()`, which
        `test_every_gate_names_a_real_field_and_real_choices` rejects, and
        `is_visible` compares `str(True)` — so `("true",)` and `(True,)` both
        fail their own schema-lint test. Nothing can ship broken; you just have
        to spell it that one way.
        """
        for key in self._gate_keys_in_use():
            widget = self._widgets[key]
            if isinstance(widget, QtWidgets.QComboBox):
                widget.currentTextChanged.connect(self._apply_field_visibility)
            elif isinstance(widget, QtWidgets.QCheckBox):
                widget.toggled.connect(self._apply_field_visibility)
            else:
                raise TypeError(f"{key} gates another field but renders as "
                                f"{type(widget).__name__}, which has no watched "
                                f"change signal")

    def _field_visible(self, f: settings.Field, gate_values: dict) -> bool:
        """Should one field be on screen right now?

        The single place field-level visibility is decided, so later phases
        compose HERE rather than in the loop below. The two halves AND together
        and neither overrides the other: ticking "show advanced" never reveals a
        field whose `show_if` gate is shut (it would describe machinery the
        current configuration cannot run), and an open gate never reveals an
        advanced field while the disclosure is folded.
        """
        return ((not f.advanced or self._show_advanced)
                and settings.is_visible(f, gate_values))

    def _apply_field_visibility(self, *_) -> None:
        """Re-render every field's visibility from the CURRENT gate values.

        Runs once at build, on every gate change, on the advanced toggle, and
        from `_repopulate` — so Revert, Restore defaults and snapshot-load all
        re-evaluate. Walks the whole schema, not just the gated fields, so an
        ungated field is actively asserted visible instead of merely never
        touched. The advanced count rides along because a gate change moves it.
        """
        gate_values = self._gate_values()
        for f in settings.SETTINGS_SCHEMA:
            self._set_field_visible(f.key, self._field_visible(f, gate_values))
        self._refresh_advanced_label(gate_values=gate_values)

    def _label_cell(self, f: settings.Field) -> QtWidgets.QWidget:
        cell = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(cell)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(QtWidgets.QLabel(f.label))
        # Storage tag as a small mono bordered chip (".env" / "config.json").
        tag = QtWidgets.QLabel(settings.storage_location(f))
        tag.setProperty("storageTag", True)
        theme.set_type_role(tag, "mono")
        h.addWidget(tag)
        return cell

    def _add_field(self, form: QtWidgets.QFormLayout, f, value):
        widget = self._make_widget(f, value)
        # The visible label lives in a composite cell (label + storage chip),
        # which QFormLayout can't expose as the field's label to assistive
        # tech -- name the input explicitly so screen readers announce it.
        widget.setAccessibleName(f.label)
        # A field owns TWO rows: label+chip, then the muted help paragraph.
        # Record both so `_set_field_visible` takes the explanation with the
        # control instead of leaving an orphaned paragraph on screen.
        rows = self._rows.setdefault(f.key, [])
        form.addRow(self._label_cell(f), widget)
        rows.append((form, form.rowCount() - 1))
        if f.help:
            help_lab = QtWidgets.QLabel(f.help)
            help_lab.setProperty("muted", True)
            help_lab.setWordWrap(True)
            form.addRow("", help_lab)
            rows.append((form, form.rowCount() - 1))

    def _make_widget(self, f, value):
        """Build one field's input cell and register its control in `self._widgets`.

        The registered widget is the one a caller would read, write, style or
        mark dirty. For the four composite cells that is the INNER control, not
        the container this returns: `_slider_widget` registers its `QSlider`,
        `_path_widget` and `_secret_widget` their `QLineEdit`. `multichoice` is
        the deliberate exception — it has no single inner control (one
        `QCheckBox` per choice, all of which move together), so it registers the
        cell and the per-choice boxes stay addressable through `self._multi[key]`.
        """
        if f.secret:
            return self._secret_widget(f, value)
        if f.type == "bool":
            cb = QtWidgets.QCheckBox()
            cb.setChecked(bool(value))
            self._getters[f.key] = cb.isChecked
            self._setters[f.key] = lambda v, c=cb: c.setChecked(bool(v))
            self._widgets[f.key] = cb
            return cb
        if f.type == "choice":
            combo = QtWidgets.QComboBox()
            combo.addItems([str(c) for c in f.choices])
            self._set_combo(combo, value)
            self._getters[f.key] = combo.currentText
            self._setters[f.key] = lambda v, c=combo: self._set_combo(c, v)
            self._widgets[f.key] = combo
            return combo
        if f.type == "editable_choice":
            combo = QtWidgets.QComboBox()
            combo.setEditable(True)
            combo.addItems([str(c) for c in f.choices])
            combo.setCurrentText(str(value))
            combo.lineEdit().installEventFilter(_PopupOnClick(combo))
            self._getters[f.key] = combo.currentText
            self._setters[f.key] = lambda v, c=combo: c.setCurrentText("" if v is None else str(v))
            self._widgets[f.key] = combo
            return combo
        if getattr(f, "slider", False) and f.type == "int":
            return self._slider_widget(f, value)
        if f.type == "multichoice":
            return self._multichoice_widget(f, value)
        if f.type == "list":
            txt = QtWidgets.QPlainTextEdit()
            txt.setAccessibleName(f.label)
            txt.setMinimumHeight(150)
            txt.setPlainText("\n".join(str(v) for v in (value if isinstance(value, list) else [])))
            self._lists[f.key] = txt
            self._widgets[f.key] = txt
            return txt
        if f.type == "path":
            return self._path_widget(f, value)
        edit = QtWidgets.QLineEdit("" if value is None else str(value))
        self._getters[f.key] = edit.text
        self._setters[f.key] = lambda v, e=edit: e.setText("" if v is None else str(v))
        self._widgets[f.key] = edit
        return edit

    @staticmethod
    def _set_combo(combo: QtWidgets.QComboBox, value) -> None:
        i = combo.findText("" if value is None else str(value))
        combo.setCurrentIndex(i if i >= 0 else 0)

    def _slider_widget(self, f, value):
        cell = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(cell)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)
        row = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        slider.setAccessibleName(f.label)
        slider.setMinimum(int(f.min))
        slider.setMaximum(int(f.max))
        try:
            slider.setValue(int(value))
        except (TypeError, ValueError):
            slider.setValue(int(f.default))
        readout = QtWidgets.QLabel(str(slider.value()))
        slider.valueChanged.connect(lambda v, lab=readout: lab.setText(str(v)))
        slider.setFixedWidth(220)
        h.addWidget(slider)
        h.addWidget(readout)
        h.addStretch(1)
        col.addWidget(row)
        self._getters[f.key] = lambda s=slider: str(s.value())
        self._setters[f.key] = lambda v, s=slider: s.setValue(int(v) if str(v).strip() else int(f.default))
        self._widgets[f.key] = slider    # the inner control, not the cell
        return cell

    def _multichoice_widget(self, f, value):
        cell = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(cell)
        h.setContentsMargins(0, 0, 0, 0)
        current = set(value if isinstance(value, list) else [])
        self._multi[f.key] = {}
        for choice in f.choices:
            cb = QtWidgets.QCheckBox(choice)
            cb.setChecked(choice in current)
            self._multi[f.key][choice] = cb
            h.addWidget(cb)
        h.addStretch(1)
        # No single inner control here: the field IS the row of checkboxes, so the
        # cell is what a caller styles or marks dirty. Per-choice boxes: _multi[key].
        self._widgets[f.key] = cell
        return cell

    def _path_widget(self, f, value):
        cell = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(cell)
        h.setContentsMargins(0, 0, 0, 0)
        edit = QtWidgets.QLineEdit("" if value is None else str(value))
        edit.setAccessibleName(f.label)
        h.addWidget(edit, 1)
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(lambda: self._browse(edit, f.path_kind))
        h.addWidget(browse)
        self._getters[f.key] = edit.text
        self._setters[f.key] = lambda v, e=edit: e.setText("" if v is None else str(v))
        self._widgets[f.key] = edit     # the inner control, not the cell
        return cell

    def _secret_widget(self, f, value):
        """A secret field is MASKED by default (restyle 3f — locked user
        decision): the saved value is present in the box (straight from the
        local .env — nothing leaves this PC) but shown as dots until the Hide
        toggle is unticked. Edit it to change it, clear the box to remove the
        key."""
        cell = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(cell)
        h.setContentsMargins(0, 0, 0, 0)
        edit = QtWidgets.QLineEdit("" if value is None else str(value))
        edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        edit.setPlaceholderText("not set")
        h.addWidget(edit, 1)
        hide = QtWidgets.QCheckBox("Hide")
        hide.toggled.connect(lambda on, e=edit: e.setEchoMode(
            QtWidgets.QLineEdit.EchoMode.Password if on
            else QtWidgets.QLineEdit.EchoMode.Normal))
        hide.setChecked(True)
        h.addWidget(hide)
        self._secret_edits[f.key] = edit
        self._secret_hides[f.key] = hide
        self._widgets[f.key] = edit     # the inner control, not the cell
        return cell

    def _add_buttons(self):
        bar = QtWidgets.QHBoxLayout()
        save = QtWidgets.QPushButton("Save settings")
        save.setProperty("accent", True)
        save.clicked.connect(self.save)
        bar.addWidget(save)
        revert = QtWidgets.QPushButton("Discard changes")
        revert.setProperty("tier", "tertiary")
        revert.clicked.connect(self.revert)
        bar.addWidget(revert)
        archive = QtWidgets.QPushButton("Restore from archive…")
        archive.clicked.connect(self.open_archive)
        bar.addWidget(archive)
        restore = QtWidgets.QPushButton("Restore defaults")
        restore.setProperty("tier", "tertiary")
        restore.clicked.connect(self.restore_defaults)
        bar.addWidget(restore)
        self.status = QtWidgets.QLabel("")
        self.status.setProperty("muted", True)
        bar.addWidget(self.status)
        bar.addStretch(1)
        self._body.addLayout(bar)

    # ---- actions -------------------------------------------------------------

    def _browse(self, edit: QtWidgets.QLineEdit, kind: str) -> None:
        if kind == "file":
            chosen, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select file")
        else:
            chosen = QtWidgets.QFileDialog.getExistingDirectory(self, "Select folder")
        if chosen:
            edit.setText(chosen)

    def collect(self) -> tuple[dict, dict[str, str]]:
        """Read every field's current value out of its widget.

        THE ROUND-TRIP GUARANTEE — do not "optimise" this to iterate
        `settings.visible_keys()`. It walks the SCHEMA and reads `self._getters`,
        so a field hidden by `show_if` still returns the value it is holding and
        still gets written. Iterate visible fields instead and the very first
        Save after switching `provider` to claude silently wipes both Gemini
        model ids off disk (they fall out of `values`, `settings.save` never
        groups them, and the next `load` hands back the schema default) — the one
        failure mode `test_provider_round_trip_does_not_wipe_hidden_model_choices`
        exists to catch.
        """
        values: dict = {}
        errors: dict[str, str] = {}
        for f in settings.SETTINGS_SCHEMA:
            if f.secret:
                # The box shows the saved value, so whatever it holds is the truth:
                # write it as-is (an empty box clears the key).
                values[f.key] = self._secret_edits[f.key].text()
                continue
            if f.type == "multichoice":
                values[f.key] = [c for c, cb in self._multi[f.key].items() if cb.isChecked()]
                continue
            if f.type == "list":
                raw = self._lists[f.key].toPlainText()
                values[f.key] = [ln.strip() for ln in raw.splitlines() if ln.strip()]
                continue
            value, err = self._coerce(f, self._getters[f.key]())
            values[f.key] = value
            if err:
                errors[f.key] = err
        return values, errors

    @staticmethod
    def _coerce(f: settings.Field, raw):
        if f.type == "bool":
            return bool(raw), None
        text = str(raw).strip()
        if f.type == "int":
            try:
                return int(text), None
            except ValueError:
                return raw, f"{f.label}: must be a whole number."
        return text, None

    @staticmethod
    def _changed_summary(before: dict, values: dict) -> list[str]:
        def fmt(v):
            s = "" if v is None else str(v)
            return s if s != "" else "(blank)"

        by_key = {f.key: f for f in settings.SETTINGS_SCHEMA}
        out: list[str] = []
        for key, new in values.items():
            f = by_key.get(key)
            if f is None:
                continue
            old = before.get(key, f.default)
            if f.secret:
                # never echo a secret value — report only that it changed
                if str(old) != str(new):
                    out.append(f"{f.label}: {'cleared' if str(new).strip() == '' else 'updated'}")
                continue
            if f.type == "multichoice":
                if set(old or []) != set(new or []):
                    out.append(f"{f.label}: updated ({len(new or [])} selected)")
            elif f.type == "list":
                if [str(x).strip() for x in (old or [])] != [str(x).strip() for x in (new or [])]:
                    out.append(f"{f.label}: updated ({len(new or [])} items)")
            elif old != new:
                out.append(f"{f.label}: {fmt(old)} -> {fmt(new)}")
        return out

    def save(self) -> bool:
        values, errors = self.collect()
        labels = {f.key: f.label for f in settings.SETTINGS_SCHEMA}
        errors.update(settings.validate(values))
        if errors:
            msg = "\n".join(f"{labels.get(k, k)}: {m}" for k, m in errors.items())
            self.status.setText("Not saved — see error.")
            QtWidgets.QMessageBox.critical(self, "Settings", msg)
            return False
        before = settings.load(self.targets)
        try:
            settings.save(values, self.targets)
        except (ValueError, OSError) as exc:
            self.status.setText("Save failed.")
            QtWidgets.QMessageBox.critical(self, "Settings", str(exc))
            return False
        summary = self._changed_summary(before, values)
        archived = self._archive_after_save(values) if summary else False
        self._opening_values = settings.load(self.targets)
        self._sync_secret_boxes(self._opening_values)  # reflect the canonical stored values
        self.status.setText("Saved." if summary else "Saved — no changes.")
        if summary:
            note = "\n\nA snapshot was saved to the archive." if archived else ""
            QtWidgets.QMessageBox.information(
                self, "Settings", "Settings saved. Updated:\n\n- " + "\n- ".join(summary) + note)
        else:
            QtWidgets.QMessageBox.information(
                self, "Settings", "No changes to save — your settings are unchanged.")
        self._maybe_prompt_vm_push(before, values, summary)
        if self.on_saved:
            self.on_saved()
        return True

    @staticmethod
    def _value_changed(f: settings.Field, old, new) -> bool:
        """Did a field's value change between two settings dicts? Mirrors the
        per-type comparison in `_changed_summary` (set-wise for multichoice,
        normalised for lists, string-wise for secrets)."""
        if f.type == "multichoice":
            return set(old or []) != set(new or [])
        if f.type == "list":
            return ([str(x).strip() for x in (old or [])]
                    != [str(x).strip() for x in (new or [])])
        if f.secret:
            return str(old) != str(new)
        return old != new

    def _maybe_prompt_vm_push(self, before: dict, values: dict, summary: list[str]) -> None:
        """After a Save that changed something, if VM features are on and a changed
        setting is one the VM reads from its OWN config copy (a 'search'/'scoring'
        target — see vm_sync.TARGET_REMOTE_FILE), offer to push the updated config
        up to the VM. Nothing else reminds the user their VM config has drifted."""
        if not summary or not values.get("vm_enabled"):
            return
        by_key = {f.key: f for f in settings.SETTINGS_SCHEMA}
        changed_vm: set[str] = set()
        for key in values:
            f = by_key.get(key)
            if f is None or f.target not in vm_sync.TARGET_REMOTE_FILE:
                continue  # unknown key or a setting the VM doesn't read — ignore
            if self._value_changed(f, before.get(key, f.default), values[key]):
                changed_vm.add(key)
        if not changed_vm:
            return
        text = ("You changed settings the VM reads from its config copy. Push the "
                "updated config to the VM now?")
        if "drop_easy_apply" in changed_vm:
            text += ("\n\nNote: score_jobs.py itself must be re-uploaded to the VM once "
                     "(there is no automated code push) — run:\n"
                     "  gcloud compute scp score_jobs.py <user>@<vm>:~ --zone=<zone>")
        if QtWidgets.QMessageBox.question(
                self, "Push config to VM?", text,
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        if self._vm_panel is not None:
            self._vm_panel.push_config(skip_confirm=True)
        else:
            QtWidgets.QMessageBox.information(
                self, "Push config to VM?",
                "Turn on the VM section and use its 'Push config to VM' button to "
                "copy the updated config up.")

    def _archive_after_save(self, values: dict) -> bool:
        """Snapshot all settings then apply the retention policy. Never raises into
        Save — archiving is a safety net, not something that should be able to block
        a save.

        One `archive_mode` drives both halves: "Off" takes no new snapshot (and
        leaves existing ones alone), and every other value carries its own
        retention in the string, so `prune` needs no count keyword here."""
        mode = str(values.get("archive_mode", settings.ARCHIVE_KEEP_ALL))
        if mode == settings.ARCHIVE_OFF:
            return False
        try:
            made = settings_archive.snapshot(self.targets)
            settings_archive.prune(mode, targets=self.targets)
            return made is not None
        except OSError:
            return False

    def _sync_secret_boxes(self, values_now: dict) -> None:
        """Set each secret box to its current stored value, re-masked (secrets
        are masked by default; only an explicit Hide-untick reveals them)."""
        for key, edit in self._secret_edits.items():
            v = values_now.get(key, "")
            edit.setText("" if v is None else str(v))
            self._secret_hides[key].setChecked(True)

    def _repopulate(self, value_for: Callable[[settings.Field], object]) -> None:
        for f in settings.SETTINGS_SCHEMA:
            if f.secret:
                continue
            val = value_for(f)
            if f.type == "multichoice":
                want = set(val if isinstance(val, list) else [])
                for choice, cb in self._multi[f.key].items():
                    cb.setChecked(choice in want)
            elif f.type == "list":
                self._lists[f.key].setPlainText(
                    "\n".join(str(v) for v in (val if isinstance(val, list) else [])))
            else:
                self._setters[f.key](val)
        self._apply_section_visibility()
        # Revert / Restore defaults / snapshot-load all land here, and any of them
        # can change a gate. Re-evaluate EXPLICITLY: every gate is a QComboBox
        # today, whose setter emits currentTextChanged and would get there by
        # itself, but that is a side-effect of the widget type, not a contract.
        self._apply_field_visibility()

    def restore_defaults(self) -> None:
        # Defaults reset the tunables but never wipe saved keys — leave secrets as-is.
        self._repopulate(lambda f: f.default)
        if self._vm_panel is not None:
            self._vm_panel.revert()
        self.status.setText("Defaults restored — press Save to apply.")

    def revert(self) -> None:
        self._repopulate(lambda f: self._opening_values.get(f.key, f.default))
        self._sync_secret_boxes(self._opening_values)
        if self._vm_panel is not None:
            self._vm_panel.revert()
        self.status.setText("Reverted to your last-opened settings — press Save to apply.")

    def open_archive(self) -> None:
        ArchiveDialog(self).exec()

    def load_from_snapshot(self, snap_path) -> None:
        """Fill the form from a saved snapshot for review. Nothing is written until
        the user clicks Save. A snapshot only carries the secrets it actually had,
        so a key the snapshot didn't include is left at its current value."""
        vals = settings_archive.load_snapshot(snap_path, self.targets)
        self._repopulate(lambda f: vals.get(f.key, f.default))
        snap_secrets = settings_archive.snapshot_secrets(snap_path, self.targets)
        for key, edit in self._secret_edits.items():
            self._secret_hides[key].setChecked(False)
            if key in snap_secrets:
                edit.setText(snap_secrets[key])
        self.status.setText("Loaded snapshot — review the fields, then Save to apply.")


class ArchiveDialog(QtWidgets.QDialog):
    """Browse saved settings snapshots: preview, load into the form, or delete one."""

    def __init__(self, form: SettingsForm, parent=None):
        super().__init__(parent or form)
        self._form = form
        self._snaps: list[settings_archive.Snapshot] = []
        self.setWindowTitle("Settings archive")
        self.resize(560, 470)

        v = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "Saved snapshots (newest first). Load one into the form to review, then Save to apply it. "
            "Secrets are restored too — loading un-masks them in the form so you can review "
            "exactly what would be saved.")
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        v.addWidget(intro)

        self.listw = QtWidgets.QListWidget()
        self.listw.currentRowChanged.connect(self._on_select)
        v.addWidget(self.listw, 1)

        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setAccessibleName("File preview")
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(150)
        v.addWidget(self.preview)

        bar = QtWidgets.QHBoxLayout()
        self.load_btn = QtWidgets.QPushButton("Load into form")
        self.load_btn.setProperty("accent", True)
        self.load_btn.clicked.connect(self._load)
        bar.addWidget(self.load_btn)
        self.del_btn = QtWidgets.QPushButton("Delete")
        self.del_btn.clicked.connect(self._delete)
        bar.addWidget(self.del_btn)
        bar.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.reject)
        bar.addWidget(close)
        v.addLayout(bar)

        self._refresh()

    def _refresh(self) -> None:
        self.listw.clear()
        self._snaps = settings_archive.list_snapshots(self._form.targets)
        for s in self._snaps:
            self.listw.addItem(s.label)
        has = bool(self._snaps)
        self.load_btn.setEnabled(has)
        self.del_btn.setEnabled(has)
        if has:
            self.listw.setCurrentRow(0)
        else:
            self.preview.setPlainText("No snapshots yet — they're created each time you Save.")

    def _current(self) -> settings_archive.Snapshot | None:
        i = self.listw.currentRow()
        return self._snaps[i] if 0 <= i < len(self._snaps) else None

    def _on_select(self, *_) -> None:
        s = self._current()
        if s is None:
            return
        vals = settings_archive.load_snapshot(s.path, self._form.targets)
        lines = [f"Snapshot: {s.label}", ""]
        for f in settings.SETTINGS_SCHEMA:
            if f.secret:
                continue  # never preview secret values
            val = vals.get(f.key, f.default)
            if isinstance(val, list):
                val = f"[{len(val)} items]"
            lines.append(f"{f.label}: {val}")
        self.preview.setPlainText("\n".join(lines))

    def _load(self) -> None:
        s = self._current()
        if s is not None:
            self._form.load_from_snapshot(s.path)
            self.accept()

    def _delete(self) -> None:
        s = self._current()
        if s is None:
            return
        if QtWidgets.QMessageBox.question(
                self, "Delete snapshot",
                f"Delete snapshot {s.label}? This cannot be undone."
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        settings_archive.delete_snapshot(s.path)
        self._refresh()
