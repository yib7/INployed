"""Schema-driven settings form (Qt) — the Settings tab and the standalone window.

Renders `settings.SETTINGS_SCHEMA` grouped by section: one labelled, explained
input per Field, the right widget per type (dropdown / editable dropdown / slider /
checkboxes / multiline list / path+Browse / credential (shown, with a Hide
toggle) / entry), a muted "(filename)" storage tag, a collapsible VM section
gated by a master checkbox, and Save / Revert changes / Restore from archive /
Restore defaults. Save validates via `settings.validate`/`settings.save`, reports
a changed-field summary, and never echoes a secret value into that summary.
"""
from __future__ import annotations

from typing import Callable

from PySide6 import QtCore, QtWidgets

import jobsdata
import settings
import settings_archive
from qt.widgets import CollapsibleSection

SECTION_HELP = {
    "Credentials": ("API keys and tokens, saved to your private .env file on this PC. The saved "
                    "value is shown here so you can check it without opening the file — edit it to "
                    "change it, clear the box to remove it, or tick Hide to mask it from view."),
    "Connection & paths": "Your cloud project, your name, and where files live on this PC.",
    "Engine": "Which Gemini backend the resume tailor bills.",
    "Dashboard": "How the dashboard surfaces and tracks jobs.",
    "Scraper": "What the LinkedIn scraper searches for (this drives Bright Data spend).",
    "Scoring": ("Advanced — which models score jobs and the spend guards around them. The "
                "defaults are tuned; changing the model names can silently break scoring."),
    "Resume": "What the resume tailor generates, and how the cover letter reads.",
    "Settings history": ("Every Save snapshots all your settings to a dated folder so you can "
                         "roll one back later with 'Restore from archive...' below. Snapshots "
                         "include your saved keys and live alongside your settings on this PC."),
    "VM (cloud scraper)": ("Connect to your cloud scraper VM (GCP) so you can push config, schedule, "
                           "and pause changes to it. Uses your existing `gcloud` login — no SSH "
                           "password or key is ever stored."),
}
SECTION_ORDER = ["Credentials", "Connection & paths", "Engine",
                 "Dashboard", "Scraper", "Scoring", "Resume", "Settings history",
                 "VM (cloud scraper)"]

