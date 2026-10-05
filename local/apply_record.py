"""The job's record (`apply_record.md`, `write_record`, the earlier
attempts it keeps) and the drain's report (`summary_line`, `drain_table`,
`write_drain_report`).

Split out of `apply_run`, which re-exports these names. It logs as
`apply_run` (one of `apply_trace.LOGGERS`).
"""
from __future__ import annotations

import logging
import os
import posixpath
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import apply_queue
import apply_trace
import jev
import jsonutil
from apply_outcome import _cap, Outcome
from apply_page import _is_password
from apply_route import generated_count

log = logging.getLogger("apply_run")


RECORD_NAME = "apply_record.md"
HIDDEN = "<hidden>"

_TRACE_LINE = "- Trace: "
_TRACE_LINK = re.compile(r"\]\((" + re.escape(apply_trace.TRACE_DIR) + r"/[^)]*)\)")


def _link_from(target: str, here: str) -> str:
    """`target` (relative to the job folder) as a link from the folder
    `here` (relative to the job folder too)."""
    rel = posixpath.relpath(target.rstrip("/") or ".", here)
    return rel + "/" if target.endswith("/") else rel


def _record_head(path: Path) -> tuple[str, str, str]:
    """(status, reason, written) from a record's header lines."""
    found = {"Status": "", "Reason": "", "Written": ""}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            for key in found:
                if not found[key] and line.startswith(f"- {key}: "):
                    found[key] = line[len(key) + 4:].strip()
            if line.startswith("## "):
                break
    except OSError:
        pass
    return found["Status"], found["Reason"], found["Written"]


