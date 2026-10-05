"""The flow harness's registry: the scripted judges a flow reads its pages
with, the pause answers (`PauseSpec`, `PauseResponder`, `pausing`), the
`Flow` record and `FLOWS`, every multi-page fixture flow with the end it
must reach. `flow(name)` looks one up.

Part of the auto-apply flow harness; `apply_harness` re-exports it. A test
that swaps the registry patches `apply_flows.FLOWS`.
"""
from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

# apply_pages first: it puts `local/` on the path the modules below import from
from apply_pages import (board_chain_routes, board_routes, _CAREERS, _COMBINED, FIXTURES_DIR,
                         _host, _LINKEDIN_JOB, linkedin_job_routes, _linkedin_routes, _no_routes,
                         on_linkedin, Patches, tracker_routes)
import apply_run
import jev

if TYPE_CHECKING:
    from apply_invariants import Recorder


def read_as(out: dict, state: str, conf: float, probabilities: dict | None = None, *,
            nouls: str = "coherent") -> dict:
    """A test judge's scripted reading of a page as `state` at `conf` (the
    page read is a Choice and Nouls). `nouls`: "coherent" reads the
    Nouls with it (`state`'s yes, every other kind's no), as a judge that
    took the page for `state` would; "neutral" sets them to 0.5 (no word
    either way); "keep" leaves the wrapped judge's. Only the answers the
    request asked are set; `out` is returned."""
    if "page_state" in out:
        out["page_state"] = jev.Answer(kind="choice", choice=state, confidence=conf,
                                       probabilities=dict(probabilities or {state: conf}))
    if nouls == "keep":
        return out
    for kind, qids in jev.PAGE_KIND_NOULS.items():
        for qid in qids:
            if qid in out:
                p = 0.5 if nouls == "neutral" else (0.9 if kind == state else 0.1)
                out[qid] = jev.Answer(kind="noul", noul=p)
    return out


class LinkedInReadAsOther:
    """A judge that reads every LinkedIn page as `other` at 0.30 (as a real
    run once read one, and parked) and passes the rest to the judge it wraps."""

    def __init__(self, inner: Any):
        self.inner = inner

    def judge(self, state: Any, questions: dict) -> dict:
        out = dict(self.inner.judge(state, questions))
        host = str(((state or {}).get("page") or {}).get("url_host") or "")
        if "page_state" in out and on_linkedin(f"https://{host}/"):
            out["page_state"] = jev.Answer(
                kind="choice", choice="other", confidence=0.30,
                probabilities={"other": 0.30, "job_posting": 0.28, "application_form": 0.22,
                               "login_wall": 0.20})
        return out


def _button_texts(state: Any) -> list[tuple[Any, str]]:
    """(n, text) of a request's buttons (the page read's and the mapping's)."""
    return [(b.get("n"), str(b.get("text") or "")) for b in (state or {}).get("buttons") or []]


def _noul(p: float) -> Any:
    return jev.Answer(kind="noul", noul=p)


class ModalReadAsForm:
    """A judge that reads Workday's start dialog (a page with an "Apply
    Manually" button) as an application form at 0.90, its Nouls with it (a
    form's details yes, a posting's no), and "Apply Manually" as the advance
    at 0.90, and passes the rest to the judge it wraps."""

    def __init__(self, inner: Any):
        self.inner = inner

    def judge(self, state: Any, questions: dict) -> dict:
        out = dict(self.inner.judge(state, questions))
        buttons = _button_texts(state)
        if not any(text == "Apply Manually" for _, text in buttons):
            return out
        if "page_state" in out:
            out["page_state"] = jev.Answer(
                kind="choice", choice="application_form", confidence=0.90,
                probabilities={"application_form": 0.90, "job_posting": 0.10})
            for qid, p in (("page_applicant_details", 0.9), ("page_job_description", 0.1),
                           ("page_apply_entry", 0.1)):
                if qid in out:
                    out[qid] = _noul(p)
        for n, text in buttons:
            qid = f"button_{n}_role"
            if text == "Apply Manually" and qid in out:
                out[qid] = jev.Answer(kind="choice", choice="advance", confidence=0.90,
                                      probabilities={"advance": 0.90, "apply_entry": 0.10})
        return out


class CommitmentUnderFloor:
    """A judge that reads a background-check box as a consent under
    `CONSENT_MIN_CONF` on every look: 0.84 of the inner judge's confidence
    (under NoisyJev, 0.63 to 0.84). The commitment floor then decides, and
    the run must park on the box, never tick it."""

    def __init__(self, inner: Any):
        self.inner = inner

    def judge(self, state: Any, questions: dict) -> dict:
        out = dict(self.inner.judge(state, questions))
        for row in (state or {}).get("fields") or []:
            qid = f"field_{row.get('n')}_source"
            a = out.get(qid)
            if "background check" in str(row.get("label", "")).lower() and a is not None \
                    and a.choice == "consent_attest":
                conf = round(0.84 * float(a.confidence or 0.0), 4)
                out[qid] = jev.Answer(kind="choice", choice="consent_attest", confidence=conf,
                                      probabilities={"consent_attest": conf,
                                                     "leave_blank": round(1 - conf, 4)})
        return out


class HeadlineLeftBlank:
    """A judge that maps a "Headline" box to `leave_blank` at 0.95, and
    reads any value in it as not the sheet's (0.10): the sheet names no
    headline, as a real judge sees, where the fake one's word match takes a
    fact and finds "form" and "field" in every sheet. The rest goes to the
    judge it wraps."""

    def __init__(self, inner: Any):
        self.inner = inner

    def judge(self, state: Any, questions: dict) -> dict:
        out = dict(self.inner.judge(state, questions))
        for row in (state or {}).get("fields") or []:
            qid = f"field_{row.get('n')}_source"
            if str(row.get("label", "")).strip() == "Headline" and qid in out:
                out[qid] = jev.Answer(kind="choice", choice="leave_blank", confidence=0.95,
                                      probabilities={"leave_blank": 0.95, "full_name": 0.05})
        for qid, q in questions.items():
            instructions = q.get("instructions")
            if qid.startswith("verify_") and isinstance(instructions, dict) \
                    and instructions.get("field_label") == "Headline" and qid in out:
                out[qid] = _noul(0.1)
        return out


class OptionalLeftBlank:
    """A judge whose first look leaves the optional-looking "Years of
    experience", "Portfolio URL" and "Badge number" boxes without a mapping
    (a read under the floor, which an optional box gets no second look
    for); a request that carries them as required (the repair's, after the
    form said so) is the wrapped judge's."""
    LABELS = ("Years of experience", "Portfolio URL", "Badge number")

    def __init__(self, inner: Any):
        self.inner = inner

    def judge(self, state: Any, questions: dict) -> dict:
        out = dict(self.inner.judge(state, questions))
        for row in (state or {}).get("fields") or []:
            if str(row.get("label", "")).strip() in self.LABELS and not row.get("required"):
                out.pop(f"field_{row.get('n')}_source", None)
        return out


