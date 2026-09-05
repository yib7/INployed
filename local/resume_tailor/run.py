"""Orchestrate the tailor pipeline and expose a single entry point + CLI.

    tailor(job, cover_letter=False, ats_report=True, prep_sheet=False,
           tone="professional", on_status=None) -> Path (output directory)

job is a dict with: company_name, job_title, job_summary, url (job_posting_id optional).

CLI:  python -m resume_tailor.run --job-id <id> [--cover-letter]
        [--no-ats-report] [--prep] [--tone <tone>] [--csv <path>]
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

from . import (apply_data, ats, compose, config, coverletter, llm, measure, output,
               research, verify)
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

# The NOTE taxonomy — same line shape ("<kind>: <message>"), one severity down.
# A note records something the run could not fully deliver but that leaves a
# correct, shippable résumé, so it is written to the report and NOT streamed to
# `on_warning`. That callback is what the dashboard's batch summary calls a
# degraded run; putting cosmetic findings on it would make "finished with
# warnings" mean nothing (see DECISIONS, SP4).
#   UNDERFULL — a bullet the underfull fill rewrote is still underfull after the
#               re-trim: the fill was paid for and the trim took it back.
KIND_UNDERFULL = "underfull"


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
    """
    on_warning: WarnFn = None
    stages: List[str] = field(default_factory=list)
    entries: List[Tuple[str, str]] = field(default_factory=list)
    notes: List[Tuple[str, str]] = field(default_factory=list)
    pages: int = 0

    def stage(self, name: str) -> None:
        self.stages.append(name)

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


def _report_text(rep: RunLog, *, job: Dict[str, str], company: str, job_title: str,
                 out_dir: Path) -> str:
    """Render `tailor_report.txt`: the run's stages, its warnings, its notes, its
    page count. Notes sit directly under warnings, one severity down: same
    `<kind>: <message>` line shape, but a note never made the run degraded."""
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


def _field(job: Dict[str, str], key: str) -> str:
    """String getter that coerces NaN floats / None (pandas rows) to ''."""
    v = job.get(key)
    if not isinstance(v, str):
        return ""
    s = v.strip()
    return "" if s.lower() in ("nan", "none") else s


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
    """The richest available JD text: full description first, summary last.

    LinkedIn's job_summary is often truncated or empty, so tailoring against it
    alone wastes most of the JD signal (and hard-fails when it's blank).
    """
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
    """
    jd: str
    job_title: str
    sel: Dict[str, Any]
    bullets: Dict[str, str]
    verbatim: Dict[str, str]
    reserved: FrozenSet[str]
    log: Callable[[str], None]
    report: Optional[RunLog] = None


def _always() -> bool:
    return True


@dataclass(frozen=True)
class Pass:
    """One bullet-mutating stage, plus how the driver has to bracket it.

    name    — human-readable stage label (SP3 keys its per-run report off this).
    run     — mutates `ctx.bullets` in place.
    enabled — consulted once per run, so a config toggle stays live.
    retrim  — re-run `_trim_to_caps` after the pass (for a pass that lengthens text).
    verify  — snapshot before the pass and re-run the grounding gate after it, with
              that snapshot as the revert target. False only for a pass that cannot
              un-ground anything (the verbatim merge folds in the user's own text).
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
          fallback: Optional[Dict[str, str]] = None) -> Dict[str, List[str]]:
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

    `enforce_grounded` itself is deliberately untouched — the suite pins its exact
    signature `(sel, bullets, *, fallback=None, log=None)` — so this reads the return
    value rather than taking a callback.
    """
    handled = verify.enforce_grounded(ctx.sel, ctx.bullets, fallback=fallback, log=ctx.log)
    if handled and ctx.report is not None:
        for gkey, tokens in handled.items():
            action = "reverted" if gkey in ctx.bullets else "dropped"
            ctx.report.warn(
                KIND_GROUNDING,
                f"[{stage}] {action} bullet '{gkey}' (ungrounded: {', '.join(tokens)})")
    return handled


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


def _pass_dedupe_verbs(ctx: PassCtx) -> None:
    """Guarantee every tailored bullet opens with a DISTINCT action verb — none reused,
    none colliding with a verbatim block's opener (verbatim text is reserved, never
    modified)."""
    compose.dedupe_leading_verbs(ctx.bullets, compose.group_map(ctx.sel), ctx.jd,
                                 reserved=ctx.reserved)


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
    (the trim can hand back exactly what the fill was paid to grow)."""
    ctx.log("filling underfull bullets from spare atoms…")
    compose.fill_underfull(ctx.jd, ctx.job_title, ctx.sel, ctx.bullets)