def _write_text_atomic(path: Path, text: str) -> None:
    """`text` into `path` through a temp file beside it and a replace, so
    a run that dies mid-write leaves the earlier file whole."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        jsonutil.replace_with_retry(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _keep_untraced_record(folder: Path) -> None:
    """A record about to be replaced that no attempt folder holds (one
    written before the trace, by a run whose trace could not start, or one
    whose copy into its attempt folder failed) is kept as
    `apply_trace/earlier-<k>.md`."""
    path = folder / RECORD_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    traced = next((line for line in text.splitlines() if line.startswith(_TRACE_LINE)), "")
    link = _TRACE_LINK.search(traced)
    if link and (folder / link.group(1).rstrip("/") / RECORD_NAME).is_file():
        return      # its attempt folder has its copy
    keep = folder / apply_trace.TRACE_DIR
    try:
        keep.mkdir(parents=True, exist_ok=True)
        k = 1 + max((int(m.group(1)) for p in keep.glob("earlier-*.md")
                     if (m := re.match(r"earlier-(\d+)\.md$", p.name))), default=0)
        _write_text_atomic(keep / f"earlier-{k}.md", text)
    except OSError as e:
        log.warning("the earlier record in %s was not kept: %s", folder, type(e).__name__)


def _earlier_attempts(folder: Path, current: str) -> list[str]:
    """One line per earlier record: those kept from before the trace, then
    each attempt folder's (`current`, this attempt's folder, left out)."""
    rows = []
    keep = folder / apply_trace.TRACE_DIR
    earlier = sorted(keep.glob("earlier-*.md"),
                     key=lambda p: int(re.sub(r"\D", "", p.stem) or 0)) if keep.is_dir() else []
    for path in earlier:
        status, reason, written = _record_head(path)
        rows.append(f"- Before the trace: {status}: {reason} ({written}); "
                    f"[record]({apply_trace.TRACE_DIR}/{path.name})")
    for n, path in apply_trace.attempt_dirs(folder):
        rel = f"{apply_trace.TRACE_DIR}/{path.name}"
        if rel == current:
            continue
        record = path / RECORD_NAME
        if record.exists():
            status, reason, written = _record_head(record)
            rows.append(f"- Attempt {n}: {status}: {reason} ({written}); "
                        f"[record]({rel}/{RECORD_NAME})")
        else:
            rows.append(f"- Attempt {n}: no record; [trace]({rel}/)")
    return rows


def write_record(folder: Path, entry: dict, outcome_status: str, reason: str,
                 pages: list[dict], jev_usage: dict, page_text: str, *,
                 missing: list[dict] | None = None, trace_dir: str = "",
                 attempt: int = 0) -> Path:
    """`apply_record.md` in the job folder. A value from a password field
    (`_is_password`) or a row marked `hidden` (the emailed code) is written
    as `<hidden>`.

    `trace_dir` is this attempt's trace folder relative to `folder`
    (`apply_trace/attempt-<n>`): the record links it and each page's trace
    file, and a copy of the record goes into it. The earlier attempts'
    records stay in their own folders and are listed at the end; a record
    being replaced that no attempt folder holds is kept first
    (`_keep_untraced_record`), so no attempt's record is lost."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    _keep_untraced_record(folder)
    lines = [f"# Apply record: {entry.get('title', '')} at {entry.get('company', '')}", "",
             f"- Job id: {entry.get('job_posting_id', '')}",
             f"- Company: {entry.get('company', '')}",
             f"- Title: {entry.get('title', '')}",
             f"- Status: {outcome_status}",
             f"- Reason: {reason}",
             f"- Written: {datetime.now().isoformat(timespec='seconds')}",
             f"- Apply URL: {entry.get('apply_url', '')}"]
    if attempt:
        lines.append(f"- Attempt: {int(attempt)}")
    if trace_dir:
        lines.append(f"{_TRACE_LINE}[{trace_dir}]({trace_dir}/) (per page: page-<n>.json and "
                     f"page-<n>.jpg; end.jpg, run.json, {apply_trace.LOG_NAME})")
    lines.append("")
    for i, p in enumerate(pages, 1):
        # the address without its query: a method=get form carries the
        # answers there
        lines.append(f"## Page {i}: {apply_trace.bare_url(p.get('url', ''))}")
        lines.append(f"- State: {p.get('state', '')} ({float(p.get('confidence', 0.0)):.2f})")
        if trace_dir:
            lines.append(f"{_TRACE_LINE}[page-{i}.json]({trace_dir}/page-{i}.json), "
                         f"[page-{i}.jpg]({trace_dir}/page-{i}.jpg)")
        # a value taken out again (an optional answer that failed its check,
        # a box the page wrote and the run emptied) is no value the employer
        # received: listed under Cleared and dropped from Filled
        cleared = [str(c) for c in p.get("cleared") or []]
        filled = [r for r in p.get("filled", [])
                  if not r.get("upload") and str(r.get("label", "")) not in cleared]
        uploads = [r for r in p.get("filled", []) if r.get("upload")]
        if filled:
            lines.append("- Filled:")
            for r in filled:
                value = HIDDEN if r.get("hidden") or _is_password(r) else str(r.get("value", ""))
                mark = " (generated)" if r.get("generated") else ""
                lines.append(f"  - {r.get('label', '')}: {value}{mark}")
        if cleared:
            lines.append("- Cleared:")
            lines.extend(f"  - {label}" for label in cleared)
        if uploads:
            lines.append("- Uploads:")
            for r in uploads:
                lines.append(f"  - {r.get('label', '')}: {r.get('value', '')}")
        if p.get("fill_outcomes"):
            # How each field was acted on, and the error's type when
            # the act failed (never a value)
            lines.append("- Fill outcomes:")
            for o in p["fill_outcomes"]:
                what = (f"failed ({o.get('error')})" if o.get("error")
                        else str(o.get("how") or o.get("action") or ""))
                lines.append(f"  - {o.get('label', '')}: {what}")
        if p.get("verification"):
            lines.append("- Verification:")
            for v in p["verification"]:
                mark = "ok" if v.get("ok") else "FAILED"
                lines.append(f"  - {v.get('label', '')}: {mark} (p_correct "
                             f"{float(v.get('p_correct', 0.0)):.2f}, p_placeholder "
                             f"{float(v.get('p_placeholder', 0.0)):.2f})")
        if p.get("generated"):
            lines.append("- Generated answers:")
            for g in p["generated"]:
                state = "generated" if g.get("ok") else "rejected"
                lines.append(f"  - {g.get('label', '')}: {state} ({g.get('note', '')})")
        if p.get("clicked"):
            lines.append("- Clicked: " + "; ".join(str(c) for c in p["clicked"]))
        if p.get("flags"):
            lines.append("- Flags: " + ", ".join(f"{k} {float(v):.2f}"
                                                   for k, v in p["flags"].items()))
        lines.append("")
    lines.append("## Missing questions")
    rows = missing if missing is not None else entry.get("missing_answers") or []
    if rows:
        for m in rows:
            ctx = str(m.get("context", "") or "")
            lines.append(f"- {m.get('question', '')}" + (f" ({ctx})" if ctx else ""))
    else:
        lines.append("- none")
    lines += ["", "## Jev",
              f"- Requests: {int(jev_usage.get('requests', 0))}",
              f"- Input tokens: {int(jev_usage.get('input_tokens', 0))}",
              f"- Cost: ${float(jev_usage.get('usd', 0.0)):.4f}",
              f"- Model: {jev.MODEL}",
              f"- Generated answers used: {generated_count(pages)}", ""]
    if page_text:
        lines += ["## Final page text", "", "```", page_text.strip(), "```", ""]
    earlier = _earlier_attempts(folder, trace_dir)
    if earlier:
        lines += ["## Earlier attempts", *earlier, ""]
    text = "\n".join(lines)
    path = folder / RECORD_NAME
    _write_text_atomic(path, text)
    if trace_dir:
        # the copy sits in the attempt folder: its links point from there
        copy = _TRACE_LINK.sub(lambda m: f"]({_link_from(m.group(1), trace_dir)})", text)
        try:
            (folder / trace_dir / RECORD_NAME).write_text(copy, encoding="utf-8")
        except OSError as e:
            log.warning("the record copy in %s was not written: %s", trace_dir,
                        type(e).__name__)
    return path


