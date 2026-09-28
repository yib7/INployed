"""SP1: the noisy judge, the invariant checks, and the flow matrix.

- `NoisyJev`: deterministic per (seed, request), fresh noise for a changed
  request, misreads only toward plausible neighbours, never moves a submit
  role, drops only field answers; no production mode reaches it.
- `apply_harness.invariant_breaks`: each check fails on a planted breach and
  stays quiet on a clean run; end to end, a loop sabotaged to click its
  submit outside the gate and a judge that reads a form as a confirmation
  are both caught.
- The matrix: one test per registered flow under `FakeJev` and the first
  three noisy seeds (the script runs twenty), zero invariant breaks, the
  fake judge reaching every flow's end but a known one's; then the success
  rates at or above the pinned floors (`apply_harness.SUCCESS_FLOOR`,
  `FAKE_SUCCESS_FLOOR`), which later phases raise.

Headless Chromium through the module-scoped test browser; the judge is the
fake or the noisy one; no network but the local server and routed hosts."""
import dataclasses
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_harness as h  # noqa: E402
import apply_run  # noqa: E402
import jev  # noqa: E402

pytest_plugins = ["conftest_browser"]


# --- NoisyJev ----------------------------------------------------------------------------------

_STATES = list(jev.PAGE_STATE_NEIGHBOURS)


def _request(title="Apply", buttons=("Continue", "Back", "Help", "Submit application"),
             fields=("First name", "Email")):
    state = {"page": {"title": title, "headline_text": f"{title} for Analytics Engineer"},
             "fields": [{"n": i, "label": f} for i, f in enumerate(fields)],
             "buttons": [{"n": i, "text": b} for i, b in enumerate(buttons)]}
    questions = {"page_state": {"type": "choice", "instructions": "Which kind of screen?",
                                "criteria": {s: s for s in _STATES}},
                 "has_captcha": {"type": "noul", "instructions": "A captcha on `page`?"}}
    for i in range(len(fields)):
        questions[f"field_{i}_source"] = {"type": "choice", "instructions": "Which fact?",
                                          "criteria": {"first_name": "", "email": "",
                                                       "leave_blank": ""}}
    for i in range(len(buttons)):
        questions[f"button_{i}_role"] = {"type": "choice", "instructions": "Which role?",
                                         "criteria": {r: r for r in ("advance", "submit", "back",
                                                                     "apply_entry", "upload",
                                                                     "other")}}
    return state, questions


class _Scripted:
    """An inner judge with fixed reads: the page state, and a role per
    button text."""
    ROLES = {"Continue": "advance", "Back": "back", "Help": "other",
             "Submit application": "submit", "Apply now": "apply_entry"}

    def __init__(self, state="application_form"):
        self.state = state

    def judge(self, state, questions):
        out = {}
        for qid, q in questions.items():
            names = list(q.get("criteria") or {})
            if qid == "page_state":
                choice = self.state
            elif qid.startswith("button_"):
                n = int(qid.split("_")[1])
                choice = self.ROLES.get(state["buttons"][n]["text"], "other")
            elif q["type"] == "noul":
                out[qid] = jev.Answer(kind="noul", noul=0.1)
                continue
            else:
                choice = names[0]
            out[qid] = jev.Answer(kind="choice", choice=choice,
                                  probabilities={x: (1.0 if x == choice else 0.0) for x in names},
                                  confidence=1.0)
        return out


def test_noisy_answers_are_the_same_for_the_same_request_and_seed():
    state, questions = _request()
    a = jev.NoisyJev(jev.FakeJev(), 7, swap_p=0.5, drop_p=0.3).judge(state, questions)
    b = jev.NoisyJev(jev.FakeJev(), 7, swap_p=0.5, drop_p=0.3).judge(state, questions)
    c = jev.NoisyJev(jev.FakeJev(), 7, swap_p=0.5, drop_p=0.3).judge(state, questions)
    assert a == b == c


def test_a_changed_request_or_seed_gets_fresh_noise():
    judge = jev.NoisyJev(_Scripted(), 3, swap_p=0.5)
    reads = [judge.judge(*_request(title=f"Apply {i}"))["page_state"].choice for i in range(60)]
    assert "application_form" in reads and set(reads) - {"application_form"}, reads
    # the same page re-read: the same answer every time
    again = [judge.judge(*_request(title=f"Apply {i}"))["page_state"].choice for i in range(60)]
    assert again == reads
    seeds = {jev.NoisyJev(_Scripted(), s, swap_p=0.5).judge(*_request())["page_state"].choice
             for s in range(1, 40)}
    assert len(seeds) > 1


def test_no_noise_is_the_inner_judge():
    state, questions = _request()
    inner = jev.FakeJev().judge(state, questions)
    quiet = jev.NoisyJev(jev.FakeJev(), 5, swap_p=0.0, conf_scale=1.0,
                         drop_p=0.0).judge(state, questions)
    assert quiet == inner


@pytest.mark.parametrize("truth", _STATES)
def test_a_swapped_page_state_is_a_neighbour_read_between_0_30_and_0_60(truth):
    judge = jev.NoisyJev(_Scripted(truth), 1, swap_p=1.0)
    for i in range(20):
        a = judge.judge(*_request(title=f"Page {i}"))["page_state"]
        assert a.choice in jev.PAGE_STATE_NEIGHBOURS[truth]
        assert 0.30 <= a.confidence <= 0.60
        assert a.probabilities[a.choice] == max(a.probabilities.values())
        assert a.probabilities[truth] > 0


def test_confidences_are_scaled_within_the_floor_and_nouls_are_left_alone():
    # a Noul outside the page read (a verification, a flag of the mapping) is
    # left alone; the page read's own Nouls are misread (SP4, test_apply_read)
    judge = jev.NoisyJev(_Scripted(), 2, swap_p=0.0, conf_scale=0.75, drop_p=0.0)
    seen = []
    for i in range(30):
        state, questions = _request(title=f"Form {i}")
        questions["verify_0"] = {"type": "noul", "instructions": "A value?"}
        out = judge.judge(state, questions)
        for qid, a in out.items():
            if a.kind == "choice":
                assert 0.75 <= a.confidence <= 1.0, (qid, a)
                assert a.probabilities[a.choice] == max(a.probabilities.values())
                seen.append(a.confidence)
            elif qid not in jev.READ_NOULS:
                assert a.noul == 0.1
    assert min(seen) < 0.9 < max(seen)


