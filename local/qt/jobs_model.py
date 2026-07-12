"""Qt model + sort proxy for the job tables.

`JobsTableModel` wraps the display DataFrame: DisplayRole for cells, BackgroundRole
for the per-row tint (tailored-resume / tracker-status / recommendation), and
header labels from COLUMN_LABELS. It exposes a SORT_ROLE so a plain
`QSortFilterProxyModel` sorts the numeric columns numerically on a header click.

Filtering happens in pandas (`jobsdata.filter_and_sort`) before `set_dataframe`,
so the model always holds exactly the rows to show, in the default order; the
proxy only re-orders them when the user clicks a header.
"""
from __future__ import annotations

import pandas as pd
from PySide6 import QtCore

from jobsdata import COLUMN_LABELS
from qt import theme

# Columns whose values sort numerically rather than as text.
NUMERIC_COLS = {
    "score", "deep_score", "applicants", "days", "rows_in", "filtered_out",
    "llm_scored", "llm_errors", "stage2_done", "rescore_attempted",
    "rescore_scored", "llm_calls", "prompt_tokens", "output_tokens",
}

SORT_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1
_DISPLAY = QtCore.Qt.ItemDataRole.DisplayRole
_BACKGROUND = QtCore.Qt.ItemDataRole.BackgroundRole


class JobsTableModel(QtCore.QAbstractTableModel):
    def __init__(self, columns, mode="high", parent=None):
        super().__init__(parent)
        self._columns = list(columns)
        # Tinting mode, one per tab: "high"/unseen keys recommendation + resume;
        # "tracker" keys application status + follow-up; "all" is never tinted.
        self._mode = mode
        self._col_lists: list[list[str]] = []
        self._ids: list[str] = []
        self._tags: list[str] = []
        self._nrows = 0

    # ---- population ----------------------------------------------------------

    def set_dataframe(self, df: pd.DataFrame | None, resume_ids=frozenset(),
                      failed_ids=frozenset()) -> None:
        self.beginResetModel()
        n = 0 if df is None else len(df)
        self._nrows = n
        if df is None or df.empty:
            self._col_lists, self._ids, self._tags = [], [], []
        else:
            self._col_lists = [
                df[c].astype(str).tolist() if c in df.columns else [""] * n
                for c in self._columns
            ]
            self._ids = (df["job_posting_id"].astype(str).tolist()
                         if "job_posting_id" in df.columns else [""] * n)
            recos = (df["recommendation"].astype(str).str.strip().str.lower().tolist()
                     if "recommendation" in df.columns else [""] * n)
            statuses = (df["status"].astype(str).str.strip().str.lower().tolist()
                        if "status" in df.columns else [""] * n)
            followups = (df["follow_up"].astype(str).str.strip().tolist()
                         if "follow_up" in df.columns else [""] * n)
            rids = set(resume_ids)
            fids = set(failed_ids)
            self._tags = [
                self._row_tag(self._mode, self._ids[i], recos[i], statuses[i],
                              followups[i], rids, fids)
                for i in range(n)
            ]
        self.endResetModel()

    @staticmethod
    def _row_tag(mode: str, jid: str, reco: str, status: str, followup: str,
                 resume_ids: set, failed_ids: set = frozenset()) -> str:
        """The row's color tag, by tab.

        - "all": never tinted — a plain list to scan every job.
        - "tracker": application status + follow-up state (see `_tracker_tag`).
        - "high"/unseen (default): a FAILED tailor run wins (red — the row
          needs a re-run), then a tailored resume, then the recommendation; a
          "skip" reco is the untinted "don't consider" default.
        """
        if mode == "all":
            return ""
        if mode == "tracker":
            return JobsTableModel._tracker_tag(status, followup)
        if jid and jid in failed_ids:
            return "tailor_failed"
        if jid and jid in resume_ids:
            return "has_resume"
        if reco == "apply":
            return "apply"
        if reco == "consider":
            return "consider"
        return ""

    @staticmethod
    def _tracker_tag(status: str, followup: str) -> str:
        """Tracker row color. Terminal outcomes win (rejected -> red, offer ->
        green); then the follow-up state (sent -> pending pink, due -> followup
        orange); then the in-flight status (interviewing -> yellow, applied ->
        blue); else no tint."""
        f = (followup or "").strip().lower()
        if status == "rejected":
            return "rejected"
        if status == "offer":
            return "offer"
        if f == "done":
            return "pending"
        if f == "due":
            return "followup"
        if status == "interviewing":
            return "interviewing"
        if status == "applied":
            return "applied"
        return ""

    # ---- Qt model interface --------------------------------------------------

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else self._nrows

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index, role=_DISPLAY):
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        if role == _DISPLAY:
            return self._col_lists[c][r]
        if role == _BACKGROUND:
            color = theme.row_color(self._tags[r])
            return color if color.isValid() else None
        if role == SORT_ROLE:
            val = self._col_lists[c][r]
            if self._columns[c] in NUMERIC_COLS:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return float("-inf")
            return val.lower()
        return None

    def headerData(self, section, orientation, role=_DISPLAY):
        if role != _DISPLAY:
            return None
        if orientation == QtCore.Qt.Orientation.Horizontal:
            cid = self._columns[section]
            return COLUMN_LABELS.get(cid, cid)
        return section + 1

    # ---- helpers used by the tab/tests --------------------------------------

    def job_id(self, row: int) -> str:
        return self._ids[row] if 0 <= row < len(self._ids) else ""

    def column_id(self, col: int) -> str:
        return self._columns[col] if 0 <= col < len(self._columns) else ""

    def row_tag(self, row: int) -> str:
        return self._tags[row] if 0 <= row < len(self._tags) else ""

    @property
    def columns(self) -> list[str]:
        return list(self._columns)
