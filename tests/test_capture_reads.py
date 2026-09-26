"""The page read over the real-page captures under a recorded judge (SP8b).

The captures (`tests/fixtures/local_captures/`, git-excluded) are real
third-party pages and stay on this machine; so do this module's cache and
results, beside them in `_jev/`. `AUTO_APPLY_CAPTURE_JEV` picks the judge:

- unset: every test skips (the suite never reads the captures with a judge);
- `replay`: `ReplayJev(None, cache)`, no key and no network; a miss fails
  the test;
- `record`: `ReplayJev(SpendCap(TypeSafeJev(), cap), cache)`, through
  `scripts/jev_record.ps1 -Target captures` (it holds the key for that one
  process); the cap is `AUTO_APPLY_RECORD_USD_CAP`, and a read the cap stops
  skips with its reason;
- `dry`: the fake answers at each request's estimated size (`jev.DryRun`)
  into a temp copy of the cache, under the same cap: the request count and
  the spend a recording would make, with no key.

Record and dry run serially (one cache, one spend counter): never under
`-n`.

Each capture is read offline with no script running, as
`test_local_captures` reads it, then asked the run's own page-read request
(`apply_judge.read_questions`: the page's text as a real run sends it) and
combined with the page's structure (`apply_judge.read_page`), as
`apply_run._JobRun._read` does. The labels (`_page_kind_expected.json`,
beside the captures) name the kinds a correct read may give, the page's
own first. A read outside them is an xfail naming the misread.
`_jev/results.json` holds every read, and `_jev/summary.txt` the accuracy
per kind and each misread, for the report.
"""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
for sub in ("local", "tests"):
    if str(REPO / sub) not in sys.path:
        sys.path.insert(0, str(REPO / sub))

import apply_judge  # noqa: E402
import apply_run  # noqa: E402
import jev  # noqa: E402
import jev_harness  # noqa: E402
from test_local_captures import CAPTURES, _captures, _snapshot_parts  # noqa: E402

pytest_plugins = ["conftest_browser"]

MODE_ENV = "AUTO_APPLY_CAPTURE_JEV"
MODES = ("record", "replay", "dry")
LABELS = CAPTURES / "_page_kind_expected.json"
JEV_DIR = CAPTURES / "_jev"
CACHE = JEV_DIR / "cache.json"
RESULTS = JEV_DIR / "results.json"
SUMMARY = JEV_DIR / "summary.txt"


def _mode() -> str:
    raw = (os.environ.get(MODE_ENV) or "").strip().lower()
    if raw and raw not in MODES:
        raise ValueError(f"{MODE_ENV}={raw!r}: expected one of {', '.join(MODES)}")
    return raw


def _labels() -> dict[str, list[str]]:
    if not LABELS.is_file():
        return {}
    return {k: list(v) for k, v in json.loads(LABELS.read_text(encoding="utf-8")).items()
            if not k.startswith("_")}


class _Reads:
    """The module's judge and every read it gave, written out at the end."""

    def __init__(self, mode: str, cache: Path = CACHE):
        self.mode = mode
        self.cap: jev.SpendCap | None = None
        if mode == "replay":
            self.cache = cache
            inner = None
        else:
            self.cache = jev_harness.dry_copy(cache) if mode == "dry" else cache
            # a live recording names its cap (refused without it); a dry run
            # has a default
            self.cap = jev.SpendCap(jev.DryRun() if mode == "dry" else jev.TypeSafeJev(),
                                    jev.record_cap(live=mode != "dry"))
            inner = self.cap
        self.judge = jev.ReplayJev(inner, self.cache)
        self.rows: list[dict] = []
        self._before = jev.usage()

    def spend(self) -> dict:
        after = jev.usage()
        tokens = after["input_tokens"] - self._before["input_tokens"]
        return {"requests": after["requests"] - self._before["requests"],
                "input_tokens": tokens, "usd": round(jev.usd_for(tokens), 6)}

    def write(self) -> None:
        JEV_DIR.mkdir(parents=True, exist_ok=True)
        spend = self.spend()
        RESULTS.write_text(json.dumps({"mode": self.mode, "cache": str(self.cache),
                                       "spend": spend, "reads": self.rows}, indent=1,
                                      ensure_ascii=False), encoding="utf-8")
        SUMMARY.write_text(summary(self.rows, self.mode, spend, self.cap), encoding="utf-8")