class PauseLeftBlank:
    """A judge that maps the boxes labelled as in `LABELS` to `leave_blank`
    at 0.95 (no fact on the sheet answers them), as a real judge reads them:
    pause_disabled.html's optional referral code, so its
    Submit stays disabled after the fill, and pause_form.html's two "Please
    explain" boxes, so the run asks both. The rest goes to the judge it
    wraps."""
    LABELS = ("Referral code", "Please explain")

    def __init__(self, inner: Any):
        self.inner = inner

    def judge(self, state: Any, questions: dict) -> dict:
        out = dict(self.inner.judge(state, questions))
        for row in (state or {}).get("fields") or []:
            qid = f"field_{row.get('n')}_source"
            label = str(row.get("label", "")).strip().rstrip(" *")
            if label in self.LABELS and qid in out:
                out[qid] = jev.Answer(kind="choice", choice="leave_blank", confidence=0.95,
                                      probabilities={"leave_blank": 0.95})
        return out


class VerifiedReadAsConfirmation:
    """A judge that reads an email-verified page as a confirmation at 0.90,
    its received Noul yes (the I5 shape: "Your email is verified, thank
    you")."""

    def __init__(self, inner: Any):
        self.inner = inner

    def judge(self, state: Any, questions: dict) -> dict:
        out = dict(self.inner.judge(state, questions))
        text = str(((state or {}).get("page") or {}).get("headline_text") or "")
        if "page_state" in out and "email is verified" in text:
            out["page_state"] = jev.Answer(kind="choice", choice="confirmation", confidence=0.90,
                                           probabilities={"confirmation": 0.90, "other": 0.10})
            if "page_received" in out:
                out["page_received"] = _noul(0.9)
        return out


# local stand-ins for a bot-check provider's frames (the flows route the
# provider's URL here; nothing reaches the provider)
_CHECKBOX_STUB = ("<!doctype html><html><body><div role=\"checkbox\" aria-checked=\"false\">"
                  "I'm not a robot</div></body></html>")
_CHALLENGE_STUB = ("<!doctype html><html><body><p>Select every image with a bus</p>"
                   "<button type=\"button\">Verify</button></body></html>")


@dataclass(frozen=True)
class PauseSpec:
    """How a pause flow's person answers: `mode` is "fill",
    "browser" or "park", or "timeout" (no answer comes); `values` are
    (words of the question's label, the value) pairs; `save` names the
    labels whose value is kept for future runs; `by_id` are (field id, value)
    pairs, read first (two fields with the same label)."""
    mode: str
    values: tuple[tuple[str, str], ...] = ()
    save: tuple[str, ...] = ()
    by_id: tuple[tuple[str, str], ...] = ()

    def answer(self, request: dict) -> tuple[str, dict[str, str], dict[str, bool]]:
        values: dict[str, str] = {}
        save: dict[str, bool] = {}
        ids = dict(self.by_id)
        for q in request.get("questions") or []:
            label = str(q.get("label") or "").lower()
            if str(q.get("field_id") or "") in ids:
                values[str(q["key"])] = ids[str(q["field_id"])]
                continue
            for words, value in self.values:
                if words.lower() in label:
                    values[str(q["key"])] = value
                    break
            if any(words.lower() in label for words in self.save):
                save[str(q["key"])] = True
        return self.mode, values, save


class PauseResponder:
    """Answers the run's pauses from a thread as the dashboard's card does:
    each request that lands in `apply_pause.pause_dir()` gets the answer
    `spec` gives (`PauseSpec.answer`), written with `apply_pause.write_answer`.
    With a `recorder`, every value given is noted with the field it answers
    and the page the run paused on (the pause invariants), before the answer
    file lands."""

    def __init__(self, spec: PauseSpec, recorder: Recorder | None = None, *,
                 poll_s: float = 0.05):
        self.spec = spec
        self.recorder = recorder
        self.poll_s = poll_s
        self.requests: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pause-responder", daemon=True)

    def start(self) -> "PauseResponder":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def __enter__(self) -> "PauseResponder":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _run(self) -> None:
        import apply_pause
        seen: set[str] = set()
        while not self._stop.is_set():
            for req in apply_pause.pending_requests():
                pid = str(req.get("pause_id") or "")
                if pid in seen:
                    continue
                seen.add(pid)
                self.requests.append(req)
                mode, values, save = self.spec.answer(req)
                if self.recorder is not None:
                    for q in req.get("questions") or []:
                        value = values.get(str(q["key"]))
                        if not value:
                            continue
                        if q.get("sensitive"):
                            self.recorder.sensitive_values.add(value)
                        else:
                            self.recorder.user_values[value] = (str(q.get("field_id") or ""),
                                                                str(req.get("page_url") or ""))
                apply_pause.write_answer(req["job"], mode, values, save, pause_id=pid)
            self._stop.wait(self.poll_s)


@contextmanager
def pausing(rundir: Path, spec: PauseSpec | None, recorder: Recorder | None = None):
    """A pause flow's run: the pause folder in `rundir`, pauses on (the
    suite's `apply_pause.NEVER_WAIT` off), a short poll, a minute of one
    second for the timeout flow, and the responder answering. Yields the
    `auto_apply_pause_minutes` the run takes: 0 (no pause) with no `spec`."""
    import apply_pause
    if spec is None:
        yield 0
        return
    p = Patches()
    responder = None
    try:
        p.setenv("LOCALAPPDATA", str(Path(rundir) / "appdata"))
        p.setattr(apply_pause, "NEVER_WAIT", False)
        p.setattr(apply_pause, "POLL_S", 0.05)
        if spec.mode == "timeout":
            p.setattr(apply_pause, "SECONDS_PER_MINUTE", 1.0)
        else:
            responder = PauseResponder(spec, recorder).start()
        yield 1 if spec.mode == "timeout" else 10
    finally:
        if responder is not None:
            responder.stop()
        p.undo()


# The real judge's name in a run's results and the matrix's columns
# (`apply_harness.run_real`); `Flow.reached` reads it.
REAL = "real"