def _pass_enforce_style(ctx: PassCtx) -> None:
    """Deterministic style gate: banned AI-tell phrasing (em dashes, contrast framing,
    buzzword verbs, ...) never reaches the page.

    `retrim=True` because the repair REWRITES a bullet: the prompt asks it to stay
    within `max_chars`, but nothing verified that, and this is the LAST bullet pass —
    downstream `compile.enforce_one_page` only drops whole bullets, it never re-trims
    text. So a repair that came back longer than the text it replaced used to ship
    over its line budget and silently wrap onto an extra line."""
    fixed = compose.enforce_style(ctx.jd, ctx.job_title, ctx.sel, ctx.bullets)
    if fixed:
        ctx.log(f"style gate: repaired {fixed} bullet(s).")


# The order of this tuple IS the pipeline: it transcribes the sequence tailor() used to
# spell out inline, and reordering it is now the only way to reorder the stages.
# `enabled` holds the toggle FUNCTION rather than its value, so it is read per run.
_BULLET_PASSES = (
    Pass("verb dedupe", _pass_dedupe_verbs),
    Pass("verbatim + trim", _pass_merge_verbatim, verify=False),
    Pass("underfull fill", _pass_fill_underfull,
         enabled=config.fill_underfull_enabled, retrim=True, recheck_fill=True),
    Pass("style gate", _pass_enforce_style, retrim=True),
)


