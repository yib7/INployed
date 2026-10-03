"""Orchestrate the tailor pipeline and expose a single entry point + CLI.

    tailor(job, cover_letter=False, ats_report=True, prep_sheet=False,
           tone="professional", on_status=None) -> Path (output directory)

job is a dict with: company_name, job_title, job_summary, url (job_posting_id optional).

CLI:  python -m resume_tailor.run --job-id <id> [--cover-letter]
        [--no-ats-report] [--prep] [--tone <tone>] [--csv <path>]
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from . import (apply_data, assets, ats, compose, config, coverletter, jev_assist, llm,
               measure, output, research, sweep, verify)
from .compile import enforce_one_page, pdflatex_available

StatusFn = Optional[Callable[[str], None]]
WarnFn = Optional[Callable[[str], None]]

# The name of the durable per-run record tailor() leaves in the output folder.
REPORT_NAME = "tailor_report.txt"

# The warning taxonomy. Every collected warning carries its kind as a prefix, so a
# line stays self-describing once it is out of the report and in a Qt dialog.
#   GROUNDING — the grounding gate reverted or dropped a bullet (with the tokens).
#   PAGE_LIMIT — the PDF shipped over config.PAGE_LIMIT pages.
#   ADVISORY  — one of the optional artifacts failed; the résumé itself is fine.
KIND_GROUNDING = "grounding"
KIND_PAGE_LIMIT = "page limit"
KIND_ADVISORY = "advisory"
# KIND_MODEL: the installed claude CLI refused the chosen Claude model, so the run
# used claude_cli's fallback for it. A warning the first time a process sees
# the swap, a note on every later run (_report_model_swaps).
KIND_MODEL = "model"

# The NOTE taxonomy — same line shape ("<kind>: <message>"), one severity down.
# A note records something the run could not fully deliver but that leaves a
# correct, shippable résumé, so it is written to the report and NOT streamed to
# `on_warning`. That callback is what the dashboard's batch summary calls a
# degraded run; putting cosmetic findings on it would make "finished with
# warnings" mean nothing (see DECISIONS).
#   UNDERFULL — a bullet the underfull fill rewrote is still underfull after the
#               re-trim: the fill was paid for and the trim took it back.
#   AI WRITING — what the item-level AI-writing sweep did and did not do: how many
#               bullets it rewrote, which rewrites it refused and why, and the P2
#               findings it left alone ON PURPOSE. Every one of those leaves text that
#               is grounded, clean and fitting, because a refused rewrite keeps the
#               original, so none of them is a degradation. A sweep call that RAISED is
#               the one thing here that is, and it goes out as a warning instead.
#   GROUNDING — a first-draft drop the re-ask then recovered, where the outcome
#               decides the severity; the rejected text behind every gate finding,
#               reverted or dropped; and an underfull fill the pass refused outright.
KIND_UNDERFULL = "underfull"
KIND_AIWRITING = "ai writing"


def _noop(_msg: str) -> None:
    pass


@dataclass
class RunLog:
    """What actually happened during one tailor run.

    `log` (the status callback) goes to a transient Qt status line and is gone the
    moment the next message replaces it, which on its own lets a half-worked run
    read as a clean one. This is the durable half: the stages that ran, every warning with
    its kind, and the final page count. It is written to `tailor_report.txt` in the
    output folder and, when the caller passes `on_warning`, streamed out live.

    `notes` is the same record one severity down: written to the report, never
    streamed, so it cannot mark a run degraded.

    `jev` holds one usage line per Jev step the run took (`jev_assist.usage_line`):
    its requests, their estimated tokens and cost, and why it fell back to the LLM
    path when it did. It stays empty when Jev is off for the tailor.
    """
    on_warning: WarnFn = None
    stages: List[str] = field(default_factory=list)
    entries: List[Tuple[str, str]] = field(default_factory=list)
    notes: List[Tuple[str, str]] = field(default_factory=list)
    jev: List[str] = field(default_factory=list)
    pages: int = 0

    def stage(self, name: str) -> None:
        self.stages.append(name)

    def jev_step(self, step: str) -> None:
        """Record the usage line of Jev step `step` (a `jev_assist.STEP_*`)."""
        self.jev.append(jev_assist.usage_line(step))

    def warn(self, kind: str, message: str) -> None:
        """Record a warning and hand it to the caller's collector.

        The collector is somebody else's code, so it is fenced: a broken callback
        degrades the report, it never sinks a run that already produced a PDF."""
        line = f"{kind}: {message}"
        self.entries.append((kind, line))
        if self.on_warning is not None:
            try:
                self.on_warning(line)
            except Exception:  # noqa: BLE001 - a bad collector must not sink the run
                pass

    def advisory(self, message: str) -> None:
        self.warn(KIND_ADVISORY, message)

    def note(self, kind: str, message: str) -> None:
        """Record something the report should carry but that must NOT make the run
        read as degraded. Deliberately does not touch `on_warning`: that channel is
        the dashboard's degraded-run signal, and a cosmetic finding on it would cry
        wolf against a résumé that is correct and shippable."""
        self.notes.append((kind, f"{kind}: {message}"))

    @property
    def warnings(self) -> List[str]:
        return [line for _kind, line in self.entries]

    @property
    def note_lines(self) -> List[str]:
        return [line for _kind, line in self.notes]


# Swaps this process has already put on `on_warning`. Parallel batch jobs run
# tailor() on worker threads, hence the lock.
_SWAP_NOTICE_LOCK = threading.Lock()
_SWAPS_ANNOUNCED: Set[str] = set()


def reset_model_swap_notices() -> None:
    """Forget which swaps were announced (for tests)."""
    with _SWAP_NOTICE_LOCK:
        _SWAPS_ANNOUNCED.clear()


def _report_model_swaps(report: RunLog) -> None:
    """Record each Claude model swap the transport made this process.

    The dashboard runs under pythonw, where claude_cli's stderr warning goes
    nowhere, so a user on an old CLI would get every résumé from the fallback
    model and never hear of it. The first run that sees a swap warns (the
    dashboard's batch summary shows it); every later run carries it as a note,
    so one old CLI does not mark every job in the process degraded. A lookup
    that fails records nothing: the résumé is already written."""
    try:
        swaps = llm.claude_model_swaps()
    except Exception:  # noqa: BLE001
        return
    for model, ran in sorted(swaps.items()):
        message = (f"the claude CLI is too old for {model}, so runs use {ran}. "
                   f"Run `claude update` and restart the dashboard to use {model}.")
        with _SWAP_NOTICE_LOCK:
            first = model not in _SWAPS_ANNOUNCED
            _SWAPS_ANNOUNCED.add(model)
        if first:
            report.warn(KIND_MODEL, message)
        else:
            report.note(KIND_MODEL, message)


def _report_text(rep: RunLog, *, job: Dict[str, str], company: str, job_title: str,
                 out_dir: Path) -> str:
    """Render `tailor_report.txt`: the run's stages, its warnings, its notes, its
    page count. Notes sit directly under warnings, one severity down: same
    `<kind>: <message>` line shape, but a note never made the run degraded. A run
    that used Jev ends with a `jev` section, one usage line per step."""
    def section(title: str, body: List[str], empty: str) -> List[str]:
        return ["", title, "-" * len(title)] + ([f"  {b}" for b in body] or [f"  {empty}"])

    lines = [
        "INployed tailor report",
        "======================",
        f"job     : {job_title} @ {company}",
        f"job id  : {_field(job, 'job_posting_id') or '(none)'}",
        f"run at  : {datetime.now().isoformat(timespec='seconds')}",
        f"output  : {out_dir}",
        f"pages   : {rep.pages or 'not measured'} (limit {config.PAGE_LIMIT})",
    ]
    lines += section(f"stages ({len(rep.stages)})", rep.stages, "none")
    lines += section(f"warnings ({len(rep.entries)})", rep.warnings, "none")
    lines += section(f"notes ({len(rep.notes)})", rep.note_lines, "none")
    if rep.jev:
        lines += section(f"jev ({len(rep.jev)})", rep.jev, "none")
    lines.append("")
    return "\n".join(lines)


def _write_report(rep: RunLog, out_dir: Path, *, job: Dict[str, str], company: str,
                  job_title: str, log: Callable[[str], None]) -> None:
    """Write the run report into the output folder. Never sinks the run — but a
    failure to write the record of failures is itself said out loud."""
    try:
        (Path(out_dir) / REPORT_NAME).write_text(
            _report_text(rep, job=job, company=company, job_title=job_title,
                         out_dir=Path(out_dir)),
            encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - the report is a record, never the run
        log(f"{REPORT_NAME} not written ({exc})")


def _cover_text(body: str, company: str, log: Callable[[str], None],
                warn: WarnFn = None) -> str:
    """The copy-pasteable plain-text letter, bound for apply.md's `## Cover letter`
    section (the folder ships the letter's .tex, and LaTeX is useless in a paste
    box). Advisory: a failure never sinks the (already-written) PDF, it just
    logs, warns and the sheet carries no letter."""
    try:
        return coverletter.cover_letter_text(body, company)
    except Exception as exc:  # noqa: BLE001 - the text is a convenience, never fatal
        log(f"cover letter text skipped ({exc})")
        if warn is not None:
            warn(f"cover letter text skipped ({exc})")
        return ""


_LETTER_SECTIONS = (("experience", "org"), ("projects", "name"), ("leadership", "org"))


def _letter_atom_ids(sel: Dict[str, Any], bullets: Dict[str, str],
                     master: Dict[str, Any]) -> set:
    """The master atom ids behind the bullets that made the page. They MARK the
    entries the cover letter's BACKGROUND block lists: assets.flatten_entries
    lists every entry that owns one of them, in full, so the letter holds all
    the candidate's notes on each employer or project that printed.

    A tailored bullet's group key is its atom ids joined with '+', so the ids are
    read straight off `bullets` (the post-enforcement dict, which is the set that
    printed). A verbatim block carries the user's exact text under synthetic keys
    and no atom ids at all, so its master entry's own atoms mark it instead."""
    ids = {aid for gk in bullets if not compose.is_verbatim_gkey(gk)
           for aid in gk.split("+") if aid}
    verbatim_names = {
        e.get("name") for sec, _key in _LETTER_SECTIONS for e in (sel.get(sec) or [])
        if any(compose.is_verbatim_gkey(gid) for g in (e.get("groups") or []) for gid in g)
    }
    for sec, key in _LETTER_SECTIONS:
        for entry in master.get(sec) or []:
            if isinstance(entry, dict) and entry.get(key) in verbatim_names:
                ids |= {a.get("id") for a in (entry.get("achievements") or [])
                        if isinstance(a, dict) and a.get("id")}
    return ids


def _letter_inputs(sel: Optional[Dict[str, Any]], bullets: Dict[str, str],
                   log: Callable[[str], None], warn: WarnFn = None) -> Tuple[str, str]:
    """(background, seed) for coverletter.generate_body.

    `sel` None means the standalone letter (no selection to filter by), which
    gets every entry; the tailor run gets the entries that printed, each in
    full. Advisory: a master the flattening cannot read leaves the letter to run
    from the bullets alone, and says so."""
    try:
        master = assets.load_master()
        ids = None if sel is None else _letter_atom_ids(sel, bullets, master)
        background = assets.flatten_entries(master, entry_atoms=ids)
        if background.endswith(assets.TRUNCATED_MARKER):
            # Say so: a letter written from part of the record is a quieter
            # failure than no letter, and the fix (a shorter master, fewer
            # printed entries) is the user's to make. The trim takes atoms off
            # the longest entries and keeps every header, so the count is atoms.
            full = assets.flatten_entries(master, entry_atoms=ids, cap=10 ** 9)
            total = _atom_lines(full)
            dropped = total - _atom_lines(background)
            msg = (f"cover letter background truncated at "
                   f"{assets.LETTER_BACKGROUND_CAP:,} characters "
                   f"({dropped} of {total} achievement notes left out, "
                   f"every entry kept)")
            log(msg)
            if warn is not None:
                warn(msg)
        return background, assets.letter_seed()
    except Exception as exc:  # noqa: BLE001 - the background is an enrichment; any failure here is caught and logged
        log(f"cover letter background unavailable ({exc})")
        if warn is not None:
            warn(f"cover letter background unavailable ({exc})")
        return "", ""


def _atom_lines(background: str) -> int:
    """How many achievement notes a flattened background lists (its indented
    `    - atom` lines)."""
    return sum(1 for line in background.splitlines() if line.startswith("    - "))


def _field(job: Dict[str, str], key: str) -> str:
    """String getter that coerces NaN floats / None (pandas rows) to ''."""
    v = job.get(key)
    if not isinstance(v, str):
        return ""
    s = v.strip()
    return "" if s.lower() in ("nan", "none") else s


def _line_field(job: Dict[str, str], key: str) -> str:
    """_field for the two scraped values that head every prompt as ONE line.

    `TARGET JOB: {job_title}` opens each tailor prompt, and the company name
    heads the cover letter's; neither is fenced the way the description is
    (fence_jd), because a title is a line, not a document. A scraped value
    carrying a newline therefore forged a fresh prompt line of its own, the
    same route apply_data._one_line closes for apply.md. Whitespace runs
    collapse to one space, so the field stays the one line the prompt reads
    it as. Not truncated: the same two values name the output folder, and
    output.sanitize collapses whitespace exactly this way, so the folder a
    tailor run creates is the one apply.find_folder and the queue's
    reconcile look up by the RAW company and title. A cap here would make
    the two disagree on a long title.
    """
    return re.sub(r"\s+", " ", _field(job, key)).strip()


def _to_plain(text: str) -> str:
    """HTML -> markdown-ish plain text (descriptions are often raw HTML)."""
    if "<" in text and ">" in text:
        try:
            from markdownify import markdownify
            return markdownify(text, heading_style="ATX").strip()
        except ImportError:
            return re.sub(r"<[^>]+>", " ", text).strip()
    return text


def _job_description_text(job: Dict[str, str]) -> str:
    """The richest available JD text: markdown first, full description next,
    summary last.

    job_description_md (score_jobs.py's markdownify output) is returned
    UNCHANGED: the tailor already reasons over markdown directly, and
    flattening it here would throw away the structure the prompt uses. It
    clears the same 40-character floor as the other three columns; this
    function measures that floor against the raw markdown itself, while
    jobsdata.job_detail_fields measures the same floor against md_to_text's
    converted output. LinkedIn's job_summary is often truncated or empty, so
    tailoring against it alone wastes most of the JD signal (and hard-fails
    when it's blank).
    """
    md = _field(job, "job_description_md")
    if len(md) >= 40:
        return md
    for key in ("job_description_formatted", "job_description", "job_summary"):
        text = _to_plain(_field(job, key))
        if len(text) >= 40:
            return text
    return ""


def _resolve_bullets(jd: str, job_title: str, sel: dict, log: Callable[[str], None],
                     briefs: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Rephrase selected groups, block-grouped with their cohesion briefs. Skip the
    anti-inflation gate to reduce LLM calls."""
    bullets = compose.rephrase(jd, job_title, sel, briefs=briefs)
    log(f"rephrased {len(bullets)} bullet(s).")
    return bullets


def _resolve_best_of(jd: str, job_title: str, sel: dict, log: Callable[[str], None], *,
                     briefs: Optional[Dict[str, str]], judge: Any) -> Dict[str, str]:
    """Best of three (Settings: "Best of 3 bullet drafts", with Jev on): one rephrase
    call for three drafts of every bullet, and one draft kept per bullet.

    A draft is a candidate when it passes the grounding gate's check
    (`verify.group_unseen`) and the faithfulness check (`jev_assist.faithfulness`,
    every draft in one check); a faithfulness check that cannot run leaves the gate
    alone, as it does for every bullet. Jev picks among a bullet's candidates
    (`jev_assist.best_variant`); a bullet with one candidate keeps it, and a pick
    that fails keeps the first. A bullet with no candidate keeps its first draft the
    grounding gate passes, else its first draft, the text the single rephrase call
    gives; the prologue gate and the faithfulness check then treat it as any bullet.
    The caller skips this with the breaker open, since nothing could judge the
    drafts. A drafts answer with no bullet in it falls back to the single rephrase
    call. Every text kept
    is one the rephrase wrote."""
    drafts = compose.rephrase_drafts(jd, job_title, sel, briefs=briefs)
    if not drafts:
        log("the drafts call returned no bullet; rephrasing once…")
        return _resolve_bullets(jd, job_title, sel, log, briefs=briefs)
    gm = compose.group_map(sel)
    grounded = {gk: [t for t in texts
                     if not verify.group_unseen(sel, gm.get(gk) or gk.split("+"), t)]
                for gk, texts in drafts.items()}
    entries: List[Dict[str, Any]] = []
    for name, gkeys in compose._blocks_in_order(sel):
        items = [{"gkey": f"{gk}#{n}", "text": t,
                  "atoms": [compose._atom_payload(a) for a in gm[gk]]}
                 for gk in gkeys for n, t in enumerate(grounded.get(gk) or [])]
        if items:
            entries.append({"entry": name, "bullets": items})
    verdicts = jev_assist.faithfulness(entries, judge=judge) if entries else None
    passing = {gk: [t for n, t in enumerate(texts)
                    if verdicts is None or verdicts.get(f"{gk}#{n}") == ""]
               for gk, texts in grounded.items()}
    groups = [{"gkey": gk, "drafts": texts} for gk, texts in passing.items()
              if len(texts) >= 2]
    picks = jev_assist.best_variant(jd, job_title, groups, judge=judge) or {}
    bullets: Dict[str, str] = {}
    for gk, texts in drafts.items():
        ok = passing.get(gk) or []
        n = picks.get(gk, (1, 0.0))[0]
        bullets[gk] = (ok[n - 1] if 1 <= n <= len(ok)
                       else (ok or grounded.get(gk) or texts)[0])
    total = sum(len(texts) for texts in drafts.values())
    log(f"rephrased {len(bullets)} bullet(s) from {total} draft(s); Jev picked among the "
        f"passing drafts of {len(groups)}")
    return bullets


# Words that must never be the LAST word of a trimmed bullet — they leave the
# sentence dangling (e.g. "...utilizing Gemini Flash to.").
_TRAILING_STOPWORDS = frozenset((
    "a", "an", "the", "to", "of", "for", "and", "or", "but", "with", "by",
    "in", "on", "at", "as", "from", "into", "that", "which", "while", "via",
    "using", "utilizing", "like", "such", "including", "enabling", "is", "was",
    "were", "are", "be", "been", "their", "its", "this", "these", "those",
    "where", "when", "than", "then", "so", "up", "out", "over", "per", "about",
))
# Connectives that introduce a clause; if one shows up near the end of a trimmed
# bullet with only a fragment after it, drop the whole dangling clause.
_CLAUSE_INTROS = frozenset((
    "while", "when", "where", "as", "since", "although", "after", "before",
    "by", "via", "using", "utilizing", "including", "enabling", "to", "that",
    "which", "and", "or", "with", "for", "of", "from",
))

# Plain prepositions get a NARROWER rule than _CLAUSE_INTROS: a cut landing inside
# a prepositional phrase strands a modifier the stopword check can't see
# ('...linear regressions on categorical.'), but a preposition can also head a
# tail that reads complete ('for over 40,000+ users', 'among the top 95%',
# 'on AWS') which must survive. _prep_fragment tells the two apart.
_PREP_INTROS = frozenset((
    "on", "in", "at", "into", "onto", "upon", "over", "under", "across",
    "within", "between", "during", "against", "through", "toward", "towards",
    "among", "without", "per", "about", "like", "than", "versus",
))


def _prep_fragment(words: list) -> int:
    """How many trailing words form an ORPHANED prepositional fragment:
    '<prep> <word>' or '<prep> <article> <word>' at the very end, where the final
    word is a bare lowercase modifier (no digit, no capital). A digit-bearing or
    capitalized head ('among the top 95%', 'over 40,000+ users', 'on AWS') reads
    complete and stays. Returns 0 when the tail is fine."""
    if len(words) < 2:
        return 0
    last = words[-1].strip(",;:")
    if not last or not last.islower() or any(ch.isdigit() for ch in last):
        return 0
    if words[-2].lower().strip(",;:") in _PREP_INTROS:
        return 2
    if (len(words) >= 3 and words[-2].lower() in ("a", "an", "the")
            and words[-3].lower().strip(",;:") in _PREP_INTROS):
        return 3
    return 0


# A trailing BARE number/range with no unit ('took 1', 'took 1-2') — what a chopped
# quantity leaves behind. '95%', '40,000+', '7.4x' carry a unit and are NOT bare.
_BARE_NUM = re.compile(r"\d+([.,]\d+)*([–\-]\d+([.,]\d+)*)?$")


def _bare_num_tail(words: list) -> bool:
    return bool(words and _BARE_NUM.fullmatch(words[-1].lower().strip(",;:")))


def _strip_dangling(text: str) -> str:
    """Drop trailing words/clauses that leave a sentence grammatically incomplete:
    first any trailing pure stopword, then an orphaned prepositional fragment
    ('...regressions on categorical' -> '...regressions'; see _prep_fragment),
    then a trailing fragment introduced by a
    clause connective (e.g. '...periods while maintaining' -> '...periods'), and
    finally a dangling BARE NUMBER left when a quantity was chopped mid-phrase
    (e.g. the model spelled '1-2 weeks' as '1 to 2 weeks' and the trim cut it to
    '...that previously took 1' -> drop the whole '...took 1' clause). The last step
    repeats ONLY while a bare number still trails, so it stops at the first complete
    clause and never eats into well-formed text."""
    words = text.split()
    while len(words) > 3 and words[-1].lower().strip(",;:") in _TRAILING_STOPWORDS:
        words.pop()
    # An orphaned '<prep> [article] <modifier>' tail ('...regressions on
    # categorical') reads broken: drop the whole fragment.
    k = _prep_fragment(words)
    if k and len(words) - k >= 4:
        words = words[:-k]
    for k in range(1, min(5, len(words))):
        if words[-k].lower().strip(",;:") in _CLAUSE_INTROS:
            candidate = words[:-k]
            if len(candidate) >= 4:
                words = candidate
            break
    while len(words) > 4 and _bare_num_tail(words):
        n = len(words)
        for k in range(1, min(7, len(words))):
            if words[-k].lower().strip(",;:") in _CLAUSE_INTROS:
                candidate = words[:-k]
                if len(candidate) >= 4:
                    words = candidate
                break
        if len(words) == n:          # no clause intro applied -> shed the bare number itself
            words.pop()
    return " ".join(words).rstrip(",;: ")


# How full a clause cut has to leave the line before `_word_trim` will take one.
#
# This is a PERMISSION threshold, not a target: the loop below takes the RIGHTMOST
# ';'/',' in the over-budget prefix, and this constant only decides whether that
# separator is high enough to use. So the fraction is a floor on how much text
# survives, and every separator above it is equally acceptable — there is no
# preference for the one nearest the budget, because there is only ever one candidate
# (the rightmost).
#
# It was 0.6, which reads like "keep the line reasonably full" but actually means
# "accept a cut that throws away up to 40% of the bullet". When a bullet's only
# separator sits low — say at 62% of budget — the clause cut fired and the bullet lost
# more than a third of its text in one step, with nothing downstream to grow it back:
# the underfull fill runs BEFORE the trim in `_BULLET_PASSES`, never after, and
# `compile.enforce_one_page` only drops whole bullets, it never re-lengthens one.
#
# At 0.85 a clause cut is taken only when it barely shortens (<= 15% lost) — which is
# the case it was actually for, ending on a real clause boundary instead of mid-phrase.
# Below that we fall through to the word cut, which sheds one or two words and is
# grammar-protected by `_strip_dangling`.
_CLAUSE_CUT_FLOOR = 0.85


def _word_trim(text: str, max_visible: int) -> str:
    """Trim to <= max_visible rendered glyphs, ending on a clean grammatical
    boundary (clean_bullet re-adds the trailing period). Numbers/impact are
    front-loaded, so trimming the tail preserves the metrics. Prefer cutting at a
    clause boundary (comma/semicolon) that keeps the line nearly full
    (_CLAUSE_CUT_FLOOR); else word-trim and strip any dangling connective. The
    deterministic last resort when the model overshoots its length target."""
    text = text.rstrip().rstrip(".")
    budget = max_visible - 1  # leave room for the period clean_bullet appends
    if len(text) <= budget:
        return text
    cut = text[:budget]
    # A clause boundary makes the cleanest cut, if it doesn't gut the line.
    floor = int(budget * _CLAUSE_CUT_FLOOR)
    for sep in (";", ","):
        idx = cut.rfind(sep)
        while idx >= floor:
            # Skip a thousands-separator comma (digit,digit) — not a clause break.
            if (sep == "," and 0 < idx < len(cut) - 1
                    and cut[idx - 1].isdigit() and cut[idx + 1].isdigit()):
                idx = cut.rfind(sep, 0, idx)
                continue
            return cut[:idx].rstrip(",;: ")
    sp = cut.rfind(" ")
    trimmed = (cut[:sp] if sp > 0 else cut).rstrip(",; ")
    return _strip_dangling(trimmed) or trimmed


def _fit_to_lines(text: str, target_lines: int) -> str:
    """Width-aware trim: shorten a bullet until it renders within `target_lines` printed
    lines, measured by real glyph widths (measure.line_count) — not a flat char count, so
    a wide-word bullet that the char cap missed ('...cross-encoder reranking...') is caught.
    Numbers are front-loaded, so the overflow tail trims safely; under-length is left as-is
    (never padded). The clean cut reuses _word_trim's clause/word-boundary + dangling
    handling, so we never end mid-clause on a connective."""
    text = text.strip()
    if measure.line_count(text) <= target_lines:
        return text
    # Longest character prefix that still renders within the target (line_count is
    # monotonic in length), then clean-cut at a word/clause boundary at or below it.
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure.line_count(text[:mid]) <= target_lines:
            lo = mid
        else:
            hi = mid - 1
    return _word_trim(text, lo + 1)


def _trim_to_caps(sel: Dict[str, str], bullets: Dict[str, str]) -> None:
    """Deterministically trim each bullet to its per-bullet printed-line target
    (config.block_targets / project_targets, else config.PROJECT_BULLET_LINES), measured
    by real rendered width (measure.line_count). Over-length is trimmed at a word boundary
    (numbers are front-loaded, so the tail trims safely); under-length is left as-is — we
    never pad, which would mean inventing facts."""
    targets = compose.bullet_line_targets(sel)
    for gk, text in list(bullets.items()):
        if compose.is_verbatim_gkey(gk):
            continue  # the user's exact bullets are rendered as typed, never trimmed
        target_lines = targets.get(gk, config.PROJECT_BULLET_LINES)
        bullets[gk] = _fit_to_lines(text, target_lines)


# ── The bullet passes ────────────────────────────────────────────────────────
# Every stage that mutates the bullets after the first grounding gate obeys the same
# discipline: snapshot -> mutate -> (optionally) re-trim -> re-verify against that
# snapshot, so a pass that pushes a bullet off its own atoms reverts to the last
# grounded text instead of printing. Copied out by hand at each site, that
# bracketing is easy to forget, and forgetting one is SILENT: the gate is a no-op on
# grounded text, so a missing call site changes neither the compile nor the rendered
# .tex. So it is structural: a pass declares what bracketing it needs and the driver
# does the bookkeeping.


@dataclass
class PassCtx:
    """What a bullet pass may read and mutate.

    `bullets` is mutated IN PLACE; a pass must never rebind it, because the driver
    snapshots and re-verifies that same dict.

    `report` is optional so a caller can drive the passes without one; when it is
    present the driver records each stage and `_gate` routes the gate's findings
    into it.

    `judge` is the run's Jev judge (`jev_assist.default_judge()`), None when Jev is
    off for the tailor. With it, the faithfulness check (`_check_faithfulness`)
    runs after the prologue gate and after every verified pass.
    """
    jd: str
    job_title: str
    sel: Dict[str, Any]
    bullets: Dict[str, str]
    verbatim: Dict[str, str]
    reserved: FrozenSet[str]
    log: Callable[[str], None]
    report: Optional[RunLog] = None
    judge: Any = None


def _always() -> bool:
    return True


@dataclass(frozen=True)
class Pass:
    """One bullet-mutating stage, plus how the driver has to bracket it.

    name    : human-readable stage label (the per-run report keys off this).
    run     : mutates `ctx.bullets` in place.
    enabled : consulted once per run, so a config toggle stays live.
    retrim  : re-run `_trim_to_caps` after the pass (for a pass that lengthens text).
    verify  : snapshot before the pass and re-run the grounding gate after it, with
              that snapshot as the revert target. False only for a pass that cannot
              un-ground anything (the verbatim merge folds in the user's own text).
              A pass that RE-KEYS a bullet must gate its OWN change before committing it (as
              `compose.fill_underfull` does), because this snapshot is keyed by the OLD gkey
              and cannot revert a key it does not hold; the driver's gate here is then only
              the backstop.
    recheck_fill — after the bracketing above, re-measure every bullet this pass
              actually changed and note the ones that are STILL underfull. Only
              meaningful on a pass whose job is to lengthen (see
              `_note_still_underfull` for why the re-check has to be last).
    """
    name: str
    run: Callable[["PassCtx"], None]
    enabled: Callable[[], bool] = _always
    retrim: bool = False
    verify: bool = True
    recheck_fill: bool = False


def _gate(ctx: PassCtx, *, stage: str = "rephrase",
          fallback: Optional[Dict[str, str]] = None,
          drops_as_notes: bool = False) -> Dict[str, List[str]]:
    """The single call site for the deterministic grounding gate. Returns
    {gkey: unseen_tokens} for every bullet it reverted or dropped (empty = all
    grounded).

    Discarding that return value is exactly how a half-grounded run reads as a clean
    one: the gate is silent by design on grounded text, so the .tex it produces after
    a drop is indistinguishable from one that never needed the gate. Here it is
    folded into `ctx.report` instead, labelled
    with the stage that caused it, and whether the bullet survived: a gkey still in
    `bullets` after the call was REVERTED to its snapshot, one that is gone was
    DROPPED outright (no grounded text to fall back to).

    `drops_as_notes` downgrades a DROP to a note instead of a warning. Only the
    prologue passes it (see `_prologue_gate`), and only because a prologue drop is not
    yet the run's last word on the bullet: reground is about to either overturn it (the
    bullet comes back, recovered, nothing left to warn about) or confirm it (a second
    `_gate` call, flag off, warns for real). So it is the RE-ASK's outcome that decides
    the severity, not this call. A REVERTED bullet always warns regardless of the
    flag — the pass that produced it already ran to completion and wrote ungrounded
    text, and there is no second attempt coming to change that verdict.

    Every finding handled here — reverted or dropped, flagged or not — also gets one
    note recording the REJECTED text verbatim, captured before `enforce_grounded` runs
    (a drop deletes the key outright, so the text has to be captured first). The
    offending token alone cannot tell a fabricated fact from a tokenizer false
    positive (a version string, a number spelled two ways); the rejected text is what
    lets that call be made after the run, from the report alone.

    `enforce_grounded` itself is deliberately untouched — the suite pins its exact
    signature `(sel, bullets, *, fallback=None, log=None)` — so this reads the return
    value rather than taking a callback.
    """
    before = dict(ctx.bullets)
    handled = verify.enforce_grounded(ctx.sel, ctx.bullets, fallback=fallback, log=ctx.log)
    if handled and ctx.report is not None:
        for gkey, tokens in handled.items():
            action = "reverted" if gkey in ctx.bullets else "dropped"
            line = f"[{stage}] {action} bullet '{gkey}' (ungrounded: {', '.join(tokens)})"
            if action == "dropped" and drops_as_notes:
                ctx.report.note(KIND_GROUNDING, line)
            else:
                ctx.report.warn(KIND_GROUNDING, line)
            ctx.report.note(
                KIND_GROUNDING,
                f"[{stage}] rejected text for '{gkey}': "
                f"{json.dumps(before.get(gkey, ''), ensure_ascii=False)}")
    return handled


def _prologue_gate(ctx: PassCtx) -> None:
    """Run the prologue grounding gate and, when reground is enabled, its recovery.

    The prologue is the only fallback-less gate (see `_gate`): a drop here has no
    earlier grounded text to revert to. That makes every prologue drop PROVISIONAL
    while a re-ask can still overturn it, so it is filed as a note rather than a
    warning (`drops_as_notes=True`) — the outcome of `_recover_dropped`, not this
    call, is what decides whether the run actually lost the bullet. With reground OFF
    there is no re-ask coming, so the drop is not provisional at all and this warns
    immediately, the same as any other stage's drop.

    The toggle is read once, into `reground`, and reused for both calls below: the
    dashboard can rewrite config.json while a tailor worker runs, and a flip between
    two reads could file the drop as a note and then skip the re-ask that was
    supposed to resolve it.
    """
    reground = config.reground_enabled()
    handled = _gate(ctx, stage="rephrase", drops_as_notes=reground)
    if reground:
        _recover_dropped(ctx, handled)


def _name_dropped(dropped: Dict[str, List[str]]) -> str:
    """Render `{gkey: tokens}` as `"p1 (ungrounded: ETL), p2 (ungrounded: Kafka)"`,
    sorted by gkey, so a re-ask that could not save any bullet still names every one
    it lost in the one warning that reports the loss, instead of a bare count."""
    return ", ".join(f"{gk} (ungrounded: {', '.join(dropped[gk])})"
                     for gk in sorted(dropped))


def _recover_dropped(ctx: PassCtx, handled: Dict[str, List[str]]) -> None:
    """Give the prologue gate's DELETIONS one bounded re-ask, then re-gate the result.

    A gkey in `handled` that is no longer in `ctx.bullets` was deleted rather than
    reverted, because the prologue runs on rephrase's first output and has no earlier
    grounded text to fall back to. That deletion is usually one stray label away from a
    perfectly grounded line, and it lands hardest on a block's opening bullet — an entry
    that loses its overview leads with a detail bullet and stops explaining itself.

    `compose.reground` re-asks from the same atoms with the offending tokens banned. The
    gate then runs again over the whole set, which is what makes this safe: nothing the
    re-ask returns is trusted, a still-ungrounded line is deleted a second time and
    reported, and only a line that passes the same deterministic check as every other
    bullet reaches the page. Recovered bullets rejoin `ctx.bullets` before the bullet
    passes run, so they are trimmed and re-verified exactly like the rest.

    Fires only on a run that already lost a bullet; a clean run costs nothing. Advisory,
    never fatal — on any failure the deletions simply stand.

    The caller (`_prologue_gate`) files the prologue's drop as a note on the bet that
    this function is about to undo it. This function is what has to make that bet
    honest: exactly one warning for every bullet still missing once it returns, named
    here on a failed re-ask, an empty re-ask, or a re-ask that answered only some of
    the dropped bullets, or by the second `_gate` call below on a re-ask that is still
    ungrounded, and none at all for a bullet it recovers.
    """
    dropped = {gk: toks for gk, toks in handled.items() if gk not in ctx.bullets}
    if not dropped:
        return
    if ctx.report is not None:
        ctx.report.stage("reground")
    ctx.log(f"re-asking {len(dropped)} dropped bullet(s) against their own atoms…")
    try:
        recovered = compose.reground(ctx.jd, ctx.job_title, ctx.sel, dropped)
    except Exception as exc:  # noqa: BLE001 - recovery is advisory; the drop stands
        if ctx.report is not None:
            ctx.report.warn(
                KIND_GROUNDING,
                f"[reground] re-ask failed, {len(dropped)} bullet(s) stay dropped "
                f"({_name_dropped(dropped)}): {exc}")
        return
    if not recovered:
        # `compose.reground` handles its own transport failure and returns {}, so this is
        # the only place a dead re-ask becomes visible. Without it the run reports the
        # drop and then goes quiet about the attempt to undo it.
        if ctx.report is not None:
            ctx.report.warn(
                KIND_GROUNDING,
                f"[reground] the re-ask returned nothing; {len(dropped)} bullet(s) "
                f"stay dropped ({_name_dropped(dropped)})")
        return
    ctx.bullets.update({gk: text for gk, text in recovered.items() if gk in dropped})
    unanswered = {gk: toks for gk, toks in dropped.items() if gk not in recovered}
    if unanswered and ctx.report is not None:
        ctx.report.warn(
            KIND_GROUNDING,
            f"[reground] the re-ask answered {len(recovered)} of {len(dropped)} bullet(s); "
            f"{len(unanswered)} stay dropped ({_name_dropped(unanswered)})")
    # The same pinned gate, second time around. Anything still ungrounded is deleted
    # again and warned about under the "reground" label, so a failed recovery is as
    # visible as the original drop rather than reading like a clean run.
    _gate(ctx, stage="reground")
    kept = sorted(gk for gk in dropped if gk in ctx.bullets)
    if kept and ctx.report is not None:
        ctx.report.note(
            KIND_GROUNDING,
            f"[reground] recovered {len(kept)} dropped bullet(s) on a re-ask: "
            f"{', '.join(kept)}")


def _note_still_underfull(ctx: PassCtx, before: Dict[str, str], *,
                          stage: str) -> List[str]:
    """Re-measure the bullets `stage` actually changed and note the ones that are STILL
    underfull. Returns their gkeys (empty = every fill stuck).

    The underfull fill runs *measure -> ask the model to lengthen -> trim back*, and
    until now nothing re-measured after that trim. `_fit_to_lines` can hand back
    exactly the text the fill was paid to grow — a filled bullet whose extra material
    pushes it onto one more printed line is binary-searched back to the longest prefix
    that fits, and when the added material is one wide token that prefix IS the
    original. The run then reported a fill that did not happen.

    Three things about the shape of this check matter:

      * It runs LAST, after the re-trim AND the grounding gate, because both can undo
        the fill and only the final text is worth reporting.
      * It only looks at bullets whose text this pass CHANGED (a committed fill re-keys
        the bullet onto its borrowed atom, so it is not in `before` at all). A bullet
        left underfull because its block had no spare atom was never filled, is a
        documented no-op, and reporting it would be noise.
      * It is a NOTE, not a warning. Nothing is re-called: a second billed model call
        per bullet to recover a part-empty last line is not worth it, and with the
        user's two-line layout a short last line is a cosmetic blemish, not a broken
        résumé. The report says so; the run does not claim to be degraded over it.
    """
    targets = compose.bullet_line_targets(ctx.sel)
    still: List[str] = []
    for gkey, text in ctx.bullets.items():
        if compose.is_verbatim_gkey(gkey) or before.get(gkey) == text:
            continue
        target = targets.get(gkey, config.PROJECT_BULLET_LINES)
        if not measure.is_underfull(text, target):
            continue
        still.append(gkey)
        if ctx.report is not None:
            fill = measure.text_width(text) / max(1, target * measure.BODY_LINE_CAPACITY)
            ctx.report.note(
                KIND_UNDERFULL,
                f"[{stage}] bullet '{gkey}' is still underfull after the re-trim "
                f"(fills {fill:.0%} of its {target}-line budget)")
    if still:
        ctx.log(f"{len(still)} filled bullet(s) trimmed back to underfull; "
                f"see {REPORT_NAME}.")
    return still


def _jev_kw(ctx: PassCtx) -> Dict[str, Any]:
    """`judge=` for a stage a Jev step sits in, or nothing with Jev off. Off, the stage
    is called with no `judge` keyword, so a caller or a test double written without
    one keeps working."""
    return {"judge": ctx.judge} if ctx.judge is not None else {}


def _pass_dedupe_verbs(ctx: PassCtx) -> None:
    """Guarantee every tailored bullet opens with a DISTINCT action verb — none reused,
    none colliding with a verbatim block's opener (verbatim text is reserved, never
    modified). With Jev on, Jev picks each repeated opener's new verb first
    (`jev_assist.pick_verb`)."""
    compose.dedupe_leading_verbs(ctx.bullets, compose.group_map(ctx.sel), ctx.jd,
                                 reserved=ctx.reserved, **_jev_kw(ctx))
    if ctx.judge is not None and ctx.report is not None:
        ctx.report.jev_step(jev_assist.STEP_VERB)


def _pass_merge_verbatim(ctx: PassCtx) -> None:
    """Fold in the user's exact (untailored) bullets, then trim every tailored bullet to
    its per-bullet printed-line target. Unverified on purpose: the merged text is the
    user's own, and the trim only shortens text the gate already passed."""
    if ctx.verbatim:
        ctx.bullets.update(ctx.verbatim)
        ctx.log(f"using {len(ctx.verbatim)} verbatim bullet(s) (untailored, as typed).")
    _trim_to_caps(ctx.sel, ctx.bullets)


def _pass_fill_underfull(ctx: PassCtx) -> None:
    """Grow any bullet that rendered shorter than its configured line target by folding in
    one detail from an unused SAME-block atom (never fabricates — a no-op when there's no
    spare material). `retrim=True` re-trims the (over)filled bullets back to a clean line
    boundary before the gate re-checks them, and `recheck_fill=True` re-measures the result
    (the trim can hand back exactly what the fill was paid to grow).

    The fill gates itself: a committed fill re-keys the bullet onto its augmented gkey, and
    this driver's own snapshot — taken before the pass runs, keyed by the OLD gkey — cannot
    revert a fill it rejects. `on_reject` is what still gets a refusal into the run report, as
    a NOTE rather than a warning: the bullet that ships is the grounded original, which is
    correct and shippable.
    """
    ctx.log("filling underfull bullets from spare atoms…")

    def on_reject(gk: str, tokens: List[str], text: str) -> None:
        if ctx.report is not None:
            ctx.report.note(
                KIND_GROUNDING,
                f"[underfull fill] refused a fill for '{gk}' (ungrounded: {', '.join(tokens)}); "
                f"kept the grounded original. Rejected text: "
                f"{json.dumps(text, ensure_ascii=False)}")

    compose.fill_underfull(ctx.jd, ctx.job_title, ctx.sel, ctx.bullets, on_reject=on_reject)


def _pass_enforce_style(ctx: PassCtx) -> None:
    """Deterministic style gate: banned AI-tell phrasing (em dashes, contrast framing,
    buzzword verbs, ...) never reaches the page.

    `retrim=True` because the repair REWRITES a bullet: the prompt asks it to stay
    within `max_chars`, but nothing verified that, and this is the LAST bullet pass —
    downstream `compile.enforce_one_page` only drops whole bullets, it never re-trims
    text. Without it, a repair that came back longer than the text it replaced would
    ship over its line budget and silently wrap onto an extra line."""
    fixed = compose.enforce_style(ctx.jd, ctx.job_title, ctx.sel, ctx.bullets)
    if fixed:
        ctx.log(f"style gate: repaired {fixed} bullet(s).")


# The stage labels. Named once so the `Pass` registration and every report line the
# sweep writes cannot drift apart; the faithfulness check reads the first two.
VERB_DEDUPE_STAGE = "verb dedupe"
STYLE_GATE_STAGE = "style gate"
AIWRITING_SWEEP_STAGE = "ai writing sweep"


def _report_sweep(ctx: PassCtx, result: sweep.SweepResult, *, stage: str) -> None:
    """Fold one `SweepResult` into `ctx.report`, at two severities.

    A WARNING for an item whose model call raised: the stage the run paid for did not
    happen for that item, which is the same class of event as a skipped ATS report or a
    failed cover letter, and those are warnings.

    A NOTE for everything else. A refused rewrite keeps the original bullet, and the
    original was already grounded, already within its line budget and already past the
    deterministic style gate, so nothing about it is degraded. The P2 findings are the
    same: the sweep repairs P0 and P1 and reports P2 by decision, so a P2 line is the
    record of a policy, and putting it on `on_warning` would tell the dashboard a correct
    résumé finished degraded. Each line says so in its own words, because a line is read
    out of context once it is in a Qt dialog.
    """
    if ctx.report is None:
        return
    rep = ctx.report
    for item in result.failures:
        rep.warn(KIND_AIWRITING,
                 f"[{stage}] item '{item}' was not swept (its model call failed); its "
                 f"bullets ship exactly as they were")
    reask = (f"; the bounded re-ask ran for {len(result.reasked)} item(s): "
             + ", ".join(result.reasked)) if result.reasked else \
            "; no item needed the bounded re-ask"
    rep.note(KIND_AIWRITING,
             f"[{stage}] swept {result.items} item(s) in {result.calls} call(s) and "
             f"rewrote {len(result.changed)} bullet(s){reask}")
    # The sweep gate (`jev_assist.sweep_flags`), with Jev on. Notes: a skipped call is
    # the gate doing its job, and a flag only says why a call was made.
    if result.skipped:
        rep.note(KIND_AIWRITING,
                 f"[{stage}] Jev's sweep gate skipped the call for "
                 f"{len(result.skipped)} item(s) with no tell and no detector finding: "
                 + ", ".join(result.skipped))
    for flag in result.judge_flags:
        rep.note(KIND_AIWRITING,
                 f"[{stage}] Jev read {', '.join(flag.names)} in bullet '{flag.gkey}' "
                 f"in '{flag.item}', so that item was sent to the sweep")
    for rejection in result.rejected:
        rep.note(KIND_AIWRITING,
                 f"[{stage}] kept the original of bullet '{rejection.gkey}' in "
                 f"'{rejection.item}' ({rejection.reason}: {rejection.detail})")
    for finding in result.unfixed_p2:
        # itemcheck.findings_payload() renders the same material as JSON for a prompt
        # body. The report is plain text a person reads, so the fields are spelled out
        # here instead: detector, item, and the sentence the detector wrote.
        rep.note(KIND_AIWRITING,
                 f"[{stage}] {finding.tier} polish left in place by policy (this stage "
                 f"repairs P0 and P1 only): {finding.detector} in '{finding.item}' "
                 f"({finding.detail})")
    for lingering in result.unfixed_phrasing:
        # A WARNING, unlike the two loops above. A rejection means the guard worked and
        # correct text was kept; a P2 finding is a policy choice this stage made on
        # purpose. This one is the stage failing at the job it was paid for: the rule
        # fired, the bullet went to the model naming it, and the tell is still on the
        # page. Silence here is what made the first production run read as a clean
        # 8-call no-op, so it gets the severity that reaches a reader.
        rep.warn(KIND_AIWRITING,
                 f"[{stage}] bullet '{lingering.gkey}' in '{lingering.item}' still "
                 f"reads as {', '.join(lingering.names)} after the sweep: it was "
                 f"flagged and sent, and the rewrite did not clear it")


def _pass_aiwriting_sweep(ctx: PassCtx) -> None:
    """Item-level AI-writing sweep: one model call per résumé entry, reading the entry
    and all of its bullets together so the tells that only exist ACROSS an item can be
    seen at all. `compose.enforce_style` reads one bullet at a time and is structurally
    blind to those. See `sweep.py` for the five acceptance conditions.

    `retrim=True` is a BACKSTOP ONLY, and it should never do anything. The sweep's own
    acceptance check refuses any rewrite that renders past its per-bullet line budget, so
    every committed bullet already fits and `_trim_to_caps` has nothing to cut
    (`tests/test_sweep_layout_invariant.py` asserts exactly that). The flag is set
    anyway because the alternative is trusting one check with the page-fit guarantee
    and nothing behind it. So a trim FIRING here is not
    the backstop working, it is the report that the acceptance check has a hole: the
    whole design is fit-or-revert, and a silent trim would quietly convert a rejected
    rewrite into a mid-sentence bullet, which is the exact outcome the fit-or-revert rule
    exists to prevent.

    `verify=True` because the grounding gate covers the direction the sweep's own check
    cannot. `verify.enforce_grounded` reverts a bullet that INTRODUCES a token with no
    trace in its atoms; the sweep's fifth condition rejects one that DROPS a fact the
    original carried. Neither implies the other, so both run.

    With Jev on, the run's judge reads every bullet first, and an item makes its call
    only for a tell Jev reads or a detector finding (`jev_assist.sweep_flags`, the
    judge gate in `sweep.py`). The gate's usage line is recorded whichever way the
    pass ends.
    """
    ctx.log("sweeping each résumé item for AI-writing tells…")
    try:
        result = sweep.sweep_items(ctx.jd, ctx.job_title, ctx.sel, ctx.bullets,
                                   **_jev_kw(ctx))
    except Exception as exc:  # noqa: BLE001 - phrasing polish never sinks a résumé
        # `sweep_items` guards each item's model call itself, so what reaches here is a
        # defect rather than a flaky transport. It is still caught: by the time this pass
        # runs, every bullet is grounded, style-gated and inside its line budget, and
        # losing that résumé over the stage that only polishes phrasing is the worse
        # outcome. Warned, not noted, because the stage really did not run.
        ctx.log(f"AI-writing sweep skipped ({exc})")
        if ctx.report is not None:
            ctx.report.warn(KIND_AIWRITING,
                            f"[{AIWRITING_SWEEP_STAGE}] skipped ({exc}); every bullet "
                            f"ships as the style gate left it")
        return
    finally:
        if ctx.judge is not None and ctx.report is not None:
            ctx.report.jev_step(jev_assist.STEP_SWEEP_GATE)
    if result.changed:
        ctx.log(f"AI-writing sweep: rewrote {len(result.changed)} bullet(s).")
    _report_sweep(ctx, result, stage=AIWRITING_SWEEP_STAGE)


# ── The faithfulness check ───────────────────────────────────────────────────
# The grounding gate traces distinctive tokens only, so a claim written in lowercase
# common words passes it: "Led the team" over an atom that says the candidate helped
# (verify.py's docstring states the gap). With Jev on, the check asks the judge about
# every bullet a stage wrote, against the atoms it was written from, after the
# prologue gate and after every verified pass. It may only reject, revert or drop a
# bullet: the reground call writes any new text, and the grounding gate runs
# unchanged.
FAITHFULNESS_REGROUND_STAGE = "faithfulness reground"

# The check after these stages runs once the style gate has repaired banned phrasing
# and stripped em dashes, so a regrounded text there must add no style finding, and a
# revert at the style gate carries the gate's own em-dash strip.
_LATE_STAGES = frozenset((STYLE_GATE_STAGE, AIWRITING_SWEEP_STAGE))


def _faith_texts(ctx: PassCtx, snapshot: Optional[Dict[str, str]]) -> Dict[str, str]:
    """{gkey: text} for the bullets a stage wrote: every tailored bullet at the
    prologue (no snapshot), else each one whose text differs from `snapshot`, a
    re-keyed bullet included. A change that is only the style gate's em-dash strip is
    left out: it is punctuation, on text the check already passed."""
    out: Dict[str, str] = {}
    for gk, text in ctx.bullets.items():
        if compose.is_verbatim_gkey(gk):
            continue
        if snapshot is not None and gk in snapshot and text in (
                snapshot[gk], compose._strip_em_dashes(snapshot[gk])):
            continue
        out[gk] = text
    return out


def _faith_entries(ctx: PassCtx, texts: Dict[str, str]) -> List[Dict[str, Any]]:
    """`jev_assist.faithfulness`'s input: `texts` grouped by résumé entry in print
    order, each bullet with its own atoms as the writer saw them."""
    gm = compose.group_map(ctx.sel)
    entries: List[Dict[str, Any]] = []
    for name, gkeys in compose._blocks_in_order(ctx.sel):
        bullets = [{"gkey": gk, "text": texts[gk],
                    "atoms": [compose._atom_payload(a) for a in gm[gk]]}
                   for gk in gkeys if gk in texts]
        if bullets:
            entries.append({"entry": name, "bullets": bullets})
    return entries


def _faith_refusal(ctx: PassCtx, gk: str, text: str, flagged_text: str, *, stage: str,
                   openers: FrozenSet[str] = frozenset(), max_lines: int = 0) -> str:
    """Why regrounded `text` may not replace `flagged_text`, or "" when it goes on to
    Jev's re-check. `openers` holds the opening verbs it must not reuse, and a
    non-zero `max_lines` the printed lines it may not run past (the sweep's)."""
    if not text:
        return "the re-ask returned nothing"
    if text == flagged_text:
        return "the re-ask returned the same text"
    ids = compose.group_map(ctx.sel).get(gk) or gk.split("+")
    unseen = verify.group_unseen(ctx.sel, ids, text)
    if unseen:
        return f"the re-ask's text is ungrounded: {', '.join(unseen)}"
    if stage in _LATE_STAGES:
        before = sweep._violations(flagged_text)
        fresh = [v for v in sweep._violations(text) if v not in before]
        if fresh:
            return f"the re-ask's text adds {', '.join(fresh)}"
    verb = compose.leading_verb(text)
    if verb and verb in openers:
        return f"the re-ask's text opens with '{verb}', which another bullet already uses"
    if max_lines:
        lines = measure.line_count(text)
        if lines > max_lines:
            return (f"the re-ask's text runs to {lines} printed lines, past the "
                    f"{max_lines} the bullet had before the sweep")
    return ""


def _undo_fill(ctx: PassCtx, gk: str, snapshot: Dict[str, str]) -> bool:
    """Undo the underfull fill's re-key of `gk`. The fill appends one borrowed atom to
    a group and moves the bullet onto the longer key (`compose.fill_underfull`), so
    the group gets its old atoms back and the bullet its old key and text. False when
    `gk` is no such fill."""
    ids = compose.group_map(ctx.sel).get(gk) or []
    old_ids = list(ids[:-1])
    old_gk = compose._gkey(old_ids)
    if not old_ids or old_gk not in snapshot or old_gk in ctx.bullets:
        return False
    for sec in ("experience", "projects", "leadership"):
        for entry in ctx.sel.get(sec) or []:
            groups = entry.get("groups") or []
            for gi, group in enumerate(groups):
                if list(group) == list(ids):
                    groups[gi] = old_ids
                    del ctx.bullets[gk]
                    ctx.bullets[old_gk] = snapshot[old_gk]
                    return True
    return False


def _faith_revert(ctx: PassCtx, gk: str, snapshot: Optional[Dict[str, str]],
                  stage: str) -> str:
    """Put a still-flagged bullet back to its last passing version, and say how:
    "reverted" to its `snapshot` text (a fill's re-key undone first), else "dropped",
    exactly as `verify.enforce_grounded` drops an ungrounded bullet. At the style gate
    the revert takes the gate's em-dash strip, which is the text the gate leaves when
    it refuses a repair."""
    if snapshot is not None and gk in snapshot:
        text = snapshot[gk]
        ctx.bullets[gk] = compose._strip_em_dashes(text) if stage == STYLE_GATE_STAGE else text
        return "reverted"
    if snapshot is not None and _undo_fill(ctx, gk, snapshot):
        return "reverted"
    del ctx.bullets[gk]
    return "dropped"


def _fresh_opener(ctx: PassCtx, gk: str, flagged_text: str) -> Optional[str]:
    """At the verb dedupe, give `gk` an opener no other bullet or verbatim block uses,
    through the dedupe's own in-category swap (`compose._pick_unused_verb`), never the
    verb Jev flagged. The dedupe's revert target is the text it had to change, so its
    opener repeats by construction.

    Returns the faithful text a swap replaced, "" when the opener was unique already,
    and None when every palette verb is taken (the faithful text then stays). The
    swapped text is written after the faithfulness verdict, so the caller checks it
    (`_check_fresh_openers`)."""
    text = ctx.bullets[gk]
    verb = compose.leading_verb(text)
    used = ({compose.leading_verb(t) for other, t in ctx.bullets.items() if other != gk}
            | set(ctx.reserved))
    if not verb or verb not in used:
        return ""
    repl = compose._pick_unused_verb(assets.active_verbs(), verb,
                                     used | {verb, compose.leading_verb(flagged_text)})
    if not repl:
        return None
    ctx.bullets[gk] = compose._swap_leading_verb(text, repl)
    return text


def _check_fresh_openers(ctx: PassCtx, swapped: Dict[str, str]) -> Set[str]:
    """One faithfulness request over the bullets `_fresh_opener` gave a new opener
    ({gkey: the faithful text it replaced}). The swap's verb can come from any palette
    category, so it can inflate as the flagged text did, and no later pass asks about
    a bullet it left alone. A swapped text Jev flags, or one it cannot check (the
    request failed or the breaker is open), goes back to its faithful text. Returns
    the gkeys put back, whose openers now repeat."""
    if not swapped:
        return set()
    verdicts = jev_assist.faithfulness(
        _faith_entries(ctx, {gk: ctx.bullets[gk] for gk in swapped}), judge=ctx.judge)
    back: Set[str] = set()
    for gk, faithful in swapped.items():
        if verdicts is None or verdicts.get(gk) != "":
            ctx.bullets[gk] = faithful
            back.add(gk)
    return back


def _check_faithfulness(ctx: PassCtx, *, stage: str,
                        snapshot: Optional[Dict[str, str]] = None,
                        retrim: bool = False) -> Dict[str, str]:
    """Ask Jev whether each bullet `stage` wrote says only what its atoms say, and
    act on the verdict. Returns {gkey: finding} for the bullets it flagged: empty
    when Jev is off or failed (the grounding gate then stands alone) or when every
    bullet passed.

    `snapshot` is the stage's pre-pass copy, None at the prologue. It picks which
    bullets are asked about (`_faith_texts`) and is the revert target. `retrim` says
    the stage re-trims what it writes, so a regrounded text is trimmed the same way.

    The flagged bullets share ONE `compose.reground` call, each with its finding
    named. A regrounded text replaces the flagged one only when it:
      1. is non-empty and differs from the flagged text,
      2. passes the grounding gate's own check (`verify.group_unseen`),
      3. adds no style finding, at the style gate and the sweep (`_LATE_STAGES`),
         and at the sweep runs to no more printed lines than the bullet had before it,
      4. opens with a verb no other bullet uses, on the passes after the verb dedupe
         (the revert target at the dedupe itself is the text it had to change),
      5. passes a second faithfulness check; one that cannot run counts as flagged.
    Anything else leaves the bullet flagged, and `_faith_revert` puts it back to its
    last passing version or drops it. At the verb dedupe a regrounded or reverted
    bullet whose opener repeats gets the dedupe's swap (`_fresh_opener`), and the
    swapped texts share one more faithfulness request (`_check_fresh_openers`). With
    no verb left, or a swap that request flags or cannot check, the bullet keeps its
    faithful text and warns with "repeated opener", whether it was regrounded or
    reverted.

    Each flagged bullet gets a note with its text, like the gate's rejected-text note;
    a revert or a drop warns, as the gate's do, and a repaired bullet gets a note
    unless its opener repeats.
    """
    if ctx.judge is None:
        return {}
    texts = _faith_texts(ctx, snapshot)
    verdicts = jev_assist.faithfulness(_faith_entries(ctx, texts), judge=ctx.judge) or {}
    flagged = {gk: finding for gk, finding in verdicts.items()
               if finding and gk in ctx.bullets}
    if not flagged:
        return {}
    flagged_texts = {gk: ctx.bullets[gk] for gk in flagged}
    rep = ctx.report
    if rep is not None:
        rep.stage(FAITHFULNESS_REGROUND_STAGE)
        for gk, finding in flagged.items():
            rep.note(KIND_GROUNDING,
                     f"[{stage}] faithfulness: flagged text for '{gk}' ({finding}): "
                     f"{json.dumps(ctx.bullets[gk], ensure_ascii=False)}")
    ctx.log(f"faithfulness check: re-asking {len(flagged)} flagged bullet(s) against "
            f"their own atoms…")
    failed = ""
    try:
        answers = compose.reground(ctx.jd, ctx.job_title, ctx.sel,
                                   {gk: [] for gk in flagged}, findings=flagged,
                                   printed=set(ctx.bullets) | set(ctx.verbatim))
    except Exception as exc:  # noqa: BLE001 - the re-ask is advisory; the flag stands
        ctx.log(f"faithfulness re-ask failed ({exc})")
        answers, failed = {}, "the re-ask failed"
    refused: Dict[str, str] = {}
    candidates: Dict[str, str] = {}

    def refuse(gk: str, why: str, text: str) -> None:
        refused[gk] = why
        if text and rep is not None:
            rep.note(KIND_GROUNDING,
                     f"[{stage}] faithfulness: refused the re-ask's text for '{gk}': "
                     f"{json.dumps(text, ensure_ascii=False)}")

    targets = compose.bullet_line_targets(ctx.sel)
    settled = snapshot is not None and stage != VERB_DEDUPE_STAGE
    for gk in flagged:
        text = (answers.get(gk) or "").strip()
        if text and retrim:
            text = _fit_to_lines(text, targets.get(gk, config.PROJECT_BULLET_LINES))
        openers: FrozenSet[str] = frozenset()
        if settled:
            openers = frozenset(
                {compose.leading_verb(t) for other, t in ctx.bullets.items() if other != gk}
                | {compose.leading_verb(t) for t in candidates.values()}
                | set(ctx.reserved))
        max_lines = 0
        if stage == AIWRITING_SWEEP_STAGE and snapshot is not None and gk in snapshot:
            max_lines = measure.line_count(snapshot[gk])
        why = failed or _faith_refusal(ctx, gk, text, ctx.bullets[gk], stage=stage,
                                       openers=openers, max_lines=max_lines)
        if why:
            refuse(gk, why, text)
        else:
            candidates[gk] = text
    regrounded: List[str] = []
    if candidates:
        recheck = jev_assist.faithfulness(_faith_entries(ctx, candidates), judge=ctx.judge)
        for gk, text in candidates.items():
            verdict = None if recheck is None else recheck.get(gk)
            if verdict == "":
                ctx.bullets[gk] = text
                regrounded.append(gk)
            elif verdict is None:
                refuse(gk, "the re-check could not run", text)
            else:
                refuse(gk, f"the re-check still flags it: {verdict}", text)
    actions = {gk: _faith_revert(ctx, gk, snapshot, stage) for gk in refused}
    # At the verb dedupe a regrounded or reverted bullet whose opener repeats gets the
    # dedupe's swap, and every swapped text shares one more check.
    repeated: Set[str] = set()
    if stage == VERB_DEDUPE_STAGE:
        swapped: Dict[str, str] = {}
        for gk in regrounded + list(refused):
            if gk not in ctx.bullets:
                continue
            faithful = _fresh_opener(ctx, gk, flagged_texts[gk])
            if faithful is None:
                repeated.add(gk)
            elif faithful:
                swapped[gk] = faithful
        repeated |= _check_fresh_openers(ctx, swapped)
    for gk in regrounded:
        if rep is not None:
            # A kept opener that repeats breaks the dedupe's distinct-opener promise,
            # so it warns, as a revert with a repeated opener does.
            if gk in repeated:
                rep.warn(KIND_GROUNDING, f"[{stage}] faithfulness: regrounded bullet "
                                         f"'{gk}' ({flagged[gk]}; repeated opener)")
            else:
                rep.note(KIND_GROUNDING,
                         f"[{stage}] faithfulness: regrounded bullet '{gk}' ({flagged[gk]})")
    for gk, why in refused.items():
        if gk in repeated:
            why += "; repeated opener"
        if rep is not None:
            rep.warn(KIND_GROUNDING,
                     f"[{stage}] faithfulness: {actions[gk]} bullet '{gk}' "
                     f"({flagged[gk]}; {why})")
    return flagged


# The order of this tuple IS the pipeline, and reordering it is the only way to
# reorder the stages.
# `enabled` holds the toggle FUNCTION rather than its value, so it is read per run.
#
# The AI-writing sweep is LAST, after the deterministic style gate, for two reasons that
# pull the same way. The gate is free and mechanical, so running it first means the
# judgment pass reads text the banned phrasing is already out of and spends its one call
# per item on the structural tells only it can see. And the sweep commits a rewrite only
# when that rewrite fits its own printed-line budget, which makes the pass's total line
# count non-increasing; a stage placed after it could re-lengthen a bullet and take that
# guarantee away, so no pass follows it. The one step after the sweep is its own
# faithfulness check (with Jev on), and it keeps the guarantee: at the sweep it
# refuses a regrounded text that runs past the bullet's pre-sweep printed lines, and a
# revert goes back to that pre-sweep text.
_BULLET_PASSES = (
    Pass(VERB_DEDUPE_STAGE, _pass_dedupe_verbs),
    Pass("verbatim + trim", _pass_merge_verbatim, verify=False),
    Pass("underfull fill", _pass_fill_underfull,
         enabled=config.fill_underfull_enabled, retrim=True, recheck_fill=True),
    Pass(STYLE_GATE_STAGE, _pass_enforce_style, retrim=True),
    Pass(AIWRITING_SWEEP_STAGE, _pass_aiwriting_sweep,
         enabled=config.aiwriting_sweep_enabled, retrim=True),
)


def _run_bullet_passes(ctx: PassCtx,
                       passes: Sequence[Pass] = _BULLET_PASSES) -> None:
    """Run each enabled pass under the snapshot -> mutate -> re-trim -> re-verify ->
    (optionally) re-measure discipline. Nobody writes a snapshot by hand, so nobody can
    forget one. Re-verifying is the grounding gate and then, with Jev on, the
    faithfulness check over the bullets the pass changed, against the same
    snapshot."""
    for p in passes:
        if not p.enabled():
            continue
        if ctx.report is not None:
            ctx.report.stage(p.name)
        snapshot = dict(ctx.bullets) if (p.verify or p.recheck_fill) else None
        p.run(ctx)
        if p.retrim:
            _trim_to_caps(ctx.sel, ctx.bullets)
        if p.verify:
            _gate(ctx, stage=p.name, fallback=snapshot)
            _check_faithfulness(ctx, stage=p.name, snapshot=snapshot, retrim=p.retrim)
        if p.recheck_fill:
            _note_still_underfull(ctx, snapshot or {}, stage=p.name)


def tailor(
    job: Dict[str, str],
    *,
    cover_letter: bool = False,
    ats_report: bool = True,
    prep_sheet: bool = False,
    tone: str = "professional",
    on_status: StatusFn = None,
    on_warning: WarnFn = None,
    reset_usage: bool = True,
) -> Path:
    """Tailor one job end to end; returns the output folder.

    `on_status` is the live progress line (transient). `on_warning` is the DEGRADED
    channel: it fires once per warning — a grounding-gate revert or drop, a résumé
    that shipped over `config.PAGE_LIMIT` pages, or any of the optional artifacts
    (ATS report, company research, cover letter body/compile/text, prep sheet,
    apply.md) failing, and the first run in a process after the claude CLI refused
    the chosen Claude model (`_report_model_swaps`). Optional, defaulting to None, so no existing call site
    changes; the same warnings are written to `tailor_report.txt` in the output
    folder either way, which is the copy that outlives a status bar.

    A finding that leaves a correct, shippable résumé, such as a bullet the underfull
    fill could not keep full once it was re-trimmed, or a prologue drop that reground
    recovered on its re-ask, is a NOTE instead: report only, never on `on_warning`, so
    it cannot mark the run degraded.

    When Jev is on for the tailor (`jev_switch.client("tailor")`), one judge serves
    the whole run: it rates the skills and the atoms before `select`
    (`jev_assist.skills_pick`, `jev_assist.atom_relevance`), picks the new verb for
    each repeated opener (`jev_assist.pick_verb`), reads each bullet for the
    banned-pattern tells that gate the AI-writing sweep's calls
    (`jev_assist.sweep_flags`), and checks each bullet the rephrase and every later
    rewrite wrote against its atoms (`_check_faithfulness`). Three Settings options,
    off by default, add a step each: best of three keeps one of three rephrase
    drafts per bullet (`_resolve_best_of`), the cover-letter claims check reads each
    letter sentence against its sources (`jev_assist.letter_unsupported`), and the
    ATS meaning line counts the keywords the résumé shows by meaning
    (`jev_assist.keyword_meaning`). A step whose request fails keeps its LLM path
    (the faithfulness check leaves the grounding gate to stand alone), and once the
    judge's breaker opens every later step does too. Each step's
    usage line goes to `tailor_report.txt` and to the status log.
    """
    log = on_status or _noop
    report = RunLog(on_warning=on_warning)
    # Parallel callers reset llm.USAGE once before fan-out and pass reset_usage=False,
    # so concurrent jobs don't clear each other's token accounting (see DECISIONS).
    if reset_usage:
        llm.reset_usage()
    # Jev's step counts are per thread, and a worker thread tailors one job after
    # another, so every run clears its own.
    jev_assist.reset_usage()

    company = _line_field(job, "company_name") or "Unknown Company"
    job_title = _line_field(job, "job_title") or "Role"
    jd = _job_description_text(job)
    if not pdflatex_available():
        raise RuntimeError(f"pdflatex not found at '{config.PDFLATEX_PATH}'. Install MiKTeX/TeX Live.")
    if len(jd) < 40:
        raise RuntimeError("Job description is empty/too short to tailor against.")

    # The run's Jev judge, None when Jev is off for the tailor. Every Jev step gets
    # this one, so an outage in one step moves the rest of the run to the LLM path.
    judge = jev_assist.default_judge()
    log(f"selecting evidence for: {job_title} @ {company}")
    report.stage("select")
    skill_pick = relevance = None
    if judge is not None:
        skill_pick = jev_assist.skills_pick(jd, job_title, judge=judge)
        report.jev_step(jev_assist.STEP_SKILLS)
        relevance = jev_assist.atom_relevance(jd, job_title, judge=judge)
        report.jev_step(jev_assist.STEP_SHORTLIST)
    sel = compose.select(jd, job_title, company, atom_relevance=relevance,
                         skill_pick=skill_pick)
    if not sel.get("experience"):
        raise RuntimeError("Selection returned no experience; aborting (check the JD/model).")

    # Per-block "don't tailor": swap selected verbatim blocks to the user's exact
    # bullets BEFORE rephrase, so the LLM never sees (or rewrites) them.
    verbatim = compose.inject_verbatim(sel)
    # Every entry leads with its overview bullet (the first atom under it in the master),
    # then the rest in relevance order. Runs BEFORE briefs/rephrase, so the cohesion
    # framing and the per-position line budgets build on the final order. Never invents.
    if config.lead_overview_enabled():
        report.stage("lead overview")
        compose.lead_with_overview(sel)
    # One cheap batched call: a cohesion brief per (non-verbatim) block so its bullets
    # read as one story instead of glued-together atoms.
    log("framing each block for cohesion…")
    report.stage("block briefs")
    briefs = compose.block_briefs(jd, job_title, sel)
    report.stage("rephrase")
    if judge is not None and config.best_of_n():
        # Best of three pays for three drafts before Jev's first request, so an open
        # breaker (an earlier step met an outage) keeps the single rephrase call.
        if jev_assist.breaker_open(jev_assist.STEP_BEST_OF, judge):
            bullets = _resolve_bullets(jd, job_title, sel, log, briefs=briefs)
        else:
            bullets = _resolve_best_of(jd, job_title, sel, log, briefs=briefs, judge=judge)
        report.jev_step(jev_assist.STEP_BEST_OF)
    else:
        bullets = _resolve_bullets(jd, job_title, sel, log, briefs=briefs)
    ctx = PassCtx(
        jd=jd, job_title=job_title, sel=sel, bullets=bullets, verbatim=verbatim,
        # The verbatim blocks' opening verbs: reserved, because the dedupe pass may
        # not rewrite the user's own text, so it must not reuse their openers either.
        reserved=frozenset(compose.leading_verb(t) for t in verbatim.values()),
        log=log, report=report, judge=judge,
    )
    # Deterministic grounding gate: every bullet's distinctive tokens
    # must trace to its own group's atoms — a hallucinated or JD-injected fact is
    # dropped here, never printed. This first call is the prologue and the only
    # fallback-less one: there is no earlier grounded text to revert to yet. Every
    # later bullet-mutating pass re-runs it against its own snapshot, reverting
    # instead of dropping when it can (see _run_bullet_passes). A prologue drop is
    # provisional, not final, while reground can still recover it — see
    # _prologue_gate for how that changes its severity.
    _prologue_gate(ctx)
    # The faithfulness check, with Jev on: the judge reads every surviving bullet
    # against its atoms for the claims the gate cannot trace. A bullet still flagged
    # after one reground has no earlier text to go back to here, so it is dropped as
    # the gate drops one.
    _check_faithfulness(ctx, stage="rephrase")
    if not bullets and not verbatim:
        raise RuntimeError("No grounded bullets survived selection/rephrase.")
    # Verb dedupe -> verbatim merge + trim -> underfull fill + re-trim -> style gate ->
    # item-level AI-writing sweep.
    _run_bullet_passes(ctx)
    if judge is not None:
        report.jev_step(jev_assist.STEP_FAITHFULNESS)

    log("compressing skills…")
    report.stage("skills")
    skill_lines = compose.compress_skills(jd, job_title, sel)

    # Optional 5th line: the JD's concept buzzwords the candidate owns (anchored
    # to concepts_and_methodologies; the JD's own spelling via skill_aliases, then padded
    # with the model's role-relevant ranking). Never invents; one-page enforcement is the
    # backstop. Appended last so it sits below the four tool lines.
    if config.methods_line_enabled():
        report.stage("methods line")
        methods = compose.methods_line(jd, sel)
        if methods:
            skill_lines.append(methods)

    out_dir = output.resolve_dir(company, job_title)
    with tempfile.TemporaryDirectory(prefix="resume_tailor_") as tmp:
        tmp_path = Path(tmp)
        tex_path = tmp_path / "resume.tex"
        log("rendering + compiling (one-page enforcement)…")
        report.stage("render + compile")
        result, final_bullets, tex = enforce_one_page(
            sel, bullets, skill_lines, tex_path, tmp_path, jd, on_status=log
        )
        if not result.ok or not result.pdf_path:
            raise RuntimeError(f"LaTeX compile failed: {result.error}\n{result.log_tail}")

        # enforce_one_page returns ok=True on an OVER-LENGTH pdf when it ran out of
        # project bullets to drop (overflow that originates elsewhere is not something
        # it can fix). The PDF still ships — a two-page résumé beats no résumé — but
        # the run stops claiming it was clean. getattr, because the field is new and a
        # stubbed enforce_one_page may return a result without it; 0 means "never
        # measured", which is not evidence of overflow.
        report.pages = int(getattr(result, "pages", 0) or 0)
        if report.pages > config.PAGE_LIMIT:
            report.warn(KIND_PAGE_LIMIT,
                        f"résumé shipped on {report.pages} pages "
                        f"(limit is {config.PAGE_LIMIT}); nothing was left to drop")
            log(f"WARNING: résumé is {report.pages} pages, over the "
                f"{config.PAGE_LIMIT}-page limit")

        shutil.copyfile(result.pdf_path, out_dir / output.resume_filename())
        shutil.copyfile(tex_path, out_dir / "resume.tex")  # keep source for inspection

        if ats_report:
            report.stage("ats report")
            # The ATS meaning check only with the option on, handed in as a
            # function so the report module stays free of Jev.
            ats_kw: Dict[str, Any] = {}
            if judge is not None and config.ats_meaning():
                def meaning(keywords: Sequence[str], text: str) -> Optional[List[str]]:
                    return jev_assist.keyword_meaning(keywords, text, judge=judge)
                ats_kw["meaning"] = meaning
            try:
                cov = ats.write_report(jd, out_dir / output.resume_filename(), out_dir,
                                       **ats_kw)
                log(f"ATS keyword coverage: {cov:.0%} (details in ats_report.txt)")
            except Exception as exc:  # noqa: BLE001 - the report is advisory, never fatal
                log(f"ATS check skipped ({exc})")
                report.advisory(f"ATS check skipped ({exc})")
            if ats_kw:
                report.jev_step(jev_assist.STEP_ATS_MEANING)

        cover_body = ""      # the paste-ready letter apply.md embeds, when generated
        if cover_letter:
            log("writing cover letter…")
            report.stage("cover letter")
            # The cover-letter claims check: the letter gets the run's judge only
            # with the check on, so a letter writer that takes no judge keeps
            # working with it off.
            letter_kw = ({"judge": judge}
                         if judge is not None and config.cover_letter_jev_check() else {})
            try:
                blurb = ""
                try:
                    log("researching company (grounded search)…")
                    blurb = research.company_blurb(company, job_title)
                except Exception as exc:  # noqa: BLE001 - research is optional
                    log(f"company research unavailable ({exc})")
                    report.advisory(f"company research unavailable ({exc})")
                # The full notes on every entry that printed a bullet, plus the
                # seed: the letter tells those entries as a story, so it needs the
                # material the bullets compressed away.
                background, seed = _letter_inputs(sel, final_bullets, log,
                                                  warn=report.advisory)
                body = coverletter.generate_body(jd, job_title, company, final_bullets,
                                                 research=blurb, tone=tone,
                                                 background=background, seed=seed,
                                                 **letter_kw)
                cl_tex = tmp_path / "cover_letter.tex"
                cl_res, _ = coverletter.render_cover_letter(body, company, cl_tex, tmp_path)
                if cl_res.ok and cl_res.pdf_path:
                    shutil.copyfile(cl_res.pdf_path, out_dir / output.cover_filename())
                    # Ship the source too (as resume.tex is): a hand fix recompiles
                    # instead of regenerating the letter from scratch.
                    shutil.copyfile(cl_tex, out_dir / output.cover_tex_filename())
                    cover_body = _cover_text(body, company, log, warn=report.advisory)
                else:
                    log(f"cover letter compile failed: {cl_res.error}")
                    report.advisory(f"cover letter compile failed ({cl_res.error})")
            except Exception as exc:  # noqa: BLE001 - cover letter is optional, never fatal
                log(f"cover letter skipped ({exc})")
                report.advisory(f"cover letter skipped ({exc})")
            if letter_kw:
                report.jev_step(jev_assist.STEP_LETTER)

        if prep_sheet:
            log("building interview-prep sheet…")
            report.stage("interview prep")
            try:
                # Same path the "Interview prep" button uses; reads the resume.tex
                # we just wrote into out_dir for the tailored-bullet evidence.
                from .prep import generate_prep_sheet
                generate_prep_sheet(job, out_dir)
                log("interview_prep.md written")
            except Exception as exc:  # noqa: BLE001 - advisory artifact, never fatal
                log(f"interview prep skipped ({exc})")
                report.advisory(f"interview prep skipped ({exc})")

        report.stage("apply sheet")
        try:
            apply_data.write(job, out_dir, sel=sel, bullets=final_bullets,
                             skill_lines=skill_lines, cover_body=cover_body,
                             on_warning=report.advisory)
            log("apply.md written (self-contained apply sheet)")
        except Exception as exc:  # noqa: BLE001 - advisory artifact, never fatal
            log(f"apply sheet skipped ({exc})")
            report.advisory(f"apply sheet skipped ({exc})")

    # After every model call, so a swap the transport learned during this run counts.
    _report_model_swaps(report)
    # The durable record, written last so it carries everything above it.
    _write_report(report, out_dir, job=job, company=company, job_title=job_title, log=log)
    if report.entries:
        log(f"finished with {len(report.entries)} warning(s); see {REPORT_NAME}")
    log(f"done -> {out_dir}")
    for line in report.jev:
        log(line)
    log("token usage: " + llm.usage_summary())
    return out_dir


def generate_cover_letter(
    job: Dict[str, str],
    out_dir: Path,
    *,
    tone: str = "professional",
    on_status: StatusFn = None,
) -> Path:
    """Generate (or regenerate) JUST the cover letter for an already-tailored job.

    Mirrors tailor()'s cover-letter block step for step, but sources the résumé
    bullets from the folder's existing apply.md (written on every tailor,
    deterministic) instead of the in-flight tailoring state — so no re-tailor and
    no extra selection/rephrase calls. Unlike inside tailor() (where the letter is
    an optional extra and failures only log), here the letter IS the job, so
    failures raise. Returns the path of the PDF copied into `out_dir`.
    """
    log = on_status or _noop
    llm.reset_usage()
    jev_assist.reset_usage()

    company = _line_field(job, "company_name") or "Unknown Company"
    job_title = _line_field(job, "job_title") or "Role"
    jd = _job_description_text(job)
    if not pdflatex_available():
        raise RuntimeError(f"pdflatex not found at '{config.PDFLATEX_PATH}'. Install MiKTeX/TeX Live.")
    if len(jd) < 40:
        raise RuntimeError("Job description is empty/too short to tailor against.")

    out_dir = Path(out_dir)
    apply_md = out_dir / "apply.md"
    if not apply_md.exists():
        raise RuntimeError(
            "No apply.md found in this job's tailored folder; re-tailor the job "
            "first, then generate the cover letter.")
    parsed = apply_data.parse_resume_bullets(apply_md.read_text(encoding="utf-8"))
    if not parsed:
        raise RuntimeError(
            "This job's apply.md carries no tailored résumé bullets; re-tailor "
            "the job first, then generate the cover letter.")
    # coverletter.generate_body reads bullets.values() (tailor hands it the
    # group-key -> text dict); synthetic keys keep the same shape + order.
    bullets = {f"b{i}": text for i, text in enumerate(parsed)}

    log(f"writing cover letter for: {job_title} @ {company}")
    blurb = ""
    try:
        log("researching company (grounded search)…")
        blurb = research.company_blurb(company, job_title)
    except Exception as exc:  # noqa: BLE001 - research is optional
        log(f"company research unavailable ({exc})")
    # No selection survives here (the bullets came off the sheet), so the
    # background is the whole master, bounded, and the seed rides as always.
    background, seed = _letter_inputs(None, bullets, log)
    # The cover-letter claims check, as in tailor(): the judge only with the check
    # on, built only then.
    judge = jev_assist.default_judge() if config.cover_letter_jev_check() else None
    letter_kw = {"judge": judge} if judge is not None else {}
    body = coverletter.generate_body(jd, job_title, company, bullets,
                                     research=blurb, tone=tone,
                                     background=background, seed=seed, **letter_kw)
    if letter_kw:
        log(jev_assist.usage_line(jev_assist.STEP_LETTER))

    with tempfile.TemporaryDirectory(prefix="resume_tailor_") as tmp:
        tmp_path = Path(tmp)
        cl_tex = tmp_path / "cover_letter.tex"
        log("rendering + compiling cover letter…")
        cl_res, _ = coverletter.render_cover_letter(body, company, cl_tex, tmp_path)
        if not cl_res.ok or not cl_res.pdf_path:
            raise RuntimeError(f"Cover letter compile failed: {cl_res.error}")
        dest = out_dir / output.cover_filename()
        shutil.copyfile(cl_res.pdf_path, dest)
        shutil.copyfile(cl_tex, out_dir / output.cover_tex_filename())
        # This folder's apply.md already exists (guarded above) and carries the
        # expensive tailored résumé, so the letter is SPLICED in, never rebuilt.
        try:
            apply_data.refresh_cover_letter(out_dir, _cover_text(body, company, log))
            log("cover letter text refreshed in apply.md")
        except Exception as exc:  # noqa: BLE001 - the PDF already landed; advisory
            log(f"apply.md cover-letter section skipped ({exc})")

    log(f"done -> {dest}")
    log("token usage: " + llm.usage_summary())
    return dest


# ── CLI ──────────────────────────────────────────────────────────────────────
_MASTER_NAMES = ("linkedin_jobs_master.csv.gz", "linkedin_jobs_master.csv")


def _default_csv() -> Optional[str]:
    """Resolve the master CSV without baking in a machine-specific drive letter.

    Preference order: the synced Drive root jobsdata discovers from config.json's
    ``gdrive_root`` (machine-independent — the same lookup the dashboard uses),
    then the repo-root master scraper.py writes. Returns None when neither
    resolves, so the CLI can require an explicit ``--csv`` with a clear message
    instead of a confusing file-not-found on a hardcoded path.
    """
    candidates: list[Path] = []
    try:
        local_dir = Path(__file__).resolve().parents[1]  # .../local
        if str(local_dir) not in sys.path:
            sys.path.insert(0, str(local_dir))
        import jobsdata  # local sibling module (unavailable on the bare VM)

        root = jobsdata.gdrive_root_dir([])
        if root:
            candidates += [Path(root) / n for n in _MASTER_NAMES]
    except Exception:  # noqa: BLE001 - jobsdata missing -> fall through to repo root
        pass
    repo_root = Path(__file__).resolve().parents[2]  # repo root
    candidates += [repo_root / n for n in _MASTER_NAMES]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _job_from_csv(job_id: str, csv_path: str) -> Dict[str, str]:
    import pandas as pd

    df = pd.read_csv(csv_path, dtype=str)
    row = df.loc[df["job_posting_id"].astype(str) == str(job_id)]
    if row.empty:
        raise SystemExit(f"job_posting_id {job_id} not found in {csv_path}")
    r = row.iloc[0]
    return {
        "job_posting_id": str(job_id),
        "company_name": r.get("company_name", ""),
        "job_title": r.get("job_title", ""),
        "job_description_formatted": r.get("job_description_formatted", ""),
        "job_description": r.get("job_description", ""),
        "job_summary": r.get("job_summary", ""),
        "url": r.get("url", ""),
    }


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Tailor a resume for one scraped job.")
    ap.add_argument("--job-id", required=True, help="job_posting_id from the master CSV")
    ap.add_argument("--csv", default=None,
                    help="path to the master CSV(.gz); auto-resolved from your "
                         "synced Drive / repo-root master when omitted")
    ap.add_argument("--cover-letter", action="store_true", help="also generate a cover letter")
    ap.add_argument("--ats-report", dest="ats_report", action="store_true", default=True,
                    help="write ats_report.txt keyword coverage (default on)")
    ap.add_argument("--no-ats-report", dest="ats_report", action="store_false",
                    help="skip the ATS keyword-coverage report")
    ap.add_argument("--prep", action="store_true",
                    help="also generate the interview-prep sheet")
    ap.add_argument("--tone", default="professional",
                    choices=("professional", "concise", "enthusiastic", "impactful"),
                    help="tone used when generating the cover letter")
    args = ap.parse_args()

    csv_path = args.csv or _default_csv()
    if not csv_path:
        ap.error("could not locate the master CSV automatically; pass --csv <path> "
                 "(e.g. the linkedin_jobs_master.csv.gz in your synced Drive folder).")
    job = _job_from_csv(args.job_id, csv_path)
    print(f"Tailoring: {job['job_title']} @ {job['company_name']}")
    out = tailor(
        job,
        cover_letter=args.cover_letter,
        ats_report=args.ats_report,
        prep_sheet=args.prep,
        tone=args.tone,
        on_status=lambda m: print("  ·", m),
    )
    print(f"\nOutput: {out}")


if __name__ == "__main__":
    main()