def summary(rows: list[dict], mode: str, spend: dict, cap=None) -> str:
    """Accuracy per labelled kind (the page's own kind, the first label):
    the judge's own pick and the combined read, each against the labels;
    then each read outside them."""
    kinds: dict[str, list[dict]] = {}
    for r in rows:
        kinds.setdefault(r["expected"][0], []).append(r)
    lines = [f"capture page reads ({mode}): {len(rows)} capture(s); requests "
             f"{spend['requests']}, {spend['input_tokens']} input tokens, {spend['usd']:.4f} USD"
             + (f"; cap stopped: {cap.reason}" if cap is not None and cap.reached else ""),
             "", f"{'kind':<22} {'n':>3} {'judged':>7} {'read':>7}"]
    for kind in sorted(kinds):
        got = kinds[kind]
        judged = sum(1 for r in got if r["judged"] in r["expected"])
        read = sum(1 for r in got if r["read"] in r["expected"])
        lines.append(f"{kind:<22} {len(got):>3} {judged:>3}/{len(got):<3} {read:>3}/{len(got):<3}")
    judged = sum(1 for r in rows if r["judged"] in r["expected"])
    read = sum(1 for r in rows if r["read"] in r["expected"])
    lines.append(f"{'all':<22} {len(rows):>3} {judged:>3}/{len(rows):<3} {read:>3}/{len(rows):<3}")
    lines.append("")
    for r in rows:
        if r["read"] not in r["expected"] or r["judged"] not in r["expected"]:
            lines.append(f"misread: {r['capture']}: expected {'/'.join(r['expected'])}; judged "
                         f"{r['judged']} {r['judged_conf']:.2f}, read {r['read']} "
                         f"{r['read_conf']:.2f}" + (f" ({r['why']})" if r["why"] else ""))
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def reads():
    mode = _mode()
    if not mode:
        pytest.skip(f"set {MODE_ENV}=record|replay|dry to read the captures with a judge")
    if not CAPTURES.is_dir() or not LABELS.is_file():
        pytest.skip("no local captures or labels on this machine")
    if mode != "replay" and os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail(f"{MODE_ENV}={mode} writes one cache and counts one spend: run it "
                    "serially, never under -n")
    try:
        r = _Reads(mode)
    except jev.JevUnavailable as e:
        pytest.skip(str(e))
    yield r
    r.write()


def _load(browser, folder: Path, meta_url: str):
    """(the digest, the page's URL, a bot check showing), read offline with
    no script running, as `test_local_captures._read_capture` reads it."""
    import apply_form
    ctx = browser.new_context(viewport={"width": 1400, "height": 1000}, java_script_enabled=False)
    try:
        mhtml = folder / "snapshot.mhtml"
        if mhtml.is_file() and mhtml.stat().st_size:
            main, served = _snapshot_parts(mhtml)

            def _serve(route):
                hit = served.get(route.request.url)
                if hit is None:
                    route.abort()
                else:
                    route.fulfill(status=200, content_type=hit[0], body=hit[1])
            ctx.route("**/*", _serve)
            page = ctx.new_page()
            page.goto(main, wait_until="load", timeout=30_000)
            url = page.url
        else:
            page = ctx.new_page()
            page.set_content((folder / "page.html").read_text(encoding="utf-8",
                                                               errors="replace"),
                             wait_until="domcontentloaded", timeout=20_000)
            url = meta_url
        page.wait_for_timeout(300)
        d = apply_form.extract(page)
        urls = [str(f.url) for f in apply_form.frames(page)]
        bot = {i for i, u in enumerate(urls) if apply_run._is_captcha_url(u)}
        d.fields = [f for f in d.fields if int(f.locator[0]) not in bot]
        d.buttons = [b for b in d.buttons if int(b.locator[0]) not in bot]
        # the run's own check for a bot check waiting on the page
        showing = apply_run._JobRun._human_check_showing(types.SimpleNamespace(page=page))
        return d, url, showing
    finally:
        ctx.close()