@dataclass(frozen=True)
class Flow:
    """One flow of the registry (see the module docstring)."""
    name: str
    start: str                      # a fixture under forms/, or an absolute URL
    submit: bool                    # auto_apply_submit
    status: str                     # the expected end
    reason: str                     # a regex the end's reason must match
    confirm: str = ""               # the confirmation marker (a Playwright selector)
    gate: str = ""                  # park mode: shown on the page the run stopped at
    send_urls: tuple[str, ...] = ()  # requests that are the send (globs)
    routes: Callable[[str], dict[str, Any]] = _no_routes   # a glob -> HTML or a route handler
    password: bool = False          # a synthetic master password is stored
    inbox: bool = False             # the fixture inbox is the run's inbox
    inbox_page: str = "outlook_list.html"   # which one, under tests/fixtures/inbox/
    ats: dict[str, str] = field(default_factory=dict)
    settle_s: float | None = None   # the quiet window when a page moves on by a timer
    # ((module, name), value) caps raised for this flow on top of `FAST_TIMING`:
    # a wait that ends on its condition (a placeholder clearing) is given room
    # to, so a busy machine never ends it by its cap (the skeleton fixture)
    timing: tuple = ()
    # JS the run's page evaluates after every `apply_form.extract` of it: a
    # fixture that moves on once it has been read (the skeleton)
    # waits for that condition, never for a clock
    on_read: str = ""
    covers: str = ""                # what the flow exercises
    # "<phase>: why": the fake judge does not reach the end yet; the matrix
    # reports the flow apart and leaves it out of the rates the floors read
    known: str = ""
    easy_apply: bool = False        # the queue entry's `is_easy_apply`
    wrap: Callable[[Any], Any] | None = None    # wraps every judge the flow runs under
    opens_no_page: bool = False     # the run must end before any page opens
    suite_seeds: int | None = None  # the suite runs this many noisy seeds (a slow flow: fewer)
    # the run ends before the judge reads a page (LinkedIn's job page is
    # decided by its handler alone): when the fake run asked the judge
    # nothing, every noisy seed's run is that run, and the suite copies it
    # (`judge_requests` 0 is checked); the script runs every seed
    judge_free: bool = False
    # the page's text changes with the clock, so no two runs ask the judge
    # the same request: a replay of the real judge's answers leaves the flow
    # out (its recording's run is its real column)
    replayable: bool = True
    # the real judge's cache holds a run of it: False for a flow added after
    # the last recording (a round that records nothing adds flows too). A
    # replay leaves it out and names it; the next recording takes it in
    recorded: bool = True
    # the real judge's end, (status, a regex the reason must match), when it
    # reads what the fake cannot: the fake answers not_settled to every settle
    # read, so a reworded question the saved answers settle parks under the
    # fake and its noisy seeds and fills under the real judge
    real_end: tuple[str, str] = ()
    # the run pauses on a question it can ask, and this is how
    # the person answers (`PauseSpec`); None: the run never pauses
    pause: PauseSpec | None = None

    def start_url(self, base: str) -> str:
        return self.start if "://" in self.start else f"{base}/forms/{self.start}"

    def app_hosts(self, base: str) -> set[str]:
        """Where the application lives, the only hosts the master password may
        be typed on: the fixture server, and the start page's host off
        LinkedIn."""
        hosts = {_host(base)}
        start = _host(self.start_url(base))
        if not on_linkedin(self.start_url(base)):
            hosts.add(start)
        return hosts

    def reached(self, status: str, reason: str, final: dict, judge: str = "") -> bool:
        """The run reached this flow's expected end: `real_end` under the
        real judge when the flow names one."""
        want, pattern = (self.real_end if judge == REAL and self.real_end
                         else (self.status, self.reason))
        if status != want or not re.search(pattern, reason or ""):
            return False
        if status == "submitted" and self.confirm and not final.get("confirmed"):
            return False
        if status == "ready_to_submit" and self.gate and not final.get("at_gate"):
            return False
        return True


# another company's job, where a run that took another job's Apply would land
_OTHER_JOB = """<!doctype html><html><head><title>Data Analyst at Contoso</title></head><body>
<h1>Data Analyst</h1><p>Contoso, New York</p>
<label>Full name * <input name="name" required></label>
<button type="button" onclick="document.body.dataset.wrongJob = 1">Submit application</button>
</body></html>"""

_SUBMITTED = r"^confirmation page"
_PARKED = r"^auto_apply_submit is off$"
_EASY_APPLY = "^" + re.escape(apply_run.EASY_APPLY_REASON) + "$"