def test_button_roles_trade_only_between_neighbours_and_a_submit_never_moves():
    moved = 0
    for i in range(80):
        state, questions = _request(title=f"Step {i}")
        out = jev.NoisyJev(_Scripted(), i, swap_p=1.0, drop_p=0.0).judge(state, questions)
        roles = [out[f"button_{n}_role"].choice for n in range(4)]
        assert roles[3] == "submit"                         # "Submit application"
        assert sorted(roles[:3]) == ["advance", "back", "other"]
        assert not (roles[0] == "back" and roles[1] == "advance")   # never Back for Continue
        moved += roles[:3] != ["advance", "back", "other"]
    assert moved > 20


def test_only_field_answers_are_dropped():
    dropped = set()
    for i in range(40):
        state, questions = _request(title=f"Drop {i}")
        out = jev.NoisyJev(_Scripted(), i, swap_p=0.0, drop_p=0.5).judge(state, questions)
        dropped |= set(questions) - set(out)
    assert dropped and all(q.startswith("field_") for q in dropped), dropped


def test_no_production_mode_reaches_the_noisy_judge(monkeypatch):
    assert "noisy" not in jev.MODES
    with pytest.raises(ValueError):
        jev.get("noisy")
    monkeypatch.setattr(apply_run, "_load_env", lambda: None)
    monkeypatch.setattr(apply_run, "load_settings", lambda: {**apply_run.DEFAULT_SETTINGS,
                                                             "auto_apply_jev_mode": "noisy"})
    claimed = []
    monkeypatch.setattr(apply_run.apply_queue, "claim", lambda *a, **k: claimed.append(1))
    with pytest.raises(SystemExit) as e:
        apply_run.main(["drain", "--jev", "noisy"])
    assert e.value.code == 2
    assert apply_run.main(["drain"]) == 2             # the configured mode is refused as well
    assert claimed == []


# --- the invariant checks on planted breaches ---------------------------------------------------

class _Out:
    def __init__(self, status="ready_to_submit", reason="auto_apply_submit is off"):
        self.status = status
        self.reason = reason


def _clean(park=True, **final):
    rec = h.Recorder(h.flow("ashby_wizard_park" if park else "ashby_wizard"), park_mode=park,
                     password=h.PASSWORD)
    rec.final = {"confirmed": False, "at_gate": park, **final}
    return rec, h.Sends(rec)


def _codes(breaks):
    return sorted({b.split(":")[0] for b in breaks})


def test_a_clean_run_breaks_nothing():
    rec, sends = _clean()
    rec.actions += [h.Action("click", "http://127.0.0.1/forms/a.html", text="Continue"),
                    h.Action("fill", "http://127.0.0.1/forms/a.html", tag="input"),
                    h.Action("click", "https://www.linkedin.com/jobs/view/1/", text="Apply"),
                    h.Action("gate", "http://127.0.0.1/forms/a.html", in_gate=True)]
    # confirmed answers typed and picked (cycle 18, FL-1)
    rec._add("fill", "Locator.fill", {"url": "http://127.0.0.1/forms/a.html", "tag": "input"},
             value="Jane")
    rec._add("pick", "Locator.select_option", {"url": "http://127.0.0.1/forms/a.html",
                                                "tag": "select"}, value=["Yes"])
    assert h.invariant_breaks(_Out(), rec, sends) == []
    rec, sends = _clean(park=False, confirmed=True)
    sends.events.append(h.Send("dom", "http://127.0.0.1/forms/a.html", in_gate=True))
    rec.actions.append(h.Action("click", "http://127.0.0.1/a", text="Submit", in_gate=True))
    assert h.invariant_breaks(_Out("submitted", "confirmation page"), rec, sends) == []


@pytest.mark.parametrize("plant, code", [
    ("two_sends", "SEND-COUNT"),
    ("park_send", "PARK-SENT"),
    ("send_outside", "SEND-OUTSIDE-GATE"),
    ("submitted_no_marker", "FALSE-SUBMITTED"),
    ("unconfirmed_no_send", "FALSE-SUBMITTED"),
    ("submit_click_outside", "CLICK-OUTSIDE-GATE"),
    ("finish_click_outside", "CLICK-OUTSIDE-GATE"),
    ("linkedin_fill", "LINKEDIN-FILL"),
    ("linkedin_upload", "LINKEDIN-UPLOAD"),
    ("linkedin_gate", "LINKEDIN-GATE"),
    ("password_in_record", "PASSWORD-LEAK"),
    ("password_in_log", "PASSWORD-LEAK"),
    ("ready_after_send", "READY-AFTER-SEND"),
    ("captcha_touch", "CAPTCHA-TOUCH"),
    ("not_sent_after_send", "NOT-SENT-AFTER-SEND"),
    ("unsent_after_send", "NOT-SENT-AFTER-SEND"),
    ("guard_unsent_after_send", "NOT-SENT-AFTER-SEND"),
    # final review C-M3: the unconfirmed prefix is the runner's own, and a
    # submitted end needs a send
    ("unconfirmed_word_quoted", "FALSE-SUBMITTED"),
    ("submitted_no_send", "SUBMITTED-WITHOUT-SEND"),
    ("unconfirmed_no_send", "SUBMITTED-WITHOUT-SEND"),
    # final review C-M2: a park the person may re-queue after an accepted send
    ("needs_human_after_send", "REQUEUABLE-AFTER-SEND"),
    ("failed_after_send", "REQUEUABLE-AFTER-SEND"),
    # cycle 18 (FL-1): an answer the user has not confirmed is never filled
    ("unconfirmed_fill", "UNCONFIRMED-ANSWER"),
    ("unconfirmed_pick", "UNCONFIRMED-ANSWER"),
    ("unconfirmed_option_click", "UNCONFIRMED-ANSWER")])
