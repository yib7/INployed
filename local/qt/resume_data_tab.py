"""The Résumé Data tab (Qt): structured editor for master_experience.yaml + the
scorer-resume.md generator.

A scrollable editor over `basics` and the three atom-bearing sections
(experience / projects / leadership): every field and achievement atom is editable,
entries/atoms can be added or deleted, and Education/Skills are shown read-only.
Every write goes through `master_edit` (which backs up to `.bak`); Validate runs
`master_validate`; Revert restores the on-open snapshot. The top bar regenerates
the scorer's `resume.md` from the YAML via Gemini (preview-then-write), with the
LLM call injectable so tests never spend a credit.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import yaml
from PySide6 import QtCore, QtWidgets

import jobsdata
import resume_md
import settings
from qt import workers
from qt.widgets import CollapsibleSection
from resume_tailor import config, master_edit, master_validate

_SECTION_FIELDS = {
    "experience": [("org", "Org"), ("title", "Title"), ("location", "Location"), ("dates", "Dates")],
    "projects": [("name", "Name"), ("dates", "Dates"), ("live_url", "Live URL"), ("repo", "Repo")],
    "leadership": [("org", "Org"), ("title", "Title"), ("dates", "Dates")],
}
_NAME_KEY = {"experience": "org", "projects": "name", "leadership": "org"}
_BASICS_FIELDS = [("name", "Name"), ("email", "Email"), ("phone", "Phone"),
                  ("location", "Location"), ("linkedin", "LinkedIn"), ("github", "GitHub")]

_TIPS = ("Tips: store FACTS as atoms (what / how / scope / impact), not finished sentences — the "
         "tailor re-angles them per job. Quantify everything. Add 'angles' tags so an atom matches "
         "a job's keywords. Hold MORE than fits one page; the pipeline SELECTS, never invents.")


def _parse_targets(text: str) -> list[int]:
    """Parse a 'Resume Layout' targets box ('2, 2, 1' or '2 2 1') into [2, 2, 1].
    Non-integer tokens are dropped; the engine clamps values/length on read."""
    out: list[int] = []
    for tok in text.replace(",", " ").split():
        try:
            out.append(int(tok))
        except ValueError:
            continue
    return out


def _parse_tiers(text: str) -> list[dict]:
    """Parse a 'Bullets by strength' box ('2:3, 2:2, 1:1') into tier dicts
    [{'projects': 2, 'bullets': 3}, ...]. Each token is projects:bullets; tokens that
    aren't two integers are dropped (jobsdata/engine clamp on save/read)."""
    out: list[dict] = []
    for tok in text.replace(";", ",").split(","):
        parts = tok.strip().split(":")
        if len(parts) != 2:
            continue
        try:
            out.append({"projects": int(parts[0]), "bullets": int(parts[1])})
        except ValueError:
            continue
    return out


def _tiers_to_text(tiers: list) -> str:
    """Render saved tier dicts back into the 'projects:bullets, ...' box text."""
    return ", ".join(f"{t['projects']}:{t['bullets']}" for t in (tiers or [])
                     if isinstance(t, dict) and "projects" in t and "bullets" in t)


