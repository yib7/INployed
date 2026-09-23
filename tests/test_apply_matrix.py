"""SP1: the noisy judge, the invariant checks, and the flow matrix.

- `NoisyJev`: deterministic per (seed, request), fresh noise for a changed
  request, misreads only toward plausible neighbours, never moves a submit
  role, drops only field answers; no production mode reaches it.
- `apply_harness.invariant_breaks`: each check fails on a planted breach and
  stays quiet on a clean run; end to end, a loop sabotaged to click its
  submit outside the gate and a judge that reads a form as a confirmation
  are both caught.
- The matrix: every registered flow under `FakeJev` and the first three
  noisy seeds (the script runs twenty), zero invariant breaks, and the
  success rates at or above the pinned floors (`apply_harness.SUCCESS_FLOOR`,
  `FAKE_SUCCESS_FLOOR`), which later phases raise.

Headless Chromium through the module-scoped test browser; the judge is the
fake or the noisy one; no network but the local server and routed hosts."""
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
    judge = jev.NoisyJev(_Scripted(), 2, swap_p=0.0, conf_scale=0.75, drop_p=0.0)
    seen = []
    for i in range(30):
        out = judge.judge(*_request(title=f"Form {i}"))
        for qid, a in out.items():
            if a.kind == "choice":
                assert 0.75 <= a.confidence <= 1.0, (qid, a)
                assert a.probabilities[a.choice] == max(a.probabilities.values())
                seen.append(a.confidence)
            else:
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
    ("password_in_log", "PASSWORD-LEAK")])
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
    breaks = h.invariant_breaks(out, rec, sends)
    assert code in _codes(breaks), breaks
    with pytest.raises(AssertionError, match=code):
        h.assert_invariants(out, rec, sends)


def test_a_submitted_unconfirmed_with_a_send_is_within_the_invariants():
    rec, sends = _clean(park=False)
    sends.events.append(h.Send("dom", "x", True))
    out = _Out("submitted", "submitted (unconfirmed): the page after submit reads as other (0.5)")
    assert h.invariant_breaks(out, rec, sends) == []


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
            out["page_state"] = jev.Answer(kind="choice", choice="confirmation",
                                           probabilities={"confirmation": 0.9,
                                                          "application_form": 0.1},
                                           confidence=0.9)
        return out


def test_a_confident_confirmation_misread_before_any_submit_is_caught(
        _browser, flow_server, tmp_path):
    r = h.run_flow(h.flow("ashby_wizard"), _FormAsConfirmation(), "misread", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert (r.status, r.reason) == ("submitted", "confirmation page"), r
    assert _codes(r.breaks) == ["FALSE-SUBMITTED"], r.breaks


# --- the matrix -------------------------------------------------------------------------------------

def test_the_flow_matrix_holds_every_invariant_and_the_success_floors(
        _browser, flow_server, tmp_path):
    results = h.run_matrix(h.FLOWS, h.judges(h.SUITE_SEEDS), browser=_browser,
                           server=flow_server, workdir=tmp_path)
    table = h.summary(results)
    rates = h.rates(results)
    assert len(h.FLOWS) >= 12
    assert rates["breaks"] == 0, table
    assert rates["fake"] >= h.FAKE_SUCCESS_FLOOR, table
    assert rates["noisy"] >= h.SUCCESS_FLOOR, table
