"""Schema-driven settings form (Qt) — mounted as the dashboard's Settings tab.

Renders `settings.SETTINGS_SCHEMA` grouped by section: one labelled, explained
input per Field, the right widget per type (dropdown / editable dropdown / slider /
spin box / checkboxes / multiline list / path+Browse / credential (masked by
default, with a Hide toggle) / entry), a muted "(filename)" storage tag, a
"restart" chip on every `Field.restart` row, a collapsible VM section gated by a
master checkbox, a "Show advanced settings (N hidden)" disclosure that folds
every `Field.advanced` row away, a debounced search box that filters the whole
tab by label / help / config key / chip, and Save / Discard changes / Restore from
archive / Restore defaults. Save validates via `settings.validate`/
`settings.save`, reports a changed-field summary, names any restart-required
setting that summary contains, and never echoes a secret value into either.

THREE VIEW FOLDS, ONE CONFIGURATION GATE. A collapsed section, the advanced
disclosure and an active search all hide rows the settings still apply to, so the
form opens them on the user's behalf when it has to (`_reveal_view_folds`) and
never persists an opening it made itself. A `show_if` gate and the VM master
switch are the other kind — they mean "this does nothing for the way you have
things set up" — so the form names them instead of flipping them.

Problems are reported IN PLACE, not in a modal: the offending input outlines in
red, a danger note appears under it, the form scrolls to the first one the user
can act on, and the status line counts them ("2 settings need fixing"). The one
surviving modal is the disk-failure arm of Save — a rejected field is something
you can see and fix where you are, an unwritable config.json is neither.
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
    # Rewritten in P8: the old blurb ("which models score jobs … changing the model
    # names can silently break scoring") described rows a fresh profile cannot see.
    # P4 made all four model pickers `advanced`, so what renders by default is
    # provider / stage2_threshold / max_scored_per_run / min_filter_years — a
    # section header describing a screen the reader is not looking at is worse than
    # none, because it reads as a bug in the form.
    "Scoring": ("Which AI service scores collected jobs, the score that earns a deeper "
                "second pass, and the caps on how much one run may spend. The per-stage "
                "model names and throughput knobs live under \"Show advanced settings\" — "
                "their defaults are tuned, and a model id your account cannot use breaks "
                "scoring silently."),
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
    # Not "(advanced)": the section is not, and four of its rows are on screen at
    # shipped defaults. The parenthetical was a leftover from before P4 gave the
    # word a specific meaning in this tab (the disclosure checkbox), and a tagline
    # that claims a folded-away section reads as an empty one.
    "Scoring": "Scoring engine, thresholds & spend guards",
    "Resume": "Cover letter, ATS report & prep-sheet toggles",
    "Auto-apply": "Batch cap & inbox for the apply agent",
    "Settings history": "Snapshot & restore your settings",
    "VM (cloud scraper)": "Manage the cloud job-discovery VM",
}
COLLAPSIBLE_SECTIONS = {"VM (cloud scraper)": "vm_enabled"}

# The two flavours of inline note a field row can carry (one at a time — see
# `_set_field_note`). An ERROR blocks Save; a WARN is information about what the
# form already did to a stored value.
NOTE_ERROR = "error"
NOTE_WARN = "warn"

# How long the search box waits after the last keystroke before re-filtering.
# One refilter walks ~60 fields and ~130 form rows and can force-expand ten
# sections, so doing it per keystroke is what makes a filter box feel broken.
SEARCH_DEBOUNCE_MS = 180


_repolish = theme.repolish   # dynamic properties only restyle if you ask (see theme)


class _FocusOutValidator(QtCore.QObject):
    """Validate one field the moment focus leaves it.

    Finding out at Save time that a box has been wrong for ten minutes is the
    modal's other failure, so leaving the field is when to say so. Installed on
    the REGISTERED control; note that a `QSpinBox` hands focus to its internal
    line edit, so its FocusOut arrives there rather than here — harmless, because
    a spin box is structurally in range and has nothing to report.
    """

    def __init__(self, form: "SettingsForm", key: str):
        super().__init__(form)          # parented: lives exactly as long as the form
        self._form = form
        self._key = key

    def eventFilter(self, obj, event):  # noqa: N802 - Qt override name
        if event.type() == QtCore.QEvent.Type.FocusOut:
            self._form._validate_field(self._key)
        return False


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
        # Inline validation. `_notes` is one always-present, initially EMPTY and
        # HIDDEN QLabel per field, sitting inside the field's own cell (see
        # `_add_field`) — which is why nothing here has to touch the positional
        # row indices above. `_errors` is the subset currently blocking Save.
        self._notes: dict[str, QtWidgets.QLabel] = {}
        self._errors: dict[str, str] = {}
        # Unsaved-change markers. `_dirty` is every key whose value differs from
        # `_opening_values` — i.e. exactly what the next Save would write — and
        # `_off_default` every key a ↺ button would move. Both are kept as SETS,
        # not recomputed onto every widget, so an edit repolishes only the markers
        # that actually flipped (a keystroke would otherwise restyle ~60 rows).
        # `_dots` has no entry for `vm_enabled`, which has no form row to hold one.
        self._dirty: set[str] = set()
        self._off_default: set[str] = set()
        # Set while `_repopulate` is writing 60 values in a row: each setter emits,
        # so without it one Revert costs 60 full rescans of the form. Every path
        # that raises it does its own single refresh afterwards.
        self._suspend_markers: bool = False
        self._dots: dict[str, QtWidgets.QLabel] = {}
        self._resets: dict[str, QtWidgets.QToolButton] = {}
        self._section_counts: dict[str, tuple[int, str]] = {}
        self._save_btn: QtWidgets.QPushButton | None = None
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

        # Search / filter. `_search_terms` is the parsed, lower-cased, AND-ed term
        # list and is the ONLY search state anything else reads — empty means "not
        # searching", which is the single test every branch below keys off.
        # Deliberately NOT persisted: a filter is a thing you are doing right now,
        # and reopening the tab into a two-section view with no memory of why is
        # the same trap as a search that leaves the sections unfolded.
        self._search_terms: list[str] = []
        # One "(advanced)" chip per advanced field, built hidden in `_label_cell`
        # and shown only while a search is running (search ignores `advanced`, so a
        # result needs to say when it is one). `_search_footer` is the muted line
        # naming the gates keeping matches off screen — also built hidden, because
        # `self._rows` holds positional indices and nothing may be inserted later.
        self._advanced_tags: dict[str, QtWidgets.QLabel] = {}
        self._search_footer: QtWidgets.QLabel | None = None

        self._build()

    # ---- construction --------------------------------------------------------

    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        self._scroll = scroll   # Save scrolls the first offender into view
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

        self._add_search_box()
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

        # Below the sections, because it is the footer of the RESULTS: the muted
        # "3 more settings apply when …" line. Built before the first visibility
        # render, which is what fills it in.
        self._add_search_footer()

        # Second pass, AFTER every field exists: wire the `show_if` gates and do
        # the first visibility render. See `_connect_gate_signals` for why this
        # cannot happen inside the section loop above.
        self._connect_gate_signals()
        self._apply_field_visibility()

        self._add_buttons()
        self._body.addStretch(1)
        # Last, because it writes to the Save button `_add_buttons` just made. Not
        # a no-op on a fresh form either: a stored int the widget had to CLAMP is
        # genuinely dirty — the form is holding 5000 where the file says 99999, and
        # the next Save writes it — so the marker opens alongside P5's note saying
        # so, rather than the two disagreeing.
        self._refresh_dirty()

    # ---- search / filter ------------------------------------------------------

    def _add_search_box(self) -> None:
        """The filter box, at the very top of the form.

        This is what legitimises the two folds below it. Hiding a setting behind
        a disclosure toggle or a collapsed section is only defensible while the
        setting stays FINDABLE; without a search an advanced or folded-away row is
        genuinely lost, and the user's only recourse is to scroll ~130 rows
        looking for a word they already know.
        """
        box = QtWidgets.QLineEdit()
        box.setPlaceholderText("Search settings — name, description, or config key…")
        box.setClearButtonEnabled(True)
        box.setAccessibleName("Search settings")
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(SEARCH_DEBOUNCE_MS)
        timer.timeout.connect(self._commit_search)
        box.textChanged.connect(lambda *_a: timer.start())
        self._search_edit = box
        self._search_timer = timer
        self._body.addWidget(box)

    def _add_search_footer(self) -> None:
        """The muted line under the results naming the gates holding matches back.

        Built empty and HIDDEN, then toggled — never inserted on demand. Same
        constraint as the per-field notes: `self._rows` holds positional
        `QFormLayout` indices captured at build, and while this label lives in
        `self._body` rather than in a form layout, "build it hidden" is the rule
        this form follows so nothing has to reason about which containers are
        index-sensitive.
        """
        lab = QtWidgets.QLabel("")
        lab.setProperty("muted", True)
        lab.setWordWrap(True)
        lab.setVisible(False)
        self._search_footer = lab
        self._body.addWidget(lab)

    @staticmethod
    def _search_haystack(f: settings.Field) -> str:
        """What one field is searchable BY: its label, its help, its config key,
        and the text of any CHIP the row carries.

        The key is not padding. A user reading `.env`, a GitHub issue or this
        project's docs searches `GEMINI_API_KEYS` — a string that appears in no
        label and in no help text — and that person is the most likely to reach
        for a search box in the first place.

        The chips are here for the same reason, and P8 is what made it necessary:
        it deleted "Restart the dashboard after changing" from three help strings
        and "Advanced:" from two more, because a chip now says both — and
        `test_no_restart_field_repeats_the_word_in_its_help` ENFORCES the
        deletion. Without this line the word printed on a row's own chip finds
        nothing in the box directly above it, which is the exact failure the key
        is included to avoid. The words have to be added here rather than put back
        in the help, since the lint forbids the latter.
        """
        return ("\n".join([f.label, f.help, f.key]
                          + (["restart"] if f.restart else [])
                          + (["advanced"] if f.advanced else []))).lower()

    def _field_matches(self, f: settings.Field) -> bool:
        """Does `f` match the current terms? Every term must hit (AND), anywhere in
        the haystack, case-insensitively. No terms = everything matches."""
        hay = self._search_haystack(f)
        return all(t in hay for t in self._search_terms)

    def set_search(self, text: str) -> None:
        """Drive the box programmatically and re-filter NOW, skipping the debounce.

        The one entry point for a caller (or a test) that wants the filter applied
        before the next event loop turn — `_reveal_view_folds` needs exactly that,
        because it has to put a rejected field on screen inside the same Save.
        """
        self._search_timer.stop()
        self._search_edit.blockSignals(True)
        self._search_edit.setText(text)
        self._search_edit.blockSignals(False)
        self._commit_search()

    def _commit_search(self) -> None:
        """Re-read the box and re-filter. What the debounce timer fires.

        The early return is not just a saving: while a search is running,
        `_apply_field_visibility` force-expands every matching section, so
        re-committing an unchanged term would undo a fold the user had just made
        by hand mid-search. Note the exact scope of that — an UNCHANGED term. A
        section the user folds mid-search and then types another letter into is
        force-expanded again, because the new term is a new set of results and
        every matching section is opened for it. The fold is not lost: it went
        into `self._collapsed` like any other, so clearing the search restores it.
        """
        terms = self._search_edit.text().lower().split()
        if terms == self._search_terms:
            return
        was_searching = bool(self._search_terms)
        self._search_terms = terms
        self._apply_field_visibility()
        if was_searching and not terms:
            self._restore_collapse_state()

    def _restore_collapse_state(self) -> None:
        """Put every section back to the fold the USER chose, on clearing a search.

        `self._collapsed` is untouched by search from end to end — force-expanding
        goes through `CollapsibleSection.set_collapsed`, which does not fire
        `on_toggled` — so this is a restore, not a guess. That is the whole point:
        this repo's owner runs with 9 of the 10 sections folded, and a search that
        permanently unfolds the tab is worse than no search at all.

        Called only on the searching → cleared transition, never from
        `_apply_field_visibility`: that runs on every gate change, and re-asserting
        the persisted fold there would immediately re-collapse the section
        `_reveal_view_folds` deliberately opened without recording.
        """
        for section, sec in self._section_widgets.items():
            sec.set_collapsed(section in self._collapsed)

    def _apply_search_chrome(self, gate_values: dict) -> None:
        """Everything a search changes beyond the field rows themselves: the
        "(advanced)" chips, which sections survive, and the gate footer.

        A section with zero surviving rows is hidden outright rather than left as
        an empty card — ten headers with nothing under them is the same wall of
        chrome the filter exists to cut. A section WITH a hit is force-expanded,
        because a result the user still has to go and unfold is not a result.

        `shown` counts `_field_visible`, which is ROW-FLAG level and knows nothing
        about the VM section's master switch hiding the container those rows sit
        in. That is deliberate, not the oversight it looks like: search "gcloud"
        with VM features off and both matches are behind that switch, so no row
        survives — and the card stays anyway, because the switch is INSIDE it.
        Counting "really on screen" would drop the card and leave the footer
        telling the user to flip a control the same search had just hidden.
        Pinned by `test_a_section_whose_only_matches_are_gated_keeps_its_switch_on_screen`.
        """
        searching = bool(self._search_terms)
        for tag in self._advanced_tags.values():
            tag.setVisible(searching)
        # The disclosure governs nothing while a search is running — search ignores
        # `advanced` — so it neither counts (see `_advanced_hidden_count`) nor
        # pretends to be live.
        self._advanced_check.setEnabled(not searching)
        self._advanced_check.setToolTip(
            "Search results already include advanced settings" if searching else "")
        if searching:
            shown: dict[str, int] = dict.fromkeys(self._section_widgets, 0)
            for f in settings.SETTINGS_SCHEMA:
                if f.section in shown and self._field_visible(f, gate_values):
                    shown[f.section] += 1
            for section, sec in self._section_widgets.items():
                sec.setVisible(bool(shown[section]))
                if shown[section] and sec.is_collapsed():
                    sec.set_collapsed(False)   # a view change, so never persisted
        else:
            for sec in self._section_widgets.values():
                sec.setVisible(True)
        self._refresh_search_footer()

    def _refresh_search_footer(self) -> None:
        """Name the gates keeping matching settings off screen — or say nothing.

        The honest third option. A search that silently omits results teaches the
        user the tool lies; one that lists a field their configuration makes inert
        hands them a row that does nothing. Naming the gate does neither: the
        setting is accounted for AND the sentence says what would bring it back.
        """
        if self._search_footer is None:
            return                      # mid-build; `_build` fills it in right after
        text = self._gated_out_summary() if self._search_terms else ""
        self._search_footer.setText(text)
        self._search_footer.setVisible(bool(text))

    def _gated_out_summary(self) -> str:
        """"3 more settings apply when Scoring provider is 'claude'", per gate.

        Grouped by the gate CONDITION and emitted in schema order, so five hidden
        matches behind two gates read as two clauses rather than five.
        """
        counts: dict[str, int] = {}
        for f in settings.SETTINGS_SCHEMA:
            if not self._field_matches(f):
                continue
            condition = self._gate_condition(f)
            if condition is not None:
                counts[condition] = counts.get(condition, 0) + 1
        return "; ".join(
            f"{n} more setting{'' if n == 1 else 's'} "
            f"{'applies' if n == 1 else 'apply'} when {condition}"
            for condition, n in counts.items())

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

    def _shut_section_gate(self, f: settings.Field) -> settings.Field | None:
        """The master switch of `f`'s section when it is OFF — or None.

        `f.key != gate_key` is the carve-out that makes this usable, and it is
        the fix for a defect three features said out loud: A MASTER SWITCH IS
        NEVER HIDDEN BY ITSELF. It is the control that OPENS its section, so it
        is on screen precisely when the gate is shut. Reading the gate without
        that exemption made every sentence built off this walk circular — P7's
        search footer printed "1 more setting applies when Enable VM features is
        on" underneath a form plainly showing that checkbox, and P6's section
        badge told a user who had just unticked it that their unsaved change was
        "not shown on this form" and that the fix was to turn on the thing they
        had just turned off.
        """
        gate_key = COLLAPSIBLE_SECTIONS.get(f.section)
        if gate_key is None or f.key == gate_key or self._getters[gate_key]():
            return None
        return next(x for x in settings.SETTINGS_SCHEMA if x.key == gate_key)

    def _section_gate_open(self, f: settings.Field) -> bool:
        """Is the master checkbox of `f`'s section (if it has one) switched on?

        Read ONLY by the advanced count, never by `_field_visible`. The section
        gate already hides its fields by hiding their whole container, so
        repeating it per row would fight `_set_field_visible`'s property that
        showing a row inside a gated-off container does not force the container
        open — and would make this a second field-visibility path.
        """
        return self._shut_section_gate(f) is None

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
        the label — and zero again while a SEARCH is running, for the same reason
        rather than a different one: search ignores `advanced`, so the checkbox is
        withholding nothing and a count there would promise rows a tick cannot
        deliver. (What the search itself is withholding is not this label's claim;
        the gate footer makes that one.)
        """
        if self._show_advanced or self._search_terms:
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
        # This switch decides whether its section's fields apply at all, so it
        # moves BOTH things that report on withheld settings: the disclosure count
        # and — since P7 — the search footer naming the gates ("2 more settings
        # apply when Enable VM features is on"). `_apply_field_visibility` drives
        # both, and connecting only the label left that footer standing after the
        # user flipped the very switch it named. Connected HERE rather than from
        # `_apply_section_visibility`, which also runs mid-`_build` — neither
        # claim may depend on SECTION_ORDER happening to render this section after
        # every gate widget `_gate_values` reads. (setChecked is above the
        # connects, so nothing fires during construction.)
        check.toggled.connect(self._apply_field_visibility)
        # ...and it is a real setting, so flipping it is a real unsaved change.
        # `_add_field` never sees this widget, so the one hook every other field
        # gets through `_connect_field_signals` has to be wired by hand. It has no
        # form row, hence no dot and no ↺ — but it counts, in the Save button and
        # in its section's header, because the Save WILL write it.
        check.toggled.connect(lambda *_a: self._on_field_edited(gate_key))
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
                self._vm_panel = extra  # so Discard changes can reset it too
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

        The single place field-level visibility is decided — P3 put the gate here,
        P4 composed the advanced flag in, and search composes here too rather than
        opening a third rule that the other two would have to be kept in step with.

        The gate half is in EVERY branch and nothing overrides it: ticking "show
        advanced" never reveals a field whose `show_if` gate is shut (it would
        describe machinery the current configuration cannot run), and neither does
        a search hit.

        What search DOES override is `advanced`, deliberately and asymmetrically.
        The disclosure is a view fold — "not now" — and a user who has typed the
        name of the thing they want has said "now"; leaving an advanced row out of
        its own search results is what would make the fold indefensible. A
        `show_if` gate is not a fold, so it survives, and `_refresh_search_footer`
        names it instead of dropping the field in silence.
        """
        if self._search_terms:
            return self._field_matches(f) and settings.is_visible(f, gate_values)
        return ((not f.advanced or self._show_advanced)
                and settings.is_visible(f, gate_values))

    def _apply_field_visibility(self, *_) -> None:
        """Re-render every field's visibility from the CURRENT gate values.

        Runs once at build, on every gate change, on the advanced toggle, on every
        committed search, and from `_repopulate` — so Revert, Restore defaults and
        snapshot-load all re-evaluate. Walks the whole schema, not just the gated
        fields, so an ungated field is actively asserted visible instead of merely
        never touched. The advanced count and the search chrome ride along because
        a gate change moves both (flip `provider` mid-search and a section can go
        from three hits to none).

        It does NOT restore the persisted collapse state — see
        `_restore_collapse_state` for why that would fight `_reveal_view_folds`.
        """
        gate_values = self._gate_values()
        for f in settings.SETTINGS_SCHEMA:
            self._set_field_visible(f.key, self._field_visible(f, gate_values))
        self._apply_search_chrome(gate_values)
        self._refresh_advanced_label(gate_values=gate_values)

    def _label_cell(self, f: settings.Field) -> QtWidgets.QWidget:
        cell = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(cell)
        h.setContentsMargins(0, 0, 0, 0)
        # The unsaved-change dot, ahead of the label. Always present at a FIXED
        # width and only its text/colour toggle: hiding and showing it would shunt
        # the label sideways every time a value changed, which is a lot of motion
        # to pay for one glyph.
        dot = QtWidgets.QLabel("")
        dot.setProperty("dirtyDot", True)
        dot.setProperty("dirty", False)   # a real bool from the start, never unset
        dot.setFixedWidth(12)
        self._dots[f.key] = dot
        h.addWidget(dot)
        h.addWidget(QtWidgets.QLabel(f.label))
        # Storage tag as a small mono bordered chip (".env" / "config.json").
        tag = QtWidgets.QLabel(settings.storage_location(f))
        tag.setProperty("storageTag", True)
        theme.set_type_role(tag, "mono")
        h.addWidget(tag)
        # The "restart" chip, beside the storage one because it is the same kind
        # of fact about where the value lives: this one is read out of `.env` once
        # per launch, so editing it changes a file the running process has already
        # stopped consulting. Always visible (not toggled like the advanced chip) —
        # the whole failure it addresses is a user who saves, sees "Saved.", and
        # then watches the old model / key / output folder keep being used.
        # Borrows `storageTag`'s SHAPE deliberately — the two chips are siblings
        # by design — but `restartTag` carries its own colour rule in theme.py, or
        # a reader scanning the row would just see two storage tags. (Setting a
        # dynamic property no QSS matches is the P5 defect: counted, styled,
        # invisible. `test_the_restart_chip_has_a_selector_the_widget_matches`.)
        if f.restart:
            chip = QtWidgets.QLabel("restart")
            chip.setProperty("storageTag", True)
            chip.setProperty("restartTag", True)
            theme.set_type_role(chip, "mono")
            chip.setToolTip("Saved immediately, but the dashboard reads this one at "
                            "startup — restart it for the new value to take effect.")
            h.addWidget(chip)
        # The "(advanced)" chip: built for every advanced field, hidden, and shown
        # only in search results. Search ignores `advanced` so that a folded-away
        # knob stays findable — but a result that arrives with no explanation of
        # why it was not there a moment ago is its own small confusion, and the
        # chip is also where the user learns the disclosure toggle exists.
        if f.advanced:
            chip = QtWidgets.QLabel("(advanced)")
            chip.setProperty("muted", True)
            chip.setToolTip('Normally folded away behind "Show advanced settings"')
            chip.setVisible(False)
            self._advanced_tags[f.key] = chip
            h.addWidget(chip)
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
        form.addRow(self._label_cell(f), self._input_cell(f, widget))
        rows.append((form, form.rowCount() - 1))
        if f.help:
            help_lab = QtWidgets.QLabel(f.help)
            help_lab.setProperty("muted", True)
            help_lab.setWordWrap(True)
            form.addRow("", help_lab)
            rows.append((form, form.rowCount() - 1))
        self._connect_field_signals(f)
        self._flag_a_rewritten_int(f, value)

    def _input_cell(self, f: settings.Field, widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """The field's control (plus its ↺ reset) with the inline note underneath.

        The note lives INSIDE the field's cell rather than in a form row of its
        own, and it is built here — empty and hidden — for every field, never
        added on demand. Two reasons, both load-bearing:

        * `self._rows` holds POSITIONAL QFormLayout indices captured at build, so
          an `insertRow` would silently re-point every stored index below it (see
          the constraint at the `_rows` declaration). Inside the cell there is no
          index to invalidate.
        * `_set_field_visible` flips whole rows, so a note in the cell is hidden
          and restored WITH its control for free — a note left behind by a field
          the advanced toggle just folded away would be an error message with
          nothing to attach it to.

        The reset button rides in the same cell for the same two reasons, and the
        note stays at layout index 1 so it is still the row's last word.
        """
        cell = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(cell)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)
        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(widget, 1)
        reset = self._reset_button(f)
        if reset is not None:
            row.addWidget(reset, 0)
        col.addLayout(row)
        note = QtWidgets.QLabel("")
        note.setWordWrap(True)
        note.setVisible(False)
        col.addWidget(note)
        self._notes[f.key] = note
        return cell

    def _reset_button(self, f: settings.Field) -> QtWidgets.QToolButton | None:
        """The per-field ↺, or None for a secret.

        NO reset on a credential, and not merely hidden: a secret's schema default
        is `""`, so the button would be offering to clear a live API key — an
        action with no undo (the old value is gone from .env and from the box that
        was showing it) sitting one stray click away from a row the user opened to
        read, not to edit. Every other field's default is a value you can type
        back.

        Built hidden and shown only while the value differs from the default, so a
        form at its defaults carries none of them.

        `NoFocus`, deliberately. A `QToolButton` is a tab stop by default, which
        would put ~19 one-keypress "overwrite this value" controls into the tab
        chain of a real profile — several holding text nobody can retype
        (`keywords`, the inbox map) — and double the number of stops between one
        setting and the next, which is itself the friction this cycle exists to
        remove. It follows Qt's own convention for an inline auxiliary control
        (`QLineEdit`'s built-in clear button is not a tab stop either). The
        keyboard route to a default is unchanged: select the field's text and type
        it, or use Restore defaults for the whole form.
        """
        if f.secret:
            return None
        btn = QtWidgets.QToolButton()
        btn.setText("↺")
        btn.setProperty("resetField", True)
        btn.setAutoRaise(True)
        btn.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        btn.setToolTip(f"Reset to the default ({self._default_label(f)})")
        btn.setAccessibleName(f"Reset {f.label} to its default")
        btn.setVisible(False)
        btn.clicked.connect(lambda *_a, k=f.key: self._reset_field(k))
        self._resets[f.key] = btn
        return btn

    @staticmethod
    def _default_label(f: settings.Field) -> str:
        """`f.default` as a short phrase for the ↺ tooltip — the button says what
        it would do BEFORE it does it, since the value it overwrites is gone."""
        d = f.default
        text = ", ".join(str(x) for x in d) if isinstance(d, list) else str(d)
        if not text.strip():
            return "empty" if isinstance(d, list) else "blank"
        return text if len(text) <= 60 else text[:59] + "…"

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
        if f.type == "int":
            return self._spin_widget(f, value)
        edit = QtWidgets.QLineEdit("" if value is None else str(value))
        self._getters[f.key] = edit.text
        self._setters[f.key] = lambda v, e=edit: e.setText("" if v is None else str(v))
        self._widgets[f.key] = edit
        return edit

    @staticmethod
    def _set_combo(combo: QtWidgets.QComboBox, value) -> None:
        i = combo.findText("" if value is None else str(value))
        combo.setCurrentIndex(i if i >= 0 else 0)

    @staticmethod
    def _as_int(value, default: int) -> int:
        """A stored value as an int, falling back to `default`. Accepts the string
        form ("5"), because a hand-edited config.json and an .env both hand back
        text for a key the schema calls an int."""
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return int(default)

    def _spin_widget(self, f, value):
        """A bounded whole number as a spin box rather than a free-text box.

        The getter returns `str(spin.value())`, NOT the int: every other scalar
        getter in this form returns text, and `_coerce` / `_changed_summary` /
        `load_from_snapshot`'s round-trip all read them that way. Returning an int
        here would pass a casual test and break those.

        Structurally this makes `_coerce`'s "must be a whole number" arm and
        `settings.validate`'s range arm unreachable from these rows — which is the
        point, and also why `_flag_a_rewritten_int` exists: what the widget cannot
        be driven to, it silently CLAMPS to on the way in.
        """
        spin = QtWidgets.QSpinBox()
        # A schema int without bounds is not a thing today; the fallbacks keep a
        # future one from inheriting QSpinBox's stock 0..99 ceiling, which would
        # clamp almost anything.
        spin.setMinimum(int(f.min) if f.min is not None else -1_000_000_000)
        spin.setMaximum(int(f.max) if f.max is not None else 1_000_000_000)
        spin.setValue(self._as_int(value, f.default))
        self._getters[f.key] = lambda s=spin: str(s.value())
        self._setters[f.key] = lambda v, s=spin, d=f.default: s.setValue(
            SettingsForm._as_int(v, d))
        self._widgets[f.key] = spin
        return spin

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
        # Without this the three credential boxes announce nothing on focus (the
        # cell around them is a bare QWidget, which a screen reader skips), while
        # every other field type names its inner control.
        edit.setAccessibleName(f.label)
        h.addWidget(edit, 1)
        hide = QtWidgets.QCheckBox("Hide")
        hide.setAccessibleName(f"Hide {f.label}")
        hide.toggled.connect(lambda on, e=edit: e.setEchoMode(
            QtWidgets.QLineEdit.EchoMode.Password if on
            else QtWidgets.QLineEdit.EchoMode.Normal))
        hide.setChecked(True)
        h.addWidget(hide)
        self._secret_edits[f.key] = edit
        self._secret_hides[f.key] = hide
        self._widgets[f.key] = edit     # the inner control, not the cell
        return cell

    # ---- inline validation ---------------------------------------------------

    def _connect_field_signals(self, f: settings.Field) -> None:
        """Wire one field's "the user touched this" and "the user left this"
        signals.

        Clearing on CHANGE rather than on the next Save is what keeps a red box
        from following someone around after they have already fixed it — and, for
        a clamp note, the moment they type the widget IS the value, so a note
        about what used to be on disk is stale.

        Reads `self._widgets[f.key]`, i.e. the REGISTERED control, NOT whatever
        `_make_widget` returned: for the four composite cells those differ, and
        connecting to the wrapper finds no signal at all — sliders, path boxes and
        credential boxes would silently never clear. (The registry exists for
        exactly this; see `_make_widget`'s docstring.) `multichoice` is the
        documented exception with no single inner control, so its per-choice boxes
        are wired individually.

        `_on_field_edited` is deliberately one narrow hook per key: P6's dirty
        markers want exactly this signal set, and should extend that method rather
        than connect a second pass of their own.
        """
        widget = self._widgets[f.key]
        edited = getattr(widget, "textChanged", None)                # QLineEdit / QPlainTextEdit
        if edited is None:
            edited = getattr(widget, "valueChanged", None)           # QSpinBox / QSlider
        if edited is None:
            edited = getattr(widget, "currentTextChanged", None)     # QComboBox
        if edited is None:
            edited = getattr(widget, "toggled", None)                # QCheckBox
        if edited is not None:
            edited.connect(lambda *_a, k=f.key: self._on_field_edited(k))
        elif f.type == "multichoice":
            for cb in self._multi[f.key].values():                   # no single control
                cb.toggled.connect(lambda *_a, k=f.key: self._on_field_edited(k))
        widget.installEventFilter(_FocusOutValidator(self, f.key))

    def _set_field_note(self, key: str, text: str, *, kind: str = NOTE_ERROR) -> None:
        """Show one field's inline note (empty `text` clears it) and outline the
        control to match.

        ONE note per field, last writer wins: an error supersedes a clamp warning
        rather than stacking, because the two cannot both be actionable at once
        (a spin box the form clamped on the way in cannot then be out of range).
        A field with no note label — `vm_enabled`, the VM section's master
        checkbox, which is added straight to the section body rather than through
        `_add_field` — is a deliberate no-op, exactly as it is for
        `_set_field_visible`.
        """
        note = self._notes.get(key)
        if note is None:
            return
        note.setText(text)
        note.setProperty("danger", bool(text) and kind == NOTE_ERROR)
        note.setProperty("warn", bool(text) and kind == NOTE_WARN)
        note.setVisible(bool(text))
        _repolish(note)
        widget = self._widgets.get(key)
        if widget is not None:
            widget.setProperty("error", bool(text) and kind == NOTE_ERROR)
            _repolish(widget)

    def _flag_a_rewritten_int(self, f: settings.Field, stored) -> None:
        """Say so when building the widget CHANGED the stored value.

        The real bug in this feature: `QSpinBox.setValue` (and `QSlider.setValue`)
        clamp SILENTLY, so a hand-edited `max_scored_per_run: 99999` becomes 5000
        and is written back on the next Save with no visible cause — a "safety"
        feature quietly rewriting the user's config. Keyed off `Field.type`, not
        off which widget got built, because the five slider ints clamp identically.

        A WARNING, not an error: the value on screen is legal and Save works, the
        user is simply told what happened before it is written. Cleared the moment
        they edit the field (`_on_field_edited`), because from then on the widget
        is the value and this note describes history.
        """
        if f.type != "int":
            return
        widget = self._widgets[f.key]
        shown = widget.value()
        # "Readable" is decided by PARSING, not by `str(stored) == str(wanted)`:
        # the string form says "05" and "+3" are unreadable when int() takes both
        # happily, and the message then claims a number is not a whole number and
        # calls the value it is showing "the default" when it is nothing of the
        # sort.
        try:
            wanted, readable = int(str(stored).strip()), True
        except (TypeError, ValueError):
            wanted, readable = int(f.default), False
        if readable and wanted == shown:
            return
        where = settings.storage_location(f)
        if not readable:
            self._set_field_note(
                f.key, f"{where} has {stored!r}, which is not a whole number. Showing "
                       f"the default {shown} — saving stores that.", kind=NOTE_WARN)
        else:
            self._set_field_note(
                f.key, f"{where} has {wanted}, outside the allowed {f.min}–{f.max}. "
                       f"Showing {shown} — saving stores that.", kind=NOTE_WARN)

    def _on_field_edited(self, key: str) -> None:
        """The user changed a field: drop whatever note it was carrying, then
        re-read the unsaved-change markers.

        The one narrow per-key hook, as P5 promised it would be — the dirty
        markers extend it rather than opening a second pass of connections over
        the same signals.
        """
        if self._notes.get(key) is not None and not self._notes[key].isHidden():
            self._set_field_note(key, "")
        if self._errors.pop(key, None) is not None:
            self._refresh_error_status()
        self._refresh_dirty()

    def _validate_field(self, key: str) -> None:
        """Re-check ONE field (focus-out) and flag or clear it in place."""
        f = next((x for x in settings.SETTINGS_SCHEMA if x.key == key), None)
        if f is None:
            return
        value, err = self._field_value(f)
        err = err or settings.validate({key: value}).get(key)
        had = key in self._errors
        if err:
            self._errors[key] = err
            self._set_field_note(key, err)
        elif had:
            self._errors.pop(key, None)
            self._set_field_note(key, "")
        if had != (key in self._errors):
            self._refresh_error_status()

    def _refresh_error_status(self) -> None:
        """Rewrite (or clear) the counting status line from `self._errors`.

        Only ever called after the error set CHANGED, so it cannot stomp on a
        "Saved." / "Reverted…" message that has nothing to do with validation.

        The hints are RECOMPUTED here rather than passed down from the Save that
        first reported them, because the reachable half is the half the user fixes
        first: flag two, one of them behind a configuration gate, fix the one on
        screen, and a status line built without hints would drop to a bare
        "1 setting needs fixing" naming a field that is nowhere on the form. Every
        path that touches `self._errors` therefore lands on the same sentence.
        """
        self.status.setText(self._error_status(self._unreachable()) if self._errors else "")

    def _unreachable(self) -> list[settings.Field]:
        """The flagged fields a CONFIGURATION gate is keeping off screen, in schema
        order — the ones the status line has to name, because nothing else on the
        form will."""
        return [f for f in settings.SETTINGS_SCHEMA
                if f.key in self._errors and self._blocking_gate(f) is not None]

    def _error_status(self, unreachable: list[settings.Field] | None = None) -> str:
        n = len(self._errors)
        msg = f"{n} setting{'' if n == 1 else 's'} need{'s' if n == 1 else ''} fixing"
        hints = [f"{f.label} — {self._blocking_gate(f)}" for f in (unreachable or [])]
        if hints:
            msg += " (" + "; ".join(hints[:2])
            msg += f"; and {len(hints) - 2} more)" if len(hints) > 2 else ")"
        return msg

    def _blocking_gate_field(self, f: settings.Field):
        """The CONFIGURATION gate keeping `f` off screen, as
        `(gate field, the values that would open it)` — or None when nothing
        configuration-level is. An empty `allowed` means a bool master switch.

        The line P4 drew, reused by three features now (P5's error status, P6's
        section-badge tooltip, P7's search footer): a `show_if` gate and the VM
        section's master switch both mean "this field does nothing for the way you
        have things set up", and the form must not flip either one to make its own
        message true. A collapsed section, the advanced disclosure and an active
        search are the other kind — view folds — and `_reveal_view_folds` opens
        those instead.

        READS THE WIDGETS (`_gate_values`), never `settings.load()`, and that is
        load-bearing rather than incidental. `settings.is_visible` compares gate
        values EXACTLY while `_set_combo` coerces an unrecognised stored value to
        `choices[0]` before the form ever sees it, so a hand-edited
        `"provider": "openai"` makes `visible_keys(load())` hide all twelve gated
        fields while the form renders them normally. Anything phrased off the file
        would then tell the user that the rows in front of them are missing.
        """
        by_key = {x.key: x for x in settings.SETTINGS_SCHEMA}
        section_gate = self._shut_section_gate(f)
        if section_gate is not None:
            return section_gate, ()
        values = self._gate_values()
        current = f
        while current.show_if is not None:
            key, allowed = current.show_if
            gate = by_key[key]
            if str(values.get(key, gate.default)) not in allowed:
                return gate, allowed
            current = gate
        return None

    def _blocking_gate(self, f: settings.Field) -> str | None:
        """That gate phrased as the thing the user would DO about it — the shape
        P5's status line and P6's badge tooltip both append to a field label."""
        found = self._blocking_gate_field(f)
        if found is None:
            return None
        gate, allowed = found
        if not allowed:
            return f'turn on "{gate.label}" to see it'
        return f'set "{gate.label}" to {" or ".join(allowed)} to see it'

    def _gate_condition(self, f: settings.Field) -> str | None:
        """The same gate phrased as the CONDITION under which the field applies —
        what the search footer needs ("… when Scoring provider is 'claude'").

        A second phrasing over one shared walk, rather than a second walk: the two
        sentences must never be able to disagree about which gate is shut."""
        found = self._blocking_gate_field(f)
        if found is None:
            return None
        gate, allowed = found
        if not allowed:
            return f"{gate.label} is on"
        return f"{gate.label} is " + " or ".join(f"'{a}'" for a in allowed)

    def _reveal_view_folds(self, f: settings.Field) -> None:
        """Open every VIEW fold hiding `f` — an active search, the advanced
        disclosure, and the collapsed section it lives in — WITHOUT persisting any
        of them.

        The user did not choose to open these, so neither `_save_collapsed` nor
        `_save_show_advanced` is called: `CollapsibleSection.set_collapsed` does
        not fire `on_toggled`, `self._collapsed` is left holding the state the user
        actually chose, and the checkbox is ticked with its signal blocked. A
        restart comes back to their layout.

        The search clears FIRST, and it has to: clearing restores the persisted
        collapse layout, which would otherwise re-fold the section this method is
        about to open. It also cannot be skipped — Save validates every collected
        field, so a filter that leaves the rejected row off screen breaks this
        method's whole guarantee (the user must be able to REACH every problem the
        status line claims exists) with no gate to name.

        A keystroke still inside the debounce window is flushed FIRST, because
        the guarantee is about the box's real state, not its last committed one:
        typing a term and pressing Save within `SEARCH_DEBOUNCE_MS` used to leave
        an armed timer that re-filtered a fraction of a second later and took the
        field this method had just focused — red note and all — back off screen.
        """
        if self._search_timer.isActive():
            self._search_timer.stop()
            self._commit_search()
        if self._search_terms and not self._field_matches(f):
            self.set_search("")
        if f.advanced and not self._show_advanced:
            self._show_advanced = True
            self._advanced_check.blockSignals(True)
            self._advanced_check.setChecked(True)
            self._advanced_check.blockSignals(False)
            self._apply_field_visibility()
        section = self._section_widgets.get(f.section)
        if section is not None and section.is_collapsed():
            section.set_collapsed(False)

    def _show_errors(self, errors: dict[str, str]) -> None:
        """Replace the flagged set with `errors`, note by note."""
        for key in list(self._errors):
            if key not in errors:
                self._set_field_note(key, "")
        for key, message in errors.items():
            self._set_field_note(key, message)
        self._errors = dict(errors)

    def _clear_all_notes(self) -> None:
        """Drop every inline note and every flag, errors and clamp warnings alike.

        For the moments when the values the notes describe stop existing: a
        successful Save (the clamp note says "config.json has 99999" — it no longer
        does, this Save is what wrote 5000 there) and `_repopulate` (Revert,
        Restore defaults and snapshot-load each replace every value on screen).
        Leaving them is not a cosmetic wrinkle: a note naming a disk state that is
        no longer true is the same lie as the silent rewrite this feature exists to
        expose.
        """
        for key in list(self._notes):
            self._set_field_note(key, "")
        if self._errors:
            self._errors.clear()
            self._refresh_error_status()

    def _report_errors(self, errors: dict[str, str]) -> None:
        """Put every rejected field on screen if it can be, name the ones it
        cannot, focus the first one the user can actually act on.

        The bar this has to clear: the user must be able to REACH every problem
        the status line claims exists. Auto-revealing a view fold does that for
        free; a configuration gate cannot be opened on their behalf, so the
        status line names the field and the switch that brings it into view —
        telling someone their config is broken and hiding the broken part is the
        one outcome worse than the modal this replaces.
        """
        self._show_errors(errors)
        self._refresh_error_status()        # names the unreachable ones, same as everywhere
        blocked = {f.key for f in self._unreachable()}
        reachable = [f for f in settings.SETTINGS_SCHEMA
                     if f.key in errors and f.key not in blocked]
        if not reachable:
            return
        # The first REACHABLE one, not simply the first: sending someone to a row
        # a configuration gate keeps off screen is the same lie as not mentioning
        # it, just harder to spot.
        first = reachable[0]
        self._reveal_view_folds(first)
        widget = self._widgets[first.key]
        widget.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
        self._scroll.ensureWidgetVisible(widget)

    # ---- unsaved-change markers ----------------------------------------------

    @classmethod
    def _as_form_value(cls, f: settings.Field, raw):
        """A STORED value read the way the widget holding it would read it.

        `settings.load()` hands back whatever is literally in the file, so a
        hand-edited `"min_score": "5"` compares unequal to the 5 the spin box
        reports: that field would open permanently dirty with nothing to undo, and
        the post-save summary would announce "5 -> 5" (and take an archive snapshot
        for it). Put through the same `_coerce` `_field_value` uses, so both sides
        of every comparison in this form are produced the same way — the reason
        `_field_value` itself exists.

        `_coerce` deliberately hands an UNREADABLE int straight back ("lots"),
        which keeps such a field dirty, and that is right: the next Save really
        would replace it with the default. `list`/`multichoice` are returned as-is
        because `_coerce` does not speak them (`_field_value` reaches them through
        their own branches), and a secret because .env values are already text.
        """
        if f.secret or f.type in ("multichoice", "list"):
            return raw
        return cls._coerce(f, raw)[0]

    @classmethod
    def _as_form_values(cls, stored: dict) -> dict:
        """`_as_form_value` across a whole stored mapping."""
        return {f.key: cls._as_form_value(f, stored.get(f.key, f.default))
                for f in settings.SETTINGS_SCHEMA}

    def _opening_value(self, f: settings.Field):
        """`f`'s value as the form opened it, read the way the WIDGET reads it."""
        return self._as_form_value(f, self._opening_values.get(f.key, f.default))

    def _changed_from(self, baseline: Callable[[settings.Field], object]) -> set[str]:
        """Every key whose current value differs from `baseline(field)`, compared
        with `_value_changed` — the same per-type comparison the post-save summary
        and the VM-push prompt use, so "changed" means one thing in this form."""
        return {f.key for f in settings.SETTINGS_SCHEMA
                if self._value_changed(f, baseline(f), self._field_value(f)[0])}

    def _refresh_dirty(self) -> None:
        """Re-read every field and move only the markers that actually flipped.

        VISIBILITY IS NOT CONSULTED, and that is the whole line — the opposite of
        P4's advanced count, deliberately. That count promises "ticking this box
        reveals N rows", so a field a configuration gate holds shut has to be
        subtracted or the promise is false. This one promises "Save writes N
        changes", and `collect()` walks the SCHEMA: a gated-off or folded-away
        field's edit is written exactly like any other, so leaving it out would
        make the number wrong in the direction that loses data quietly. Edit a
        Gemini model, switch the provider to Claude, and the count still says one —
        because the Save still writes it.

        The reachability question P5 answered for errors therefore does not arise
        here: P5 names an unreachable ERROR because the user has to reach it to
        act, while a dirty field asks nothing of them. The section header — on
        screen even when the section is folded — is where it becomes findable.
        """
        if self._save_btn is None or self._suspend_markers:
            return                       # mid-build or mid-bulk-fill; both refresh after
        dirty = self._changed_from(self._opening_value)
        off_default = {k for k in self._changed_from(lambda f: f.default)
                       if k in self._resets}
        for key in dirty ^ self._dirty:
            self._set_field_dirty(key, key in dirty)
        for key in off_default ^ self._off_default:
            self._resets[key].setVisible(key in off_default)
        self._dirty, self._off_default = dirty, off_default
        self._refresh_section_badges()
        self._refresh_save_label()

    def _set_field_dirty(self, key: str, on: bool) -> None:
        """Toggle one field's accent dot. A field with no dot — `vm_enabled`, which
        has no form row — is a deliberate no-op, as it is for `_set_field_note` and
        `_set_field_visible`; it still counts in the totals."""
        dot = self._dots.get(key)
        if dot is None:
            return
        dot.setText("●" if on else "")
        dot.setProperty("dirty", on)
        dot.setToolTip("Changed — not saved yet" if on else "")
        _repolish(dot)

    def _refresh_section_badges(self) -> None:
        """Push each section's dirty count onto its header, plus a tooltip for the
        part of that count expanding the section would NOT show.

        The badge's whole claim is "open me and you will find them". That holds for
        a view fold — collapse, or the advanced disclosure — where the dot is
        waiting behind it. It does not hold for a configuration gate: that row is
        off screen entirely, so a section reading "· 2 changed" can expand to
        exactly one dot and look like a lie. `_blocking_gate` already produces the
        sentence that closes the gap, in the same shape the error status line uses.
        """
        counts: dict[str, int] = dict.fromkeys(self._section_widgets, 0)
        hidden: dict[str, list[str]] = {s: [] for s in self._section_widgets}
        for f in settings.SETTINGS_SCHEMA:
            if f.key not in self._dirty or f.section not in counts:
                continue
            counts[f.section] += 1
            gate = self._blocking_gate(f)
            if gate is not None:
                hidden[f.section].append(f"{f.label} — {gate}")
        for section, n in counts.items():
            hint = ("Not shown on this form: " + "; ".join(hidden[section])
                    if n and hidden[section] else "")
            if self._section_counts.get(section) != (n, hint):
                self._section_widgets[section].set_changed_count(n, hint)
                self._section_counts[section] = (n, hint)

    def _refresh_save_label(self) -> None:
        """"Save 3 changes" while dirty, "Save settings" when clean — and NEVER
        disabled. Restore defaults leaves a form that differs from disk only in
        ways the user has not committed yet, so the button has to stay pressable;
        and the "No changes to save" path is real feedback, not a dead end."""
        n = len(self._dirty)
        self._save_btn.setText(
            f"Save {n} change{'' if n == 1 else 's'}" if n else "Save settings")

    def _reset_field(self, key: str) -> None:
        """Put one field back to its schema default (the ↺ button)."""
        f = next(x for x in settings.SETTINGS_SCHEMA if x.key == key)
        self._set_field_value(f, f.default)
        # Insurance, and idempotent. The button is only ever VISIBLE while the value
        # differs from the default, so today every setter this reaches really does
        # emit and land in `_on_field_edited` on its own — but that is a property of
        # the visibility rule, not of the reset, and a programmatic call has no such
        # guarantee.
        self._on_field_edited(key)

    def _add_buttons(self):
        bar = QtWidgets.QHBoxLayout()
        save = QtWidgets.QPushButton("Save settings")
        save.setProperty("accent", True)
        self._save_btn = save
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
            value, err = self._field_value(f)
            values[f.key] = value
            if err:
                errors[f.key] = err
        return values, errors

    def _field_value(self, f: settings.Field):
        """ONE field's current value, plus any coercion error.

        Shared by `collect()` and the focus-out validator so both read a field
        exactly the same way — a second copy of this per-type dispatch is how a
        field ends up validating differently depending on whether you tabbed out
        of it or pressed Save.
        """
        if f.secret:
            # The box shows the saved value, so whatever it holds is the truth:
            # write it as-is (an empty box clears the key).
            return self._secret_edits[f.key].text(), None
        if f.type == "multichoice":
            return [c for c, cb in self._multi[f.key].items() if cb.isChecked()], None
        if f.type == "list":
            raw = self._lists[f.key].toPlainText()
            return [ln.strip() for ln in raw.splitlines() if ln.strip()], None
        return self._coerce(f, self._getters[f.key]())

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
        errors.update(settings.validate(values))
        if errors:
            # In place, not in a modal: a newline-dump of every offender named
            # them but pointed at nothing, so the user dismissed it and then hunted
            # ~130 form rows for the box it meant.
            self._report_errors(errors)
            return False
        self._show_errors({})
        # Normalised, for the same reason `_opening_value` is: `settings.load()`
        # returns the file verbatim, so a hand-edited `"min_score": "5"` made the
        # summary announce "5 -> 5" — take an archive snapshot for it, and offer a
        # VM push over it — while the Save button, which normalises, correctly read
        # "Save settings". Two claims about the same question must not disagree.
        before = self._as_form_values(settings.load(self.targets))
        try:
            settings.save(values, self.targets)
        except (ValueError, OSError) as exc:
            # The one arm that stays modal. A rejected field is something the user
            # can see and fix where they are; an unwritable config.json is neither,
            # and a status line alone would let them walk away believing they saved.
            self.status.setText("Save failed.")
            QtWidgets.QMessageBox.critical(self, "Settings", str(exc))
            return False
        summary = self._changed_summary(before, values)
        restart = self._restart_notice(before, values)
        archived = self._archive_after_save(values) if summary else False
        self._opening_values = settings.load(self.targets)
        self._clear_all_notes()   # every note described the file this Save replaced
        self._sync_secret_boxes(self._opening_values)  # reflect the canonical stored values
        self._refresh_dirty()     # ...and the new baseline is what was just written
        self.status.setText(
            ("Saved. " + restart if restart else "Saved.") if summary else "Saved — no changes.")
        if summary:
            note = "\n\nA snapshot was saved to the archive." if archived else ""
            QtWidgets.QMessageBox.information(
                self, "Settings", "Settings saved. Updated:\n\n- " + "\n- ".join(summary)
                + (f"\n\n{restart}" if restart else "") + note)
        else:
            QtWidgets.QMessageBox.information(
                self, "Settings", "No changes to save — your settings are unchanged.")
        self._maybe_prompt_vm_push(before, values, summary)
        if self.on_saved:
            self.on_saved()
        return True

    def _restart_notice(self, before: dict, values: dict) -> str:
        """"Restart the dashboard for these to take effect: A, B." — or "".

        The chip beside the input is the reminder while you are editing; this is
        the one at the moment it matters, which is the click that writes the file
        and puts "Saved." on screen. Without it the whole failure mode survives
        the badge: the user saves a new model id or API key, is told it saved
        (truthfully — it is on disk), and then watches the old one keep being used
        with nothing to connect the two.

        Names only what THIS Save changed, so the sentence has to be earned. A
        line on every Save is a line nobody reads on the Save that needed one.
        LABELS only, never values — `GEMINI_API_KEYS` is both restart-required and
        a credential, and `_changed_summary`'s no-echo rule applies here for
        exactly the same reason.
        """
        labels = [f.label for f in settings.SETTINGS_SCHEMA
                  if f.restart
                  and self._value_changed(f, before.get(f.key, f.default), values[f.key])]
        if not labels:
            return ""
        # Elided at three, the same shape `_error_status` uses. `self.status` is a
        # plain unwrapped QLabel, and a snapshot load can change all sixteen of
        # these at once — a ~450-character status line does not wrap, it just runs
        # off the end of the bar and takes the rest of the sentence with it.
        named = ", ".join(labels[:3])
        if len(labels) > 3:
            named += f", and {len(labels) - 3} more"
        return ("Restart the dashboard for "
                f"{'this' if len(labels) == 1 else 'these'} to take effect: "
                + named + ".")

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
                     "  gcloud compute scp pipeline/score_jobs.py <user>@<vm>:~ --zone=<zone>")
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

    def _set_field_value(self, f: settings.Field, val) -> None:
        """Write one value into its widget, per type — the three-way dispatch
        `_repopulate` and the ↺ reset both need, in one place so a bulk restore and
        a single reset can never diverge.

        NOT for a secret: `_secret_widget` registers no setter (its box is driven
        by `_sync_secret_boxes`), which is why `_repopulate` skips them and why no
        ↺ button is built for one.
        """
        if f.type == "multichoice":
            want = set(val if isinstance(val, list) else [])
            for choice, cb in self._multi[f.key].items():
                cb.setChecked(choice in want)
        elif f.type == "list":
            self._lists[f.key].setPlainText(
                "\n".join(str(v) for v in (val if isinstance(val, list) else [])))
        else:
            self._setters[f.key](val)

    def _repopulate(self, value_for: Callable[[settings.Field], object]) -> None:
        incoming: dict[str, object] = {}
        # Markers off for the fill: every setter emits, and each emission would
        # otherwise re-read all ~60 fields — 60 rescans for one Revert. One refresh
        # at the end says the same thing once.
        self._suspend_markers = True
        try:
            for f in settings.SETTINGS_SCHEMA:
                if f.secret:
                    continue
                incoming[f.key] = value_for(f)
                self._set_field_value(f, incoming[f.key])
        finally:
            # In the `finally` with the flag, not after the try: a setter CAN raise
            # (the slider's does `int(v)`, reachable from a corrupt snapshot), and
            # a half-filled form whose every dot, badge and count still describes
            # the values it replaced is the worst of both states. Re-describe what
            # actually landed, then let the exception carry on.
            self._suspend_markers = False
            # Every value on screen was just replaced, so every note about the old
            # ones is stale — and a setter that happened to write the SAME value
            # emits no change signal, so `_on_field_edited` cannot be relied on to
            # have cleared them. Then re-run the clamp check against what actually
            # arrived: the setters clamp exactly as the constructor does, so without
            # this a snapshot holding `max_scored_per_run: 99999` loads as 5000 in
            # silence — the rewrite P5 exists to expose, back through its own door.
            self._clear_all_notes()
            for f in settings.SETTINGS_SCHEMA:
                if f.type == "int" and f.key in incoming:
                    self._flag_a_rewritten_int(f, incoming[f.key])
            self._apply_section_visibility()
            # Revert / Restore defaults / snapshot-load all land here, and any of
            # them can change a gate. Re-evaluate EXPLICITLY: every gate is a
            # QComboBox today, whose setter emits currentTextChanged and would get
            # there by itself, but that is a side-effect of the widget type, not a
            # contract.
            self._apply_field_visibility()
            # The one marker refresh for the whole fill — the per-setter ones were
            # suspended above, so this is not a belt-and-braces call: drop it and
            # Revert leaves every dot, badge and count describing the old values.
            self._refresh_dirty()

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
