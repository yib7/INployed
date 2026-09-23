"""Print the recorded Jev answers per question kind next to the current
thresholds in `local/apply_judge.py`, for tuning after a record run.

    python scripts/jev_thresholds.py [--cache PATH] [--outcomes PATH] [--judge PATH]

Reads `tests/fixtures/jev_cache/cache.json` (the `ReplayJev` cache: one entry
per request, `{question_id: answer}`) and `outcomes.jsonl` beside it (one line
per runner test, written by the harness in record / replay mode). For every
question kind it prints the count, the choices the model made, the spread of
the gated number (`confidence` for a choice, `noul` for a noul) as a 0.1-wide
histogram, the threshold that gates it and how many answers fall on the wrong
side of it. Then the outcomes: every test's statuses and the divergences.

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

# (kind, question id pattern, the answer field the gate reads, threshold name,
#  "min" when the answer must sit at or above it, "max" when at or below,
#  None for an ungated question)
KINDS = (
    ("page state", re.compile(r"^page_state$"), "confidence", "PAGE_STATE_MIN_CONF", "min"),
    ("field map", re.compile(r"^field_\d+_source$"), "confidence", "FIELD_MAP_MIN_CONF", "min"),
    ("consent", re.compile(r"^field_\d+_source$"), "confidence", "CONSENT_MIN_CONF", "min"),
    ("option", re.compile(r"^field_\d+_(option|pick)$"), "confidence", "OPTION_MIN_CONF", "min"),
    ("button submit", re.compile(r"^button_\d+_role$"), "confidence", "BUTTON_SUBMIT_MIN_CONF", "min"),
    ("button advance", re.compile(r"^button_\d+_role$"), "confidence", "BUTTON_ADVANCE_MIN_CONF", "min"),
    ("button other", re.compile(r"^button_\d+_role$"), "confidence", None, None),
    ("verify", re.compile(r"^verify_\d+$"), "noul", "VERIFY_MIN", "min"),
    ("placeholder", re.compile(r"^placeholder_\d+$"), "noul", "PLACEHOLDER_MAX", "max"),
    # recorded only since 2026-09-22: neither flag parks a job on its own
    ("prohibited", re.compile(r"^asks_for_prohibited$"), "noul", None, None),
    ("captcha", re.compile(r"^has_captcha$"), "noul", None, None),
    ("requires account", re.compile(r"^requires_account$"), "noul", None, None),
    ("inbox", re.compile(r"^msg_\d+_(from_site|has_code)$"), "noul", "INBOX_MIN", "min"),
    ("code pick", re.compile(r"^code_pick$"), "confidence", None, None),
    ("grounding", re.compile(r"^grounded_\d+$"), "noul", "GROUNDING_MIN", "min"),
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


def load_outcomes(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def histogram(values: list[float]) -> str:
    buckets = Counter(min(int(v * 10), 9) for v in values)
    return " ".join(f"{b / 10:.1f}:{buckets[b]}" for b in range(10) if buckets[b])


def report(cache: dict, outcomes: list[dict], thresholds: dict[str, float],
           untuned: bool) -> list[str]:
    by_kind: dict[str, list[dict]] = {kind: [] for kind, *_ in KINDS}
    by_kind["other"] = []
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        for qid, answer in entry.items():
            by_kind[kind_of(qid, answer)].append(answer)

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
            lines.append(f"  {field_name}: min {min(values):.2f}, median "
                         f"{statistics.median(values):.2f}, max {max(values):.2f}; "
                         f"histogram {histogram(values)}")
            if thr is not None:
                wrong = (sum(1 for v in values if v < thr) if side == "min"
                         else sum(1 for v in values if v > thr))
                word = "below" if side == "min" else "above"
                lines.append(f"  {wrong} of {len(values)} {word} the gate")
    if by_kind["other"]:
        lines.append(f"\nother: {len(by_kind['other'])} answer(s) with an unrecognised id")

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
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--outcomes", type=Path, default=None,
                    help="default: outcomes.jsonl beside the cache")
    ap.add_argument("--judge", type=Path, default=DEFAULT_JUDGE)
    args = ap.parse_args(argv)
    outcomes = args.outcomes or args.cache.parent / "outcomes.jsonl"
    thresholds, untuned = read_thresholds(args.judge)
    for line in report(load_cache(args.cache), load_outcomes(outcomes), thresholds, untuned):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