# --- the summary and the CLI ---------------------------------------------------------------

def summary_line(outcomes: list[Outcome]) -> str:
    counts = {s: 0 for s in ("submitted", "ready_to_submit", "needs_human", "failed")}
    requests = tokens = 0
    usd = 0.0
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
        requests += int(o.jev_usage.get("requests", 0))
        tokens += int(o.jev_usage.get("input_tokens", 0))
        usd += float(o.jev_usage.get("usd", 0.0))
    # a job the judge's outage handed back to the queue
    back = f", re-queued {counts['queued']}" if counts.get("queued") else ""
    return (f"drained {len(outcomes)}: submitted {counts['submitted']}, "
            f"ready_to_submit {counts['ready_to_submit']}, needs_human {counts['needs_human']}, "
            f"failed {counts['failed']}{back}; Jev {requests} requests, {tokens} tokens, "
            f"${usd:.4f}")


DRAIN_REPORT_PREFIX = "apply_drain-"    # apply_drain-<YYYYMMDD-HHMMSS>.md, beside the job folders
REASON_CELL_MAX = 200                   # a reason's characters in the drain's table


def _cell(text: Any, limit: int) -> str:
    """One table cell: one line, at most `limit` characters, its bars escaped."""
    return _cap(str(text or ""), limit).replace("|", "\\|")


def drain_table(outcomes: list[Outcome], base: Path | None = None) -> str:
    """One markdown table over the jobs a drain worked: the job, its end, its
    pages, the reason and its trace folder. With `base` the trace is a link
    relative to it (the report file's folder); without, the folder's path."""
    rows = ["| # | job | end | pages | reason | trace |",
            "|--:|-----|-----|------:|--------|-------|"]
    for i, o in enumerate(outcomes, 1):
        trace = "-"
        if o.trace_dir:
            where = Path(o.trace_dir)
            trace = where.as_posix()
            if base is not None:
                try:
                    rel = Path(os.path.relpath(where, base)).as_posix()
                except ValueError:      # another drive: the full path
                    rel = trace
                trace = f"[{where.parent.parent.name}/{where.name}](<{rel}/>)"
        rows.append(f"| {i} | {_cell(o.job_id, 40)} | {o.status} | {o.pages} | "
                    f"{_cell(o.reason, REASON_CELL_MAX)} | {trace} |")
    return "\n".join(rows)


def drain_report_dir(outcomes: list[Outcome], queue_path: Path | None = None) -> Path:
    """Where a drain's report goes: beside the job folders whose traces it
    links (`<folder>/apply_trace/attempt-<n>`), when they share one parent;
    else the queue file's folder."""
    parents = {Path(o.trace_dir).parent.parent.parent for o in outcomes if o.trace_dir}
    if len(parents) == 1:
        return parents.pop()
    return apply_queue.queue_path(queue_path).parent


def write_drain_report(outcomes: list[Outcome], queue_path: Path | None = None, *,
                       now: datetime | None = None, header: str = "") -> Path:
    """`apply_drain-<stamp>.md` in `drain_report_dir`: `header` (the
    answers not confirmed, when there are any), the drain's summary line and
    its table, the traces linked relative to the file."""
    where = drain_report_dir(outcomes, queue_path)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    path, n = where / f"{DRAIN_REPORT_PREFIX}{stamp}.md", 2
    while path.exists():
        path, n = where / f"{DRAIN_REPORT_PREFIX}{stamp}-{n}.md", n + 1
    where.mkdir(parents=True, exist_ok=True)
    head = f"{header}\n\n" if header else ""
    path.write_text(f"# Apply drain {stamp}\n\n{head}{summary_line(outcomes)}\n\n"
                    f"{drain_table(outcomes, base=where)}\n", encoding="utf-8")
    return path
