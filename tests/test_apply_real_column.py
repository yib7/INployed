"""SP8b: the flow matrix's real-judge column (`apply_harness.real_judge`,
`run_real`, the real column in `rates` and `summary`, the flows a replay
leaves out, the traced page reads).

Hermetic: no key, no network, no browser. The "live" judge is a fake that
counts each request at its estimated size; `run_flow` is replaced by a stub
that asks the judge one request per flow and ends as the run does when the
judge is unavailable (the runner catches `JevUnavailable`)."""
import dataclasses
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_harness as h  # noqa: E402
import jev  # noqa: E402

QUESTIONS = {"page_state": {"type": "choice", "instructions": "Which kind of page is `page`?",
                            "criteria": {"application_form": "a form", "other": "anything else"}}}


def _state(name: str) -> dict:
    return {"page": {"title": f"Apply: {name}", "headline_text": "Analytics Engineer " * 20}}


def _usd(name: str) -> float:
    return jev.usd_for(jev.request_size(_state(name), QUESTIONS)[1])


class _Sized:
    def __init__(self):
        self.calls = 0

    def judge(self, state, questions):
        self.calls += 1
        jev.count_usage(jev.request_size(state, questions)[1])
        return jev.FakeJev().judge(state, questions)


@pytest.fixture(autouse=True)
def _clean_usage():
    jev.reset_usage()
    yield
    jev.reset_usage()


def _stub_run_flow(calls: list):
    def run_flow(f, judge, judge_name, **kw):
        calls.append((f.name, judge_name))
        try:
            judge.judge(_state(f.name), QUESTIONS)
        except jev.JevUnavailable as e:
            return h.RunResult(f.name, judge_name, "failed", f"{type(e).__name__} at read",
                               False, [], 0, 1, 0.0)
        return h.RunResult(f.name, judge_name, "submitted", "confirmation page", True, [], 1, 1,
                           0.0, policy=None)
    return run_flow


def _flows(*names, replayable=True):
    base = h.flow("post_form")
    return tuple(dataclasses.replace(base, name=n, replayable=replayable) for n in names)


# --- the judge list and the real judge's modes ----------------------------------------------

def test_the_real_judge_runs_last_beside_the_fake_and_the_noisy_seeds():
    real = object()
    names = [n for n, _ in h.judges((1, 2), real=real)]
    assert names == ["fake", "noisy-1", "noisy-2", "real"]
    assert h.judges((1,), real=real)[-1][1] is real
    assert [n for n, _ in h.judges((), fake=False, real=real)] == ["real"]
    assert [n for n, _ in h.judges((1,))] == ["fake", "noisy-1"]


def test_real_judge_replay_reads_the_cache_and_asks_no_one(tmp_path):
    rj = h.real_judge("replay", tmp_path / "m.json")
    assert isinstance(rj.judge, jev.ReplayJev) and rj.judge.inner is None
    assert h.replay_only(rj.judge) and rj.cap is None and not rj.capped
    with pytest.raises(jev.JevUnavailable):
        rj.judge.judge(_state("a"), QUESTIONS)
    assert not (tmp_path / "m.json").exists()


def test_real_judge_dry_answers_with_the_fake_into_a_temp_copy_under_the_cap(tmp_path):
    cache = tmp_path / "m.json"
    rj = h.real_judge("dry", cache, cap_usd=0.5)
    assert rj.cache != cache and isinstance(rj.cap.inner, jev.DryRun)
    assert rj.cap.cap_usd == 0.5 and not h.replay_only(rj.judge)
    rj.judge.judge(_state("a"), QUESTIONS)
    assert rj.cache.is_file() and not cache.exists()
    assert jev.usage()["requests"] == 1


def test_real_judge_record_asks_the_live_judge_through_the_cap(tmp_path, monkeypatch):
    live = _Sized()
    rj = h.real_judge("record", tmp_path / "m.json", cap_usd=0.25, live=lambda: live)
    assert rj.cap.inner is live and rj.cache == tmp_path / "m.json"
    rj.judge.judge(_state("a"), QUESTIONS)
    rj.judge.judge(_state("a"), QUESTIONS)
    assert live.calls == 1, "the second request replays from the cache"
    monkeypatch.setenv(jev.RECORD_CAP_ENV, "0.07")
    assert h.real_judge("record", tmp_path / "n.json", live=_Sized).cap.cap_usd == 0.07


def test_real_judge_refuses_an_unknown_mode(tmp_path):
    with pytest.raises(ValueError, match="real-judge mode"):
        h.real_judge("live", tmp_path / "m.json")


# --- the recording's column stops at the cap ------------------------------------------------

