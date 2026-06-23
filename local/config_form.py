"""Schema-driven configuration form — shared by the dashboard Settings tab and
the standalone `configure.pyw` window.

Renders `settings.SETTINGS_SCHEMA` grouped by section into a scrollable form:
one labelled, explained input per Field, with the right widget per type so a
non-technical user can't easily break things —

  * choice            -> a dropdown (no free-typing a bad value)
  * multichoice       -> a row of checkboxes (e.g. remote types)
  * path              -> an entry with a "Browse..." button
  * secret            -> a masked, write-only box (the saved value is never shown;
                         leaving it blank keeps the existing value; a "Clear"
                         checkbox unsets it)
  * bool              -> a checkbox
  * list              -> a multi-line box, one item per line
  * str/int/float     -> a plain entry (numbers are range-checked on Save)

Save validates via `settings.validate`/`settings.save` and reports friendly
errors; "Restore defaults" repopulates the widgets (nothing is written until you
press Save). An optional `on_saved` callback lets the dashboard refresh state.

Theme-agnostic: widget colors are read from the active ttk style (ui.apply_theme
sets the dark palette; the standalone launcher applies the same one), so this
module never imports the dashboard — no circular dependency.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable

import settings
import smoothscroll

# One-line "what this section controls" blurbs under each section header.
SECTION_HELP = {
    "Credentials": ("API keys and tokens, saved to your private .env file and never shown again. "
                    "Each box stays blank even when a key is saved: leave it blank to keep the saved "
                    "key, type a new value to replace it, or tick Clear to delete it."),
    "Connection & paths": "Your cloud project, your name, and where files live on this PC.",
    "Engine": "Which Gemini backend the resume tailor bills.",
    "Dashboard": "How the dashboard surfaces and tracks jobs.",
    "Scraper": "What the LinkedIn scraper searches for (this drives Bright Data spend).",
    "Scoring": ("Advanced — which models score jobs and the spend guards around them. The "
                "defaults are tuned; changing the model names can silently break scoring, "
                "so leave them unless you know the exact model IDs your account can use."),
    "Resume": "What the resume tailor generates, and how the cover letter reads.",
    "VM (cloud scraper)": ("Connect to your cloud scraper VM (GCP) so the VM tab can push config, "
                           "schedule, and pause changes to it. Uses your existing `gcloud` login — "
                           "no SSH password or key is ever stored."),
}

# New users need credentials/connection first; show those sections at the top.
# (Apply-form answers are edited in the richer Apply Answers tab, not here.)
SECTION_ORDER = [
    "Credentials", "Connection & paths", "Engine",
    "Dashboard", "Scraper", "Scoring", "Resume", "VM (cloud scraper)",
]

# Sections rendered with a master on/off checkbox in their header: section name ->
# the bool Field.key that gates it. When the gate is off, the section's fields (and
# any section extra mounted under it) collapse out of view. The VM section is off
# by default so users with no cloud VM never see it.
COLLAPSIBLE_SECTIONS = {"VM (cloud scraper)": "vm_enabled"}

_SECRET_SET = "saved — blank keeps it, type to replace"
_SECRET_UNSET = "not set"


def _ordered_sections() -> list[tuple[str, list[settings.Field]]]:
    """Group schema Fields by section, ordered by SECTION_ORDER (extras appended)."""
    by_section: dict[str, list[settings.Field]] = {}
    for f in settings.SETTINGS_SCHEMA:
        by_section.setdefault(f.section, []).append(f)
    ordered = [(s, by_section[s]) for s in SECTION_ORDER if s in by_section]
    ordered += [(s, fs) for s, fs in by_section.items() if s not in SECTION_ORDER]
    return ordered


class ConfigForm:
    """Builds the form into `parent` and owns its widget state."""

    def __init__(self, parent: tk.Widget, on_saved: Callable[[], None] | None = None,
                 targets: dict | None = None,
                 section_extras: dict[str, Callable[[ttk.Frame], tk.Widget]] | None = None):
        self.parent = parent
        self.on_saved = on_saved
        self.targets = targets  # None -> real files; tests pass a tmp mapping
        # section name -> builder(parent)->widget, mounted inside a collapsible
        # section under its fields (the dashboard passes the VM operations panel).
        self.section_extras = section_extras or {}
        self._collapse_frames: dict[str, ttk.Frame] = {}  # collapsible section bodies

        self.vars: dict[str, tk.Variable] = {}        # scalar widgets (entry/combo/check)
        self.texts: dict[str, tk.Text] = {}           # list fields
        self.scales: dict[str, ttk.Scale] = {}        # slider fields (int, bounded)
        self.multi: dict[str, dict[str, tk.BooleanVar]] = {}   # multichoice -> {choice: var}
        self.clear_vars: dict[str, tk.BooleanVar] = {}         # secret -> "clear it" toggle
        self._secret_labels: dict[str, ttk.Label] = {}         # secret -> status label
        self._storage_labels: dict[str, ttk.Label] = {}        # field -> "stored in X" tag
        self.status: ttk.Label | None = None

        style = ttk.Style(parent)
        self._bg = style.lookup("TFrame", "background") or "#1b2230"
        self._field_bg = style.lookup("TEntry", "fieldbackground") or "#0f1420"
        self._fg = style.lookup("TLabel", "foreground") or "#e6e9ef"
        # tk.Text ignores the ttk theme font, so capture it and apply it explicitly
        # below — otherwise the list box renders in Tk's default (often monospace).
        self._font = style.lookup("TLabel", "font") or "Segoe UI 10"

        self._build()

    # ---- construction --------------------------------------------------------

    def _build(self) -> None:
        canvas = tk.Canvas(self.parent, bg=self._bg, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(self.parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = ttk.Frame(canvas, padding=(16, 12))
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(body_window, width=e.width))
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        stored = settings.load(self.targets)
        secret_set = settings.secret_status(self.targets)
        # Snapshot the values the form opens with, so "Revert changes" can return
        # to this session's starting point (distinct from factory defaults).
        self._opening_values = dict(stored)

        row = 0
        for section, fields in _ordered_sections():
            if section in COLLAPSIBLE_SECTIONS:
                row = self._add_collapsible_section(
                    body, section, fields, stored, secret_set, row)
                continue
            row = self._add_section_header(body, section, row)
            for f in fields:
                row = self._add_field(body, f, stored.get(f.key, f.default), secret_set, row)

        row = self._add_buttons(body, row)
        self._wire_wheel(canvas, body)

    def _add_collapsible_section(self, body: ttk.Frame, section: str,
                                 fields: list[settings.Field], stored: dict,
                                 secret_set: dict, row: int) -> int:
        """Render a section whose header carries a master on/off checkbox; the rest
        of the section (blurb, fields, and any section extra) lives in a sub-frame
        that shows/hides live as the checkbox flips. Initial state follows storage."""
        gate_key = COLLAPSIBLE_SECTIONS[section]
        gate = next((f for f in fields if f.key == gate_key), None)

        outer = ttk.Frame(body)
        outer.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(14 if row else 0, 0))
        row += 1
        ttk.Label(outer, text=section, style="Subtitle.TLabel").grid(
            row=0, column=0, sticky="w")
        var = tk.BooleanVar(value=bool(stored.get(gate_key, getattr(gate, "default", False))))
        self.vars[gate_key] = var
        ttk.Checkbutton(outer, text=gate.label if gate else "Enable", variable=var,
                        command=self._apply_section_visibility).grid(
            row=0, column=1, sticky="w", padx=(12, 0))

        collapse = ttk.Frame(outer)
        collapse.grid(row=1, column=0, columnspan=2, sticky="ew")
        self._collapse_frames[section] = collapse

        sub = 0
        blurb = SECTION_HELP.get(section)
        if blurb:
            ttk.Label(collapse, text=blurb, style="Muted.TLabel", wraplength=560).grid(
                row=sub, column=0, columnspan=2, sticky="w", pady=(2, 4))
            sub += 1
        for f in fields:
            if f.key == gate_key:
                continue  # the gate is the header checkbox, not a normal row
            sub = self._add_field(collapse, f, stored.get(f.key, f.default), secret_set, sub)
        builder = self.section_extras.get(section)
        if builder is not None:
            extra = builder(collapse)
            if extra is not None:
                extra.grid(row=sub, column=0, columnspan=2, sticky="ew", pady=(10, 0))
                sub += 1

        self._apply_section_visibility()
        return row

    def _apply_section_visibility(self) -> None:
        """Show/hide each collapsible section's body to match its master checkbox."""
        for section, gate_key in COLLAPSIBLE_SECTIONS.items():
            frame = self._collapse_frames.get(section)
            var = self.vars.get(gate_key)
            if frame is None or var is None:
                continue
            if bool(var.get()):
                frame.grid()
            else:
                frame.grid_remove()

    def _add_section_header(self, body: ttk.Frame, section: str, row: int) -> int:
        ttk.Label(body, text=section, style="Subtitle.TLabel").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(14 if row else 0, 0))
        row += 1
        blurb = SECTION_HELP.get(section)
        if blurb:
            ttk.Label(body, text=blurb, style="Muted.TLabel", wraplength=560).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
            row += 1
        return row

    def _add_field(self, body: ttk.Frame, f: settings.Field, value, secret_set: dict,
                   row: int) -> int:
        anchor = "nw" if f.type in ("list", "multichoice") else "w"
        # Label + a muted "(filename)" tag so the user can see — and go find — the
        # exact file each value is saved to (e.g. ".env" / "search_config.json").
        labcell = ttk.Frame(body)
        labcell.grid(row=row, column=0, sticky=anchor, padx=(8, 12), pady=(6, 0))
        ttk.Label(labcell, text=f.label).pack(side="left")
        tag = ttk.Label(labcell, text=f"({settings.storage_location(f)})",
                        style="Muted.TLabel")
        tag.pack(side="left", padx=(6, 0))
        self._storage_labels[f.key] = tag
        widget = self._make_widget(body, f, value, secret_set)
        widget.grid(row=row, column=1, sticky="w", pady=(6, 0))
        row += 1
        if f.help:
            ttk.Label(body, text=f.help, style="Muted.TLabel", wraplength=640).grid(
                row=row, column=1, sticky="w", pady=(0, 2))
            row += 1
        return row

    def _make_widget(self, parent: ttk.Frame, f: settings.Field, value, secret_set: dict):
        if f.secret:
            return self._secret_widget(parent, f, secret_set.get(f.key, False))
        if f.type == "bool":
            var = tk.BooleanVar(value=bool(value))
            self.vars[f.key] = var
            return ttk.Checkbutton(parent, variable=var)
        if f.type == "choice":
            var = tk.StringVar(value=str(value))
            self.vars[f.key] = var
            return ttk.Combobox(parent, textvariable=var, state="readonly",
                                width=38, values=list(f.choices))
        if f.type == "editable_choice":
            # pick from the list OR type a custom value (state="normal").
            var = tk.StringVar(value=str(value))
            self.vars[f.key] = var
            return ttk.Combobox(parent, textvariable=var, state="normal",
                                width=40, values=list(f.choices))
        if getattr(f, "slider", False) and f.type == "int":
            return self._slider_widget(parent, f, value)
        if f.type == "multichoice":
            return self._multichoice_widget(parent, f, value)
        if f.type == "list":
            txt = tk.Text(parent, width=58, height=12, wrap="none", font=self._font,
                          bg=self._field_bg, fg=self._fg, insertbackground=self._fg,
                          relief="flat", highlightthickness=1, highlightbackground="#2a3344")
            items = value if isinstance(value, list) else []
            txt.insert("1.0", "\n".join(str(v) for v in items))
            self.texts[f.key] = txt
            return txt
        if f.type == "path":
            return self._path_widget(parent, f, value)
        # str / int / float
        var = tk.StringVar(value="" if value is None else str(value))
        self.vars[f.key] = var
        return ttk.Entry(parent, textvariable=var, width=58 if f.type == "str" else 16)

    def _slider_widget(self, parent: ttk.Frame, f: settings.Field, value):
        """A bounded int rendered as a drag slider + a live numeric readout. The
        readout is a StringVar in self.vars (so collect()/validate treat it like
        any int field); the scale is tracked in self.scales for restore_defaults."""
        frame = ttk.Frame(parent)
        try:
            cur = int(value)
        except (TypeError, ValueError):
            cur = int(f.default)
        var = tk.StringVar(value=str(cur))
        self.vars[f.key] = var
        scale = ttk.Scale(
            frame, from_=float(f.min), to=float(f.max), orient="horizontal", length=240,
            command=lambda v, k=f.key: self.vars[k].set(str(int(round(float(v))))))
        scale.set(cur)
        self.scales[f.key] = scale
        scale.grid(row=0, column=0, sticky="w")
        ttk.Label(frame, textvariable=var, width=5).grid(row=0, column=1, sticky="w", padx=(10, 0))
        return frame

    def _multichoice_widget(self, parent: ttk.Frame, f: settings.Field, value):
        frame = ttk.Frame(parent)
        current = set(value if isinstance(value, list) else [])
        self.multi[f.key] = {}
        for i, choice in enumerate(f.choices):
            var = tk.BooleanVar(value=choice in current)
            self.multi[f.key][choice] = var
            ttk.Checkbutton(frame, text=choice, variable=var).grid(
                row=0, column=i, sticky="w", padx=(0, 12))
        return frame

    def _path_widget(self, parent: ttk.Frame, f: settings.Field, value):
        frame = ttk.Frame(parent)
        var = tk.StringVar(value="" if value is None else str(value))
        self.vars[f.key] = var
        ttk.Entry(frame, textvariable=var, width=56).pack(side="left")
        ttk.Button(frame, text="Browse…",
                   command=lambda: self._browse(var, f.path_kind)).pack(side="left", padx=(6, 0))
        return frame

    def _secret_widget(self, parent: ttk.Frame, f: settings.Field, is_set: bool):
        frame = ttk.Frame(parent)
        var = tk.StringVar(value="")  # never pre-filled with the stored secret
        self.vars[f.key] = var
        ttk.Entry(frame, textvariable=var, width=52, show="•").grid(
            row=0, column=0, sticky="w")
        status = ttk.Label(frame, text=_SECRET_SET if is_set else _SECRET_UNSET,
                           style="Muted.TLabel")
        status.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self._secret_labels[f.key] = status
        clear_var = tk.BooleanVar(value=False)
        self.clear_vars[f.key] = clear_var
        ttk.Checkbutton(frame, text="Clear (delete saved key)", variable=clear_var).grid(
            row=0, column=2, sticky="w", padx=(8, 0))
        return frame

    def _add_buttons(self, body: ttk.Frame, row: int) -> int:
        btnbar = ttk.Frame(body)
        btnbar.grid(row=row, column=0, columnspan=2, sticky="w", pady=(18, 4))
        ttk.Button(btnbar, text="Save", command=self.save,
                   style="Accent.TButton").pack(side="left")
        ttk.Button(btnbar, text="Revert changes",
                   command=self.revert).pack(side="left", padx=(8, 0))
        ttk.Button(btnbar, text="Restore defaults",
                   command=self.restore_defaults).pack(side="left", padx=(8, 0))
        self.status = ttk.Label(btnbar, text="", style="Muted.TLabel")
        self.status.pack(side="left", padx=(12, 0))
        return row + 1

    def _wire_wheel(self, canvas: tk.Canvas, body: ttk.Frame) -> None:
        # Coalesce wheel bursts into one deferred scroll so a fast flick over this
        # ~285-widget form doesn't pile up synchronous repaints into a freeze.
        smoothscroll.bind_canvas_wheel(canvas, body)

    # ---- actions -------------------------------------------------------------

    def _browse(self, var: tk.StringVar, kind: str) -> None:
        top = self.parent.winfo_toplevel()
        if kind == "file":
            chosen = filedialog.askopenfilename(parent=top, title="Select file")
        else:
            chosen = filedialog.askdirectory(parent=top, title="Select folder")
        if chosen:
            var.set(chosen)

    def collect(self) -> tuple[dict, dict[str, str]]:
        """Read the widgets into a {key: value} dict ready for settings.save, plus
        a {key: message} dict of coercion errors. Secret boxes are included only
        when the user typed a new value or ticked Clear (so a blank box keeps the
        existing value)."""
        values: dict = {}
        errors: dict[str, str] = {}
        for f in settings.SETTINGS_SCHEMA:
            if f.secret:
                typed = str(self.vars[f.key].get())
                if self.clear_vars[f.key].get():
                    values[f.key] = ""          # explicit unset
                elif typed.strip():
                    values[f.key] = typed       # new value
                # else: omit -> existing value preserved
                continue
            if f.type == "multichoice":
                values[f.key] = [c for c, v in self.multi[f.key].items() if v.get()]
                continue
            if f.type == "list":
                raw = self.texts[f.key].get("1.0", "end")
                values[f.key] = [ln.strip() for ln in raw.splitlines() if ln.strip()]
                continue
            value, err = self._coerce(f, self.vars[f.key].get())
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
        return text, None  # str / path / choice

    @staticmethod
    def _changed_summary(before: dict, values: dict) -> list[str]:
        """Human-readable list of which fields `values` changes vs `before`, by
        label. Secrets (only present in `values` when typed or explicitly cleared)
        report 'updated'/'cleared' and never echo the value; multichoice/list
        compare order-insensitively / whitespace-insensitively to avoid noise."""
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

    def _notify_saved(self, summary: list[str]) -> None:
        if summary:
            msg = "Settings saved. Updated:\n\n- " + "\n- ".join(summary)
        else:
            msg = "No changes to save - your settings are unchanged."
        messagebox.showinfo("Settings", msg, parent=self.parent.winfo_toplevel())

    def save(self) -> bool:
        values, errors = self.collect()
        labels = {f.key: f.label for f in settings.SETTINGS_SCHEMA}
        errors.update(settings.validate(values))
        if errors:
            msg = "\n".join(f"{labels.get(k, k)}: {m}" for k, m in errors.items())
            if self.status:
                self.status.configure(text="Not saved — see error.")
            messagebox.showerror("Settings", msg, parent=self.parent.winfo_toplevel())
            return False
        before = settings.load(self.targets)  # pre-save state, for the change summary
        try:
            settings.save(values, self.targets)
        except (ValueError, OSError) as exc:
            if self.status:
                self.status.configure(text="Save failed.")
            messagebox.showerror("Settings", str(exc), parent=self.parent.winfo_toplevel())
            return False
        summary = self._changed_summary(before, values)
        self._refresh_secret_labels()
        if self.status:
            self.status.configure(text="Saved." if summary else "Saved — no changes.")
        # Re-snapshot so a later Revert returns to this just-saved state.
        self._opening_values = settings.load(self.targets)
        self._notify_saved(summary)
        if self.on_saved:
            self.on_saved()
        return True

    def _refresh_secret_labels(self) -> None:
        status = settings.secret_status(self.targets)
        for key, label in self._secret_labels.items():
            label.configure(text=_SECRET_SET if status.get(key) else _SECRET_UNSET)
            self.vars[key].set("")            # clear the box after a successful save
            self.clear_vars[key].set(False)

    def _repopulate(self, value_for: Callable[[settings.Field], object]) -> None:
        """Reset every non-secret widget to value_for(field). Shared by Restore
        defaults (factory) and Revert changes (this session's opening values)."""
        for f in settings.SETTINGS_SCHEMA:
            if f.secret:
                continue
            val = value_for(f)
            if f.type == "multichoice":
                want = set(val if isinstance(val, list) else [])
                for choice, var in self.multi[f.key].items():
                    var.set(choice in want)
            elif f.type == "list":
                txt = self.texts[f.key]
                txt.delete("1.0", "end")
                items = val if isinstance(val, list) else []
                txt.insert("1.0", "\n".join(str(v) for v in items))
            elif f.type == "bool":
                self.vars[f.key].set(bool(val))
            elif getattr(f, "slider", False) and f.key in self.scales:
                try:
                    self.scales[f.key].set(int(val))  # command syncs the readout var
                except (TypeError, ValueError):
                    self.scales[f.key].set(int(f.default))
            else:
                self.vars[f.key].set("" if val is None else str(val))
        self._apply_section_visibility()  # a changed gate may re-hide its section

    def restore_defaults(self) -> None:
        """Repopulate widgets with each Field's default. Nothing is written until
        Save. Secret boxes are left blank (a saved secret is only changed if you
        type a new one or tick Clear)."""
        self._repopulate(lambda f: f.default)
        if self.status:
            self.status.configure(text="Defaults restored — press Save to apply.")

    def revert(self) -> None:
        """Repopulate widgets with the values the form opened with this session
        (undo my edits). Nothing is written until Save. Secret boxes return to
        blank with Clear unticked, so a saved secret is left untouched."""
        self._repopulate(lambda f: self._opening_values.get(f.key, f.default))
        for key, clear in self.clear_vars.items():
            clear.set(False)
            self.vars[key].set("")
        if self.status:
            self.status.configure(text="Reverted to your last-opened settings — press Save to apply.")
