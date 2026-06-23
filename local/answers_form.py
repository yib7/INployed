"""The Apply Answers editor — manage the master answer store from the dashboard.

A table over `apply_answers.json`: one row per screening-question answer, each with
its question, answer, kind (fixed / open-ended) and status (active / needs-review).
Add your own answers, edit captured ones, delete what you don't need, and filter to
the needs-review items the apply skill flagged. Mark a question **fixed** (race,
DOB, work-auth — never changed) or **open-ended** (the apply skill may adapt it per
job).

Safety net: Save validates via `apply_answers.validate`; every write backs up to
`.bak`; "Revert to opening state" restores the snapshot taken when the editor
opened. Theme-agnostic (reads colors from the active ttk style) — no ui import.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path
from typing import Callable

from resume_tailor import apply_answers


class AnswersEditor:
    """Builds the table editor into `parent` and owns its widget state."""

    def __init__(self, parent: tk.Widget, on_saved: Callable[[], None] | None = None,
                 store_path: Path | None = None):
        self.parent = parent
        self.on_saved = on_saved
        self.store_path = Path(store_path) if store_path is not None else apply_answers.STORE_PATH
        self.snapshot = self.store_path.read_bytes() if self.store_path.exists() else b""

        style = ttk.Style(parent)
        self._bg = style.lookup("TFrame", "background") or "#1b2230"

        self.rows: list[dict] = []
        self.filter_needs_review = tk.BooleanVar(value=False)
        self.status: ttk.Label | None = None

        self._build_shell()
        self.reload()

    # ---- construction --------------------------------------------------------

    def _build_shell(self) -> None:
        top = ttk.Frame(self.parent, padding=(12, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Apply Answers", style="Subtitle.TLabel").pack(side="left")
        ttk.Label(top, style="Muted.TLabel", wraplength=540,
                  text=("Reusable answers the apply helper fills into forms. Mark each fixed "
                        "(never changed) or open-ended (adaptable per job).")).pack(
            side="left", padx=(12, 0))
        ttk.Checkbutton(top, text="Show needs-review only",
                        variable=self.filter_needs_review,
                        command=self._apply_filter).pack(side="right")

        mid = ttk.Frame(self.parent)
        mid.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(mid, bg=self._bg, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas, padding=(12, 8))
        self._body_window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.bind(
            "<Configure>", lambda e: self.canvas.itemconfigure(self._body_window, width=e.width))
        self.body.bind(
            "<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        bar = ttk.Frame(self.parent, padding=(12, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="Save changes", command=self.save,
                   style="Accent.TButton").pack(side="left")
        ttk.Button(bar, text="Add answer", command=lambda: self.add_row()).pack(
            side="left", padx=(8, 0))
        ttk.Button(bar, text="Validate", command=self._validate_clicked).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Revert to opening state", command=self._revert_clicked).pack(
            side="left", padx=(8, 0))
        self.status = ttk.Label(bar, text="", style="Muted.TLabel")
        self.status.pack(side="left", padx=(12, 0))

    def reload(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        self.rows.clear()
        header = ttk.Frame(self.body)
        header.pack(fill="x")
        for text, w in (("Question", 44), ("Answer", 30), ("Kind", 12), ("Status", 14), ("", 8)):
            ttk.Label(header, text=text, style="Muted.TLabel", width=w, anchor="w").pack(side="left")
        for entry in apply_answers.load(self.store_path):
            self._add_row_widgets(entry)
        self._wire_wheel()
        self.canvas.yview_moveto(0)

    def _add_row_widgets(self, entry: dict) -> dict:
        fr = ttk.Frame(self.body)
        fr.pack(fill="x", pady=1)
        row = {
            "id": str(entry.get("id", "")),
            "question": tk.StringVar(value=str(entry.get("question", ""))),
            "answer": tk.StringVar(value=str(entry.get("answer", ""))),
            "kind": tk.StringVar(value=str(entry.get("kind", "open-ended"))),
            "status": tk.StringVar(value=str(entry.get("status", "active"))),
            "frame": fr,
        }
        ttk.Entry(fr, textvariable=row["question"], width=44).pack(side="left")
        ttk.Entry(fr, textvariable=row["answer"], width=30).pack(side="left", padx=(2, 0))
        ttk.Combobox(fr, textvariable=row["kind"], state="readonly", width=11,
                     values=list(apply_answers.KINDS)).pack(side="left", padx=(2, 0))
        ttk.Combobox(fr, textvariable=row["status"], state="readonly", width=13,
                     values=list(apply_answers.STATUSES)).pack(side="left", padx=(2, 0))
        ttk.Button(fr, text="Delete", width=7,
                   command=lambda r=row: self._delete_row(r)).pack(side="left", padx=(2, 0))
        self.rows.append(row)
        return row

    def add_row(self, entry: dict | None = None) -> dict:
        """Append a new editable row (blank, or pre-filled from `entry`)."""
        entry = entry or {"id": "", "question": "", "answer": "",
                          "kind": "open-ended", "status": "active"}
        row = self._add_row_widgets(entry)
        # A new row defaults to "active", so the needs-review filter would hide it
        # the instant it's added (making "Add answer" look broken). Drop the filter
        # so the user can see and edit the row they just asked for.
        self.filter_needs_review.set(False)
        self._apply_filter()
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(1.0)
        return row

    def _delete_row(self, row: dict) -> None:
        row["frame"].destroy()
        self.rows.remove(row)

    def _wire_wheel(self) -> None:
        def _wheel(e):
            self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
            return "break"

        def _bind(w):
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

    def _apply_filter(self) -> None:
        """Hide non-needs-review rows when the filter is on. Rows stay in
        `self.rows` (and in `collect()`), so filtering never loses data."""
        only_nr = self.filter_needs_review.get()
        for row in self.rows:
            show = (not only_nr) or row["status"].get() == "needs-review"
            if show:
                row["frame"].pack(fill="x", pady=1)
            else:
                row["frame"].pack_forget()

    def collect(self) -> list[dict]:
        """Read the rows into the store's list shape. Blank rows are dropped; new
        rows without an id get a unique slug from their question."""
        out: list[dict] = []
        taken: set = set()
        for row in self.rows:
            question = row["question"].get().strip()
            answer = row["answer"].get()
            if not question and not str(answer).strip():
                continue
            rid = row["id"] or apply_answers.new_id(question, taken)
            if rid in taken:
                rid = apply_answers.new_id(question or rid, taken)
            taken.add(rid)
            out.append({"id": rid, "question": question, "answer": answer,
                        "kind": row["kind"].get(), "status": row["status"].get()})
        return out

    def validate(self) -> list[str]:
        return apply_answers.validate(self.collect())

    def save(self) -> bool:
        answers = self.collect()
        errs = apply_answers.validate(answers)
        if errs:
            self._set_status("Not saved — see error.")
            messagebox.showerror("Apply answers", "Problems found:\n\n• " + "\n• ".join(errs),
                                 parent=self._top())
            return False
        try:
            apply_answers.save(answers, self.store_path)
        except (ValueError, OSError) as exc:
            self._set_status("Save failed.")
            messagebox.showerror("Apply answers", str(exc), parent=self._top())
            return False
        self.reload()
        self._apply_filter()
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
        """Restore the store to the snapshot taken when this editor opened."""
        if self.snapshot:
            apply_answers.restore_bytes(self.snapshot, self.store_path)
        elif self.store_path.exists():
            self.store_path.unlink()  # opening state was "no file yet"
        self.reload()
        self._apply_filter()

    def _revert_clicked(self) -> None:
        if not messagebox.askyesno(
                "Revert", "Undo every change since you opened this tab?", parent=self._top()):
            return
        self.revert()
        self._set_status("Reverted to opening state.")