class ResumeDataEditor(QtWidgets.QWidget):
    def __init__(self, on_saved: Callable[[], None] | None = None,
                 master_path: Path | None = None, parent=None):
        super().__init__(parent)
        self.on_saved = on_saved
        self.master_path = Path(master_path) if master_path is not None else config.MASTER_YAML
        self.snapshot = self.master_path.read_bytes() if self.master_path.exists() else b""

        self._basics_edits: dict[str, QtWidgets.QLineEdit] = {}
        self._basics_orig: dict[str, str] = {}
        self._entry_edits: dict[tuple, QtWidgets.QLineEdit] = {}
        self._entry_orig: dict[tuple, str] = {}
        self._atom_edits: dict[tuple, QtWidgets.QLineEdit] = {}
        self._atom_orig: dict[tuple, str] = {}
        self._atom_impact: dict[str, QtWidgets.QPlainTextEdit] = {}
        self._atom_impact_orig: dict[str, str] = {}
        self._layout_section_edits: dict[str, QtWidgets.QLineEdit] = {}
        self._layout_project_edits: dict[str, QtWidgets.QLineEdit] = {}
        # B8: stale custom-layout rows (name no longer in the master) — (kind, name,
        # row-widget, form) so a per-row ✕ or the "Remove stale entries" button can
        # drop them from the saved map without a full tab rebuild.
        self._stale_layout_rows: list = []
        # C: per-block "don't tailor" verbatim editors, {block_name: (checkbox, editor)}.
        self._verbatim_edits: dict = {}

        self._build_shell()
        self.reload()

    # ---- io ------------------------------------------------------------------

    def _read(self) -> dict:
        try:
            data = yaml.safe_load(self.master_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            return {}
        return data if isinstance(data, dict) else {}

    def validate(self) -> list[str]:
        return master_validate.validate_master(self._read())

    # ---- construction --------------------------------------------------------

    def _build_shell(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.addLayout(self._build_md_bar())
        outer.addWidget(self._build_stale_banner())
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        # Keep every field within the visible width: a long value wraps or scrolls
        # inside its own box instead of pushing the whole form (and the Delete
        # buttons) off the right edge of the screen.
        self.scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll, 1)
        self.status = QtWidgets.QLabel("")
        self.status.setProperty("muted", True)
        outer.addWidget(self.status)

    def _build_md_bar(self) -> QtWidgets.QHBoxLayout:
        bar = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Scorer résumé (resume.md):")
        title.setProperty("heading", True)
        bar.addWidget(title)
        self.md_model = QtWidgets.QComboBox()
        self.md_model.setEditable(True)
        self.md_model.addItems(list(settings.GEMINI_MODELS))
        self.md_model.setCurrentText("gemini-3.5-flash")
        bar.addWidget(self.md_model)
        gen = QtWidgets.QPushButton("Generate from my data")
        gen.clicked.connect(self._generate)
        bar.addWidget(gen)
        self.btn_push_md = QtWidgets.QPushButton("Push resume.md to VM")
        self.btn_push_md.clicked.connect(self._push_resume_md)
        bar.addWidget(self.btn_push_md)
        bar.addStretch(1)
        self._refresh_push_state()
        return bar

    def _build_stale_banner(self) -> QtWidgets.QWidget:
        """A warning shown when resume.md has drifted behind the master YAML —
        the scorer would otherwise keep matching against an out-of-date résumé."""
        from qt import theme
        frame = QtWidgets.QFrame()
        frame.setStyleSheet(
            f"QFrame {{ border: 1px solid {theme.AMBER}; border-radius: 6px; }}"
            f"QLabel {{ color: {theme.AMBER}; border: none; }}")
        row = QtWidgets.QHBoxLayout(frame)
        row.setContentsMargins(8, 6, 8, 6)
        msg = QtWidgets.QLabel(
            "resume.md is older than your Resume Data — the job scorer is matching "
            "against an out-of-date résumé. Regenerate to bring it in sync.")
        msg.setWordWrap(True)
        row.addWidget(msg, 1)
        self.stale_regen_btn = QtWidgets.QPushButton("Regenerate resume.md")
        self.stale_regen_btn.clicked.connect(self._generate)
        row.addWidget(self.stale_regen_btn)
        self.stale_banner = frame
        return frame

    def _refresh_stale_banner(self) -> None:
        self.stale_banner.setVisible(
            resume_md.resume_md_stale(master_path=self.master_path))

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._refresh_stale_banner()

    def reload(self) -> None:
        self._basics_edits.clear()
        self._basics_orig.clear()
        self._entry_edits.clear()
        self._entry_orig.clear()
        self._atom_edits.clear()
        self._atom_orig.clear()
        self._atom_impact.clear()
        self._atom_impact_orig.clear()
        self._layout_section_edits.clear()
        self._layout_project_edits.clear()
        self._verbatim_edits.clear()

        body = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(body)
        data = self._read()

        if not self.master_path.exists():
            warn = QtWidgets.QLabel("No master_experience.yaml yet. Copy the example file to "
                                    "master_experience.yaml, then reopen this tab.")
            warn.setProperty("muted", True)
            warn.setWordWrap(True)
            v.addWidget(warn)
        tips = QtWidgets.QLabel(_TIPS)
        tips.setProperty("muted", True)
        tips.setWordWrap(True)
        v.addWidget(tips)

        self._basics_block(v, data.get("basics") or {})
        for sec in ("experience", "projects", "leadership"):
            self._section_block(v, sec, data.get(sec) or [])
        self._layout_block(v, data)
        self._readonly_block(v, data)
        self._buttons(v)
        v.addStretch(1)
        self.scroll.setWidget(body)
        self._refresh_stale_banner()  # editing the master can change drift state

    def _basics_block(self, v, basics: dict) -> None:
        box = QtWidgets.QGroupBox("Your details (basics)")
        form = QtWidgets.QFormLayout(box)
        for k, label in _BASICS_FIELDS:
            edit = QtWidgets.QLineEdit(str(basics.get(k, "") or ""))
            self._basics_edits[k] = edit
            self._basics_orig[k] = edit.text()
            form.addRow(label, edit)
        v.addWidget(box)

    def _section_block(self, v, section: str, entries: list) -> None:
        head = QtWidgets.QHBoxLayout()
        lab = QtWidgets.QLabel(section.capitalize())
        lab.setProperty("heading", True)
        head.addWidget(lab)
        head.addStretch(1)
        add = QtWidgets.QPushButton("+ Add entry")
        add.clicked.connect(lambda _=False, s=section: self._add_entry_dialog(s))
        head.addWidget(add)
        v.addLayout(head)
        if not entries:
            none = QtWidgets.QLabel("(none yet)")
            none.setProperty("muted", True)
            v.addWidget(none)
        for idx, entry in enumerate(entries):
            if isinstance(entry, dict):
                self._entry_block(v, section, idx, entry)

    def _entry_block(self, v, section: str, idx: int, entry: dict) -> None:
        name = str(entry.get(_NAME_KEY[section], "") or "(unnamed)")
        box = QtWidgets.QGroupBox(name)
        bv = QtWidgets.QVBoxLayout(box)
        form = QtWidgets.QFormLayout()
        for k, label in _SECTION_FIELDS[section]:
            edit = QtWidgets.QLineEdit(str(entry.get(k, "") or ""))
            self._entry_edits[(section, idx, k)] = edit
            self._entry_orig[(section, idx, k)] = edit.text()
            form.addRow(label, edit)
        bv.addLayout(form)

        # C: per-block "don't tailor". When checked, this block renders the user's
        # EXACT bullets (LLM bypassed); the atom editors are hidden and a simple
        # bulleted editor takes over, prefilled from the saved verbatim bullets.
        saved_vb = jobsdata.load_verbatim_blocks().get(name) or []
        vb_cb = QtWidgets.QCheckBox("Don't tailor — use my exact bullets")
        vb_cb.setToolTip("Render this block's bullets exactly as you type them, "
                         "skipping the LLM tailoring for it.")
        vb_cb.setChecked(bool(saved_vb))
        bv.addWidget(vb_cb)

        # (A) normal atom editors, grouped so they can be hidden in verbatim mode.
        atoms_holder = QtWidgets.QWidget()
        ah = QtWidgets.QVBoxLayout(atoms_holder)
        ah.setContentsMargins(0, 0, 0, 0)
        cap = QtWidgets.QLabel("Achievements (atoms) — impact: one measurable result per line")
        cap.setProperty("muted", True)
        ah.addWidget(cap)
        for atom in entry.get("achievements") or []:
            if isinstance(atom, dict):
                self._atom_block(ah, atom)
        abar = QtWidgets.QHBoxLayout()
        add_a = QtWidgets.QPushButton("+ Add achievement")
        add_a.clicked.connect(lambda _=False, s=section, i=idx: self._add_atom_dialog(s, i))
        abar.addWidget(add_a)
        abar.addStretch(1)
        ah.addLayout(abar)
        bv.addWidget(atoms_holder)

        # (B) verbatim bulleted editor + live "• " preview that mirrors the résumé.
        vb_holder = QtWidgets.QWidget()
        vh = QtWidgets.QVBoxLayout(vb_holder)
        vh.setContentsMargins(0, 0, 0, 0)
        vh_cap = QtWidgets.QLabel("Your exact bullets — one per line; rendered on the résumé as typed:")
        vh_cap.setProperty("muted", True)
        vh_cap.setWordWrap(True)
        vh.addWidget(vh_cap)
        vb_edit = QtWidgets.QPlainTextEdit("\n".join(str(b) for b in saved_vb))
        vb_edit.setPlaceholderText("One bullet per line, e.g.\n"
                                   "Led a team of 5 to ship X\nBuilt Y, cutting Z by 30%")
        vb_edit.setMinimumHeight(110)
        vh.addWidget(vb_edit)
        preview = QtWidgets.QLabel()
        preview.setProperty("muted", True)
        preview.setWordWrap(True)
        vh.addWidget(preview)
        bv.addWidget(vb_holder)

        def _refresh_preview() -> None:
            lines = [ln.strip() for ln in vb_edit.toPlainText().splitlines() if ln.strip()]
            preview.setText("\n".join(f"•  {ln}" for ln in lines) or "(no bullets yet)")

        vb_edit.textChanged.connect(_refresh_preview)
        _refresh_preview()

        def _apply_mode(checked: bool) -> None:
            atoms_holder.setVisible(not checked)
            vb_holder.setVisible(checked)

        vb_cb.toggled.connect(_apply_mode)
        _apply_mode(vb_cb.isChecked())
        self._verbatim_edits[name] = (vb_cb, vb_edit)

        bar = QtWidgets.QHBoxLayout()
        bar.addStretch(1)
        dele = QtWidgets.QPushButton("Delete entry")
        dele.clicked.connect(lambda _=False, s=section, i=idx, nm=name: self._delete_entry(s, i, nm))
        bar.addWidget(dele)
        bv.addLayout(bar)
        v.addWidget(box)

    def _gather_verbatim(self) -> dict:
        """Verbatim blocks to persist: keep saved keys for blocks not shown, and for
        each shown block set its non-empty lines when 'don't tailor' is checked, else
        drop it (revert to normal tailoring)."""
        out = dict(jobsdata.load_verbatim_blocks())
        for name, (cb, edit) in self._verbatim_edits.items():
            lines = [ln.strip() for ln in edit.toPlainText().splitlines() if ln.strip()]
            if cb.isChecked() and lines:
                out[name] = lines
            else:
                out.pop(name, None)
        return out

    def _atom_block(self, bv, atom: dict) -> None:
        aid = str(atom.get("id", ""))
        frame = QtWidgets.QFrame()
        form = QtWidgets.QFormLayout(frame)
        what = QtWidgets.QLineEdit(str(atom.get("what", "") or ""))
        self._atom_edits[(aid, "what")] = what
        self._atom_orig[(aid, "what")] = what.text()
        form.addRow("what", what)
        angles = QtWidgets.QLineEdit(", ".join(str(x) for x in (atom.get("angles") or [])))
        self._atom_edits[(aid, "angles")] = angles
        self._atom_orig[(aid, "angles")] = angles.text()
        form.addRow("angles", angles)
        imp = QtWidgets.QPlainTextEdit("\n".join(str(x) for x in (atom.get("impact") or [])))
        imp.setFixedHeight(64)
        self._atom_impact[aid] = imp
        self._atom_impact_orig[aid] = imp.toPlainText()
        form.addRow("impact", imp)
        dele = QtWidgets.QPushButton("Delete achievement")
        dele.clicked.connect(lambda _=False, a=aid: self._delete_atom(a))
        form.addRow("", dele)
        bv.addWidget(frame)

    def _readonly_block(self, v, data: dict) -> None:
        box = QtWidgets.QGroupBox("Education & Skills (view only here)")
        bv = QtWidgets.QVBoxLayout(box)
        note = QtWidgets.QLabel("Edit these in master_experience.yaml for now "
                                "(in-dashboard editing is on the backlog).")
        note.setProperty("muted", True)
        note.setWordWrap(True)
        bv.addWidget(note)
        lines = []
        for e in data.get("education") or []:
            if isinstance(e, dict):
                lines.append(f"•  {e.get('school', '')} — {e.get('degree', '')} ({e.get('dates', '')})")
        skills = data.get("skills") or {}
        if isinstance(skills, dict):
            for pool, items in skills.items():
                lines.append(f"{pool}: {', '.join(str(x) for x in (items or []))}")
        if lines:
            lab = QtWidgets.QLabel("\n".join(lines))
            lab.setProperty("muted", True)
            lab.setWordWrap(True)  # a long skills line must wrap, not widen the page
            bv.addWidget(lab)
        v.addWidget(box)

    def _layout_block(self, v, data: dict) -> None:
        """The 'Resume Layout' editor: per-bullet line targets for each section/project,
        backed by config.json's `resume_layout` / `project_layout` (the same maps the
        tailor engine reads). A master checkbox toggles whether the engine applies them
        at all, so custom-vs-default layout can be A/B tested without losing the targets.
        Row names are taken from the master so they match what the engine looks up."""
        section = CollapsibleSection(
            "Resume Layout (bullet sizing)",
            subtitle="per-section / per-project line targets — toggle to A/B test",
            collapsed=True)

        self._stale_layout_rows = []  # rebuilt fresh on each reload

        self._layout_enabled_cb = QtWidgets.QCheckBox("Apply custom bullet layout")
        self._layout_enabled_cb.setChecked(jobsdata.load_resume_layout_enabled())
        self._layout_enabled_cb.toggled.connect(self._on_layout_toggled)  # after setChecked
        section.add_widget(self._layout_enabled_cb)

        # B2: project count + at-most/exactly-N mode (moved here from Settings).
        section.add_widget(self._projects_control())

        help_lbl = QtWidgets.QLabel(
            'Each box is a comma-separated list of bullet line-counts, e.g. "2, 2, 1" = '
            "three bullets sized 2 / 2 / 1 printed lines (each 1-3, up to 5 bullets). Leave "
            "a box blank to let the engine choose. Unchecking the box above keeps these "
            "saved but makes the engine use its built-in defaults.")
        help_lbl.setWordWrap(True)
        help_lbl.setProperty("muted", True)
        section.add_widget(help_lbl)

        cfg_sections = jobsdata.load_resume_layout()
        cfg_projects = jobsdata.load_project_layout()

        sec_live: list[str] = []
        for s in ("experience", "leadership"):
            for e in data.get(s) or []:
                if isinstance(e, dict):
                    nm = str(e.get(_NAME_KEY[s], "") or "").strip()
                    if nm and nm not in sec_live:
                        sec_live.append(nm)
        sec_names = list(sec_live)
        for nm in cfg_sections:                       # keep stale config keys editable/clearable
            if nm not in sec_names:
                sec_names.append(nm)
        section.add_widget(self._layout_group(
            "Sections (experience / leadership)", sec_names, cfg_sections,
            self._layout_section_edits, set(sec_live), "section"))

        proj_live: list[str] = []
        for e in data.get("projects") or []:
            if isinstance(e, dict):
                nm = str(e.get("name", "") or "").strip()
                if nm and nm not in proj_live:
                    proj_live.append(nm)
        proj_names = list(proj_live)
        for nm in cfg_projects:
            if nm not in proj_names:
                proj_names.append(nm)
        section.add_widget(self._layout_group(
            "Projects", proj_names, cfg_projects, self._layout_project_edits,
            set(proj_live), "project"))

        bar = QtWidgets.QHBoxLayout()
        save = QtWidgets.QPushButton("Save layout")
        save.clicked.connect(self._save_layout)
        bar.addWidget(save)
        self._remove_stale_btn = QtWidgets.QPushButton("Remove stale entries")
        self._remove_stale_btn.setToolTip(
            "Drop every layout row whose name is no longer in your résumé data.")
        self._remove_stale_btn.clicked.connect(self._remove_stale_layout)
        self._remove_stale_btn.setEnabled(bool(self._stale_layout_rows))
        bar.addWidget(self._remove_stale_btn)
        bar.addStretch(1)
        holder = QtWidgets.QWidget()
        holder.setLayout(bar)
        section.add_widget(holder)

        v.addWidget(section)

    def _projects_control(self) -> QtWidgets.QGroupBox:
        """Spinbox (1-6) + 'At most N' / 'Exactly N' radio for how many projects the
        tailored resume lists. Persisted via jobsdata.save_projects_count on Save layout."""
        n, mode = jobsdata.load_projects_count()
        box = QtWidgets.QGroupBox("Projects on the résumé")
        form = QtWidgets.QFormLayout(box)

        self._projects_count_spin = QtWidgets.QSpinBox()
        self._projects_count_spin.setRange(1, 6)
        self._projects_count_spin.setValue(n)
        form.addRow("How many projects", self._projects_count_spin)

        self._projects_mode_max = QtWidgets.QRadioButton("At most this many")
        self._projects_mode_exact = QtWidgets.QRadioButton("Exactly this many")
        (self._projects_mode_exact if mode == "exact" else self._projects_mode_max).setChecked(True)
        mode_row = QtWidgets.QHBoxLayout()
        mode_row.addWidget(self._projects_mode_max)
        mode_row.addWidget(self._projects_mode_exact)
        mode_row.addStretch(1)
        mode_holder = QtWidgets.QWidget()
        mode_holder.setLayout(mode_row)
        form.addRow("Mode", mode_holder)

        mode_help = QtWidgets.QLabel(
            "At most N: list up to N of your strongest projects, dropping the weakest to "
            "hold one page. Exactly N: always keep N projects (when you have that many), "
            "trimming bullets instead of dropping a whole project.")
        mode_help.setWordWrap(True)
        mode_help.setProperty("muted", True)
        form.addRow(mode_help)

        self._project_tiers_edit = QtWidgets.QLineEdit(
            _tiers_to_text(jobsdata.load_project_bullet_tiers()))
        self._project_tiers_edit.setPlaceholderText("e.g. 2:3, 2:2, 1:1")
        form.addRow("Bullets by strength", self._project_tiers_edit)
        tiers_help = QtWidgets.QLabel(
            'Optional tiers (projects:bullets, strongest first): "2:3, 2:2, 1:1" gives your '
            "2 strongest projects 3 bullets each, the next 2 get 2, the 5th gets 1. Projects "
            "past the last tier use the bullet count above. Leave blank for a flat count. The "
            "strongest project is whichever best fits each job, so this re-tiers per posting; a "
            "per-project box below (if set) overrides its tier.")
        tiers_help.setWordWrap(True)
        tiers_help.setProperty("muted", True)
        form.addRow(tiers_help)

        self._projects_warn = QtWidgets.QLabel(
            "More than 4 projects rarely fits one page cleanly — the tailor may shrink "
            "bullets or (in 'at most' mode) drop your weakest projects to hold one page.")
        self._projects_warn.setWordWrap(True)
        self._projects_warn.setProperty("warn", True)
        self._projects_warn.setVisible(self._projects_count_spin.value() > 4)
        self._projects_count_spin.valueChanged.connect(
            lambda val: self._projects_warn.setVisible(val > 4))
        form.addRow(self._projects_warn)
        return box

    def _layout_group(self, title: str, names: list, cfg: dict, store: dict,
                      live_names: set, kind: str) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox(title)
        form = QtWidgets.QFormLayout(box)
        if not names:
            none = QtWidgets.QLabel("(no entries yet)")
            none.setProperty("muted", True)
            form.addRow(none)
        for nm in names:
            spec = cfg.get(nm) if isinstance(cfg, dict) else None
            targets = spec.get("line_targets") if isinstance(spec, dict) else None
            text = (", ".join(str(t) for t in targets)
                    if isinstance(targets, (list, tuple)) else "")
            edit = QtWidgets.QLineEdit(text)
            edit.setPlaceholderText("e.g. 2, 2, 1")
            store[nm] = edit
            if nm in live_names:
                form.addRow(nm, edit)
                continue
            # B8: stale row (no longer in the master) — mark it and give it a ✕ button.
            field = QtWidgets.QWidget()
            hl = QtWidgets.QHBoxLayout(field)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.addWidget(edit, 1)
            drop = QtWidgets.QToolButton()
            drop.setText("✕")
            drop.setToolTip("Remove this stale entry (no longer in your résumé data)")
            hl.addWidget(drop)
            label = f"{nm}  (removed from résumé data)"
            form.addRow(label, field)
            self._stale_layout_rows.append((kind, nm, field, form))
            drop.clicked.connect(
                lambda _=False, k=kind, n=nm, w=field, f=form: self._delete_stale_layout(k, n, w, f))
        return box

    def _delete_stale_layout(self, kind: str, nm: str, row: QtWidgets.QWidget,
                             form: QtWidgets.QFormLayout) -> None:
        """Drop one stale layout key from the saved map and remove its row, without a
        full tab rebuild (so in-progress atom edits are untouched)."""
        loader = jobsdata.load_resume_layout if kind == "section" else jobsdata.load_project_layout
        saver = jobsdata.save_resume_layout if kind == "section" else jobsdata.save_project_layout
        m = loader()
        m.pop(nm, None)
        saver(m)
        store = self._layout_section_edits if kind == "section" else self._layout_project_edits
        store.pop(nm, None)
        form.removeRow(row)  # removes both the label and the field widget
        self._stale_layout_rows = [r for r in self._stale_layout_rows if not (r[0] == kind and r[1] == nm)]
        if hasattr(self, "_remove_stale_btn"):
            self._remove_stale_btn.setEnabled(bool(self._stale_layout_rows))
        self._set_status(f"Removed stale layout entry '{nm}'.")

    def _remove_stale_layout(self) -> None:
        """Drop every stale layout row at once."""
        if not self._stale_layout_rows:
            self._set_status("No stale layout entries to remove.")
            return
        n = len(self._stale_layout_rows)
        for kind, nm, row, form in list(self._stale_layout_rows):
            self._delete_stale_layout(kind, nm, row, form)
        self._set_status(f"Removed {n} stale layout entr{'y' if n == 1 else 'ies'}.")

    def _on_layout_toggled(self, checked: bool) -> None:
        jobsdata.save_resume_layout_enabled(bool(checked))
        self._set_status("Custom bullet layout ON."
                         if checked else "Custom bullet layout OFF (engine defaults).")

    def _save_layout(self) -> None:
        sec_map = self._gather_layout(self._layout_section_edits)
        proj_map = self._gather_layout(self._layout_project_edits)
        jobsdata.save_resume_layout(sec_map)
        jobsdata.save_project_layout(proj_map)
        if hasattr(self, "_projects_count_spin"):
            mode = "exact" if self._projects_mode_exact.isChecked() else "max"
            jobsdata.save_projects_count(self._projects_count_spin.value(), mode)
            count_msg = f"{self._projects_count_spin.value()} project(s) [{mode}]; "
        else:
            count_msg = ""
        if hasattr(self, "_project_tiers_edit"):
            jobsdata.save_project_bullet_tiers(_parse_tiers(self._project_tiers_edit.text()))
        self._set_status(
            f"Layout saved ({count_msg}{len(sec_map)} section(s), {len(proj_map)} project(s)).")

    @staticmethod
    def _gather_layout(store: dict) -> dict:
        out: dict = {}
        for nm, edit in store.items():
            targets = _parse_targets(edit.text())
            if targets:
                out[nm] = {"line_targets": targets}
        return out

    def _buttons(self, v) -> None:
        bar = QtWidgets.QHBoxLayout()
        save = QtWidgets.QPushButton("Save changes")
        save.setProperty("accent", True)
        save.clicked.connect(self.save)
        bar.addWidget(save)
        val = QtWidgets.QPushButton("Validate")
        val.clicked.connect(self._validate_clicked)
        bar.addWidget(val)
        rev = QtWidgets.QPushButton("Revert to opening state")
        rev.clicked.connect(self._revert_clicked)
        bar.addWidget(rev)
        bar.addStretch(1)
        v.addLayout(bar)

    # ---- actions -------------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def save(self) -> bool:
        try:
            b_changes = {k: e.text() for k, e in self._basics_edits.items()
                         if e.text() != self._basics_orig.get(k, "")}
            if b_changes:
                master_edit.update_basics(b_changes, self.master_path)

            entry_changes: dict[tuple, dict] = {}
            for (sec, idx, k), e in self._entry_edits.items():
                if e.text() != self._entry_orig[(sec, idx, k)]:
                    entry_changes.setdefault((sec, idx), {})[k] = e.text()
            for (sec, idx), fields in entry_changes.items():
                master_edit.update_entry(sec, idx, fields, self.master_path)

            atom_changes: dict[str, dict] = {}
            for (aid, k), e in self._atom_edits.items():
                if e.text() != self._atom_orig[(aid, k)]:
                    val = e.text()
                    if k == "angles":
                        val = [a.strip() for a in val.split(",") if a.strip()]
                    atom_changes.setdefault(aid, {})[k] = val
            for aid, txt in self._atom_impact.items():
                current = txt.toPlainText()
                if current != self._atom_impact_orig.get(aid, ""):
                    atom_changes.setdefault(aid, {})["impact"] = [
                        ln.strip() for ln in current.splitlines() if ln.strip()]
            for aid, fields in atom_changes.items():
                master_edit.update_atom(aid, fields, self.master_path)
        except (ValueError, OSError) as exc:
            self._set_status("Save failed.")
            QtWidgets.QMessageBox.critical(self, "Résumé data", str(exc))
            return False

        # Per-block "don't tailor" bullets live in config.json (separate from the
        # master YAML the edits above wrote); persist them before the rebuild.
        jobsdata.save_verbatim_blocks(self._gather_verbatim())

        errs = self.validate()
        self.reload()
        self._set_status(f"Saved — but {len(errs)} problem(s) remain; click Validate."
                         if errs else "Saved.")
        if self.on_saved:
            self.on_saved()
        return True

    def _validate_clicked(self) -> None:
        errs = self.validate()
        if not errs:
            QtWidgets.QMessageBox.information(self, "Validate", "Looks good — no problems found.")
            self._set_status("Valid.")
        else:
            QtWidgets.QMessageBox.critical(
                self, "Validate", "Problems found:\n\n- " + "\n- ".join(errs))
            self._set_status(f"{len(errs)} problem(s) — see the list.")

    def revert(self) -> None:
        if self.snapshot:
            master_edit.restore_bytes(self.snapshot, self.master_path)
        self.reload()

    def _revert_clicked(self) -> None:
        if QtWidgets.QMessageBox.question(
                self, "Revert", "Undo every change since you opened this tab?"
        ) == QtWidgets.QMessageBox.StandardButton.Yes:
            self.revert()
            self._set_status("Reverted to opening state.")

    def _delete_entry(self, section: str, idx: int, name: str) -> None:
        if QtWidgets.QMessageBox.question(
                self, "Delete entry", f"Delete '{name}' and all its bullets?"
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            master_edit.delete_entry(section, idx, self.master_path)
        except (ValueError, OSError) as exc:
            QtWidgets.QMessageBox.critical(self, "Delete entry", str(exc))
            return
        self.reload()
        self._set_status(f"Deleted '{name}'.")

    def _delete_atom(self, atom_id: str) -> None:
        if QtWidgets.QMessageBox.question(
                self, "Delete achievement", "Delete this achievement?"
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            master_edit.delete_atom(atom_id, self.master_path)
        except (ValueError, OSError) as exc:
            QtWidgets.QMessageBox.critical(self, "Delete achievement", str(exc))
            return
        self.reload()
        self._set_status("Deleted an achievement.")

    def _add_atom_dialog(self, section: str, idx: int) -> None:
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Add achievement")
        form = QtWidgets.QFormLayout(dlg)
        what = QtWidgets.QLineEdit()
        angles = QtWidgets.QLineEdit()
        imp = QtWidgets.QPlainTextEdit()
        imp.setFixedHeight(64)
        form.addRow("What (required)", what)
        form.addRow("Angles (comma-separated, required)", angles)
        form.addRow("Impact (one per line)", imp)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        form.addRow(buttons)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        ang = [a.strip() for a in angles.text().split(",") if a.strip()]
        impact = [ln.strip() for ln in imp.toPlainText().splitlines() if ln.strip()]
        if not what.text().strip() or not ang:
            QtWidgets.QMessageBox.critical(self, "Add achievement",
                                           "Need a 'what' and at least one angle.")
            return
        self.add_atom(section, idx, what.text().strip(), ang, impact)

    def add_atom(self, section: str, idx: int, what: str, angles: list, impact: list) -> None:
        try:
            master_edit.add_atom(section, idx, {"what": what, "angles": angles, "impact": impact},
                                 self.master_path)
        except (ValueError, OSError) as exc:
            QtWidgets.QMessageBox.critical(self, "Add achievement", str(exc))
            return
        self.reload()
        self._set_status("Added an achievement.")

    def _add_entry_dialog(self, section: str) -> None:
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Add {section} entry")
        form = QtWidgets.QFormLayout(dlg)
        edits = {}
        for k, label in _SECTION_FIELDS[section]:
            edits[k] = QtWidgets.QLineEdit()
            form.addRow(label, edits[k])
        what = QtWidgets.QLineEdit()
        angles = QtWidgets.QLineEdit()
        impact = QtWidgets.QLineEdit()
        form.addRow("First achievement — What", what)
        form.addRow("Angles (comma-separated)", angles)
        form.addRow("Impact (comma-separated, optional)", impact)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        form.addRow(buttons)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        data = {k: e.text().strip() for k, e in edits.items() if e.text().strip()}
        achievement = {"what": what.text().strip(),
                       "angles": [a.strip() for a in angles.text().split(",") if a.strip()]}
        imp = [s.strip() for s in impact.text().split(",") if s.strip()]
        if imp:
            achievement["impact"] = imp
        data["achievements"] = [achievement]
        try:
            master_edit.append_entry(section, data, self.master_path)
        except (ValueError, OSError) as exc:
            QtWidgets.QMessageBox.critical(self, "Add entry", str(exc))
            return
        self.reload()
        self._set_status(f"Added a {section} entry.")

    # ---- resume.md generator -------------------------------------------------

    def _refresh_push_state(self) -> None:
        import vm_sync
        cfg = settings.load()
        on = bool(cfg.get("vm_enabled")) and vm_sync.VMTarget.from_env().configured()
        self.btn_push_md.setEnabled(on)

    def _generate(self) -> None:
        model = self.md_model.currentText().strip() or "gemini-3.5-flash"
        if not resume_md.MASTER_YAML_PATH.exists():
            QtWidgets.QMessageBox.critical(
                self, "Generate resume.md", "No master_experience.yaml found — add Resume Data first.")
            return
        if QtWidgets.QMessageBox.question(
                self, "Generate resume.md",
                f"Rebuild resume.md from your Resume Data with {model}?\n\nThis makes a Gemini call."
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._set_status("Generating resume.md … (Gemini)")
        yaml_text = resume_md.MASTER_YAML_PATH.read_text(encoding="utf-8")
        workers.run_async(self, lambda: resume_md.generate_resume_md(yaml_text, model),
                          on_done=self._preview, on_error=self._gen_failed)

    def _gen_failed(self, exc) -> None:
        self._set_status("resume.md generation failed.")
        QtWidgets.QMessageBox.critical(self, "Generate resume.md", f"Generation failed:\n\n{exc}")

    def _preview(self, md: str) -> None:
        self._set_status("resume.md generated — review it before it's saved.")
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Generated resume.md — review before saving")
        dlg.resize(840, 660)
        v = QtWidgets.QVBoxLayout(dlg)
        note = QtWidgets.QLabel("Review (and edit) the generated resume.md. 'Use this' backs up the "
                                "current file to resume.md.bak, then writes this version.")
        note.setProperty("muted", True)
        note.setWordWrap(True)
        v.addWidget(note)
        editor = QtWidgets.QPlainTextEdit(md)
        v.addWidget(editor, 1)
        buttons = QtWidgets.QDialogButtonBox()
        use = buttons.addButton("Use this (write resume.md)",
                                QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        use.setProperty("accent", True)
        buttons.addButton(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        v.addWidget(buttons)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self._resume_md_write(editor.toPlainText().rstrip("\n") + "\n")

    def _resume_md_write(self, text: str) -> None:
        try:
            resume_md.write_resume_md(text)
        except OSError as exc:
            QtWidgets.QMessageBox.critical(self, "resume.md", f"Could not write resume.md:\n\n{exc}")
            return
        self._set_status("resume.md updated (old version saved to resume.md.bak).")
        self._refresh_stale_banner()  # now back in sync -> hide the warning

    def _push_resume_md(self) -> None:
        import vm_sync
        target = vm_sync.VMTarget.from_env()
        if not target.configured():
            QtWidgets.QMessageBox.information(
                self, "Push resume.md", "No VM configured. Set VM_INSTANCE / VM_ZONE / VM_USER.")
            return
        if not resume_md.RESUME_MD_PATH.exists():
            QtWidgets.QMessageBox.critical(
                self, "Push resume.md", "No resume.md yet — generate it first.")
            return
        if QtWidgets.QMessageBox.question(
                self, "Push resume.md", f"Copy resume.md to {target.user}@{target.instance}?"
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._set_status("Pushing resume.md to VM …")
        workers.run_async(
            self, lambda: vm_sync.run_cmd(
                target.build_scp_cmd(str(resume_md.RESUME_MD_PATH), "resume.md")),
            on_done=self._push_md_finished,
            on_error=self._push_md_launch_error)

    @staticmethod
    def _push_outcome(res) -> tuple[bool, str]:
        """Decide success/failure from the scp result. `scp` does NOT raise on a
        non-zero exit (e.g. pscp can't reach the host / open the path), so the
        return code — not the mere absence of an exception — is what tells us the
        file actually landed. Returns (ok, message)."""
        ok = getattr(res, "returncode", 0) == 0
        out = ((getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")).strip()
        if ok:
            return True, "resume.md pushed to the VM."
        return False, "Push failed.\n\n" + (out[:1200] or "gcloud returned a non-zero exit code.")

    def _push_md_finished(self, res) -> None:
        ok, msg = self._push_outcome(res)
        self._set_status("resume.md pushed to VM." if ok else "resume.md push failed.")
        (QtWidgets.QMessageBox.information if ok
         else QtWidgets.QMessageBox.critical)(self, "Push resume.md", msg)

    def _push_md_launch_error(self, exc) -> None:
        self._set_status("resume.md push failed.")
        QtWidgets.QMessageBox.critical(
            self, "Push resume.md", f"Push failed to launch:\n\n{exc}")