def test_each_invariant_check_fails_on_its_planted_breach(tmp_path, plant, code):
    park = plant in ("park_send",)
    rec, sends = _clean(park=park)
    out = _Out() if park else _Out("needs_human", "login wall")
    if plant == "two_sends":
        sends.events += [h.Send("dom", "x", True), h.Send("post", "/submit/x", True)]
    elif plant == "park_send":
        sends.events.append(h.Send("dom", "x", True))
    elif plant == "send_outside":
        sends.events.append(h.Send("request", "GET http://127.0.0.1/forms/code_gate.html", False))
    elif plant == "submitted_no_marker":
        sends.events.append(h.Send("dom", "x", True))
        out = _Out("submitted", "confirmation page")
    elif plant == "unconfirmed_no_send":
        out = _Out("submitted", "submitted (unconfirmed): the page after submit reads as "
                                "application_form (1.00)")
    elif plant == "submit_click_outside":
        rec.actions.append(h.Action("click", "http://127.0.0.1/a", text="Submit application",
                                    tag="button"))
    elif plant == "finish_click_outside":
        rec.actions.append(h.Action("click", "http://127.0.0.1/a", text="Finish", tag="button"))
    elif plant == "linkedin_fill":
        rec.actions.append(h.Action("fill", "https://www.linkedin.com/jobs/view/1/", tag="input"))
    elif plant == "linkedin_upload":
        rec.actions.append(h.Action("upload", "https://linkedin.com/jobs/view/1/"))
    elif plant == "linkedin_gate":
        rec.actions.append(h.Action("gate", "https://www.linkedin.com/jobs/view/1/",
                                    in_gate=True))
    elif plant == "password_in_record":
        path = tmp_path / "apply_record.md"
        path.write_text(f"- Password: {h.PASSWORD}\n", encoding="utf-8")
        rec.files = [tmp_path]
    elif plant == "password_in_log":
        rec.logs.append(f"typed {h.PASSWORD}")
    elif plant == "ready_after_send":
        # a click that dispatched, then timed out, read as never landed
        sends.events.append(h.Send("post", "/submit/slow_post", True))
        out = _Out("ready_to_submit", "submit did not register")
    elif plant == "not_sent_after_send":
        # a send the site accepted, then the run says nothing went through
        sends.events.append(h.Send("post", "/submit/ajax_reset", True))
        out = _Out("needs_human", apply_run.NOT_SENT_REASON + ": validation errors (x)")
    elif plant == "unsent_after_send":
        # SP8a review R2-M2: the error page's "no connection was made" after
        # a send the site took
        sends.events.append(h.Send("post", "/submit/post_redirect", True))
        out = _Out("needs_human", "error or dead page: POST http://127.0.0.1/submit failed on "
                                  "the network (net::ERR_CONNECTION_REFUSED); no connection "
                                  "was made, so nothing was sent")
    elif plant == "guard_unsent_after_send":
        sends.events.append(h.Send("post", "/submit/post_redirect", True))
        out = _Out("needs_human", "the form posts to evil.example.net, outside the allowed "
                                  "sites; the run stopped it and nothing was sent")
    elif plant == "captcha_touch":
        rec.actions.append(h.Action("click", "https://www.google.com/recaptcha/api2/anchor?k=x",
                                    text="I'm not a robot"))
    elif plant == "unconfirmed_word_quoted":
        # a confirmation reason that quotes the page's word, with no marker
        sends.events.append(h.Send("dom", "x", True))
        out = _Out("submitted", "confirmation page ('your application stays unconfirmed until "
                                "you verify your email')")
    elif plant == "submitted_no_send":
        rec.final["confirmed"] = True
        out = _Out("submitted", "confirmation page")
    elif plant == "needs_human_after_send":
        sends.events.append(h.Send("post", "/submit/post_redirect", True))
        out = _Out("needs_human", "required field without an answer: Salary")
    elif plant == "failed_after_send":
        sends.events.append(h.Send("post", "/submit/post_redirect", True))
        out = _Out("failed", "TimeoutError: the page after the submit")
    elif plant == "unconfirmed_fill":
        rec._add("fill", "Locator.fill", {"url": "http://127.0.0.1/a", "tag": "textarea"},
                 value=f"Hello. {h.unconfirmed_values()[0]}")
    elif plant == "unconfirmed_pick":
        rec._add("pick", "Locator.select_option", {"url": "http://127.0.0.1/a",
                                                    "tag": "select"},
                 value={"label": h.unconfirmed_values()[0]})
    elif plant == "unconfirmed_option_click":
        rec._add("click", "Locator.click", {"url": "http://127.0.0.1/a", "role": "option",
                                            "toggle": True,
                                            "text": h.unconfirmed_values()[0]})
    breaks = h.invariant_breaks(out, rec, sends)
    assert code in _codes(breaks), breaks
    with pytest.raises(AssertionError, match=code):
        h.assert_invariants(out, rec, sends)


def test_a_send_that_never_made_its_connection_is_one_the_site_did_not_accept():
    # SP8a review R2-M2: a send whose request failed before any connection
    # (refused, unreachable, a name that did not resolve, blocked in the
    # browser) never reached the site; one reset after it left may have
    import types

    class _Route:
        def fallback(self):
            pass
    sends = h.Sends()
    requests = [types.SimpleNamespace(method="POST", url=f"http://127.0.0.1/submit/{n}",
                                      failure=f"net::{failure}")
                for n, failure in enumerate(("ERR_CONNECTION_REFUSED", "ERR_NAME_NOT_RESOLVED",
                                             "ERR_ADDRESS_UNREACHABLE", "ERR_BLOCKED_BY_CLIENT",
                                             "ERR_CONNECTION_RESET", "ERR_FAILED"))]
    for request in requests:
        sends._request(_Route(), request)
    for request in reversed(requests):
        sends._failed(request)
    assert [s.accepted for s in sends.events] == [False, False, False, False, True, True]
    assert sends.count == 6         # each is still an attempt


def test_a_submit_in_a_tab_window_open_made_is_one_send(_browser, flow_server):
    # final review C-M3's SUBMITTED-WITHOUT-SEND found popup_step's submit
    # unseen: the tab keeps its first blank window, and the init script's
    # watch was on that blank document
    ctx = _browser.new_context()
    try:
        sends = h.Sends()
        sends.install(ctx, h.flow("popup_step"))
        page = ctx.new_page()
        page.goto(flow_server.url("popup_step.html"))
        page.fill("#first_name", "Jane")
        with page.expect_popup() as opened:
            page.click("#btn-next")
        tab = opened.value
        tab.wait_for_load_state()
        tab.fill("#email", "jane.doe@example.com")
        with tab.expect_popup():
            tab.click("#btn-submit")
        tab.wait_for_timeout(300)
        assert [s.kind for s in sends.events] == ["dom"], sends.events
        assert "step=2" in sends.events[0].detail
    finally:
        ctx.close()