def _run_bullet_passes(ctx: PassCtx,
                       passes: Sequence[Pass] = _BULLET_PASSES) -> None:
    """Run each enabled pass under the snapshot -> mutate -> re-trim -> re-verify ->
    (optionally) re-measure discipline. Nobody writes a snapshot by hand, so nobody can
    forget one."""
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
    apply.md) failing. Optional, defaulting to None, so no existing call site
    changes; the same warnings are written to `tailor_report.txt` in the output
    folder either way, which is the copy that outlives a status bar.

    A finding that leaves a correct, shippable résumé — a bullet the underfull fill
    could not keep full once it was re-trimmed — is a NOTE instead: report only,
    never on `on_warning`, so it cannot mark the run degraded.
    """
    log = on_status or _noop
    report = RunLog(on_warning=on_warning)
    # Parallel callers reset llm.USAGE once before fan-out and pass reset_usage=False,
    # so concurrent jobs don't clear each other's token accounting (see DECISIONS).
    if reset_usage:
        llm.reset_usage()

    company = _field(job, "company_name") or "Unknown Company"
    job_title = _field(job, "job_title") or "Role"
    jd = _job_description_text(job)
    if not pdflatex_available():
        raise RuntimeError(f"pdflatex not found at '{config.PDFLATEX_PATH}'. Install MiKTeX/TeX Live.")
    if len(jd) < 40:
        raise RuntimeError("Job description is empty/too short to tailor against.")

    log(f"selecting evidence for: {job_title} @ {company}")
    report.stage("select")
    sel = compose.select(jd, job_title, company)
    if not sel.get("experience"):
        raise RuntimeError("Selection returned no experience — aborting (check the JD/model).")

    # Per-block "don't tailor": swap selected verbatim blocks to the user's exact
    # bullets BEFORE rephrase, so the LLM never sees (or rewrites) them.
    verbatim = compose.inject_verbatim(sel)
    # Float each project's overview bullet ("what is this project") to the front so detail
    # bullets don't lead. Runs BEFORE briefs/rephrase, so the cohesion framing and the
    # per-position line budgets build on the corrected order. Projects only; never invents.
    if config.lead_overview_enabled():
        report.stage("lead overview")
        compose.lead_with_overview(jd, job_title, sel)
    # One cheap batched call: a cohesion brief per (non-verbatim) block so its bullets
    # read as one story instead of glued-together atoms.
    log("framing each block for cohesion…")
    report.stage("block briefs")
    briefs = compose.block_briefs(jd, job_title, sel)
    report.stage("rephrase")
    bullets = _resolve_bullets(jd, job_title, sel, log, briefs=briefs)
    ctx = PassCtx(
        jd=jd, job_title=job_title, sel=sel, bullets=bullets, verbatim=verbatim,
        # The verbatim blocks' opening verbs: reserved, because the dedupe pass may
        # not rewrite the user's own text, so it must not reuse their openers either.
        reserved=frozenset(compose.leading_verb(t) for t in verbatim.values()),
        log=log, report=report,
    )
    # Deterministic grounding gate (audit P1-2): every bullet's distinctive tokens
    # must trace to its own group's atoms — a hallucinated or JD-injected fact is
    # dropped here, never printed. This first call is the prologue and the only
    # fallback-less one: there is no earlier grounded text to revert to yet. Every
    # later bullet-mutating pass re-runs it against its own snapshot, reverting
    # instead of dropping when it can (see _run_bullet_passes).
    _gate(ctx, stage="rephrase")
    if not bullets and not verbatim:
        raise RuntimeError("No grounded bullets survived selection/rephrase.")
    # Verb dedupe -> verbatim merge + trim -> underfull fill + re-trim -> style gate.
    _run_bullet_passes(ctx)

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
            try:
                cov = ats.write_report(jd, out_dir / output.resume_filename(), out_dir)
                log(f"ATS keyword coverage: {cov:.0%} (details in ats_report.txt)")
            except Exception as exc:  # noqa: BLE001 - the report is advisory, never fatal
                log(f"ATS check skipped ({exc})")
                report.advisory(f"ATS check skipped ({exc})")

        cover_body = ""      # the paste-ready letter apply.md embeds, when generated
        if cover_letter:
            log("writing cover letter…")
            report.stage("cover letter")
            try:
                blurb = ""
                try:
                    log("researching company (grounded search)…")
                    blurb = research.company_blurb(company, job_title)
                except Exception as exc:  # noqa: BLE001 - research is optional
                    log(f"company research unavailable ({exc})")
                    report.advisory(f"company research unavailable ({exc})")
                body = coverletter.generate_body(jd, job_title, company, final_bullets,
                                                 research=blurb, tone=tone)
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
                             skill_lines=skill_lines, cover_body=cover_body)
            log("apply.md written (self-contained apply sheet)")
        except Exception as exc:  # noqa: BLE001 - advisory artifact, never fatal
            log(f"apply sheet skipped ({exc})")
            report.advisory(f"apply sheet skipped ({exc})")

    # The durable record, written last so it carries everything above it.
    _write_report(report, out_dir, job=job, company=company, job_title=job_title, log=log)
    if report.entries:
        log(f"finished with {len(report.entries)} warning(s); see {REPORT_NAME}")
    log(f"done -> {out_dir}")
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

    company = _field(job, "company_name") or "Unknown Company"
    job_title = _field(job, "job_title") or "Role"
    jd = _job_description_text(job)
    if not pdflatex_available():
        raise RuntimeError(f"pdflatex not found at '{config.PDFLATEX_PATH}'. Install MiKTeX/TeX Live.")
    if len(jd) < 40:
        raise RuntimeError("Job description is empty/too short to tailor against.")

    out_dir = Path(out_dir)
    apply_md = out_dir / "apply.md"
    if not apply_md.exists():
        raise RuntimeError(
            "No apply.md found in this job's tailored folder — re-tailor the job "
            "first, then generate the cover letter.")
    parsed = apply_data.parse_resume_bullets(apply_md.read_text(encoding="utf-8"))
    if not parsed:
        raise RuntimeError(
            "This job's apply.md carries no tailored résumé bullets — re-tailor "
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
    body = coverletter.generate_body(jd, job_title, company, bullets,
                                     research=blurb, tone=tone)

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
