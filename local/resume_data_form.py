"""The Résumé Data editor — edit master_experience.yaml from the dashboard.

A structured, scrollable editor over the user's master experience file so a
non-technical user never has to open the YAML by hand. It lists `basics` and the
three atom-bearing sections (experience / projects / leadership); each entry's
fields and each achievement atom are editable, and entries/atoms can be added or
deleted. Education and skills are shown read-only for now (edited in the file).

Safety net (the user asked for "don't let people break things permanently"):
  * every write goes through `master_edit`, which backs the file up to `.bak`;
  * **Validate** runs `master_validate.validate_master` and shows clear problems;
  * **Revert to opening state** restores the snapshot taken when the editor opened.

Theme-agnostic: colors are read from the active ttk style (like config_form), so
this module never imports the dashboard — no circular dependency.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path
from typing import Callable

import yaml

from resume_tailor import config, master_edit, master_validate

_SECTION_FIELDS = {
    "experience": [("org", "Org"), ("title", "Title"), ("location", "Location"), ("dates", "Dates")],
    "projects": [("name", "Name"), ("dates", "Dates"), ("live_url", "Live URL"), ("repo", "Repo")],
    "leadership": [("org", "Org"), ("title", "Title"), ("dates", "Dates")],
}
_NAME_KEY = {"experience": "org", "projects": "name", "leadership": "org"}
_BASICS_FIELDS = [("name", "Name"), ("email", "Email"), ("phone", "Phone"),
                  ("location", "Location"), ("linkedin", "LinkedIn"), ("github", "GitHub")]

_TIPS = (
    "Tips for a résumé the tailor can use well:\n"
    "•  Store FACTS as atoms (what happened / how / scope / impact), not finished sentences — "
    "the tailor re-angles them per job.\n"
    "•  Quantify everything you can (%, $, counts, time saved). Numbers win.\n"
    "•  Add 'angles' tags (e.g. backend, llm, data-pipeline) so an atom matches a job's keywords.\n"
    "•  Hold MORE than fits on one page — the pipeline SELECTS the best evidence; it never invents.\n"
    "•  Every bullet must trace to something true you wrote here."
)


class ResumeDataEditor:
    """Builds the editor into `parent` and owns its widget state."""

    def __init__(self, parent: tk.Widget, on_saved: Callable[[], None] | None = None,
                 master_path: Path | None = None):
        self.parent = parent
        self.on_saved = on_saved
        self.master_path = Path(master_path) if master_path is not None else config.MASTER_YAML
        self.snapshot = self.master_path.read_bytes() if self.master_path.exists() else b""

        style = ttk.Style(parent)
        self._bg = style.lookup("TFrame", "background") or "#1b2230"
        self._fg = style.lookup("TLabel", "foreground") or "#e6e9ef"

        self.sections: dict = {}
        self._basics_vars: dict[str, tk.StringVar] = {}
        self._basics_orig: dict[str, str] = {}
        self._entry_vars: dict[tuple, tk.StringVar] = {}
        self._entry_orig: dict[tuple, str] = {}
        self._atom_vars: dict[tuple, tk.StringVar] = {}
        self._atom_orig: dict[tuple, str] = {}
        self.status: ttk.Label | None = None

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
        """Problems with the file as currently saved on disk ([] = OK)."""
        return master_validate.validate_master(self._read())

    # ---- construction --------------------------------------------------------

    def _build_shell(self) -> None:
        self.canvas = tk.Canvas(self.parent, bg=self._bg, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(self.parent, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas, padding=(16, 12))
        self._body_window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.bind(
            "<Configure>", lambda e: self.canvas.itemconfigure(self._body_window, width=e.width))
        self.body.bind(
            "<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

    def reload(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        self._basics_vars.clear()
        self._basics_orig.clear()
        self._entry_vars.clear()
        self._entry_orig.clear()
        self._atom_vars.clear()
        self._atom_orig.clear()

        data = self._read()
        self.sections = {s: (data.get(s) or []) for s in ("experience", "projects", "leadership")}
        self.sections["basics"] = data.get("basics") or {}

        if not self.master_path.exists():
            ttk.Label(self.body, style="Muted.TLabel", wraplength=620,
                      text=("No master_experience.yaml yet. Copy "
                            "resume_tailor_files/master_experience.example.yaml to "
                            "master_experience.yaml, then reopen this tab.")).pack(anchor="w")

        ttk.Label(self.body, text=_TIPS, style="Muted.TLabel", justify="left",
                  wraplength=640).pack(anchor="w", pady=(0, 10))

        self._basics_block(data.get("basics") or {})
        for sec in ("experience", "projects", "leadership"):
            self._section_block(sec, data.get(sec) or [])
        self._readonly_block(data)
        self._buttons()
        self._wire_wheel()
        self.canvas.yview_moveto(0)

    def _basics_block(self, basics: dict) -> None:
        f = ttk.LabelFrame(self.body, text="Your details (basics)", padding=8)
        f.pack(fill="x", pady=(0, 10))
        for i, (k, label) in enumerate(_BASICS_FIELDS):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", padx=(0, 8), pady=2)
            v = tk.StringVar(value=str(basics.get(k, "") or ""))
            self._basics_vars[k] = v
            self._basics_orig[k] = v.get()
            ttk.Entry(f, textvariable=v, width=48).grid(row=i, column=1, sticky="w", pady=2)

    def _section_block(self, section: str, entries: list) -> None:
        outer = ttk.Frame(self.body)
        outer.pack(fill="x", pady=(6, 2))
        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text=section.capitalize(), style="Subtitle.TLabel").pack(side="left")
        ttk.Button(head, text="+ Add entry",
                   command=lambda s=section: self._add_entry_dialog(s)).pack(side="right")
        for idx, entry in enumerate(entries):
            if isinstance(entry, dict):
                self._entry_block(outer, section, idx, entry)
        if not entries:
            ttk.Label(outer, text="(none yet)", style="Muted.TLabel").pack(anchor="w", padx=8)

    def _entry_block(self, parent: ttk.Frame, section: str, idx: int, entry: dict) -> None:
        name = str(entry.get(_NAME_KEY[section], "") or "(unnamed)")
        lf = ttk.LabelFrame(parent, text=name, padding=8)
        lf.pack(fill="x", pady=4, padx=2)
        grid = ttk.Frame(lf)
        grid.pack(fill="x")
        for i, (k, label) in enumerate(_SECTION_FIELDS[section]):
            ttk.Label(grid, text=label).grid(row=i, column=0, sticky="w", padx=(0, 8), pady=2)
            v = tk.StringVar(value=str(entry.get(k, "") or ""))
            self._entry_vars[(section, idx, k)] = v
            self._entry_orig[(section, idx, k)] = v.get()
            ttk.Entry(grid, textvariable=v, width=48).grid(row=i, column=1, sticky="w", pady=2)

        ttk.Label(lf, text="Achievements (atoms):", style="Muted.TLabel").pack(
            anchor="w", pady=(6, 2))
        for atom in entry.get("achievements") or []:
            if isinstance(atom, dict):
                self._atom_block(lf, atom)

        bar = ttk.Frame(lf)
        bar.pack(fill="x", pady=(6, 0))
        ttk.Button(bar, text="+ Add achievement",
                   command=lambda s=section, i=idx: self._add_atom_dialog(s, i)).pack(side="left")
        ttk.Button(bar, text="Delete entry",
                   command=lambda s=section, i=idx, nm=name: self._delete_entry(s, i, nm)).pack(
            side="right")

    def _atom_block(self, parent: ttk.Widget, atom: dict) -> None:
        aid = str(atom.get("id", ""))
        fr = ttk.Frame(parent)
        fr.pack(fill="x", pady=2, padx=(8, 0))
        ttk.Label(fr, text="what").grid(row=0, column=0, sticky="w")
        wv = tk.StringVar(value=str(atom.get("what", "") or ""))
        self._atom_vars[(aid, "what")] = wv
        self._atom_orig[(aid, "what")] = wv.get()
        ttk.Entry(fr, textvariable=wv, width=62).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(fr, text="angles").grid(row=1, column=0, sticky="w")
        av = tk.StringVar(value=", ".join(str(x) for x in (atom.get("angles") or [])))
        self._atom_vars[(aid, "angles")] = av
        self._atom_orig[(aid, "angles")] = av.get()
        ttk.Entry(fr, textvariable=av, width=62).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Button(fr, text="Delete", command=lambda a=aid: self._delete_atom(a)).grid(
            row=0, column=2, rowspan=2, padx=6)

    def _readonly_block(self, data: dict) -> None:
        edu = data.get("education") or []
        skills = data.get("skills") or {}
        f = ttk.LabelFrame(self.body, text="Education & Skills (view only here)", padding=8)
        f.pack(fill="x", pady=(10, 4))
        ttk.Label(f, style="Muted.TLabel", wraplength=620,
                  text=("Edit these in resume_tailor_files/master_experience.yaml for now "
                        "(full in-dashboard editing of education/skills is on the backlog).")).pack(
            anchor="w")
        lines = []
        for e in edu:
            if isinstance(e, dict):
                lines.append("•  %s — %s (%s)" % (
                    e.get("school", ""), e.get("degree", ""), e.get("dates", "")))
        if isinstance(skills, dict):
            for pool, items in skills.items():
                lines.append("%s: %s" % (pool, ", ".join(str(x) for x in (items or []))))
        if lines:
            ttk.Label(f, text="\n".join(lines), style="Muted.TLabel", justify="left").pack(
                anchor="w", pady=(4, 0))

    def _buttons(self) -> None:
        bar = ttk.Frame(self.body)
        bar.pack(fill="x", pady=(14, 4))
        ttk.Button(bar, text="Save changes", command=self.save,
                   style="Accent.TButton").pack(side="left")
        ttk.Button(bar, text="Validate", command=self._validate_clicked).pack(
            side="left", padx=(8, 0))
        ttk.Button(bar, text="Revert to opening state", command=self._revert_clicked).pack(
            side="left", padx=(8, 0))
        self.status = ttk.Label(bar, text="", style="Muted.TLabel")
        self.status.pack(side="left", padx=(12, 0))

    def _wire_wheel(self) -> None:
        def _wheel(e):
            self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
            return "break"

        def _bind(w):
            if not isinstance(w, tk.Text):
                w.bind("<MouseWheel>", _wheel)
            for child in w.winfo_children():
                _bind(child)

        self.canvas.bind("<MouseWheel>", _wheel)
        _bind(self.body)

    # ---- actions -------------------------------------------------------------

    def _top(self):
        return self.parent.winfo_toplevel()

    def _set_status(self, text: str) -> None:
        if self.status:
            self.status.configure(text=text)

    def save(self) -> bool:
        """Persist changed basics/entry/atom fields (each via master_edit, which
        backs up first), then validate + reload. Unchanged fields are skipped."""
        try:
            b_changes = {k: var.get() for k, var in self._basics_vars.items()
                         if var.get() != self._basics_orig.get(k, "")}
            if b_changes:
                master_edit.update_basics(b_changes, self.master_path)

            entry_changes: dict[tuple, dict] = {}
            for (sec, idx, k), var in self._entry_vars.items():
                if var.get() != self._entry_orig[(sec, idx, k)]:
                    entry_changes.setdefault((sec, idx), {})[k] = var.get()
            for (sec, idx), fields in entry_changes.items():
                master_edit.update_entry(sec, idx, fields, self.master_path)

            atom_changes: dict[str, dict] = {}
            for (aid, k), var in self._atom_vars.items():
                if var.get() != self._atom_orig[(aid, k)]:
                    val = var.get()
                    if k == "angles":
                        val = [a.strip() for a in val.split(",") if a.strip()]
                    atom_changes.setdefault(aid, {})[k] = val
            for aid, fields in atom_changes.items():
                master_edit.update_atom(aid, fields, self.master_path)
        except (ValueError, OSError) as exc:
            self._set_status("Save failed.")
            messagebox.showerror("Résumé data", str(exc), parent=self._top())
            return False

        errs = self.validate()
        self.reload()
        if errs:
            self._set_status("Saved — but %d problem(s) remain; click Validate." % len(errs))
        else:
            self._set_status("Saved.")
        if self.on_saved:
            self.on_saved()
        return True

    def _validate_clicked(self) -> None:
        errs = self.validate()
        if not errs:
            messagebox.showinfo("Validate", "Looks good — no problems found.", parent=self._top())
            self._set_status("Valid.")
        else:
            messagebox.showerror(
                "Validate", "Problems found:\n\n• " + "\n• ".join(errs), parent=self._top())
            self._set_status("%d problem(s) — see the list." % len(errs))

    def revert(self) -> None:
        """Restore the file to the snapshot taken when this editor opened."""
        if self.snapshot:
            master_edit.restore_bytes(self.snapshot, self.master_path)
        self.reload()

    def _revert_clicked(self) -> None:
        if not messagebox.askyesno(
                "Revert", "Undo every change since you opened this tab?", parent=self._top()):
            return
        self.revert()
        self._set_status("Reverted to opening state.")

    def _delete_entry(self, section: str, idx: int, name: str) -> None:
        if not messagebox.askyesno(
                "Delete entry", "Delete '%s' and all its bullets?" % name, parent=self._top()):
            return
        try:
            master_edit.delete_entry(section, idx, self.master_path)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Delete entry", str(exc), parent=self._top())
            return
        self.reload()
        self._set_status("Deleted '%s'." % name)

    def _delete_atom(self, atom_id: str) -> None:
        if not messagebox.askyesno(
                "Delete achievement", "Delete this achievement?", parent=self._top()):
            return
        try:
            master_edit.delete_atom(atom_id, self.master_path)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Delete achievement", str(exc), parent=self._top())
            return
        self.reload()
        self._set_status("Deleted an achievement.")

    def _add_atom_dialog(self, section: str, idx: int) -> None:
        win = tk.Toplevel(self._top())
        win.title("Add achievement")
        win.transient(self._top())
        win.grab_set()
        ttk.Label(win, text="What (required)").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 2))
        what_v = tk.StringVar()
        ttk.Entry(win, textvariable=what_v, width=64).grid(row=0, column=1, padx=8, pady=(8, 2))
        ttk.Label(win, text="Angles (comma-separated, required)").grid(
            row=1, column=0, sticky="w", padx=8, pady=2)
        ang_v = tk.StringVar()
        ttk.Entry(win, textvariable=ang_v, width=64).grid(row=1, column=1, padx=8, pady=2)
        ttk.Label(win, text="Impact (one per line)").grid(row=2, column=0, sticky="nw", padx=8, pady=2)
        imp_txt = tk.Text(win, width=50, height=3)
        imp_txt.grid(row=2, column=1, padx=8, pady=2)

        def _ok():
            what = what_v.get().strip()
            angles = [a.strip() for a in ang_v.get().split(",") if a.strip()]
            impact = [ln.strip() for ln in imp_txt.get("1.0", "end").splitlines() if ln.strip()]
            if not what or not angles:
                messagebox.showerror("Add achievement", "Need a 'what' and at least one angle.",
                                     parent=win)
                return
            try:
                master_edit.add_atom(section, idx, {"what": what, "angles": angles, "impact": impact},
                                     self.master_path)
            except (ValueError, OSError) as exc:
                messagebox.showerror("Add achievement", str(exc), parent=win)
                return
            win.destroy()
            self.reload()
            self._set_status("Added an achievement.")

        btns = ttk.Frame(win)
        btns.grid(row=3, column=0, columnspan=2, pady=10)
        ttk.Button(btns, text="Add", command=_ok, style="Accent.TButton").pack(side="left", padx=6)
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="left", padx=6)
        win.wait_window(win)

    def _add_entry_dialog(self, section: str) -> None:
        win = tk.Toplevel(self._top())
        win.title("Add %s entry" % section)
        win.transient(self._top())
        win.grab_set()
        field_vars: dict[str, tk.StringVar] = {}
        for i, (k, label) in enumerate(_SECTION_FIELDS[section]):
            ttk.Label(win, text=label).grid(row=i, column=0, sticky="w", padx=8, pady=2)
            v = tk.StringVar()
            field_vars[k] = v
            ttk.Entry(win, textvariable=v, width=56).grid(row=i, column=1, padx=8, pady=2)
        base = len(_SECTION_FIELDS[section])
        ttk.Label(win, text="First achievement — What").grid(
            row=base, column=0, sticky="w", padx=8, pady=(8, 2))
        what_v = tk.StringVar()
        ttk.Entry(win, textvariable=what_v, width=56).grid(row=base, column=1, padx=8, pady=(8, 2))
        ttk.Label(win, text="Angles (comma-separated)").grid(
            row=base + 1, column=0, sticky="w", padx=8, pady=2)
        ang_v = tk.StringVar()
        ttk.Entry(win, textvariable=ang_v, width=56).grid(row=base + 1, column=1, padx=8, pady=2)

        def _ok():
            data = {k: v.get().strip() for k, v in field_vars.items() if v.get().strip()}
            angles = [a.strip() for a in ang_v.get().split(",") if a.strip()]
            data["achievements"] = [{"what": what_v.get().strip(), "angles": angles}]
            try:
                master_edit.append_entry(section, data, self.master_path)
            except (ValueError, OSError) as exc:
                messagebox.showerror("Add entry", str(exc), parent=win)
                return
            win.destroy()
            self.reload()
            self._set_status("Added a %s entry." % section)

        btns = ttk.Frame(win)
        btns.grid(row=base + 2, column=0, columnspan=2, pady=10)
        ttk.Button(btns, text="Add", command=_ok, style="Accent.TButton").pack(side="left", padx=6)
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="left", padx=6)
        win.wait_window(win)