def test_a_submitted_unconfirmed_with_a_send_is_within_the_invariants():
    rec, sends = _clean(park=False)
    sends.events.append(h.Send("dom", "x", True))
    out = _Out("submitted", "submitted (unconfirmed): the page after submit reads as other (0.5)")
    assert h.invariant_breaks(out, rec, sends) == []


@pytest.mark.parametrize("status, reason, accepted", [
    # final review C-M2: a check-whether park is never re-queued as it is
    ("needs_human", apply_run.CHECK_SENT_REASON + ": a request left after the submit click "
                    "(POST x)", True),
    # a send the site refused (or that never made its connection) sent nothing
    ("needs_human", "required field without an answer: Salary", False),
    ("failed", "TimeoutError: x", False)])
def test_a_park_after_a_send_is_within_the_invariants_only_when_it_cannot_send_twice(
        status, reason, accepted):
    rec, sends = _clean(park=False)
    sends.events.append(h.Send("post", "/submit/post_redirect", True, accepted))
    assert h.invariant_breaks(_Out(status, reason), rec, sends) == []


_CSR = apply_run.CHECK_SENT_REASON


@pytest.mark.parametrize("reason, note, flagged", [
    # final review C N1: the parks `_send_evidence` gives the check-sent note
    # carry its words as a clause of their own after other words
    (f"the emailed code was not accepted (the code screen came back, 0.90); {_CSR}", "", False),
    (f"an error page after the submit click (0.90; Oops); {_CSR}; a request left after the "
     "click: POST http://127.0.0.1/submit", "", False),
    # the note alone: the account page's park after its submit
    ("after the account page's submit the page reads as other (0.40)", "CHECK_SENT_NOTE", False),
    # the page's words quoted in a park that asks for a re-queue
    (f"required field without an answer: Salary (the page says '{_CSR}')", "LINK_NOTE", True),
    ("required field without an answer: Salary", "", True)])
def test_a_check_whether_park_after_a_send_is_known_by_its_clause_or_its_note(
        reason, note, flagged):
    rec, sends = _clean(park=False)
    sends.events.append(h.Send("post", "/submit/post_redirect", True))
    note = getattr(apply_run, note) if note else ""
    codes = _codes(h.invariant_breaks(_Out("needs_human", reason), rec, sends, tab_note=note))
    assert ("REQUEUABLE-AFTER-SEND" in codes) is flagged, codes


@pytest.mark.parametrize("reason, confirmed, code", [
    ("submitted (unconfirmed): the page after submit reads as other (0.50)", False,
     "FALSE-SUBMITTED"),
    ("submitted (unconfirmed): the page after submit reads as other (0.50)", False,
     "SUBMITTED-WITHOUT-SEND"),
    ("confirmation page", True, "SUBMITTED-WITHOUT-SEND")])
def test_a_submitted_end_after_only_a_send_the_site_refused_breaks(reason, confirmed, code):
    # final review C N2: a post the site refused (or one that never made its
    # connection) sent nothing, so a submitted end after it alone is lost
    rec, sends = _clean(park=False, confirmed=confirmed)
    sends.events.append(h.Send("post", "/submit/server_validation", True, False))
    assert code in _codes(h.invariant_breaks(_Out("submitted", reason), rec, sends))
    sends.events.append(h.Send("post", "/submit/post_redirect", True))
    assert not {"FALSE-SUBMITTED", "SUBMITTED-WITHOUT-SEND"} & set(
        _codes(h.invariant_breaks(_Out("submitted", reason), rec, sends)))