def test_summary_counts_each_kind_and_lists_each_misread():
    # runs in the plain suite: a pure function over synthetic rows
    def row(capture, expected, judged, read, why=""):
        return {"capture": capture, "expected": expected, "judged": judged, "judged_conf": 0.9,
                "read": read, "read_conf": 0.8, "why": why}
    rows = [row("a_posting", ["job_posting"], "job_posting", "job_posting"),
            row("b_posting", ["job_posting", "application_form"], "application_form",
                "application_form"),
            row("c_closed", ["error_or_dead"], "other", "error_or_dead", "no fields"),
            row("d_form", ["application_form"], "job_posting", "job_posting")]
    cap = types.SimpleNamespace(reached=True, reason="cap 0.01 USD")
    text = summary(rows, "replay", {"requests": 4, "input_tokens": 1200, "usd": 0.0000504},
                   cap)
    lines = text.splitlines()
    assert lines[0] == ("capture page reads (replay): 4 capture(s); requests 4, 1200 input "
                        "tokens, 0.0001 USD; cap stopped: cap 0.01 USD")
    by_kind = {ln.split()[0]: ln.split()[1:] for ln in lines[3:] if ln and "/" in ln
               and not ln.startswith("misread")}
    assert by_kind == {"application_form": ["1", "0/1", "0/1"],
                       "error_or_dead": ["1", "0/1", "1/1"],
                       "job_posting": ["2", "2/2", "2/2"], "all": ["4", "2/4", "3/4"]}
    misreads = [ln for ln in lines if ln.startswith("misread")]
    assert misreads == [
        "misread: c_closed: expected error_or_dead; judged other 0.90, read error_or_dead 0.80 "
        "(no fields)",
        "misread: d_form: expected application_form; judged job_posting 0.90, read "
        "job_posting 0.80"]
    assert "cap stopped" not in summary(rows, "dry", {"requests": 0, "input_tokens": 0,
                                                      "usd": 0.0})


def test_the_mode_env_takes_only_a_known_mode(monkeypatch):
    monkeypatch.setenv(MODE_ENV, " Replay ")
    assert _mode() == "replay"
    monkeypatch.delenv(MODE_ENV)
    assert _mode() == ""
    monkeypatch.setenv(MODE_ENV, "live")
    with pytest.raises(ValueError, match="record, replay, dry"):
        _mode()


@pytest.mark.parametrize("capture", _captures())
def test_the_page_read_of_each_capture(reads, _browser, capture):
    labels = _labels()
    if capture not in labels:
        pytest.skip(f"{capture} has no label in {LABELS.name}")
    folder = CAPTURES / capture
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    digest, url, showing = _load(_browser, folder, str(meta.get("url") or ""))
    # `_JobRun._read`: the read's own request, then the page's structure
    facts = apply_judge.page_facts(digest, url, captcha_frame=showing)
    state, questions = apply_judge.read_questions(digest, url)
    try:
        raw = dict(reads.judge.judge(state, questions))
    except jev.SpendCapReached as e:
        pytest.skip(str(e))
    read = apply_judge.read_page(raw, facts)
    expected = labels[capture]
    nouls = {q: round(float(a.noul), 3) for q, a in raw.items()
             if q != "page_state" and a.noul is not None}
    reads.rows.append({
        "capture": capture, "meta_kind": meta.get("page_kind"), "expected": expected,
        "judged": read.judged, "judged_conf": round(read.judged_conf, 3),
        "judged_probabilities": dict(getattr(raw.get("page_state"), "probabilities", {}) or {}),
        "read": read.state, "read_conf": round(read.conf, 3), "why": read.why,
        "structural": apply_judge.structural_kind(facts), "nouls": nouls,
        "fields": len(digest.fields), "buttons": len(digest.buttons)})
    if read.state not in expected:
        pytest.xfail(f"misread: {capture} read {read.state} {read.conf:.2f} (judged "
                     f"{read.judged} {read.judged_conf:.2f}); expected {'/'.join(expected)}")