FLOWS: tuple[Flow, ...] = (
    Flow("ashby_wizard", "ashby_steps.html", True, "submitted", _SUBMITTED,
         confirm="#received:visible", covers="a three-step wizard to its confirmation"),
    Flow("ashby_wizard_park", "ashby_steps.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible",
         covers="the wizard in park mode stops at its submit"),
    Flow("native_wizard", "native_submit_steps.html", True, "submitted", _SUBMITTED,
         confirm="#received:visible", covers="type=submit Continue, then the final submit"),
    Flow("posting_popup_park", "job_posting.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible",
         covers="a posting whose Apply opens the form in a new tab"),
    Flow("linkedin_posting", _LINKEDIN_JOB, True, "submitted", _SUBMITTED,
         confirm="#received:visible", routes=_linkedin_routes,
         covers="LinkedIn's job page, its Apply link through the redirector, the form"),
    Flow("greenhouse_embed", "greenhouse_embed.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible", covers="a company page embedding the form in an iframe"),
    Flow("lever_single_park", "lever_single.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a one-page form in park mode"),
    Flow("login_wall_park", "login_wall.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible", password=True,
         covers="a sign-in screen, then the wizard"),
    Flow("login_two_step_park", "login_email_first.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible", password=True,
         covers="the address screen, the password screen, then the wizard"),
    Flow("signup_park", "signup.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible", password=True,
         covers="a sign-up screen, then the wizard"),
    Flow("combined_signup", f"{_CAREERS}/apply/42", True, "submitted", _SUBMITTED,
         confirm="#received:visible", password=True,
         routes=lambda base: {f"{_CAREERS}/**": _COMBINED},
         covers="one page that makes the account and sends the application"),
    Flow("submit_code", "submit_code.html", True, "submitted", _SUBMITTED,
         confirm="body[data-submitted]", send_urls=("**/forms/code_gate.html",), inbox=True,
         ats={"system": "greenhouse"},
         covers="the submit, the emailed-code gate read from the inbox, the confirmation"),
    Flow("review_steps", "review_steps.html", True, "submitted", _SUBMITTED,
         confirm="#received:visible", covers="a form step, a review page, the submit"),
    Flow("review_steps_park", "review_steps.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible",
         covers="the review page in park mode"),
    Flow("post_form", "post_form.html", True, "submitted", _SUBMITTED,
         confirm="body[data-confirmed]", covers="a real form POST the server counts"),
    Flow("captcha", "captcha.html", True, "needs_human", r"^captcha or bot check",
         covers="a bot check nobody solves parks"),
    # --- the entry (Easy Apply, the LinkedIn job page, settling, consent) ---
    Flow("linkedin_easy_apply", _LINKEDIN_JOB, True, "needs_human", _EASY_APPLY,
         routes=linkedin_job_routes("linkedin_easy_apply.html", "lever_single.html"),
         judge_free=True,
         covers="a job page whose only Apply is Easy Apply (its aria-label) stops unclicked"),
    Flow("linkedin_easy_apply_modal", _LINKEDIN_JOB, True, "needs_human", _EASY_APPLY,
         routes=linkedin_job_routes("linkedin_easy_apply_modal.html", "lever_single.html"),
         judge_free=True,
         covers="LinkedIn's own form open in a modal: nothing filled, the run stops"),
    Flow("linkedin_easy_apply_flag", _LINKEDIN_JOB, True, "needs_human", _EASY_APPLY,
         routes=linkedin_job_routes(), easy_apply=True, opens_no_page=True,
         judge_free=True,
         covers="an Easy Apply queue entry ends before any page opens"),
    Flow("linkedin_posting_late", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_posting_late.html", "lever_single.html"),
         covers="a top card that renders 2.5 s after load, then the company's form"),
    Flow("linkedin_posting_noise", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_posting_noise.html", "lever_single.html"),
         covers="an alert switch, a feedback Submit, the messaging search and a hidden "
                "sign-in form beside the offsite Apply"),
    Flow("linkedin_gts_other", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_posting_noise.html", "lever_single.html"),
         wrap=LinkedInReadAsOther,
         covers="the GTS park: a posting with stray controls the judge reads as other at "
                "0.30 still reaches the company's form"),
    Flow("linkedin_applied", _LINKEDIN_JOB, True, "needs_human", r"^already applied: ",
         routes=linkedin_job_routes("linkedin_applied.html", "lever_single.html"),
         judge_free=True,
         covers="a job LinkedIn shows as applied is not applied to again"),
    Flow("linkedin_closed", _LINKEDIN_JOB, True, "needs_human", r"^closed: ",
         routes=linkedin_job_routes("linkedin_closed.html", "lever_single.html"),
         judge_free=True,
         covers="a posting that no longer accepts applications"),
    Flow("linkedin_signed_out", _LINKEDIN_JOB, True, "needs_human", r"^LinkedIn is signed out",
         routes=linkedin_job_routes("linkedin_signed_out.html", "lever_single.html"),
         judge_free=True,
         covers="a sign-in dialog and no offsite Apply: the user signs in to LinkedIn"),
    Flow("linkedin_safety_interstitial", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_posting.html", "lever_single.html",
                                    hop="linkedin_safety_interstitial.html"),
         covers="the safety reminder on the hop, which waits for its Continue"),
    Flow("spa_late_render", "spa_late_render.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a posting that renders 800 ms after load, then its same-tab Apply"),
    Flow("consent_overlay", "consent_overlay.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a cookie dialog over the posting, declined through its input button"),
    Flow("consent_wrapper", "consent_wrapper.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a consent vendor's sizeless wrapper holding a fixed banner and a page filter"),
    Flow("linkedin_two_pane", "https://www.linkedin.com/jobs/search/?currentJobId=4438751519"
         "&keywords=analytics", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_two_pane.html", "lever_single.html"),
         covers="the two-pane view: Easy Apply pills and cards, the job's pane rendered late"),
    Flow("linkedin_button_popup", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_posting_button.html", "lever_single.html"),
         covers="a signed-in page whose offsite Apply is a button opening a tab by script"),
    Flow("linkedin_interstitial_tab", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_posting.html", "lever_single.html",
                                    hop="linkedin_safety_interstitial_tab.html", same_tab=True),
         covers="a same-tab Apply onto the safety reminder, whose Continue opens a new tab"),
    Flow("linkedin_apply_in_list", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=linkedin_job_routes("linkedin_apply_in_list.html", "lever_single.html"),
         covers="a top card's Apply in a list item, a rail of other jobs' Apply beside it"),
    Flow("linkedin_more_jobs_late", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         routes=lambda base: {**linkedin_job_routes("linkedin_more_jobs_late.html",
                                                    "lever_single.html")(base),
                              "https://careers.contoso.example/**": _OTHER_JOB},
         covers="a top card rendered late beside another job's card with a company-site Apply"),
    # --- submit truth and the gate's invariants ---
    Flow("native_required_submit", "native_required_submit.html", True, "needs_human",
         r"^required field without an answer: Cover letter", confirm="#received:visible",
         covers="a hidden required box behind a rich-text editor: the gate reads the form's "
                "validity and stops before the click"),
    Flow("server_validation", "server_validation.html", True, "needs_human",
         r"^the submit did not go through: validation errors", confirm="#received:visible",
         covers="the server answers the post with the same form marked invalid: nothing sent"),
    Flow("submit_then_challenge", "submit_then_challenge.html", True, "needs_human",
         r"^a CAPTCHA challenge appeared after the submit click", confirm="#received:visible",
         routes=lambda base: {"https://hcaptcha.com/**": _CHALLENGE_STUB},
         covers="the submit raises a bot-check challenge: the person solves it"),
    Flow("slow_post", "slow_post.html", True, "submitted", _SUBMITTED,
         confirm="body[data-confirmed]", suite_seeds=1,
         covers="a form post answered after the click's action timeout: the click stays "
                "clicked, the answer is read"),
    Flow("posting_with_alert_box", "posting_with_alert_box.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         covers="a job-alert box beside the posting's Apply: the Apply is the entry"),
    Flow("workday_start_modal", "workday_start_modal.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible", wrap=ModalReadAsForm,
         covers="Workday's start dialog read as a form: Apply Manually opens it, never the gate"),
    Flow("recaptcha_checkbox", "recaptcha_checkbox.html", True, "needs_human",
         r"^a CAPTCHA check is on the form before the submit", confirm="#received:visible",
         routes=lambda base: {"https://www.google.com/recaptcha/**": _CHECKBOX_STUB},
         covers="a 78 px reCAPTCHA checkbox with an empty token: the form is filled, then the "
                "person ticks it"),
    Flow("recaptcha_checkbox_park", "recaptcha_checkbox.html", False, "ready_to_submit",
         r"^auto_apply_submit is off; a CAPTCHA checkbox is on the form",
         confirm="#received:visible", gate="#btn-submit:visible",
         routes=lambda base: {"https://www.google.com/recaptcha/**": _CHECKBOX_STUB},
         covers="park mode fills the form and stops at the gate; the person ticks the box"),
    Flow("postback_emptied", "postback_emptied.html", True, "needs_human",
         r"^check whether the application went through: a request left",
         covers="the post back shows a note above the same form emptied: the send may have "
                "gone, never read as not sent"),
    Flow("ajax_reset", "ajax_reset.html", True, "needs_human",
         r"^check whether the application went through: a request left",
         covers="a fetch send, then the form reset: never read as not sent"),
    Flow("success_flash_emptied", "success_flash.html", True, "needs_human",
         r"^check whether the application went through: a request left",
         covers="a success flash (role=alert) above the same form emptied after the post: "
                "never read as not sent"),
    Flow("reset_aria_invalid", "reset_aria_invalid.html", True, "needs_human",
         r"^check whether the application went through: a request left",
         covers="a fetch send, then a reset form with aria-invalid and its message on every "
                "box: never read as not sent"),
    Flow("email_verify_thanks", "verify_email_code.html", True, "needs_human",
         r"^(check whether the application went through: after the emailed code"
         r"|a confirmation page before any submit)",
         inbox=True, ats={"system": "greenhouse"}, wrap=VerifiedReadAsConfirmation,
         covers="a sign-up's email Verify, then a thanks for it: never read as submitted"),
    # --- page reading that holds up ---
    Flow("review_with_next", "review_with_next.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible",
         covers="a wizard step that reads as a review with only Next: the Next is clicked, the "
                "real last step reaches the gate"),
    Flow("ticker_page", "ticker_page.html", True, "needs_human", r"^page did not advance",
         covers="a clock and a posted-ago note that change every second, a Continue that brings "
                "the same step back: read as not advancing, never as new pages",
         replayable=False),
    Flow("tracker_redirect", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", routes=tracker_routes,
         covers="an ad tracker's hop after LinkedIn's Apply, sending the tab on 3 s later: "
                "waited out, never the destination"),
    Flow("aggregator_company_site", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", routes=board_routes,
         covers="a job board's copy of the posting: its link to the company's site is followed "
                "once, the board is never filled"),
    Flow("aggregator_chain", _LINKEDIN_JOB, False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", routes=board_chain_routes,
         covers="a job board whose company link lands on a second board: the second board's "
                "own company link is followed, neither board is filled"),
    Flow("aggregator_board_only", _LINKEDIN_JOB, True, "needs_human",
         r"^aggregator posting on www\.dice\.com: no link to the company's site",
         routes=lambda base: board_routes(base, company_link=False),
         covers="a job board's posting whose only Apply is the board's own: parks at once"),
    Flow("popup_step_park", "popup_step.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible",
         covers="a Next that opens the next step in a new tab: the tab is the next page"),
    Flow("popup_step", "popup_step.html", True, "submitted", _SUBMITTED,
         confirm="#received:visible",
         covers="a Next and a submit that each open a new tab: the thank-you tab is read"),
    Flow("skeleton_then_form", "skeleton_then_form.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible",
         # the skeleton stays until the run has read it, and the form comes
         # 800 ms later: the read waits for the skeleton to clear, never for a
         # clock, so its caps sit far past any busy machine's delay
         timing=((("apply_limits", "LOADING_WAIT_S"), 30.0), (("apply_limits", "EMPTY_READ_MAX_S"), 30.0)),
         on_read="() => window.__appRead && window.__appRead()",
         covers="a loading skeleton (aria-busy, a Cancel) that stays until the page is read, "
                "then the form: the skeleton is never read as the page"),
    Flow("privacy_gate", "privacy_gate.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible",
         covers="a privacy agreement as the first screen: its I Accept is the step's advance"),
    Flow("mailto_apply", "mailto_apply.html", True, "needs_human",
         r"^apply by email to jobs@contoso\.example$",
         covers="an Apply that is an email address: parks with the address, nothing clicked"),
    # --- extraction and fill coverage (replicas of real application forms) ---
    Flow("lever_cards", "lever_cards.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="star markers in their own spans, a location typeahead with no ARIA, a "
                "question of tick boxes, a list opening on a 'Click here' placeholder"),
    Flow("ashby_yesno", "ashby_yesno.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         covers="Yes / No button pairs with aria-pressed, radios sharing one id, a resume "
                "parser's own upload left alone"),
    # the Contoso incident's form; the marker shows only for the
    # answers the typed store holds (a citizen or permanent resident: no
    # sponsorship, authorized; relocates). The relocation label names no
    # place, so the gate reads it as the stored fact's own question and the
    # run confirms all three answers end to end
    Flow("ashby_relocation", "ashby_relocation.html", True, "submitted", _SUBMITTED,
         confirm="body[data-auth=citizen][data-sponsor=no][data-relocate=willing] "
                 "#thanks:visible",
         covers="legal authorization among qualified options only (a Yes on a work visa "
                "is no Yes), sponsorship on Yes / No buttons settled by the alias set, and "
                "relocation: it confirms authorization, sponsorship and relocation from the "
                "typed answers"),
    # the same form, its relocation label naming a
    # place ("the job location (New York)"). The gate holds the stored
    # relocation answer back from a question worded another way, and the
    # settle read asks Jev whether it answers this one. The fake leaves every
    # settle read open, so the fake run parks on it; the real judge reads it
    # with where the candidate lives (the home line) and picks
    # the willing option, which the confirmation marker checks
    Flow("ashby_relocation_place", "ashby_relocation_place.html", True, "needs_human",
         r"^required field without an answer: Are you willing to relocate to the job location "
         r"\(New York\)\?$",
         confirm="body[data-auth=citizen][data-sponsor=no][data-relocate=willing] "
                 "#thanks:visible",
         real_end=("submitted", _SUBMITTED),
         covers="the place-named relocation question: the fake parks it and sends nothing, "
                "the real judge settles it from the saved relocation answer"),
    Flow("greenhouse_react_select", "greenhouse_react_select.html", True, "submitted",
         _SUBMITTED, confirm="#thanks:visible",
         covers="react-select dropdowns (the pick shown in a sibling, a hidden required twin), "
                "a resume labelled by its group, a pronouns question of tick boxes"),
    Flow("workday_create_account", "workday_chooser.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True,
         covers="Workday's start popup, its account screen with a robots-only box, dropdowns "
                "drawn as buttons (a country list of 64), a read-only email, date parts"),
    Flow("oracle_email_terms", "oracle_email_terms.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         password=True,
         covers="an email screen whose terms box is 0 x 0 behind its label, a honeypot off the "
                "page"),
    Flow("icims_iframe_login", "icims_iframe_login.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         password=True,
         covers="a posting in a content frame, an email screen whose Next stays disabled until "
                "its privacy box is ticked"),
    Flow("teamtailor_modal", "teamtailor_modal.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a cookie dialog, the form in a modal, sr-only 'Required' markers, a question "
                "drawn as a menu button of radio items"),
    Flow("paylocity_required_span", "paylocity_required_span.html", False, "ready_to_submit",
         _PARKED, confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="'(required)' spans, div dropdowns (51 states), an unnamed radiogroup of native "
                "radios, a resume box hidden behind its button"),
    Flow("rippling_generic_aria", "rippling_generic_aria.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         covers="'Search' / 'Select...' / 'textbox' aria-labels under visible questions, a "
                "role=radio question, the only submit disabled until the form is complete "
                "(its question was once 'May we text you about this application?': the "
                "live judge leaves that blank, since no fact answers it, and the job parks)"),
    Flow("bamboo_honeypot_mui", "bamboo_honeypot_mui.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         covers="a late posting with a read-only share box, a honeypot, hidden selects behind "
                "styled triggers, a 'file-input' label"),
    Flow("ukg_shadow_apply", "ukg_shadow_apply.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="buttons and a box inside open shadow roots, the Apply and the Submit among them"),
    Flow("aria_controls", "aria_controls.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         covers="a role=checkbox consent and a role=switch toggle, a resume box hidden inside a "
                "wrapper until 'Attach resume' is clicked"),
    Flow("typeahead_editor", "typeahead_editor.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         covers="a City combobox whose matches come only after typing, a rich-text cover letter"),
    Flow("consent_commitment", "consent_commitment.html", True, "needs_human",
         r"^required field without an answer: I consent to a background check$",
         confirm="#thanks:visible", wrap=CommitmentUnderFloor,
         covers="a required background-check consent beside a routine privacy box, read as a "
                "consent under 0.85 on every look: the run parks on it and never ticks it"),
    # --- advancing and repair ---
    Flow("masked_phone", "masked_phone.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a phone mask that takes keys only, a phone box beside a country code that "
                "takes bare digits"),
    Flow("date_mmddyyyy", "date_mmddyyyy.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a signature date box that takes MM/DD/YYYY only"),
    Flow("upload_resets_input", "upload_resets_input.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="an upload widget that keeps the file, shows a chip and resets its input: "
                "verified by the chip, uploaded once"),
    Flow("modal_with_combobox", "modal_with_combobox.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a form in a dialog that Escape closes, a typeahead that says expanded with "
                "no menu: no Escape without a menu"),
    Flow("conditional_fields", "conditional_fields.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", settle_s=0.8,
         covers="a source answer that reveals a required box half a second later, a consent "
                "tick that reveals the only submit"),
    Flow("resume_parse_autofill", "resume_parse_autofill.html", False, "ready_to_submit",
         _PARKED, confirm="#thanks:visible",
         gate="body[data-values-ok='1'] #btn-submit:visible", wrap=HeadlineLeftBlank,
         covers="a resume parser that writes its guesses after the upload, a profile lookup "
                "after the email: the sheet's values stand at the gate"),
    Flow("validation_errors", "validation_errors.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", wrap=OptionalLeftBlank,
         covers="a Next the form refuses: a phone it wants in digits, a blank box it needs, a "
                "summary no control names; repaired, then the gate"),
    Flow("validation_errors_submit", "validation_errors.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible", wrap=OptionalLeftBlank,
         covers="the same, then a submit the form refuses with nothing sent: repaired once and "
                "sent through the gate again"),
    Flow("validation_in_button_box", "validation_in_button_box.html", False, "ready_to_submit",
         _PARKED, confirm="#thanks:visible", gate="#btn-submit:visible", wrap=OptionalLeftBlank,
         covers="a Next refused with its summary written into the Next's own box: the same "
                "button found again after the repair"),
    Flow("validation_in_button_box_submit", "validation_in_button_box.html", True, "submitted",
         _SUBMITTED, confirm="#thanks:visible", wrap=OptionalLeftBlank,
         covers="the same, and a submit refused with its message in its own box: repaired and "
                "sent through the gate again"),
    Flow("hydration_beacon", "hydration_beacon.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="a first Next click before the page's script is ready, on a page that posts its "
                "own telemetry: the telemetry is no request of the click's, so the quiet click "
                "gets its retry"),
    Flow("validation_banner_only", "validation_banner_only.html", False, "ready_to_submit",
         _PARKED, confirm="#thanks:visible", gate="#btn-submit:visible", wrap=OptionalLeftBlank,
         covers="a Next refused with one banner no control names: the judge's mapping is the "
                "only signal of the field it wants"),
    Flow("validation_banner_only_submit", "validation_banner_only.html", True, "submitted",
         _SUBMITTED, confirm="#thanks:visible", wrap=OptionalLeftBlank,
         covers="the same in submit mode: the banner's field repaired, then sent through the "
                "gate"),
    Flow("chat_launcher", "chat_launcher.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="body:not([data-chat-started]) #btn-submit:visible",
         covers="a chat panel fixed over the step's Next: closed by its own close button, the "
                "Next clicked once more, the chat never started"),
    Flow("cookie_banner", "cookie_banner.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="body[data-consent='rejected'] #btn-submit:visible",
         covers="a consent overlay that shows on scroll, over the Next: rejected, never "
                "accepted, and the Next clicked once more"),
    Flow("next_and_feedback_submit", "next_and_feedback_submit.html", False, "ready_to_submit",
         _PARKED, confirm="#thanks:visible",
         gate="body:not([data-feedback]) #btn-submit:visible",
         covers="a step's Next beside a feedback box's own Submit: the Next goes on, the "
                "feedback is never sent, the gate waits for the last step"),
    Flow("two_forms", "two_forms.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="body:not([data-signin-typed]) #btn-submit:visible",
         password=True,
         covers="a sign-in and a sign-up side by side with no account in the ledger: the "
                "sign-up's boxes alone take the address and the password"),
    Flow("apply_with_linkedin", "apply_with_linkedin.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         covers="Apply with LinkedIn beside the posting's own Apply, Continue with LinkedIn "
                "above the step's Next: neither is taken"),
    Flow("talent_beside_application", "talent_beside_application.html", True, "submitted",
         _SUBMITTED, confirm="body:not([data-talent-sent]) #thanks:visible",
         wrap=OptionalLeftBlank,
         covers="a talent box's Submit above the application's own Submit, which refuses a "
                "blank box: repaired, the application's Submit clicked again, the talent box "
                "never sent"),
    Flow("upload_profile_kept", "upload_profile_kept.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="body[data-uploads='1'] #btn-submit:visible",
         covers="a returning candidate's page showing a kept resume of the same file name: "
                "this job's resume is uploaded, exactly once"),
    Flow("upload_profile_replace", "upload_profile_replace.html", False, "ready_to_submit",
         _PARKED, confirm="#thanks:visible", gate="body[data-uploads='1'] #btn-submit:visible",
         covers="a widget that replaces a kept resume's chip with this upload's, of the same "
                "name: verified by the change, uploaded once"),
    Flow("form_associated_invalid", "form_associated_invalid.html", True, "needs_human",
         r"^required field without an answer: Preferred shift \(a control the run cannot "
         r"read: form-associated custom element\)$", confirm="#thanks:visible",
         covers="a form-associated control its internals mark invalid, no required "
                "attribute: named, parked on, never sent"),
    Flow("form_associated_invalid_park", "form_associated_invalid.html", False, "needs_human",
         r"^required field without an answer: Preferred shift \(a control the run cannot "
         r"read: form-associated custom element\)$", confirm="#thanks:visible",
         covers="the same in park mode: never ready_to_submit with it unanswered"),
    Flow("recaptcha_disabled_submit", "recaptcha_disabled_submit.html", True, "needs_human",
         r"^a CAPTCHA check is on the form before the submit", confirm="#received:visible",
         routes=lambda base: {"https://www.google.com/recaptcha/**": _CHECKBOX_STUB},
         covers="a submit disabled until the reCAPTCHA tick: the gate's CAPTCHA path, the "
                "person ticks it"),
    Flow("recaptcha_disabled_submit_park", "recaptcha_disabled_submit.html", False,
         "ready_to_submit", r"^auto_apply_submit is off; a CAPTCHA checkbox is on the form",
         confirm="#received:visible", gate="#btn-submit:visible",
         routes=lambda base: {"https://www.google.com/recaptcha/**": _CHECKBOX_STUB},
         covers="the same in park mode: the gate with the checkbox's note"),
    # the chaos case: the form is drawn anew (the same markup, new nodes)
    # right after the run first reads it, before any act
    Flow("rerender_after_read", "lever_single.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible",
         on_read="() => { if (window.__redrawn) return; window.__redrawn = 1; "
                 "const a = document.getElementById('application'); a.innerHTML = a.innerHTML; }",
         covers="the form drawn anew between the read and the fill: every act finds its "
                "control again"),
    Flow("closed_shadow_controls", "closed_shadow_controls.html", False, "needs_human",
         r"^required field without an answer: Earliest start date \(a control the run cannot "
         r"read: closed shadow root\)$",
         confirm="#thanks:visible",
         covers="a required start date inside a closed shadow root and a form-associated "
                "relocation choice: named, and the job parks on the required one"),
    # --- accounts and email ---
    # served on the company's careers host: the code comes from
    # careers@fabrikam.example, and the live judge rightly reads that mail as
    # sent by someone other than 127.0.0.1 (0.09)
    Flow("otp_six_boxes", f"{_CAREERS}/apply/otp", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True, inbox=True,
         inbox_page="otp_list.html",
         routes=lambda base: {f"{_CAREERS}/**": (FIXTURES_DIR / "forms" / "otp_six_boxes.html")
                              .read_text(encoding="utf-8")},
         covers="a sign-up, then its code in six one-character boxes, the fresh code below an "
                "older one from the same sender, then the application"),
    Flow("workday_signin_modal", "workday_signin_modal.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True, inbox=True,
         inbox_page="link_list.html", ats={"system": "workday"},
         covers="Workday's start popup, a Sign In dialog whose way to an account is a Create "
                "Account button, the password rules, the account checked by a link in the "
                "email, the sign-in after it, then the wizard"),
    Flow("workday_link_pick", "workday_signin_modal.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True, inbox=True,
         inbox_page="link_pick_list.html", ats={"system": "workday"},
         covers="the account check's email holds two links whose words read as a check, the "
                "job alerts' confirmation first: the judge picks the account's link, the "
                "sign-in after it, then the wizard"),
    Flow("signup_exists", "signup_exists.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True,
         covers="a sign-up that says the address has an account: one sign-in instead, never a "
                "second sign-up, then the wizard"),
    Flow("password_rules", "password_rules.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True,
         covers="a sign-up that states its password rules beside the box: read, met by the "
                "stored password, then the wizard"),
    Flow("slow_signup", "slow_signup.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True, suite_seeds=1,
         covers="a sign-up that posts and then shows nothing for 8 s: waited for, clicked "
                "once"),
    Flow("sso_buttons", "sso_buttons.html", True, "needs_human",
         r"^sign-in only through another site \(Google, Microsoft, LinkedIn, Apple\)",
         password=True,
         covers="a portal whose only way on is a sign-in with Google, Microsoft, LinkedIn or "
                "Apple: parks at once, none clicked"),
    Flow("signin_alerts_link", "signin_alerts_link.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", password=True,
         covers="a sign-in whose header carries a job-alerts sign-up link: the screen's own "
                "Create Account button makes the account, never the alerts link"),
    # --- the code and link steps after the answers ---
    Flow("link_after_submit", "link_after_submit.html", True, "submitted",
         r"^submitted \(unconfirmed\): the emailed link's page on \S+ says "
         r"'application has been received'", inbox=True, inbox_page="link_confirm_list.html",
         ats={"system": "greenhouse"},
         covers="a form post answered with no redirect by a page that says a link was emailed: "
                "the link opens in a tab of its own, its page is the confirmation, and the job's "
                "tab is never loaded again, so one post goes"),
    Flow("link_after_answers_park", "link_after_answers.html", False, "ready_to_submit",
         r"^auto_apply_submit is off; the emailed link from \S+ is the step that may send the "
         r"application", gate="#link-sent:visible", inbox=True,
         inbox_page="link_confirm_list.html", ats={"system": "greenhouse"},
         covers="the answers, then a Continue to a page that says a link was emailed: park mode "
                "stops there and never opens the link"),
    # the same in submit mode. The link's page
    # confirms the address alone: a page that says the application was
    # received would end the job submitted through the link, a send this
    # harness sees only inside the submit gate
    Flow("link_after_answers", "link_after_answers.html", True, "needs_human",
         "^" + re.escape(apply_run.CHECK_SENT_REASON) + r": the emailed link on \S+ was opened "
         r"after the application's answers went on the site, and its page shows no received "
         r"words; the job's tab was not loaded again$", inbox=True,
         inbox_page="link_email_list.html", ats={"system": "greenhouse"},
         covers="submit mode: the answers, then a Continue to a page that says a link was "
                "emailed; the link opens in a tab of its own, its page confirms only the "
                "address, and the job's tab is never loaded again: the person checks whether "
                "the application went through"),
    Flow("code_after_answers", "code_after_answers.html", True, "submitted", _SUBMITTED,
         confirm="body[data-confirmed]", inbox=True, ats={"system": "greenhouse"},
         covers="the answers, a Continue to an emailed-code step, the code typed and its Verify "
                "clicked once, then the last step sent through the gate"),
    Flow("code_after_answers_park", "code_after_answers.html", False, "ready_to_submit",
         r"^auto_apply_submit is off; the emailed code is entered and its button \(Verify\) is "
         r"the step that may send the application", gate="#btn-verify:visible", inbox=True,
         ats={"system": "greenhouse"},
         covers="the same in park mode: the code is typed and its button never clicked, "
                "whatever role the judge gave it"),
    # a master password is stored, as for every flow with an address screen: a
    # noisy read of that screen as a login wall takes the account step's way
    # through it (a 20-seed matrix run found seeds 6 and 18 so)
    Flow("email_code_first_park", "email_code_first.html", False, "ready_to_submit", _PARKED,
         confirm="#thanks:visible", gate="#btn-submit:visible", inbox=True, password=True,
         ats={"system": "greenhouse"},
         covers="an email-first start, its emailed code, then the application: the address alone "
                "is no application on the site, so park mode passes the code step and stops at "
                "the submit"),
    Flow("login_get_park", "login_get_form.html", False, "ready_to_submit", _PARKED,
         confirm="#received:visible", gate="#btn-submit:visible", password=True,
         covers="a sign-in form sent with method=get, whose next page's query carries the "
                "password: the trace keeps each URL without its query, so PASSWORD-LEAK "
                "covers it"),
    # park and resume: two required questions no saved answer
    # holds pause the run; the person's answers go in and the run goes on
    Flow("pause_fill", "pause_form.html", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         pause=PauseSpec("fill", (("query language", "Datalog 2.0 (user)"),
                                  ("preferred team", "Platform"),
                                  ("conference talks", "37"))),
         covers="a pause answered in the card: each value in its own field, then the submit"),
    Flow("pause_browser", "pause_form.html?browser=1", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible", pause=PauseSpec("browser"),
         covers="a pause the person finishes in the browser: the page read again, their "
                "values kept, then the submit"),
    Flow("pause_changed_page", "pause_form.html?grow=1", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         pause=PauseSpec("fill", (("query language", "Datalog 2.0 (user)"),
                                  ("preferred team", "Platform"),
                                  ("conference talks", "37"))),
         covers="a page that changed during the pause: read and planned again before any "
                "fill, the answers put in on the new plan"),
    Flow("pause_sensitive", "pause_form.html?dob=1", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible",
         pause=PauseSpec("fill", (("query language", "Datalog 2.0 (user)"),
                                  ("preferred team", "Platform"),
                                  ("conference talks", "37"),
                                  ("date of birth", "11/11/1911"))),
         covers="a required date of birth the person types in the browser: code never "
                "types it, the value an answer gives for it is ignored"),
    Flow("pause_timeout", "pause_form.html", True, "needs_human",
         r"^required field without an answer: ", pause=PauseSpec("timeout"),
         covers="no answer in time: the job parks as a run with no pause would"),
    Flow("pause_park", "pause_form.html", True, "needs_human",
         r"^required field without an answer: ", pause=PauseSpec("park"),
         covers="Park it: the job parks as a run with no pause would"),
    Flow("pause_dup_labels", "pause_form.html?dup=1&grow=1", True, "submitted", _SUBMITTED,
         confirm="#thanks:visible", wrap=PauseLeftBlank,
         pause=PauseSpec("fill", (("query language", "Datalog 2.0 (user)"),
                                  ("preferred team", "Platform"),
                                  ("conference talks", "37")),
                         by_id=(("explain_a", "Alpha reason (user)"),
                                ("explain_b", "Beta reason (user)"))),
         covers="two required fields with the same label, answered in the card, on a page that "
                "changed during the pause: each answer goes in its own field on the replan"),
    Flow("pause_submit_in_browser", "pause_disabled.html?click=1", True, "needs_human",
         "^" + re.escape(apply_run.CHECK_SENT_REASON) + r": the page moved on during the pause",
         wrap=PauseLeftBlank, pause=PauseSpec("browser"),
         covers="a Submit disabled after the fill; the person fixes the page and clicks Submit "
                "in the browser during the pause: the run parks with the check-whether note, "
                "never fills the page after it, and the job is never re-queued"),
    Flow("pause_fix_kept", "pause_disabled.html?reformat=1", False, "ready_to_submit",
         _PARKED, confirm="#thanks:visible",
         gate="body:not([data-phone-retyped]) #btn-submit:not([disabled])",
         wrap=PauseLeftBlank, pause=PauseSpec("browser"),
         covers="a Submit disabled after the fill; the person rewrites the phone the run typed "
                "and fills the referral in the browser: the replan keeps both"),
    Flow("pause_submit_to_account", "pause_disabled.html?click=1&account=1", True,
         "needs_human",
         "^" + re.escape(apply_run.CHECK_SENT_REASON) + r": the page moved on during the pause "
         r"\(its 'Submit application' button is gone\)",
         wrap=PauseLeftBlank, pause=PauseSpec("browser"),
         covers="the person clicks the enabled Submit during the pause and the site shows an "
                "account form that shares the Email label, with no received words: the "
                "paused page's send button is gone, so the job may have been sent and parks "
                "with the check-whether note"),
    Flow("pause_wizard_next", "pause_disabled.html?click=1&wizard=1", True, "submitted",
         _SUBMITTED, confirm="#thanks:visible", wrap=PauseLeftBlank,
         pause=PauseSpec("browser"),
         covers="a Next disabled after the fill; the person fixes the page and clicks Next "
                "during the pause, and the address moves on to a review step: a page with no "
                "send button that moved on is planned again, and the gate sends"),
    # added after the last recording
    Flow("pause_wizard_fill", "pause_disabled.html?click=1&wizard=1&again=1", True,
         "submitted", _SUBMITTED, confirm="#thanks:visible", wrap=PauseLeftBlank,
         pause=PauseSpec("fill", (("referral code", "CARD-7 (user)"),)),
         covers="a Next disabled after the fill; the person answers the referral code in the "
                "card and also clicks Next in the browser, and the review step has its own "
                "Referral code box: the card's answer goes nowhere on the step it moved on to"),
)


def flow(name: str) -> Flow:
    return next(f for f in FLOWS if f.name == name)