def test_a_loop_clicking_its_submit_outside_the_gate_is_caught_end_to_end(
        _browser, flow_server, tmp_path, monkeypatch):
    real = apply_run._JobRun._form_buttons

    def _sabotaged(self, digest, plan, verification, rec):
        submit = plan.buttons.get("submit")
        if submit is not None:
            self._click(digest, submit[0], "advance", rec)
            raise apply_run._Parked("ready_to_submit", "auto_apply_submit is off")
        return real(self, digest, plan, verification, rec)
    monkeypatch.setattr(apply_run._JobRun, "_form_buttons", _sabotaged)
    r = h.run_flow(h.flow("ashby_wizard_park"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert {"PARK-SENT", "SEND-OUTSIDE-GATE", "CLICK-OUTSIDE-GATE"} <= set(_codes(r.breaks)), r
    assert not r.ok


class _FormAsConfirmation(jev.FakeJev):
    """The fake, reading the wizard's first step as a confirmation at 0.9."""

    def judge(self, state, questions):
        out = super().judge(state, questions)
        a = out.get("page_state")
        if a is not None and a.choice == "application_form" and state.get("fields"):
            h.read_as(out, "confirmation", 0.9, {"confirmation": 0.9, "application_form": 0.1})
        return out


def test_a_confident_confirmation_misread_before_any_submit_is_caught(
        _browser, flow_server, tmp_path, monkeypatch):
    # SP3: the loop reads a confirmation before any submit as the form it
    # contradicts (tests/test_apply_submit.py); a loop that took it for the
    # end again would be caught
    monkeypatch.setattr(apply_run, "confirmation_step",
                        lambda digest, answers, conf, **kw: (
                            "submitted", "confirmation page", conf))
    r = h.run_flow(h.flow("ashby_wizard"), _FormAsConfirmation(), "misread", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert (r.status, r.reason) == ("submitted", "confirmation page"), r
    # no send went either (final review C-M3)
    assert _codes(r.breaks) == ["FALSE-SUBMITTED", "SUBMITTED-WITHOUT-SEND"], r.breaks


# --- the matrix, one test per flow (M6) --------------------------------------------------------------

_RESULTS: dict[str, list] = {}


@pytest.mark.parametrize("flow_name", [f.name for f in h.FLOWS])
def test_each_flow_holds_every_invariant_under_the_fake_and_the_noisy_seeds(
        _browser, flow_server, tmp_path, flow_name):
    f = h.flow(flow_name)
    # a slow flow (a 6 s server answer) runs fewer seeds here; the script runs twenty
    seeds = h.SUITE_SEEDS[:f.suite_seeds] if f.suite_seeds is not None else h.SUITE_SEEDS
    if f.judge_free:
        # a run that asks the judge nothing is the same run under every judge
        fake = h.run_matrix([f], h.judges((), fake=True), browser=_browser,
                            server=flow_server, workdir=tmp_path)[0]
        assert fake.judge_requests == 0, f"{flow_name} asked the judge: it is not judge_free"
        results = [fake] + [dataclasses.replace(fake, judge=name)
                            for name, _ in h.judges(seeds, fake=False)]
    else:
        results = h.run_matrix([f], h.judges(seeds), browser=_browser,
                               server=flow_server, workdir=tmp_path)
    _RESULTS[flow_name] = results
    table = h.summary(results)
    assert all(not r.breaks for r in results), table
    # final review C-I2: a park outside the user's policy that missed the
    # flow's end fails its flow, under xdist too (the floors' test below runs
    # only when one process ran every flow)
    assert not [r for r in results if r.policy is False and not r.ok], table
    # SP4's checkpoint, per flow so it runs under xdist too (review M4): a
    # page is never left unread ("unsure what this page is") and a moving
    # page never reads as stuck ("page did not advance") but where the flow
    # is built to end so
    stuck = ("unsure what this page is", "page did not advance")
    wrong = [(r.judge, r.reason) for r in results
             if not r.ok and (r.reason or "").startswith(stuck)]
    assert not wrong, wrong
    fake = next(r for r in results if r.judge == "fake")
    if f.known:
        assert not fake.ok, (f"{flow_name} reaches its end now; clear its known flag "
                             f"({f.known})\n{table}")
    else:
        assert fake.ok, table


@pytest.mark.parametrize("flow_name", [f.name for f in h.FLOWS if f.pause is None])
def test_each_flow_ends_as_before_with_pauses_on_and_parked_at_once(
        _browser, flow_server, tmp_path, flow_name):
    # SP7 review M8: every flow that has no pause of its own, run with pauses
    # on and a person who answers each one with Park it, ends as it does with
    # pauses off, and holds every invariant
    f = h.flow(flow_name)
    (r,) = h.run_matrix([f], h.judges((), fake=True), browser=_browser, server=flow_server,
                        workdir=tmp_path, pause=h.PauseSpec("park"))
    assert not r.breaks, (r.status, r.reason, r.breaks)
    assert r.ok is not bool(f.known), (f.known, r.status, r.reason)


def test_the_success_floors_over_the_whole_registry():
    if set(_RESULTS) != {f.name for f in h.FLOWS}:
        pytest.skip("the per-flow matrix tests did not all run")
    results = [r for name in _RESULTS for r in _RESULTS[name]]
    table = h.summary(results)
    rates = h.rates(results)
    assert len(h.FLOWS) >= 12
    assert rates["breaks"] == 0, table
    assert rates["fake"] >= h.FAKE_SUCCESS_FLOOR, table
    assert rates["noisy"] >= h.SUCCESS_FLOOR, table


# === review round 1 ===============================================================================

# --- M5: a known failing flow is reported, and kept out of the floors ------------------------------

def test_a_known_flow_is_reported_and_left_out_of_the_floors(monkeypatch):
    # SP3 fixed the last known flow (greenhouse_embed); the registry has none
    assert [f.name for f in h.FLOWS if f.known] == []
    flows = tuple(dataclasses.replace(f, known="SP3: planted") if f.name == "greenhouse_embed"
                  else f for f in h.FLOWS)
    monkeypatch.setattr(h, "FLOWS", flows)
    rows = [h.RunResult("greenhouse_embed", "fake", "submitted", "x", False, [], 1, 2, 0.1),
            h.RunResult("ashby_wizard", "fake", "submitted", "confirmation page", True, [], 1,
                        4, 0.1),
            h.RunResult("ashby_wizard", "noisy-1", "needs_human", "x", False, [], 0, 2, 0.1)]
    rates = h.rates(rows)
    assert (rates["fake"], rates["noisy"]) == (1.0, 0.0)
    assert rates["known"] == {"greenhouse_embed": {"fake": 0.0, "noisy": 1.0, "runs": 1}}
    assert "known failing, SP3: greenhouse_embed" in h.summary(rows)


def test_a_flow_with_a_real_end_is_read_by_the_judge_that_ran_it():
    # the Contoso replica: the fake leaves the reworded relocation question
    # open and parks; the real judge settles it and submits with the willing
    # option, which the confirmation marker checks
    f = next(f for f in h.FLOWS if f.name == "ashby_relocation_place")
    park = ("needs_human", "required field without an answer: Are you willing to relocate "
                           "to the job location (New York)?")
    sent = ("submitted", "confirmation page")
    assert f.reached(*park, {}, "fake") and f.reached(*park, {}, "noisy-1")
    assert not f.reached(*sent, {"confirmed": True}, "fake")
    assert f.reached(*sent, {"confirmed": True}, h.REAL)
    assert not f.reached(*sent, {}, h.REAL)
    assert not f.reached(*park, {}, h.REAL)
    # every other flow ends the same under every judge
    assert [g.name for g in h.FLOWS if g.real_end] == ["ashby_relocation_place"]


# --- M10: the noisy distribution keeps its winner on top ---------------------------------------------

def test_a_scaled_two_option_answer_keeps_its_winner_most_probable():
    class _Half:
        def judge(self, state, questions):
            return {"q": jev.Answer(kind="choice", choice="yes",
                                    probabilities={"yes": 0.5, "no": 0.5}, confidence=0.5)}
    for seed in range(1, 30):
        a = jev.NoisyJev(_Half(), seed, conf_scale=0.6).judge({}, {"q": {"type": "choice"}})["q"]
        assert a.confidence < 0.5
        assert a.probabilities["yes"] > a.probabilities["no"], a
        assert abs(sum(a.probabilities.values()) - 1.0) < 1e-3, a


# --- M1: the harness reads submit and final words with the loop's own vocabulary --------------------

def test_the_harness_uses_the_loops_submit_and_final_words():
    assert h.SUBMIT_WORDS is apply_run.SUBMIT_WORDS
    assert h.FINAL_WORDS is apply_run.FINAL_WORDS


@pytest.mark.parametrize("text, park, breaks", [
    ("Submit application", False, True),
    ("Send application", False, True),
    ("Finish", False, True),
    ("Complete application", True, True),        # a final word in park mode
    ("Confirm and continue", True, True),
    ("Complete application", False, False),      # submit mode: "Complete profile" is a step
    ("Complete registration", True, False),      # an account step
    ("Apply now", True, False),                  # the posting's entry
    ("Continue", True, False)])
def test_click_outside_the_gate_follows_the_loops_words(text, park, breaks):
    rec, sends = _clean(park=park)
    rec.actions.append(h.Action("click", "http://127.0.0.1/a", text=text, tag="button"))
    out = _Out() if park else _Out("needs_human", "login wall")
    assert ("CLICK-OUTSIDE-GATE" in _codes(h.invariant_breaks(out, rec, sends))) is breaks


# --- test gaps: LinkedIn tick and pick; the new invariants ------------------------------------------

@pytest.mark.parametrize("plant, code", [
    ("linkedin_tick", "LINKEDIN-TICK"),
    ("linkedin_pick", "LINKEDIN-PICK"),
    ("linkedin_typing", "LINKEDIN-FILL"),
    ("enter_in_a_form", "ENTER-OUTSIDE-GATE"),
    ("password_to_judge", "PASSWORD-TO-JUDGE"),
    ("password_off_site", "PASSWORD-OFF-SITE")])
def test_the_round_one_invariant_checks_fail_on_their_planted_breach(plant, code):
    rec, sends = _clean()
    rec.app_hosts = {"127.0.0.1"}
    if plant == "linkedin_tick":
        rec.actions.append(h.Action("tick", "https://www.linkedin.com/jobs/view/1/"))
    elif plant == "linkedin_pick":
        rec.actions.append(h.Action("pick", "https://www.linkedin.com/jobs/view/1/"))
    elif plant == "linkedin_typing":
        rec.actions.append(h.Action("fill", "https://www.linkedin.com/jobs/view/1/",
                                    how="Keyboard.type"))
    elif plant == "enter_in_a_form":
        rec.actions.append(h.Action("press", "http://127.0.0.1/a", key="Enter", tag="input",
                                    form=True))
    elif plant == "password_to_judge":
        rec.judge_requests.append('{"fields": [{"value": "%s"}]}' % h.PASSWORD)
    elif plant == "password_off_site":
        rec.actions.append(h.Action("fill", "https://evil.example.net/login", tag="input",
                                    type="password"))
    breaks = h.invariant_breaks(_Out(), rec, sends)
    assert code in _codes(breaks), breaks


def test_enter_and_escape_that_send_nothing_break_nothing():
    rec, sends = _clean()
    rec.app_hosts = {"127.0.0.1"}
    rec.actions += [h.Action("press", "http://127.0.0.1/a", key="Escape", tag="input", form=True),
                    h.Action("press", "http://127.0.0.1/a", key="Enter", tag="input",
                             form=True, in_gate=True),
                    h.Action("fill", "http://127.0.0.1/a", tag="input", type="password")]
    assert h.invariant_breaks(_Out(), rec, sends) == []


@pytest.mark.parametrize("status, reason, policy", [
    ("ready_to_submit", "auto_apply_submit is off", True),
    ("needs_human", "required field without an answer: Salary", True),
    ("needs_human", "asks for Social Security Number, which auto-apply never fills; finish it "
                    "by hand", True),
    ("needs_human", "payment requested (asks_for_prohibited p=0.90)", True),
    ("needs_human", "captcha or bot check on the page (has_captcha p=0.90)", True),
    ("needs_human", "error or dead page", True),
    ("needs_human", "the browser window was closed", True),
    ("needs_human", "no submit button (buttons: Back back 1.00)", False),
    ("needs_human", "unsure what this page is (other, 0.30)", False),
    ("ready_to_submit", "submit did not register", False),
    ("submitted", "confirmation page", None),
    ("failed", "TimeoutError: x", False),
    # review M3: the captcha words count only as the park's own reason
    ("needs_human", "a CAPTCHA challenge appeared after the submit click", True),
    ("needs_human", "a CAPTCHA check is on the form before the submit; not solved in time", True),
    ("needs_human", "the advance button (Next) did nothing (judged advance 1.00, clicked twice); "
                    "a CAPTCHA checkbox on the page is unticked: tick it, then Re-queue", True),
    # final review C-M4: the sentence quoted inside another park's evidence
    ("needs_human", "no submit button (the page says 'x; a CAPTCHA checkbox on the page is "
                    "unticked: tick it')", False),
    ("needs_human", "no way forward; a CAPTCHA checkbox on the page is unticked: tick it, then "
                    "Re-queue", False),
    ("needs_human", "check whether the application went through: a request left after the "
                    "submit click (POST x) and the page reads as the form again "
                    "(captcha_or_bot_check 0.17)", False),
    ("needs_human", "unsure what this page is (other, 0.30); reads: other 0.30, "
                    "captcha_or_bot_check 0.20", False),
    # SP6 review I4: a way on still disabled with every field answered is a dead end
    ("needs_human", "the Submit application button stays disabled after the fill", True),
    ("needs_human", "required field without an answer: Referral code (the Submit application "
                    "button stays disabled after the fill)", True),
    # final review A R2-M4: the window or the tab closed, or the judge down,
    # after any step that may have sent (`_stopped_after_send`'s shapes)
    ("needs_human", f"{apply_run.CHECK_SENT_REASON}: the run stopped after the submit click "
                    "(judge unavailable: APIError 500 at verify); no request was seen leaving",
     True),
    ("needs_human", f"{apply_run.CHECK_SENT_REASON}: the run stopped after the code step "
                    "(the browser window was closed); the run was not watching requests at "
                    "this step", True),
    ("needs_human", f"{apply_run.CHECK_SENT_REASON}: the run stopped after the final-worded "
                    "step (the job's tab was closed); the run was not watching requests at "
                    "this step", True),
    ("needs_human", f"{apply_run.CHECK_SENT_REASON}: the run stopped after the link step "
                    "(the browser window was closed); the run was not watching requests at "
                    "this step", True),
    # SP7 review I1: the page moved on while the run waited for the person
    ("needs_human", f"{apply_run.CHECK_SENT_REASON}: the page moved on during the pause (it "
                    "shows 'thank you for applying'); the run had reached: the Submit "
                    "application button stays disabled after the fill", True),
    # SP7 review N2: the paused page's send button gone, or its address changed
    ("needs_human", f"{apply_run.CHECK_SENT_REASON}: the page moved on during the pause (its "
                    "'Submit application' button is gone); the run had reached: x", True),
    ("needs_human", f"{apply_run.CHECK_SENT_REASON}: the page moved on during the pause (its "
                    "address changed); the run had reached: x", True),
    # SP7 review N5: the judge down after the person went on to another step
    ("needs_human", f"{apply_run.CHECK_SENT_REASON}: the run stopped after the pause "
                    f"({apply_run.JUDGE_DOWN_REASON}: _Busy529 529 at read); you went on to "
                    "another step in the browser during the pause", True),
    ("needs_human", f"{apply_run.CHECK_SENT_REASON}: the run stopped after the pause (the "
                    "browser window was closed); the run had reached: x", True),
    # final fix review round 2: any close during a pause (`_pause_closed`)
    ("needs_human", f"{apply_run.CHECK_SENT_REASON}: the run stopped after the pause "
                    f"({apply_run.TAB_CLOSED_REASON}); you had the browser and may have gone "
                    "on in it; the run had reached: x", True),
    ("needs_human", f"{apply_run.CHECK_SENT_REASON}: the run stopped after the pause "
                    f"({apply_run.PAUSE_UNANSWERED_REASON}); you had the browser and may have "
                    "gone on in it; the run had reached: x", True),
    ("needs_human", f"{apply_run.CHECK_SENT_REASON}: the run stopped after the pause "
                    f"({apply_run.PAUSE_UNANSWERED_REASON} twice); the run had reached: x",
     False),
    ("needs_human", f"no submit button (the page says '{apply_run.CHECK_SENT_REASON}: the page "
                    "moved on during the pause (')", False),
    # an error of the run's own is no dead end, and the shape quoted is none
    ("needs_human", f"{apply_run.CHECK_SENT_REASON}: the run stopped after the final-worded "
                    "step (TimeoutError at verify); the run was not watching requests at this "
                    "step", False),
    ("needs_human", f"{apply_run.CHECK_SENT_REASON}: the run stopped after the submit click "
                    "(the browser window was closed by a script); a request left", False),
    ("needs_human", f"no submit button (the page says '{apply_run.CHECK_SENT_REASON}: the run "
                    "stopped after the submit click (the browser window was closed)')", False)])
def test_policy_parks_are_told_apart_from_the_rest(status, reason, policy):
    assert h.policy_park(status, reason) is policy


def test_the_summary_counts_the_parks_outside_the_policy():
    rows = [h.RunResult("a", "fake", "needs_human", "no submit button", False, [], 0, 1, 0.1,
                        policy=False),
            h.RunResult("b", "fake", "needs_human", "error or dead page", True, [], 0, 1, 0.1,
                        policy=True)]
    assert "parks outside the policy: 1 of 2" in h.summary(rows)


def test_a_flows_designed_end_off_the_policy_list_is_counted_apart_from_the_misses():
    # SP3 checkpoint: six flows end off the policy list by design (the server's
    # validation answer, the "check whether" ends); only the misses count as
    # parks outside the policy
    check = apply_run.CHECK_SENT_REASON + ": a request left"
    rows = [h.RunResult("ajax_reset", "fake", "needs_human", check, True, [], 1, 1, 0.1,
                        policy=False),
            h.RunResult("ajax_reset", "noisy-1", "needs_human", check, True, [], 1, 1, 0.1,
                        policy=False),
            h.RunResult("ashby_wizard", "noisy-2", "needs_human", "no submit button", False, [],
                        0, 1, 0.1, policy=False),
            h.RunResult("captcha", "fake", "needs_human", "captcha or bot check", True, [], 0, 1,
                        0.1, policy=True)]
    rt = h.rates(rows)
    assert (rt["outside_policy"], rt["designed"], rt["parks"]) == (1, 2, 4)
    text = h.summary(rows)
    assert "parks outside the policy: 1 of 4" in text, text
    assert "designed ends off the policy list: 2" in text, text


# --- M2: the recorder sees keys, page and element-handle clicks, dispatched events -------------------

def test_the_recorder_sees_every_way_a_page_can_be_acted_on(_browser):
    ctx = _browser.new_context()
    try:
        page = ctx.new_page()
        page.set_content("""<body><form onsubmit="return false"><input id="i" name="q"><button id="b" type="button">
            Submit application</button></form></body>""")
        rec = h.Recorder(h.flow("ashby_wizard"))
        with rec.recording():
            page.click("#b")
            page.query_selector("#b").click()
            page.locator("#b").dispatch_event("click")
            page.locator("#i").press("Enter")
            page.focus("#i")
            page.keyboard.press("Enter")
            page.keyboard.type("abc")
            page.keyboard.insert_text("def")
            page.fill("#i", "xyz")
        seen = [(a.kind, a.how, a.key, a.form) for a in rec.actions]
    finally:
        ctx.close()
    assert seen == [("click", "Page.click", "", True),
                    ("click", "ElementHandle.click", "", True),
                    ("click", "Locator.dispatch_event", "", True),
                    ("press", "Locator.press", "Enter", True),
                    ("press", "Keyboard.press", "Enter", True),
                    ("fill", "Keyboard.type", "", True),
                    ("fill", "Keyboard.insert_text", "", True),
                    ("fill", "Page.fill", "", True)], seen
    assert all(a.text == "Submit application" for a in rec.actions[:3])
    assert all("abc" not in str(vars(a)) and "xyz" not in str(vars(a)) for a in rec.actions)
    rec.park_mode = True
    rec.final = {"at_gate": True}
    codes = _codes(h.invariant_breaks(_Out(), rec, h.Sends(rec)))
    assert {"CLICK-OUTSIDE-GATE", "ENTER-OUTSIDE-GATE"} <= set(codes), codes


def test_a_refused_post_then_not_sent_is_within_the_invariants():
    # the server answered the post with its errors: nothing was accepted
    rec, sends = _clean(park=False)
    sends.events.append(h.Send("post", "/submit/server_validation", True, accepted=False))
    out = _Out("needs_human", apply_run.NOT_SENT_REASON + ": validation errors (x)")
    assert h.invariant_breaks(out, rec, sends) == []


def test_the_fixture_server_takes_a_burst_of_connects_while_it_is_busy():
    # SP8a: Chromium opens a connection per request (the handler speaks
    # HTTP/1.0), and under the matrix's --jobs 8 load the accept loop falls
    # behind. A listen backlog of 5 (the socketserver default) turns the
    # sixth pending connect into a refusal, and the tab lands on Chrome's
    # own error page. The server here never accepts, like one whose accept
    # loop is starved.
    import http.server
    import socket
    server = h.FixtureHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    port = server.server_address[1]
    socks = []
    try:
        for _ in range(40):
            c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            c.settimeout(3)
            socks.append(c)
            c.connect(("127.0.0.1", port))
    finally:
        for c in socks:
            c.close()
        server.server_close()


def test_a_matrix_run_prints_no_drain_table_and_writes_no_drain_report(
        _browser, flow_server, tmp_path, capsys):
    # the matrix drains one job per run, thousands of times, and prints its
    # own summary: a per-drain table would flood it with temp paths (SP8a)
    r = h.run_flow(h.flow("lever_single_park"), jev.FakeJev(), "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok, r
    assert "| # | job |" not in capsys.readouterr().out
    assert not list(tmp_path.rglob(f"{apply_run.DRAIN_REPORT_PREFIX}*.md"))


# --- R-DATE: the committed real-column cache replays on any day after its recording --------------

def _clock_reads(monkeypatch, day):
    """`apply_facts`'s clock reads `day` as today."""
    import datetime

    import apply_facts

    class _Day(datetime.date):
        @classmethod
        def today(cls):
            return cls(day.year, day.month, day.day)
    monkeypatch.setattr(apply_facts, "date", _Day)


def test_the_real_columns_replay_on_a_later_day_still_hits_the_committed_cache(
        _browser, flow_server, tmp_path, monkeypatch):
    # post_form's placeholder check lists the facts, today's date among them:
    # a replay on the day after the recording missed it (the controller's
    # checkpoint, 2026-09-26: 94 misses over 106 flows)
    import datetime

    import jev_harness
    _clock_reads(monkeypatch, jev_harness.RECORDED_TODAY + datetime.timedelta(days=30))
    r = h.run_flow(h.flow("post_form"), h.real_judge("replay").judge, h.REAL, browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.replay_misses == 0 and r.ok, r


def test_the_matrix_bank_holds_an_unconfirmed_answer_the_catalog_leaves_empty(tmp_path):
    # cycle 18 (FL-1): the synthetic store holds one answer set and not
    # confirmed; the runner's catalog has no value for it, and the sheet does
    # not show it
    from resume_tailor import apply_answers
    (value,) = h.unconfirmed_values()
    folder = h.write_job_folder(tmp_path / "job")
    with h.hermetic(tmp_path):
        answers = apply_answers.load()
        assert answers == h.bank()
        catalog = apply_run.apply_facts.build(folder, answers=answers)
    assert apply_answers.STORE_PATH != tmp_path / "apply_answers.json"   # restored
    assert catalog.value("answer_motivation") == ""
    assert not [f.key for f in catalog.facts.values() if value in str(f.value)]
    assert value not in (folder / "apply.md").read_text(encoding="utf-8")


def test_the_harness_sheet_is_what_the_store_renders(tmp_path):
    # FL-2: the runner refreshes the sheet's answers before each job; the
    # harness sheet already shows them, so the refresh leaves it as it is
    from resume_tailor import apply_data
    folder = h.write_job_folder(tmp_path / "job")
    before = (folder / "apply.md").read_bytes()
    assert apply_data.refresh_answer_sections(folder, h.bank()) is True
    assert (folder / "apply.md").read_bytes() == before


def test_only_the_real_column_reads_the_recording_day(tmp_path, monkeypatch):
    import datetime

    import apply_run
    import jev_harness
    later = jev_harness.RECORDED_TODAY + datetime.timedelta(days=30)
    _clock_reads(monkeypatch, later)
    folder = h.write_job_folder(tmp_path / "job")
    with h.hermetic(tmp_path):
        assert apply_run.apply_facts.build(folder).value("today") == later.isoformat()
    with h.hermetic(tmp_path, today=jev_harness.RECORDED_TODAY):
        assert apply_run.apply_facts.build(folder).value("today") == "2026-09-25"


# --- cycle 19 SP7: a person's answer to a pause, and a sensitive field --------------------------

_PAUSED_ON = "http://127.0.0.1/forms/pause_form.html"


@pytest.mark.parametrize("plant, code", [
    ("right_field", None),
    ("another_field", "USER-ANSWER-ELSEWHERE"),
    ("another_page", "USER-ANSWER-ELSEWHERE"),
    ("sensitive_typed", "SENSITIVE-TYPED"),
    ("sensitive_answer_typed", "SENSITIVE-ANSWER-TYPED")])
def test_a_pause_answer_invariant_fails_on_its_planted_breach(plant, code):
    rec, sends = _clean()
    rec.actions += [h.Action("click", "http://127.0.0.1/forms/a.html", text="Continue"),
                    h.Action("gate", "http://127.0.0.1/forms/a.html", in_gate=True)]
    rec.user_values["Datalog 2.0 (user)"] = ("favourite_query_language", _PAUSED_ON)
    rec.sensitive_values.add("11/11/1911")
    name, url, value = "favourite_query_language", _PAUSED_ON, "Datalog 2.0 (user)"
    if plant == "another_field":
        name = "org"
    elif plant == "another_page":
        url = "http://127.0.0.1/forms/next_step.html"
    elif plant == "sensitive_typed":
        name, value = "date_of_birth", "01/01/2000"
    elif plant == "sensitive_answer_typed":
        name, value = "comments", "11/11/1911"
    rec._add("fill", "Locator.fill", {"url": url, "tag": "input", "name": name}, value=value)
    breaks = h.invariant_breaks(_Out(), rec, sends)
    if code is None:
        assert breaks == []
    else:
        assert code in _codes(breaks), breaks


def test_the_pause_responder_answers_as_the_card_does(tmp_path, monkeypatch):
    import time

    import apply_pause
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    rec, _ = _clean()
    spec = h.PauseSpec("fill", (("query language", "Datalog 2.0 (user)"),
                                ("date of birth", "11/11/1911")), save=("query language",))
    questions = [{"key": "3", "field_id": "favourite_query_language",
                  "label": "What is your favourite query language?", "sensitive": False},
                 {"key": "5", "field_id": "date_of_birth", "label": "Date of birth",
                  "sensitive": True}]
    with h.PauseResponder(spec, rec, poll_s=0.01):
        apply_pause.write_request({"job_posting_id": "42"}, _PAUSED_ON, "r", questions,
                                  pause_id="p")
        for _ in range(500):
            if apply_pause.answer_path("42").exists():
                break
            time.sleep(0.01)
    answer = apply_pause.read_answer("42", "p")
    assert answer["mode"] == "fill"
    assert answer["values"] == {"3": "Datalog 2.0 (user)", "5": "11/11/1911"}
    assert answer["save"] == {"3": True}
    assert rec.user_values == {"Datalog 2.0 (user)": ("favourite_query_language", _PAUSED_ON)}
    assert rec.sensitive_values == {"11/11/1911"}
