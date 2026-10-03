"""Calibrate the Jev scorer against the Gemini scores already in the master (VL-2).

    python scripts/jev_score_calibrate.py --live [--cap-usd 1.00] [--sample 400]
                                          [--seed 19] [--master PATH] [--resume PATH]

Samples up to --sample already-scored jobs from the master, stratified by the
Gemini stage 1 score (an equal share per score, a small score's leftover shared
out among the rest), and runs `jev_score.stage1` on each, plus `jev_score.stage2`
wherever Gemini ran stage 2 (no requirement-line minimum). The facts and the
text are the ones `score_jobs.py` gives Jev: `score_jobs.jev_facts` and the
markdown of the formatted description (the job summary when the retention
prune blanked it). The candidate is the default one (`jev_score.DEFAULT_PROFILE`:
finished school, no clearance) whatever the user's settings say, so the code
facts and the request text describe the same candidate and the replay cache
keeps its keys.

A row Jev itself scored is left out of the sample: by its reason's prefix (a
`jev_score.SCORE_LABELS` label and a colon, or the earlier "Skills fit "), and
by an `extracted_date` on or after 2026-09-28 (the day Jev scoring began) when
the column is present and the date parses; a missing or unparseable date stays
eligible.

Every live request goes through `jev.SpendCap` under --cap-usd, and each answer
lands in a replay cache under %LOCALAPPDATA%\\INployed\\jev_calibration\\ (keyed
by a hash of the request; it holds no résumé or job text), so a rerun of the
same sample spends nothing.

It prints aggregates only: the agreement matrix against Gemini, the rank
correlation, the agreement at the stage 2 threshold, the stage 2 agreement, the
10 largest disagreements by job id and title, and the spend. No line of résumé
or job text is printed.

A live run sends the résumé and the sampled job text to TypeSafe and spends
money, so the script refuses to run without --live.

Exit codes: 0 done; 1 a missing master, résumé or judge; 2 refused (no --live,
a bad --cap-usd or --sample); 3 stopped early by the spend cap or an outage
(the aggregates cover the jobs that ran).
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
for _sub in ("pipeline", "local"):
    if str(REPO / _sub) not in sys.path:
        sys.path.insert(0, str(REPO / _sub))

import pandas as pd  # noqa: E402

import jev  # noqa: E402
import jev_score  # noqa: E402
import score_jobs  # noqa: E402

DEFAULT_SAMPLE = 400
DEFAULT_CAP_USD = 1.00
DEFAULT_SEED = 19
# The replay cache (request hashes and Jev's answers) lives outside the repo.
CACHE_DIR = (Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))
             / "INployed" / "jev_calibration")
CACHE_FILE = "jev_cache.json"
# The outage guard's waits between tries (jev.Guarded); tests set () for one try.
RETRY_DELAYS_S: tuple[float, ...] = jev.RETRY_DELAYS_S
TOP_DISAGREEMENTS = 10
TITLE_CHARS = 80
MIN_TEXT_CHARS = 200            # a shorter description or summary is no job text to judge
CHUNK = 2000
SCORES = (1, 2, 3, 4, 5)
RECOMMENDATIONS = ("apply", "consider", "skip")
# A row whose reason Jev composed: Jev against Jev says nothing. `jev_score.compose_stage1`
# writes "<SCORE_LABELS label>: <text>."; "Skills fit " is the prefix the earlier Jev
# scorer wrote, and rows carrying it are still in the master.
JEV_REASON_PREFIXES: tuple[str, ...] = ("Skills fit ", *(
    f"{label}: " for label in jev_score.SCORE_LABELS.values()))
# The day Jev scoring began; a row scored on or after this date is Jev against Jev too,
# even once the reason has been through the writer and no longer carries a prefix above.
JEV_START_DATE = "2026-09-28"
_TRUE = ("true", "1", "1.0", "yes")
_TITLE_COLS = ("job_title", "job_posting_title", "title")
# (column, text source): the formatted description first, as score_jobs reads it
_TEXT_COLS = (("job_description_formatted", "formatted"), ("job_description", "formatted"),
              ("job_summary", "summary"))
_COLUMNS = frozenset({"job_posting_id", "score", "deep_score", "recommendation", "reason",
                      "filtered_out", "score_reused", "extracted_date", *_TITLE_COLS,
                      *(col for col, _source in _TEXT_COLS)})


def live_judge() -> Any:
    """The live judge (`jev.TypeSafeJev`); tests put `jev.DryRun(jev.FakeJev())` here."""
    return jev.TypeSafeJev()


@dataclass
class Job:
    job_id: str
    title: str
    gemini: int
    gemini_deep: int | None
    gemini_rec: str
    source: str
    text: str
    jev: int | None = None
    jev_deep: int | None = None
    jev_rec: str = ""
    stage2: str = ""        # "compared" or "no_answer"


# --- reading the master ----------------------------------------------------------------

def _flag(value: Any) -> bool:
    return str(value).strip().lower() in _TRUE


def _whole(value: Any, low: int, high: int) -> int | None:
    """`value` as a whole number from low to high, else None."""
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    if not math.isfinite(number) or number != int(number) or not low <= number <= high:
        return None
    return int(number)


def _row_text(row: dict) -> tuple[str, str]:
    """(text source, raw text) for a row, or ("", "") when it has none long enough."""
    for col, source in _TEXT_COLS:
        raw = str(row.get(col, "") or "")
        if len(raw.strip()) >= MIN_TEXT_CHARS:
            return source, raw
    return "", ""


def _jev_scored_by_date(row: dict) -> bool:
    """Whether `row`'s `extracted_date` falls on or after JEV_START_DATE. A
    missing or unparseable date reads as False (stays eligible). Parsed as
    UTC so a date carrying a timezone offset compares against the naive
    start date without raising."""
    raw = row.get("extracted_date", "")
    if not str(raw or "").strip():
        return False
    parsed = pd.to_datetime(raw, format="mixed", errors="coerce", utc=True)
    if pd.isna(parsed):
        return False
    return parsed >= pd.Timestamp(JEV_START_DATE, tz="UTC")


def eligible(row: dict) -> tuple[int, str] | None:
    """(the Gemini stage 1 score, the text source) for a scraped job Gemini
    scored that still has job text, else None. Hand-added rows, filtered rows,
    reused scores and rows Jev itself scored (by a JEV_REASON_PREFIXES prefix or by
    an extracted_date on or after JEV_START_DATE) are left out."""
    job_id = str(row.get("job_posting_id", "") or "").strip()
    if not job_id or job_id.startswith(score_jobs.MANUAL_ID_PREFIX):
        return None
    if _flag(row.get("filtered_out", "")) or _flag(row.get("score_reused", "")):
        return None
    if str(row.get("reason", "") or "").startswith(("ERROR:", *JEV_REASON_PREFIXES)):
        return None
    if _jev_scored_by_date(row):
        return None
    score = _whole(row.get("score", ""), 1, 5)
    source, _raw = _row_text(row)
    if score is None or not source:
        return None
    return score, source


def _rows(master: Path):
    for chunk in pd.read_csv(master, dtype=str, keep_default_na=False,
                             usecols=lambda col: col in _COLUMNS, chunksize=CHUNK):
        yield from chunk.to_dict("records")


def eligible_ids(master: Path) -> dict[int, list[str]]:
    """Every usable job id, by its Gemini stage 1 score (the first row of an id wins)."""
    strata: dict[int, list[str]] = {score: [] for score in SCORES}
    seen: set[str] = set()
    for row in _rows(master):
        got = eligible(row)
        job_id = str(row.get("job_posting_id", "") or "").strip()
        if got is None or job_id in seen:
            continue
        seen.add(job_id)
        strata[got[0]].append(job_id)
    return strata


def _rank(seed: int, job_id: str) -> str:
    return hashlib.sha256(f"{seed}:{job_id}".encode("utf-8")).hexdigest()


def stratified_sample(strata: dict[int, list[str]], n: int, seed: int) -> list[str]:
    """Up to `n` ids: an equal share per score, and a score with too few ids
    leaves its share to the others. Within a score the ids with the lowest
    hash of the seed and the id come first, so a rerun draws the same sample
    (and reads its answers from the cache), and a master that gained jobs
    since changes it only where a new id ranks among the lowest. Ordered round
    robin across the scores, so a run the cap stops early still covers every
    score."""
    pools = {score: sorted(ids, key=lambda job_id: _rank(seed, job_id))
             for score, ids in strata.items() if ids}
    take = dict.fromkeys(pools, 0)
    left = min(n, sum(len(ids) for ids in pools.values()))
    while left > 0:
        open_scores = [s for s in sorted(pools) if take[s] < len(pools[s])]
        share = max(1, left // len(open_scores))
        for score in open_scores:
            add = min(share, len(pools[score]) - take[score], left)
            take[score] += add
            left -= add
            if not left:
                break
    picked = {score: pools[score][:take[score]] for score in pools}
    order: list[str] = []
    for i in range(max((len(ids) for ids in picked.values()), default=0)):
        order.extend(ids[i] for _score, ids in sorted(picked.items()) if i < len(ids))
    return order


def load_jobs(master: Path, ids: list[str]) -> list[Job]:
    """The sampled jobs with their text, in `ids` order."""
    want = set(ids)
    found: dict[str, Job] = {}
    for row in _rows(master):
        job_id = str(row.get("job_posting_id", "") or "").strip()
        if job_id not in want or job_id in found:
            continue
        got = eligible(row)
        if got is None:
            continue
        score, source = got
        _source, raw = _row_text(row)
        text = score_jobs.html_to_md(raw) if source == "formatted" else raw.strip()
        title = next((str(row[col]) for col in _TITLE_COLS if str(row.get(col) or "").strip()), "")
        rec = str(row.get("recommendation", "") or "").strip().lower()
        found[job_id] = Job(job_id=job_id, title=" ".join(title.split()), gemini=score,
                            gemini_deep=_whole(row.get("deep_score", ""), 1, 10),
                            gemini_rec=rec if rec in RECOMMENDATIONS else "",
                            source=source, text=text)
    return [found[job_id] for job_id in ids if job_id in found]


# --- the judge -------------------------------------------------------------------------

class CapAsOutage:
    """The spend cap's refusal read as an outage (`jev.JudgeOutage`): jev_score
    stays quiet about it and this run reports the cap once."""

    def __init__(self, inner: Any):
        self.inner = inner

    def judge(self, state: Any, questions: dict[str, dict]) -> dict:
        try:
            return self.inner.judge(state, questions)
        except jev.SpendCapReached as e:
            raise jev.JudgeOutage("SpendCapReached") from e


def build_judge(inner: Any, cap_usd: float, cache_path: Path):
    """(the judge the run asks: the replay cache, the outage guard, the spend cap).
    The cache sits outermost, so a cached request spends nothing and never
    counts toward the cap."""
    cap = jev.SpendCap(inner, cap_usd)
    guarded = jev.Guarded(CapAsOutage(cap), sleep=time.sleep, delays=RETRY_DELAYS_S)
    return jev.ReplayJev(guarded, cache_path), guarded, cap


def run(jobs: list[Job], judge: Any, resume: str, stop) -> tuple[int, str]:
    """Jev's stage 1 on each job and its stage 2 where Gemini ran one.
    (jobs run, why the run stopped early or "")."""
    # The jobs carry no profile, so their requests ask about the default candidate
    # (jev_score.candidate_for(None)); the facts read that candidate too.
    profile = score_jobs.candidate_profile(**jev_score.DEFAULT_PROFILE)
    done = 0
    for job in jobs:
        why = stop()
        if why:
            return done, why
        got = jev_score.stage1(
            judge, {"md": job.text, "facts": score_jobs.jev_facts(job.text, profile)}, resume)
        if got is None:
            why = stop()
            if why:
                return done, why
            done += 1
            continue
        job.jev = int(got["score"])
        done += 1
        if job.gemini_deep is None:
            continue
        deep = jev_score.stage2(judge, {"md": job.text}, resume)
        if deep is None:
            why = stop()
            if why:
                return done, why
            job.stage2 = "no_answer"
            continue
        job.jev_deep = int(deep["deep_score"])
        job.jev_rec = str(deep["recommendation"])
        job.stage2 = "compared"
    return done, ""


# --- the aggregates --------------------------------------------------------------------

def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman's rank correlation (average ranks for ties), or None when either
    side has fewer than two distinct values."""
    if len(xs) < 2 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    rx = pd.Series(xs, dtype=float).rank(method="average")
    ry = pd.Series(ys, dtype=float).rank(method="average")
    value = float(rx.corr(ry))
    return value if math.isfinite(value) else None