def test_run_real_starts_no_flow_past_the_cap_and_lists_what_it_left(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(h, "run_flow", _stub_run_flow(calls))
    live = _Sized()
    # room for the first flow's request and not the second's
    rj = h.real_judge("record", tmp_path / "m.json", cap_usd=_usd("a") * 1.5, live=lambda: live)
    col = h.run_real(_flows("a", "b", "c"), rj, browser=None, server=None, workdir=tmp_path)
    assert [r.flow for r in col.results] == ["a"] and col.results[0].ok
    assert col.unrecorded == ["b", "c"]
    assert calls == [("a", "real"), ("b", "real")], "no flow starts once the cap is reached"
    assert live.calls == 1 and rj.capped
    assert jev.RECORD_CAP_ENV in col.stopped
    assert json.loads((tmp_path / "m.json").read_text(encoding="utf-8")), "a's answers stay"


def test_run_real_under_a_cap_that_holds_runs_every_flow(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(h, "run_flow", _stub_run_flow(calls))
    rj = h.real_judge("dry", tmp_path / "m.json", cap_usd=1.0)
    col = h.run_real(_flows("a", "b"), rj, browser=None, server=None, workdir=tmp_path)
    assert [r.flow for r in col.results] == ["a", "b"]
    assert col.unrecorded == [] and col.stopped == ""
    assert jev.usage()["requests"] == 2


# --- the flows a replay leaves out ----------------------------------------------------------

def test_a_replay_of_the_real_judge_leaves_out_a_flow_whose_text_changes_with_the_clock(
        tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(h, "run_flow", _stub_run_flow(calls))
    flows = _flows("steady") + _flows("ticking", replayable=False)
    replay = h.real_judge("replay", tmp_path / "m.json").judge
    h.run_matrix(flows, h.judges((), real=replay), browser=None, server=None,
                 workdir=tmp_path)
    assert calls == [("steady", "fake"), ("steady", "real"), ("ticking", "fake")]
    calls.clear()
    record = h.real_judge("dry", tmp_path / "m.json", cap_usd=1.0).judge
    h.run_matrix(flows, h.judges((), fake=False, real=record), browser=None, server=None,
                 workdir=tmp_path)
    assert calls == [("steady", "real"), ("ticking", "real")], "a recording runs it"
    assert [f.name for f in h.FLOWS if not f.replayable] == ["ticker_page"]


# --- the real column in the rates and the summary -------------------------------------------

def _row(flow, judge, ok):
    return h.RunResult(flow, judge, "submitted" if ok else "needs_human",
                       "confirmation page" if ok else "no submit button", ok, [], 1, 1, 0.1,
                       policy=None if ok else False)


def test_rates_keep_the_real_judge_apart_from_the_noisy_seeds():
    rows = [_row("ashby_wizard", "fake", True), _row("ashby_wizard", "noisy-1", True),
            _row("ashby_wizard", "real", False), _row("post_form", "fake", True),
            _row("post_form", "noisy-1", True)]
    rt = h.rates(rows)
    assert (rt["fake"], rt["noisy"], rt["real"], rt["real_runs"]) == (1.0, 1.0, 0.0, 1)
    assert rt["per_flow"]["ashby_wizard"]["real"] == 0.0
    assert "real" not in rt["per_flow"]["post_form"]
    text = h.summary(rows)
    assert "real 0.0% over 1 flows" in text
    head = next(line for line in text.splitlines() if line.startswith("flow ") and "runs" in line)
    assert head.split() == ["flow", "fake", "noisy", "real", "runs"]
    post = next(line for line in text.splitlines() if line.startswith("post_form ")
                and "%" in line)
    assert post.split() == ["post_form", "100%", "100%", "-", "2"]


def test_a_real_only_summary_shows_the_real_column_alone_and_the_fake_one_is_unchanged():
    text = h.summary([_row("ashby_wizard", "real", True)])
    head = next(line for line in text.splitlines() if line.startswith("flow ") and "runs" in line)
    assert head.split() == ["flow", "real", "runs"]
    assert "success: real 100.0% over 1 flows, all 100.0%" in text
    plain = h.summary([_row("ashby_wizard", "fake", True), _row("ashby_wizard", "noisy-1", True)])
    assert "real" not in plain
    assert "success: fake 100.0%, noisy 100.0%, all 100.0% over 2 runs" in plain


# --- the traced page reads ------------------------------------------------------------------

def test_trace_reads_lists_each_pages_read_and_the_judges_own_pick_in_page_order(tmp_path):
    def page(n, state, conf, judged, judged_conf):
        (tmp_path / f"page-{n}.json").write_text(json.dumps({
            "n": n, "state": state, "confidence": conf,
            "answers": {"page_state": {"kind": "choice", "choice": state, "confidence": conf},
                        "page_state_judged": {"kind": "choice", "choice": judged,
                                              "confidence": judged_conf}}}), encoding="utf-8")
    page(10, "confirmation", 0.9, "confirmation", 1.0)
    page(2, "application_form", 0.55, "review_page", 0.7)
    (tmp_path / "page-3.json").write_text("{not json", encoding="utf-8")
    rows = h.trace_reads(tmp_path)
    assert [r["n"] for r in rows] == [2, 10]
    assert rows[0] == {"n": 2, "state": "application_form", "conf": 0.55,
                       "read": "application_form", "read_conf": 0.55, "judged": "review_page",
                       "judged_conf": 0.7}
    assert h.trace_reads("") == []