# Short one-liners shown next to each section header — always visible, even when the
# section is collapsed, so a user knows what to expand without clicking through.
SECTION_TAGLINE = {
    "Credentials": "API keys & tokens",
    "Connection & paths": "Project, your name, file locations",
    "Engine": "Which Gemini backend the tailor bills",
    "Dashboard": "How jobs are surfaced & tracked",
    "Scraper": "What LinkedIn search to run",
    "Scoring": "Models & spend guards (advanced)",
    "Resume": "Cover letter & artifact toggles",
    "Settings history": "Snapshot & restore your settings",
    "VM (cloud scraper)": "Manage the cloud scraper VM",
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
                 parent=None):
        super().__init__(parent)
        self.on_saved = on_saved
        self.targets = targets
        self._vm_factory = vm_panel_factory

        self._getters: dict[str, Callable[[], str]] = {}
        self._setters: dict[str, Callable[[object], None]] = {}
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

        self._build()

    # ---- construction --------------------------------------------------------

    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QtWidgets.QWidget()
        self._body = QtWidgets.QVBoxLayout(body)
        self._body.setContentsMargins(16, 12, 16, 12)
        scroll.setWidget(body)

        stored = settings.load(self.targets)
        self._opening_values = dict(stored)

        for section, fields in _ordered_sections():
            sec = CollapsibleSection(
                section, subtitle=SECTION_TAGLINE.get(section, ""),
                collapsed=section in self._collapsed,
                on_toggled=lambda c, s=section: self._on_section_toggled(s, c))
            self._section_widgets[section] = sec
            self._body.addWidget(sec)
            if section in COLLAPSIBLE_SECTIONS:
                self._fill_gated_section(sec, section, fields, stored)
            else:
                self._fill_section(sec, section, fields, stored)

        self._add_buttons()
        self._body.addStretch(1)

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
        sec.add_widget(check)
        self._getters[gate_key] = lambda c=check: c.isChecked()
        self._setters[gate_key] = lambda v, c=check: c.setChecked(bool(v))

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

    def _label_cell(self, f: settings.Field) -> QtWidgets.QWidget:
        cell = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(cell)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(QtWidgets.QLabel(f.label))
        tag = QtWidgets.QLabel(f"({settings.storage_location(f)})")
        tag.setProperty("muted", True)
        h.addWidget(tag)
        return cell

    def _add_field(self, form: QtWidgets.QFormLayout, f, value):
        widget = self._make_widget(f, value)
        form.addRow(self._label_cell(f), widget)
        if f.help:
            help_lab = QtWidgets.QLabel(f.help)
            help_lab.setProperty("muted", True)
            help_lab.setWordWrap(True)
            form.addRow("", help_lab)

    def _make_widget(self, f, value):
        if f.secret:
            return self._secret_widget(f, value)
        if f.type == "bool":
            cb = QtWidgets.QCheckBox()
            cb.setChecked(bool(value))
            self._getters[f.key] = cb.isChecked
            self._setters[f.key] = lambda v, c=cb: c.setChecked(bool(v))
            return cb
        if f.type == "choice":
            combo = QtWidgets.QComboBox()
            combo.addItems([str(c) for c in f.choices])
            self._set_combo(combo, value)
            self._getters[f.key] = combo.currentText
            self._setters[f.key] = lambda v, c=combo: self._set_combo(c, v)
            return combo
        if f.type == "editable_choice":
            combo = QtWidgets.QComboBox()
            combo.setEditable(True)
            combo.addItems([str(c) for c in f.choices])
            combo.setCurrentText(str(value))
            combo.lineEdit().installEventFilter(_PopupOnClick(combo))
            self._getters[f.key] = combo.currentText
            self._setters[f.key] = lambda v, c=combo: c.setCurrentText("" if v is None else str(v))
            return combo
        if getattr(f, "slider", False) and f.type == "int":
            return self._slider_widget(f, value)
        if f.type == "multichoice":
            return self._multichoice_widget(f, value)
        if f.type == "list":
            txt = QtWidgets.QPlainTextEdit()
            txt.setMinimumHeight(150)
            txt.setPlainText("\n".join(str(v) for v in (value if isinstance(value, list) else [])))
            self._lists[f.key] = txt
            return txt
        if f.type == "path":
            return self._path_widget(f, value)
        edit = QtWidgets.QLineEdit("" if value is None else str(value))
        self._getters[f.key] = edit.text
        self._setters[f.key] = lambda v, e=edit: e.setText("" if v is None else str(v))
        return edit

    @staticmethod
    def _set_combo(combo: QtWidgets.QComboBox, value) -> None:
        i = combo.findText("" if value is None else str(value))
        combo.setCurrentIndex(i if i >= 0 else 0)

    def _slider_widget(self, f, value):
        cell = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(cell)
        h.setContentsMargins(0, 0, 0, 0)
        slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
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
        self._getters[f.key] = lambda s=slider: str(s.value())
        self._setters[f.key] = lambda v, s=slider: s.setValue(int(v) if str(v).strip() else int(f.default))
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
        return cell

    def _path_widget(self, f, value):
        cell = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(cell)
        h.setContentsMargins(0, 0, 0, 0)
        edit = QtWidgets.QLineEdit("" if value is None else str(value))
        h.addWidget(edit, 1)
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(lambda: self._browse(edit, f.path_kind))
        h.addWidget(browse)
        self._getters[f.key] = edit.text
        self._setters[f.key] = lambda v, e=edit: e.setText("" if v is None else str(v))
        return cell

    def _secret_widget(self, f, value):
        """A secret field shows its saved value in plain text (it comes straight
        from the local .env — nothing leaves this PC). Edit it to change it, clear
        the box to remove the key, or tick Hide to mask it from onlookers."""
        cell = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(cell)
        h.setContentsMargins(0, 0, 0, 0)
        edit = QtWidgets.QLineEdit("" if value is None else str(value))
        edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Normal)
        edit.setPlaceholderText("not set")
        h.addWidget(edit, 1)
        hide = QtWidgets.QCheckBox("Hide")
        hide.toggled.connect(lambda on, e=edit: e.setEchoMode(
            QtWidgets.QLineEdit.EchoMode.Password if on
            else QtWidgets.QLineEdit.EchoMode.Normal))
        h.addWidget(hide)
        self._secret_edits[f.key] = edit
        self._secret_hides[f.key] = hide
        return cell

    def _add_buttons(self):
        bar = QtWidgets.QHBoxLayout()
        save = QtWidgets.QPushButton("Save")
        save.setProperty("accent", True)
        save.clicked.connect(self.save)
        bar.addWidget(save)
        revert = QtWidgets.QPushButton("Revert changes")
        revert.clicked.connect(self.revert)
        bar.addWidget(revert)
        archive = QtWidgets.QPushButton("Restore from archive…")
        archive.clicked.connect(self.open_archive)
        bar.addWidget(archive)
        restore = QtWidgets.QPushButton("Restore defaults")
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
        if f.type == "float":
            try:
                return float(text), None
            except ValueError:
                return raw, f"{f.label}: must be a number."
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
        if self.on_saved:
            self.on_saved()
        return True

    def _archive_after_save(self, values: dict) -> bool:
        """Snapshot all settings then apply the prune policy. Never raises into Save —
        archiving is a safety net, not something that should be able to block a save."""
        if not values.get("archive_enabled", True):
            return False
        try:
            made = settings_archive.snapshot(self.targets)
            settings_archive.prune(
                values.get("archive_prune_mode", settings_archive.PRUNE_OFF),
                keep=int(values.get("archive_prune_keep", 20) or 20),
                days=int(values.get("archive_prune_days", 30) or 30),
                targets=self.targets)
            return made is not None
        except OSError:
            return False

    def _sync_secret_boxes(self, values_now: dict) -> None:
        """Set each secret box to its current stored value and un-hide it."""
        for key, edit in self._secret_edits.items():
            v = values_now.get(key, "")
            edit.setText("" if v is None else str(v))
            self._secret_hides[key].setChecked(False)

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
            "Secrets are restored too, but are never shown here.")
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        v.addWidget(intro)

        self.listw = QtWidgets.QListWidget()
        self.listw.currentRowChanged.connect(self._on_select)
        v.addWidget(self.listw, 1)

        self.preview = QtWidgets.QPlainTextEdit()
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


def build_config_window(targets: dict | None = None) -> QtWidgets.QWidget:
    """A standalone window hosting the settings form (for `configure`)."""
    win = QtWidgets.QWidget()
    win.setWindowTitle("Configure — INployed")
    win.resize(940, 860)
    v = QtWidgets.QVBoxLayout(win)
    title = QtWidgets.QLabel("Configuration")
    title.setProperty("heading", True)
    v.addWidget(title)
    intro = QtWidgets.QLabel(
        "Set everything up in one place. Entries save to a private .env file and the config files "
        "beside it — the dashboard and the scraper read them on their next run.")
    intro.setProperty("muted", True)
    intro.setWordWrap(True)
    v.addWidget(intro)
    v.addWidget(SettingsForm(targets=targets), 1)
    return win
