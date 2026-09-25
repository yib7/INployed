"""Print the recorded Jev answers per question kind next to the current
thresholds in `local/apply_judge.py`, for tuning after a record run.

    python scripts/jev_thresholds.py [--cache PATH ...] [--outcomes PATH | --no-outcomes]
                                     [--reads PATH ...] [--judge PATH]

Reads each `--cache` (a `ReplayJev` cache: one entry per request,
`{question_id: answer}`; default `tests/fixtures/jev_cache/cache.json`) and
`outcomes.jsonl` beside the first (one line per runner test, written by the
harness in record / replay mode; `--no-outcomes` reads none). For every
question kind it prints the count, the choices the model made, the spread of
the gated number (`confidence` for a choice, `noul` for a noul) as a 0.1-wide
histogram, the threshold that gates it and how many answers fall on the wrong
side of it (for a yes/no split, how many fall on each side). Then the
combined page reads (`--reads`: an `apply_matrix.py --json` file, whose runs
carry the reads of their traced pages, or the captures' `results.json`):
the read's confidence per kind, which `PAGE_STATE_MIN_CONF` gates. Then the
outcomes: every test's statuses and the divergences.

Standard library only; the thresholds are read from the source with `ast`, so
nothing under `local/` is imported.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CACHE = REPO / "tests" / "fixtures" / "jev_cache" / "cache.json"
DEFAULT_JUDGE = REPO / "local" / "apply_judge.py"

# the page read's Nouls (`apply_judge.READ_NOUL_IDS`), each its own kind
READ_NOULS = ("page_job_description", "page_apply_entry", "page_applicant_details",
              "page_sign_in", "page_create_account", "page_received", "page_already_applied",
              "page_closed", "page_code", "page_payment", "page_review", "page_error",
              "has_captcha")

# (kind, question id pattern, the answer field the gate reads, threshold name,
#  "min" when the answer must sit at or above it, "max" when at or below,
#  "split" for a yes/no line, None for an ungated question)
KINDS = (
    ("page state", re.compile(r"^page_state$"), "confidence", "PAGE_STATE_MIN_CONF", "min"),
    *((f"read {q}", re.compile(rf"^{q}$"), "noul", "READ_NOUL_MIN", "split")
      for q in READ_NOULS),
    ("field map", re.compile(r"^field_\d+_source$"), "confidence", "FIELD_MAP_MIN_CONF", "min"),
    ("consent", re.compile(r"^field_\d+_source$"), "confidence", "CONSENT_MIN_CONF", "min"),
    ("option", re.compile(r"^field_\d+_(option|pick)$"), "confidence", "OPTION_MIN_CONF", "min"),
    ("button submit", re.compile(r"^button_\d+_role$"), "confidence", "BUTTON_SUBMIT_MIN_CONF",
     "min"),
    ("button advance", re.compile(r"^button_\d+_role$"), "confidence", "BUTTON_ADVANCE_MIN_CONF",
     "min"),
    ("button other", re.compile(r"^button_\d+_role$"), "confidence", None, None),
    ("button sends", re.compile(r"^button_\d+_sends$"), "noul", "BUTTON_SENDS_MIN", "split"),
    ("error field", re.compile(r"^error_\d+_field$"), "confidence", "FIELD_MAP_MIN_CONF", "min"),
    ("verify", re.compile(r"^verify_\d+$"), "noul", "VERIFY_MIN", "min"),
    ("placeholder", re.compile(r"^placeholder_\d+$"), "noul", "PLACEHOLDER_MAX", "max"),
    # recorded only since 2026-09-22: the flag never parks a job on its own
    ("prohibited", re.compile(r"^asks_for_prohibited$"), "noul", None, None),
    ("inbox", re.compile(r"^msg_\d+_(from_site|has_code|has_link)$"), "noul", "INBOX_MIN",
     "split"),
    ("link pick", re.compile(r"^link_pick$"), "confidence", None, None),
    ("code pick", re.compile(r"^code_pick$"), "confidence", None, None),
    ("grounding", re.compile(r"^grounded_\d+$"), "noul", "GROUNDING_MIN", "split"),
)


def kind_of(qid: str, answer: dict) -> str:
    """The kind a question id belongs to; the source and button choices split
    by what the model chose (a consent pick, a submit or advance role)."""
    choice = answer.get("choice")
    if re.match(r"^field_\d+_source$", qid):
        return "consent" if choice == "consent_attest" else "field map"
    if re.match(r"^button_\d+_role$", qid):
        return {"submit": "button submit", "advance": "button advance"}.get(choice, "button other")
    for kind, pattern, _field, _thr, _side in KINDS:
        if pattern.match(qid):
            return kind
    return "other"


def read_thresholds(judge_path: Path) -> tuple[dict[str, float], bool]:
    """({NAME: value} for every module-level numeric constant, whether the
    header still says UNTUNED)."""
    text = judge_path.read_text(encoding="utf-8")
    values: dict[str, float] = {}
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if (isinstance(target, ast.Name) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, (int, float))
                    and not isinstance(node.value.value, bool)):
                values[target.id] = float(node.value.value)
    return values, "UNTUNED" in text


def load_cache(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_outcomes(path: Path | None) -> list[dict]:
    if path is None or not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def load_reads(path: Path) -> list[dict]:
    """The combined page reads in a matrix `--json` file (each run's `reads`,
    with its flow and judge) or a captures `results.json` (each capture's
    read and its labels)."""
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict] = []
    if isinstance(data, dict) and "reads" in data:           # the captures' results
        for r in data["reads"]:
            rows.append({"source": r.get("capture"), "state": r.get("read"),
                         "conf": r.get("read_conf"), "judged": r.get("judged"),
                         "judged_conf": r.get("judged_conf"),
                         "right": r.get("read") in (r.get("expected") or ())})
        return rows
    runs = data.get("results", []) if isinstance(data, dict) else data
    for run in runs:
        for page in run.get("reads") or ():
            rows.append({"source": f"{run.get('flow')} {run.get('judge')} p{page.get('n')}",
                         "state": page.get("read") or page.get("state"),
                         "conf": page.get("read_conf", page.get("conf")),
                         "judged": page.get("judged"), "judged_conf": page.get("judged_conf"),
                         "right": None})
    return rows


def histogram(values: list[float]) -> str:
    buckets = Counter(min(int(v * 10), 9) for v in values)
    return " ".join(f"{b / 10:.1f}:{buckets[b]}" for b in range(10) if buckets[b])


def _spread(values: list[float]) -> str:
    return (f"min {min(values):.2f}, median {statistics.median(values):.2f}, max "
            f"{max(values):.2f}; histogram {histogram(values)}")


def report(cache: dict, outcomes: list[dict], thresholds: dict[str, float],
           untuned: bool, reads: list[dict] | None = None) -> list[str]:
    by_kind: dict[str, list[dict]] = {kind: [] for kind, *_ in KINDS}
    by_kind["other"] = []
    others: Counter = Counter()
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        for qid, answer in entry.items():
            kind = kind_of(qid, answer)
            by_kind[kind].append(answer)
            if kind == "other":
                others[re.sub(r"\d+", "{n}", qid)] += 1

    lines = [f"jev answers: {sum(len(v) for v in by_kind.values())} over "
             f"{len(cache)} request(s); thresholds {'UNTUNED' if untuned else 'tuned'} "
             f"in local/apply_judge.py"]
    for kind, _pattern, field_name, thr_name, side in KINDS:
        answers = by_kind[kind]
        thr = thresholds.get(thr_name) if thr_name else None
        gate = (f"{thr_name} = {thr:.2f}" if thr is not None
                else (f"{thr_name} (missing)" if thr_name else "no gate"))
        lines.append(f"\n{kind}: {len(answers)} answer(s); gate {gate}")
        if not answers:
            lines.append("  no answers recorded")
            continue
        values = [float(a[field_name]) for a in answers if a.get(field_name) is not None]
        choices = Counter(a["choice"] for a in answers if a.get("choice") is not None)
        if choices:
            lines.append("  choices: " + ", ".join(f"{c} x{n}" for c, n in choices.most_common()))
        if values:
            lines.append(f"  {field_name}: {_spread(values)}")
            if thr is not None and side == "split":
                yes = sum(1 for v in values if v >= thr)
                lines.append(f"  {yes} at or above the gate, {len(values) - yes} below")
            elif thr is not None:
                wrong = (sum(1 for v in values if v < thr) if side == "min"
                         else sum(1 for v in values if v > thr))
                word = "below" if side == "min" else "above"
                lines.append(f"  {wrong} of {len(values)} {word} the gate")
    if by_kind["other"]:
        lines.append(f"\nother: {len(by_kind['other'])} answer(s) with an unrecognised id: "
                     + ", ".join(f"{q} x{n}" for q, n in others.most_common()))

    if reads:
        floor = thresholds.get("PAGE_STATE_MIN_CONF")
        lines.append(f"\ncombined page reads: {len(reads)} read(s); gate "
                     + (f"PAGE_STATE_MIN_CONF = {floor:.2f}" if floor is not None
                        else "PAGE_STATE_MIN_CONF (missing)"))
        states: dict[str, list[dict]] = {}
        for r in reads:
            states.setdefault(str(r.get("state")), []).append(r)
        for state in sorted(states):
            rows = states[state]
            confs = [float(r["conf"]) for r in rows if r.get("conf") is not None]
            under = sum(1 for c in confs if floor is not None and c < floor)
            lines.append(f"  {state}: {len(rows)}; " + (_spread(confs) if confs else "no conf")
                         + (f"; {under} under the gate" if floor is not None else ""))
        labelled = [r for r in reads if r.get("right") is not None]
        if labelled:
            right = [float(r["conf"]) for r in labelled if r["right"] and r.get("conf") is not None]
            wrong = [float(r["conf"]) for r in labelled if not r["right"]
                     and r.get("conf") is not None]
            lines.append(f"  against the labels: {len(right)} right"
                         + (f" ({_spread(right)})" if right else "")
                         + f"; {len(wrong)} wrong" + (f" ({_spread(wrong)})" if wrong else ""))
            if floor is not None:
                lines.append(f"  right reads under the gate (a miss): "
                             f"{sum(1 for c in right if c < floor)}; wrong reads at or above it "
                             f"(a false pass): {sum(1 for c in wrong if c >= floor)}")
        moved = [r for r in reads if r.get("judged") and r.get("state") != r.get("judged")]
        lines.append(f"  reads that differ from the judge's own pick: {len(moved)}")
        for r in moved:
            lines.append(f"    {r['source']}: judged {r['judged']} "
                         f"{float(r.get('judged_conf') or 0):.2f}, read {r['state']} "
                         f"{float(r.get('conf') or 0):.2f}")

    lines.append(f"\noutcomes: {len(outcomes)} test(s) recorded")
    results = Counter(r.get("result", "") for r in outcomes)
    if results:
        lines.append("  results: " + ", ".join(f"{k or '?'} x{n}" for k, n in results.most_common()))
    for r in outcomes:
        statuses = ", ".join(f"{o.get('status')} ({o.get('reason')})" for o in r.get("outcomes", []))
        lines.append(f"  {r.get('result', '?'):7} {r.get('test')}"
                     + (f": {statuses}" if statuses else ""))
        if r.get("divergence"):
            lines.append(f"          diverges: {r['divergence']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cache", type=Path, action="append", default=None,
                    help="a replay cache; repeat for several (default the runner tests')")
    ap.add_argument("--outcomes", type=Path, default=None,
                    help="default: outcomes.jsonl beside the first cache")
    ap.add_argument("--no-outcomes", action="store_true", help="read no outcomes file")
    ap.add_argument("--reads", type=Path, action="append", default=[],
                    help="an apply_matrix.py --json file or the captures' results.json")
    ap.add_argument("--judge", type=Path, default=DEFAULT_JUDGE)
    args = ap.parse_args(argv)
    caches = args.cache or [DEFAULT_CACHE]
    cache: dict = {}
    for path in caches:
        cache.update(load_cache(path))
    outcomes = None if args.no_outcomes else (args.outcomes
                                              or caches[0].parent / "outcomes.jsonl")
    reads = [r for path in args.reads for r in load_reads(path)]
    thresholds, untuned = read_thresholds(args.judge)
    for line in report(cache, load_outcomes(outcomes), thresholds, untuned, reads):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
