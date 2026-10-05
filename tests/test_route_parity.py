"""`loop_step` (the probe's words) and `_JobRun._loop` take the same step on
every flow's first page.

Both read a fresh page's route from `apply_route.route_turn` and then act on
it their own way: the probe says it, the loop does it. Each flow of the
harness's registry runs once under the fake judge. On the first page, the
loop runs with its step methods stopped, then the probe reads the same page
(`apply_run._probe_page`) once any late render has landed, and the two steps
must match. A flow that opens no page has no first page and is left out.
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in (REPO / "local", REPO / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import apply_flows  # noqa: E402
import apply_harness as h  # noqa: E402
import apply_job  # noqa: E402
import apply_run  # noqa: E402
import jev  # noqa: E402
from apply_gate import submit_on  # noqa: E402
from apply_outcome import _Parked  # noqa: E402

pytest_plugins = ["conftest_browser"]

# what the probe printed and what the loop did, per flow
_FIRST_PAGES: dict[str, tuple] = {}


class _Step(Exception):
    pass


def _said(text: str) -> tuple:
    """`loop_step`'s words as (step, detail)."""
    m = re.match(r"(?:.*; )?click the (?:Apply entry \[\d+\]|offsite Apply) '([^']*)'", text)
    if m:
        return ("click", m.group(1))
    tail = text.rsplit("; ", 1)[-1] if text.startswith(("read as", "go on")) else text
    for prefix, step in (("fill the page", "form"), ("the account step", "account"),
                         ("the code step", "code"), ("wait for the person", "captcha"),
                         ("finish: submitted", "submitted"),
                         ("follow the LinkedIn redirect", "redirect")):
        if prefix in tail:
            return (step, "")
    m = re.search(r"park: ([^(;]*)", text)
    return ("park", m.group(1).strip()) if m else ("?", text)


_STOPS = (("_click_entry", "click"), ("_application_form", "form"), ("_review_page", "form"),
          ("_account_step", "account"), ("_code_gate", "code"),
          ("_wait_for_human_check", "captcha"), ("_await_destination", "redirect"))


def _first_page_only(name: str):
    """A `_JobRun._loop` that compares the probe's step with the loop's on
    the first page it reads, then ends the job."""
    loop = apply_job._JobRun._loop

    def _loop(self, start: int = 0) -> None:
        def _stop(step):
            def _raise(*a, **kw):
                raise _Step(("click", a[2]) if step == "click" else (step, ""))
            return _raise
        for attr, step in _STOPS:
            setattr(self, attr, _stop(step))
        try:
            loop(self, start)
            did = ("?", "no step")
        except _Step as s:
            did = s.args[0]
        except _Parked as p:
            did = (("submitted", "") if p.status == "submitted" else
                   ("park", re.split(r" \(|;", p.reason, maxsplit=1)[0].strip()))
        out = io.StringIO()
        apply_run._probe_page(self.page, 1, self.r.jev, out,
                              park_mode=not submit_on(self.r.settings))
        said = re.search(r"  the loop would: (.*)", out.getvalue())
        _FIRST_PAGES[name] = (said.group(1) if said else out.getvalue(), did)
        raise _Parked("needs_human", "the first page was compared")
    return _loop


@pytest.mark.parametrize("flow_name", [f.name for f in apply_flows.FLOWS if not f.opens_no_page])
def test_the_probe_and_the_loop_take_the_same_step_on_each_flows_first_page(
        _browser, flow_server, tmp_path, monkeypatch, flow_name):
    monkeypatch.setattr(apply_job._JobRun, "_loop", _first_page_only(flow_name))
    h.run_flow(h.flow(flow_name), jev.FakeJev(), "fake", browser=_browser, server=flow_server,
               workdir=tmp_path)
    assert flow_name in _FIRST_PAGES, "the job ended before its loop read a page"
    said, did = _FIRST_PAGES[flow_name]
    assert _said(said) == did, (said, did)
