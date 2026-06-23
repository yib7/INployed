"""Schema-driven settings form (Qt) — the Settings tab and the standalone window.

Renders `settings.SETTINGS_SCHEMA` grouped by section: one labelled, explained
input per Field, the right widget per type (dropdown / editable dropdown / slider /
checkboxes / multiline list / path+Browse / masked write-only secret / entry), a
muted "(filename)" storage tag, a collapsible VM section gated by a master
checkbox, and Save / Revert changes / Restore defaults. Save validates via
`settings.validate`/`settings.save`, reports a changed-field summary, and never
echoes a secret. Mirrors the old Tk `config_form.ConfigForm` behavior.
"""
from __future__ import annotations

from typing import Callable

from PySide6 import QtCore, QtWidgets

import settings

SECTION_HELP = {
    "Credentials": ("API keys and tokens, saved to your private .env file and never shown again. "
                    "Each box stays blank even when a key is saved: leave it blank to keep the saved "
                    "key, type a new value to replace it, or tick Clear to delete it."),
    "Connection & paths": "Your cloud project, your name, and where files live on this PC.",
    "Engine": "Which Gemini backend the resume tailor bills.",
    "Dashboard": "How the dashboard surfaces and tracks jobs.",
    "Scraper": "What the LinkedIn scraper searches for (this drives Bright Data spend).",
    "Scoring": ("Advanced — which models score jobs and the spend guards around them. The "
                "defaults are tuned; changing the model names can silently break scoring."),
    "Resume": "What the resume tailor generates, and how the cover letter reads.",
    "VM (cloud scraper)": ("Connect to your cloud scraper VM (GCP) so you can push config, schedule, "
                           "and pause changes to it. Uses your existing `gcloud` login — no SSH "
                           "password or key is ever stored."),
}
SECTION_ORDER = ["Credentials", "Connection & paths", "Engine",
                 "Dashboard", "Scraper", "Scoring", "Resume", "VM (cloud scraper)"]
COLLAPSIBLE_SECTIONS = {"VM (cloud scraper)": "vm_enabled"}

_SECRET_SET = "saved — blank keeps it, type to replace"
_SECRET_UNSET = "not set"


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
        self._secret_clears: dict[str, QtWidgets.QCheckBox] = {}
        self._secret_status: dict[str, QtWidgets.QLabel] = {}
        self._collapse: dict[str, QtWidgets.QWidget] = {}
        self._gate_keys: dict[str, str] = {}  # gate_key -> section

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
        secret_set = settings.secret_status(self.targets)
        self._opening_values = dict(stored)

        for section, fields in _ordered_sections():
            if section in COLLAPSIBLE_SECTIONS:
                self._add_collapsible_section(section, fields, stored, secret_set)
            else:
                self._add_section(section, fields, stored, secret_set)

        self._add_buttons()
        self._body.addStretch(1)

    def _add_section(self, section, fields, stored, secret_set, into=None):
        box = into if into is not None else self._body
        head = QtWidgets.QLabel(section)
        head.setProperty("heading", True)
        box.addWidget(head)
        blurb = SECTION_HELP.get(section)
        if blurb:
            lab = QtWidgets.QLabel(blurb)
            lab.setProperty("muted", True)
            lab.setWordWrap(True)
            box.addWidget(lab)
        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        box.addLayout(form)
        for f in fields:
            self._add_field(form, f, stored.get(f.key, f.default), secret_set)

    def _add_collapsible_section(self, section, fields, stored, secret_set):
        gate_key = COLLAPSIBLE_SECTIONS[section]
        gate = next((f for f in fields if f.key == gate_key), None)
        self._gate_keys[gate_key] = section

        header = QtWidgets.QHBoxLayout()
        head = QtWidgets.QLabel(section)
        head.setProperty("heading", True)
        header.addWidget(head)
        check = QtWidgets.QCheckBox(gate.label if gate else "Enable")
        check.setChecked(bool(stored.get(gate_key, getattr(gate, "default", False))))
        check.toggled.connect(self._apply_section_visibility)
        header.addWidget(check)
        header.addStretch(1)
        self._body.addLayout(header)
        self._getters[gate_key] = lambda c=check: c.isChecked()
        self._setters[gate_key] = lambda v, c=check: c.setChecked(bool(v))

        container = QtWidgets.QWidget()
        cbox = QtWidgets.QVBoxLayout(container)
        cbox.setContentsMargins(0, 0, 0, 0)
        self._body.addWidget(container)
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
            self._add_field(form, f, stored.get(f.key, f.default), secret_set)
        if self._vm_factory is not None:
            extra = self._vm_factory(container)
            if extra is not None:
                cbox.addWidget(extra)
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

    def _add_field(self, form: QtWidgets.QFormLayout, f, value, secret_set):
        widget = self._make_widget(f, value, secret_set)
        form.addRow(self._label_cell(f), widget)
        if f.help:
            help_lab = QtWidgets.QLabel(f.help)
            help_lab.setProperty("muted", True)
            help_lab.setWordWrap(True)
            form.addRow("", help_lab)

    def _make_widget(self, f, value, secret_set):
        if f.secret:
            return self._secret_widget(f, secret_set.get(f.key, False))
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

    def _secret_widget(self, f, is_set):
        cell = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(cell)
        h.setContentsMargins(0, 0, 0, 0)
        edit = QtWidgets.QLineEdit()
        edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        edit.setPlaceholderText("blank keeps the saved value")
        h.addWidget(edit, 1)
        status = QtWidgets.QLabel(_SECRET_SET if is_set else _SECRET_UNSET)
        status.setProperty("muted", True)
        h.addWidget(status)
        clear = QtWidgets.QCheckBox("Clear")
        h.addWidget(clear)
        self._secret_edits[f.key] = edit
        self._secret_status[f.key] = status
        self._secret_clears[f.key] = clear
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
                typed = self._secret_edits[f.key].text()
                if self._secret_clears[f.key].isChecked():
                    values[f.key] = ""
                elif typed.strip():
                    values[f.key] = typed
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
            if f.secret:
                out.append(f"{f.label}: {'cleared' if str(new).strip() == '' else 'updated'}")
                continue
            old = before.get(key, f.default)
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
        self._refresh_secret_labels()
        self.status.setText("Saved." if summary else "Saved — no changes.")
        self._opening_values = settings.load(self.targets)
        if summary:
            QtWidgets.QMessageBox.information(
                self, "Settings", "Settings saved. Updated:\n\n- " + "\n- ".join(summary))
        else:
            QtWidgets.QMessageBox.information(
                self, "Settings", "No changes to save — your settings are unchanged.")
        if self.on_saved:
            self.on_saved()
        return True

    def _refresh_secret_labels(self) -> None:
        status = settings.secret_status(self.targets)
        for key, label in self._secret_status.items():
            label.setText(_SECRET_SET if status.get(key) else _SECRET_UNSET)
            self._secret_edits[key].clear()
            self._secret_clears[key].setChecked(False)

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
        self._repopulate(lambda f: f.default)
        self.status.setText("Defaults restored — press Save to apply.")

    def revert(self) -> None:
        self._repopulate(lambda f: self._opening_values.get(f.key, f.default))
        for key, clear in self._secret_clears.items():
            clear.setChecked(False)
            self._secret_edits[key].clear()
        self.status.setText("Reverted to your last-opened settings — press Save to apply.")


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