def _pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "n/a"


def _rho(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


def report(jobs: list[Job], *, threshold: int, strata: dict[int, list[str]],
           sampled: int, seed: int, done: int, why: str) -> list[str]:
    """The aggregate lines. Job ids and titles are the only row data they carry."""
    lines = ["Eligible jobs by Gemini stage 1 score: "
             + ", ".join(f"{s}: {len(strata.get(s, []))}" for s in SCORES)]
    by_score = {s: sum(1 for j in jobs if j.gemini == s) for s in SCORES}
    lines.append(f"Sampled {sampled} (seed {seed}): "
                 + ", ".join(f"{s}: {by_score[s]}" for s in SCORES))
    sources = {src: sum(1 for j in jobs if j.source == src) for src in ("formatted", "summary")}
    lines.append(f"Job text: {sources['formatted']} from the formatted description, "
                 f"{sources['summary']} from the job summary")
    if why:
        lines.append(f"Stopped after {done} of {sampled} jobs: {why}.")
    both = [j for j in jobs if j.jev is not None]
    lines.append(f"Jev scored {len(both)} of {done} jobs run "
                 f"({done - len(both)} came back with no score).")
    if not both:
        lines.append("No job has both scores, so there is nothing to compare.")
        return lines

    lines.append("")
    lines.append(f"Stage 1 agreement, {len(both)} jobs (rows Gemini, columns Jev):")
    lines.append(" " * len("Gemini 1 ") + "".join(f"  Jev {s}" for s in SCORES))
    for g in SCORES:
        row = [sum(1 for j in both if j.gemini == g and j.jev == s) for s in SCORES]
        lines.append(f"Gemini {g} " + "".join(f"{n:7d}" for n in row))
    exact = sum(1 for j in both if j.gemini == j.jev)
    near = sum(1 for j in both if abs(j.gemini - j.jev) <= 1)
    rho = spearman([j.gemini for j in both], [j.jev for j in both])
    lines.append(f"Exact {_pct(exact, len(both))}, within one {_pct(near, len(both))}, "
                 f"Spearman rank correlation {_rho(rho)}")
    gem_pass = {j.job_id for j in both if j.gemini >= threshold}
    jev_pass = {j.job_id for j in both if j.jev >= threshold}
    agree = sum(1 for j in both if (j.job_id in gem_pass) == (j.job_id in jev_pass))
    lines.append(f"At the stage 2 threshold (score {threshold} or more): agree "
                 f"{_pct(agree, len(both))} (both pass {len(gem_pass & jev_pass)}, "
                 f"Gemini only {len(gem_pass - jev_pass)}, Jev only {len(jev_pass - gem_pass)}, "
                 f"neither {len(both) - len(gem_pass | jev_pass)})")

    ran = [j for j in both if j.gemini_deep is not None]
    deep = [j for j in ran if j.stage2 == "compared"]
    lines.append("")
    lines.append(f"Stage 2 where Gemini ran it: {len(ran)} jobs, {len(deep)} compared, "
                 f"{len(ran) - len(deep)} with no answer")
    if deep:
        gap = sum(abs(j.gemini_deep - j.jev_deep) for j in deep) / len(deep)
        rho2 = spearman([j.gemini_deep for j in deep], [j.jev_deep for j in deep])
        recs = [j for j in deep if j.gemini_rec]
        same = sum(1 for j in recs if j.gemini_rec == j.jev_rec)
        lines.append(f"Deep score: Spearman {_rho(rho2)}, mean gap {gap:.2f}; "
                     f"recommendation agrees {_pct(same, len(recs))} of {len(recs)}")

    def gap_of(j: Job) -> tuple[int, int]:
        deep_gap = abs(j.gemini_deep - j.jev_deep) if j.stage2 == "compared" else 0
        return abs(j.gemini - j.jev), deep_gap

    worst = sorted((j for j in both if gap_of(j) != (0, 0)),
                   key=lambda j: (-gap_of(j)[0], -gap_of(j)[1], j.job_id))[:TOP_DISAGREEMENTS]
    lines.append("")
    if not worst:
        lines.append("No disagreements.")
        return lines
    lines.append(f"Largest disagreements ({len(worst)}): job id, title, Gemini and Jev")
    for j in worst:
        extra = (f"; deep {j.gemini_deep} and {j.jev_deep}" if j.stage2 == "compared" else "")
        lines.append(f"  {j.job_id}  {j.title[:TITLE_CHARS]}  stage 1 {j.gemini} and {j.jev}"
                     f"{extra}")
    return lines


# --- main ------------------------------------------------------------------------------

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Calibrate the Jev scorer against the Gemini "
                                             "scores in the master (prints aggregates only).")
    ap.add_argument("--live", action="store_true",
                    help="required: sends the résumé and the sampled job text to TypeSafe "
                         "and spends money")
    ap.add_argument("--cap-usd", default=str(DEFAULT_CAP_USD),
                    help="the spend cap for this run's live requests, in USD (default 1.00)")
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                    help="how many scored jobs to sample (default 400)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="the sample's seed")
    ap.add_argument("--master", default=None, help="the master CSV (default: the repo's)")
    ap.add_argument("--resume", default=None, help="resume.md (default: the repo's)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    if not args.live:
        print("Refusing to run without --live: a live run sends the résumé and the sampled "
              "job text to TypeSafe and spends up to --cap-usd (default 1.00 USD).")
        return 2
    try:
        cap_usd = jev.checked_cap(args.cap_usd, source="--cap-usd")
    except ValueError as e:
        print(f"Refusing to run: {e}")
        return 2
    if args.sample < 1:
        print(f"Refusing to run: --sample={args.sample}: expected 1 or more")
        return 2
    master = Path(args.master) if args.master else score_jobs.MASTER_CSV
    resume_path = Path(args.resume) if args.resume else score_jobs.RESUME_PATH
    if not master.is_file():
        print(f"No master CSV at {master}; pass --master.")
        return 1
    if not resume_path.is_file():
        print(f"No résumé at {resume_path}; pass --resume.")
        return 1
    resume = resume_path.read_text(encoding="utf-8-sig")
    if not resume.strip():
        print(f"The résumé at {resume_path} is empty.")
        return 1

    strata = eligible_ids(master)
    ids = stratified_sample(strata, args.sample, args.seed)
    jobs = load_jobs(master, ids)
    if not jobs:
        print("No scored job in the master can be sampled.")
        return 1
    try:
        inner = live_judge()
    except jev.JevUnavailable as e:
        print(f"Jev is unavailable: {e}")
        return 1
    replay, guarded, cap = build_judge(inner, cap_usd, CACHE_DIR / CACHE_FILE)

    def stop() -> str:
        if cap.reached:
            return f"the spend cap ({cap.cap_usd:.4f} USD) was reached"
        if guarded.down:
            return f"Jev is unavailable ({guarded.down})"
        return ""

    print(f"Running Jev on {len(jobs)} sampled jobs (spend cap {cap_usd:.4f} USD) ...")
    done, why = run(jobs, replay, resume, stop)
    for line in report(jobs, threshold=score_jobs.STAGE2_THRESHOLD, strata=strata,
                       sampled=len(jobs), seed=args.seed, done=done, why=why):
        print(line)
    print("")
    print(f"Spend: {cap.requests} live request(s), {cap.spent_usd:.4f} USD of the "
          f"{cap.cap_usd:.4f} USD cap; cache {replay.hits} hit(s), {replay.misses} miss(es).")
    return 3 if why else 0


if __name__ == "__main__":
    raise SystemExit(main())
