"""SP4: the page read as its own small request, and the code that combines it.

- `apply_judge.read_questions`: a trimmed state (host, path shape, title, the
  text's head, a field census, button texts) and atomic questions (the
  `page_state` Choice, one Noul per signal); no field value, no mapping.
- `apply_judge.page_facts` / `read_page`: the judge's Choice, its Nouls and
  the page's structure combined; a misread Choice gives way to structure and
  Nouls, a sure one holds against one Noul.
- `structural_kind`, `already_applied`, `closed_posting`, `entry_worded`.
- `FakeJev`'s structured-criteria Noul rule and `NoisyJev`'s noise over the
  read's Nouls (at least as often and as hard as the page state's).

No browser, no network; the judges are `FakeJev`, `NoisyJev` and hand-built
answers."""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_judge  # noqa: E402
import apply_run  # noqa: E402
import jev  # noqa: E402
from apply_form import Button, Field, FormDigest  # noqa: E402
from apply_judge import FillPlan  # noqa: E402


def _f(n, label, type_="text", required=False, ident="", auto="", options=()):
    return Field(n=n, locator=(0, f"#f{n}"), label=label, type=type_, required=required,
                 id_or_name=ident, autocomplete=auto, options=list(options))


def _b(n, text, in_form=False):
    return Button(n=n, locator=(0, f"#b{n}"), text=text, in_form=in_form)


def _choice(choice, conf, second="", p2=0.0):
    probs = {choice: conf}
    if second:
        probs[second] = p2
    return jev.Answer(kind="choice", choice=choice, probabilities=probs, confidence=conf)


def _nouls(**values):
    return {qid: jev.Answer(kind="noul", noul=v) for qid, v in values.items()}


SIGNUP = FormDigest(url_host="careers.fabrikam.example", title="Create an account",
                    text="Create an account to track your application.",
                    fields=[_f(0, "Email", "email", auto="username"),
                            _f(1, "Password", "other", ident="password", auto="new-password"),
                            _f(2, "Confirm password", "other", ident="password_confirmation",
                               auto="new-password")],
                    buttons=[_b(0, "Create account")])
LOGIN = FormDigest(url_host="careers.fabrikam.example", title="Sign in",
                   text="Sign in to continue your application.",
                   fields=[_f(0, "Email", "email", auto="username"),
                           _f(1, "Password", "other", ident="password",
                              auto="current-password")],
                   buttons=[_b(0, "Sign in")])
CODE = FormDigest(url_host="careers.fabrikam.example", title="Verify your email",
                  text="We emailed you a security code. Enter it below.",
                  fields=[_f(0, "Security code", auto="one-time-code")],
                  buttons=[_b(0, "Verify and continue")])
FORM = FormDigest(url_host="careers.fabrikam.example", title="Apply",
                  text="Apply for Analytics Engineer",
                  fields=[_f(0, "First name", required=True), _f(1, "Last name", required=True),
                          _f(2, "Email", "email", required=True), _f(3, "Resume", "file")],
                  buttons=[_b(0, "Submit application", in_form=True)])
POSTING = FormDigest(url_host="careers.fabrikam.example", title="Analytics Engineer",
                     text="About the role. Responsibilities: own the models. Qualifications: SQL.",
                     fields=[], buttons=[_b(0, "Apply now")])
CAPTCHA = FormDigest(url_host="careers.fabrikam.example", title="Security check",
                     text="Verify you are human before you continue.",
                     fields=[_f(0, "I'm not a robot", "checkbox")], buttons=[])


# --- the request --------------------------------------------------------------------------------

def test_the_read_is_a_small_request_of_a_choice_and_atomic_nouls():
    url = "https://careers.fabrikam.example/jobs/4438751519/apply?email=jane%40x.com#top"
    state, q = apply_judge.read_questions(FORM, url)
    assert set(state) == {"page", "fields", "field_kinds", "buttons"}
    assert state["page"] == {"url_host": "careers.fabrikam.example", "path": "/jobs/<n>/apply",
                             "title": "Apply", "headline_text": "Apply for Analytics Engineer"}
    assert state["fields"] == [{"n": 0, "label": "First name", "kind": "text"},
                               {"n": 1, "label": "Last name", "kind": "text"},
                               {"n": 2, "label": "Email", "kind": "email"},
                               {"n": 3, "label": "Resume", "kind": "file upload"}]
    assert state["field_kinds"] == {"text": 2, "email": 1, "file upload": 1}
    assert state["buttons"] == [{"n": 0, "text": "Submit application"}]
    assert set(q) == {"page_state", *apply_judge.READ_NOUL_IDS}
    assert q["page_state"]["type"] == "choice"
    assert set(q["page_state"]["criteria"]) == set(apply_judge.PAGE_STATES)
    for qid in apply_judge.READ_NOUL_IDS:
        noul = q[qid]
        assert noul["type"] == "noul" and "`" in noul["instructions"]
        assert set(noul["criteria"]) == {"true", "false"}
        assert noul["criteria"]["true"]["examples"], qid
    # every signal the brief names, and a Noul for each kind but `other`
    assert set(apply_judge.READ_NOUL_IDS) == {
        "page_job_description", "page_apply_entry", "page_applicant_details", "page_sign_in",
        "page_create_account", "page_received", "page_already_applied", "page_closed",
        "page_code", "page_payment", "page_review", "page_error", "has_captcha"}
    covered = {k for k, qs in jev.PAGE_KIND_NOULS.items() if qs}
    assert covered == set(apply_judge.PAGE_STATES) - {"other"}
    assert set(jev.READ_NOULS) == set(apply_judge.READ_NOUL_IDS)


def test_the_read_carries_no_value_no_query_and_no_mapping_question():
    state, q = apply_judge.read_questions(SIGNUP, "https://x.example/register?token=s3cret")
    blob = json.dumps(state)
    assert "s3cret" not in blob and "token" not in blob
    assert state["field_kinds"] == {"email": 1, "new password": 2}
    assert not any(k.startswith(("field_", "button_")) for k in q)


@pytest.mark.parametrize("url, shape", [
    ("https://jobs.lever.co/acme/0b3a1c2d-9f00-4e1b-8a55-3c1d2e3f4a5b/apply",
     "/acme/<id>/apply"),
    ("https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Austin/Analyst_R-123456",
     "/en-US/Careers/job/Austin/Analyst_R-<n>"),
    ("https://boards.greenhouse.io/acme/jobs/4012345", "/acme/jobs/<n>"),
    ("http://127.0.0.1:5000/forms/login_wall.html", "/forms/login_wall.html")])
def test_the_path_shape_masks_ids(url, shape):
    assert apply_judge.path_shape(url) == shape


@pytest.mark.parametrize("url, kind", [
    ("https://jobs.lever.co/acme/0b3a/thanks", "confirmation"),
    ("https://acme.icims.com/jobs/1/login", "login_wall"),
    ("https://acme.example/careers/register", "signup_form"),
    ("https://jobs.ashbyhq.com/acme/0b3a/application", "application_form"),
    ("https://boards.greenhouse.io/acme/jobs/4012345", "job_posting"),
    ("http://127.0.0.1:5000/forms/signup.html", "")])
def test_the_url_shape_is_a_hint(url, kind):
    assert apply_judge.url_kind(url) == kind


# --- FakeJev answers the read faithfully --------------------------------------------------------

@pytest.mark.parametrize("digest, yes", [
    (SIGNUP, {"page_create_account"}),
    (LOGIN, {"page_sign_in"}),
    (CODE, {"page_code"}),
    (POSTING, {"page_job_description", "page_apply_entry"}),
    (CAPTCHA, {"has_captcha"})])
def test_the_fake_answers_the_read_nouls_by_their_example_phrases(digest, yes):
    state, q = apply_judge.read_questions(digest)
    out = jev.FakeJev().judge(state, q)
    said = {qid for qid in apply_judge.READ_NOUL_IDS if out[qid].noul >= 0.5}
    assert yes <= said, said
    assert not said & ({"page_received", "page_closed", "page_payment", "page_error",
                        "page_already_applied"}), said


def test_the_fake_noul_counts_true_phrases_against_false_ones():
    q = {"n": {"type": "noul", "instructions": "Does `page` ask to sign in?",
               "criteria": {"true": {"what": "a sign-in", "examples": ["sign in", "log in"]},
                            "false": {"what": "a sign-up", "examples": ["create an account"]}}}}
    fake = jev.FakeJev()
    assert fake.judge({"page": "Sign in or log in"}, q)["n"].noul == 0.9
    assert fake.judge({"page": "Create an account. Already have one? Sign in"},
                      q)["n"].noul == 0.1
    assert fake.judge({"page": "Signing up"}, q)["n"].noul == 0.1   # whole words only
    # a Noul without example phrases keeps the word-overlap rule
    plain = {"n": {"type": "noul", "instructions": "Is the account password shown?",
                   "criteria": {"true": "shown", "false": "hidden"}}}
    assert fake.judge({"page": "the account password field"}, plain)["n"].noul == 0.9


# --- the combination ----------------------------------------------------------------------------

def _read(digest, choice, nouls=None, url=""):
    answers = {"page_state": choice, **(nouls or {})}
    return apply_judge.read_page(answers, apply_judge.page_facts(digest, url))


def test_a_sign_up_misread_as_a_sign_in_reads_as_the_sign_up():
    # the swap NoisyJev makes (signup_form -> login_wall at 0.30 to 0.60), the
    # Nouls taken along: the box that makes the password decides
    r = _read(SIGNUP, _choice("login_wall", 0.55, "signup_form", 0.45),
              _nouls(page_sign_in=0.7, page_create_account=0.3))
    assert r.state == "signup_form" and r.conf >= apply_judge.PAGE_STATE_MIN_CONF, r
    assert r.judged == "login_wall" and "structure" in r.why


def test_a_code_screen_misread_as_a_sign_in_reads_as_the_code_screen():
    r = _read(CODE, _choice("login_wall", 0.6, "code_gate", 0.36),
              _nouls(page_sign_in=0.8, page_code=0.2))
    assert r.state == "code_gate", r
    # a read this contested stays under the floor; its second look's
    # fallback is the structure's settled kind, the code step
    if r.conf < apply_judge.PAGE_STATE_MIN_CONF:
        assert apply_run.unsure_step(r.state, CODE, apply_judge.page_facts(CODE)) == (
            "code_gate", "guess")


def test_a_form_misread_as_a_confirmation_reads_as_the_form():
    r = _read(FORM, _choice("confirmation", 0.8, "application_form", 0.2),
              _nouls(page_received=0.75, page_applicant_details=0.3))
    assert r.state == "application_form", r


def test_received_words_make_a_fieldless_page_a_confirmation():
    page = FormDigest(url_host="x", title="Thanks", text="Thank you for applying!",
                      fields=[], buttons=[])
    r = _read(page, _choice("other", 0.5, "confirmation", 0.45))
    assert r.state == "confirmation", r


def test_a_posting_read_as_other_reads_as_the_posting():
    r = _read(POSTING, _choice("other", 0.55, "job_posting", 0.45),
              _nouls(page_job_description=0.9, page_apply_entry=0.9))
    assert r.state == "job_posting" and r.conf >= apply_judge.PAGE_STATE_MIN_CONF, r


def test_a_bot_check_read_as_other_reads_as_the_check():
    r = _read(CAPTCHA, _choice("other", 0.55, "captcha_or_bot_check", 0.45),
              _nouls(has_captcha=0.3))
    assert r.state == "captcha_or_bot_check", r


def test_a_sure_read_holds_against_a_flipped_noul():
    r = _read(FORM, _choice("application_form", 0.8),
              _nouls(page_applicant_details=0.2, page_sign_in=0.85))
    assert r.state == "application_form" and r.conf >= apply_judge.PAGE_STATE_MIN_CONF, r


def test_a_lone_weak_kind_is_no_sure_read():
    empty = FormDigest(url_host="x", title="Verified", text="Your email is verified.",
                       fields=[], buttons=[])
    r = _read(empty, _choice("login_wall", 1.0), _nouls(page_sign_in=0.1))
    assert r.state == "login_wall" and r.conf < apply_judge.PAGE_STATE_MIN_CONF, r


def test_the_combined_read_is_the_page_state_answer():
    r = _read(SIGNUP, _choice("login_wall", 0.55, "signup_form", 0.45))
    a = apply_judge.read_answer(r)
    assert apply_judge.read_page_state({"page_state": a}) == (r.state, r.conf)
    assert max(a.probabilities, key=a.probabilities.get) == r.state


# --- the structure alone: the unsure fallback, and the site's own dead ends ----------------------

@pytest.mark.parametrize("digest, kind", [
    (SIGNUP, "signup_form"), (LOGIN, "login_wall"), (CODE, "code_gate"),
    (FORM, "application_form"), (POSTING, "job_posting"), (CAPTCHA, "captcha_or_bot_check"),
    (FormDigest(url_host="x", title="Step", text="Almost there", fields=[],
                buttons=[_b(0, "Continue")]), "application_form"),
    (FormDigest(url_host="x", title="Gone", text="This job is no longer available.",
                fields=[], buttons=[]), "error_or_dead"),
    (FormDigest(url_host="x", title="Home", text="Welcome", fields=[],
                buttons=[_b(0, "Menu")]), None)])
def test_the_structure_alone_gives_a_kind(digest, kind):
    assert apply_judge.structural_kind(apply_judge.page_facts(digest)) == kind


def test_an_unsure_read_takes_the_structures_kind_when_its_guess_is_ruled_out():
    facts = apply_judge.page_facts(CODE)
    # a sign-in guess on a code screen: ruled out by the structure
    assert apply_run.unsure_step("login_wall", CODE, facts) == ("code_gate", "structure")
    assert apply_run.unsure_step("code_gate", CODE, facts) == ("code_gate", "guess")
    home = FormDigest(url_host="x", title="Home", text="Welcome", fields=[],
                      buttons=[_b(0, "Menu")])
    assert apply_run.unsure_step("other", home, apply_judge.page_facts(home)) == (None, "")


STATUS = FormDigest(url_host="x", title="Application status",
                    text="You have already applied to this job. We will be in touch.",
                    fields=[], buttons=[])
# I1's shapes: a sign-in's prompt and a screening question, both questions
SIGN_IN_STATUS = FormDigest(url_host="x", title="Sign in",
                            text="Already applied? Sign in to check the status of your "
                                 "application.",
                            fields=LOGIN.fields, buttons=[_b(0, "Sign in")])
SCREENING = FormDigest(url_host="x", title="Apply",
                       text="Apply for Analytics Engineer. Have you already applied to Fabrikam "
                            "in the last 12 months?",
                       fields=[_f(0, "First name", required=True),
                               _f(1, "Have you already applied to Fabrikam in the last 12 months?",
                                  "radio", required=True, options=("Yes", "No"))],
                       buttons=[_b(0, "Continue")])


def test_already_applied_is_the_pages_own_statement_on_a_page_with_nothing_to_fill_or_open():
    facts = apply_judge.page_facts(STATUS)
    assert "already applied" in apply_judge.already_applied({}, facts, "confirmation")
    for digest, state in ((SIGN_IN_STATUS, "login_wall"), (SCREENING, "application_form")):
        facts = apply_judge.page_facts(digest)
        assert facts.already_applied == "", digest.title
        step = apply_run.loop_step("https://x.example/p", digest,
                                   FillPlan(buttons={"advance": (0, 0.9)}), state, 0.9)
        assert "already applied" not in step, step
    # the words beside an Apply entry, or a form's own box, never park
    posting = FormDigest(url_host="x", title="Analyst", text="You have already applied.",
                         fields=[], buttons=[_b(0, "Apply now")])
    assert apply_judge.page_facts(posting).already_applied == ""
    boxed = FormDigest(url_host="x", title="Apply", text="You have already applied.",
                       fields=FORM.fields, buttons=FORM.buttons)
    assert apply_judge.page_facts(boxed).already_applied == ""
    # a label that says it, with no "?", is a field's words
    labelled = FormDigest(url_host="x", title="Status", text="Already applied to Fabrikam",
                          fields=[_f(0, "Already applied to Fabrikam", "checkbox")], buttons=[])
    assert apply_judge.page_facts(labelled).already_applied == ""


def test_the_already_applied_noul_alone_never_parks_a_pre_submit_thanks_page():
    thanks = FormDigest(url_host="x", title="Thanks", text="Thanks for your interest in us.",
                        fields=[], buttons=[])
    answers = {"page_state": _choice("confirmation", 0.9),
               **_nouls(page_already_applied=0.8)}
    facts = apply_judge.page_facts(thanks)
    assert apply_judge.already_applied(answers, facts, "confirmation") == ""
    step = apply_run.loop_step("https://x.example/thanks", thanks, FillPlan(), "confirmation",
                               0.9, answers=answers)
    assert step.startswith("park: a confirmation page before any submit"), step


# I2: an open posting's boilerplate (synthetic wording of the shape)
OPEN_UNTIL_FILLED = FormDigest(
    url_host="x", title="Data Engineer",
    text="About the role: build the pipelines. The role stays open until the position is "
         "filled. Qualifications: SQL.",
    fields=[], buttons=[_b(0, "Apply for this job online")])


def test_an_open_posting_that_stays_open_until_the_position_is_filled_is_no_closed_page():
    facts = apply_judge.page_facts(OPEN_UNTIL_FILLED)
    assert facts.closed == ""
    assert apply_judge.structural_kind(facts, strict=True) == "job_posting"
    plan = FillPlan(buttons={"apply_entry": (0, 0.9)})
    for state, conf in (("other", 0.55), ("job_posting", 0.30)):
        step = apply_run.loop_step("https://x.example/jobs/1", OPEN_UNTIL_FILLED, plan, state,
                                   conf, answers={"page_state": _choice(state, conf)})
        assert "closed" not in step and "click the Apply entry" in step, step
    # past-tense words beside an Apply entry are no closed page either
    filled = FormDigest(url_host="x", title="Data Engineer",
                        text="This position has been filled.", fields=[],
                        buttons=[_b(0, "Apply now")])
    facts = apply_judge.page_facts(filled)
    assert apply_judge.closed_posting(_nouls(page_closed=0.9), facts, "other") == ""
    assert apply_judge.closed_posting(_nouls(page_closed=0.9), facts, "error_or_dead") == ""


def test_a_closed_posting_is_told_apart_from_an_error():
    closed = FormDigest(url_host="x", title="Job", text="No longer accepting applications",
                        fields=[], buttons=[])
    facts = apply_judge.page_facts(closed)
    assert "no longer accepting" in apply_judge.closed_posting({}, facts, "error_or_dead").lower()
    assert apply_judge.closed_posting({}, facts, "application_form") == ""
    flipped = _nouls(page_closed=0.8)
    # the Noul alone: on a page read as an error, never on `other`, never
    # beside an Apply entry
    gone = apply_judge.page_facts(FormDigest(url_host="x", title="Job", text="Sorry.",
                                             fields=[], buttons=[]))
    assert apply_judge.closed_posting(flipped, gone, "other") == ""
    assert apply_judge.closed_posting(flipped, gone, "error_or_dead").startswith("read as")
    assert apply_judge.closed_posting(flipped, apply_judge.page_facts(POSTING),
                                      "error_or_dead") == ""


@pytest.mark.parametrize("text, entry", [
    ("Apply", True), ("Apply now", True), ("Apply for this job", True),
    ("I'm interested", True), ("I’m interested", True), ("Start your application", True),
    ("Applying tips", False), ("Apply filters", False), ("Applied", False),
    ("Easy Apply", False), ("Apply with LinkedIn", False), ("Submit application", False),
    ("How to apply", False), ("Application status", False),
    ("Apply Later", False), ("Save for later", False)])
def test_an_apply_entry_is_read_by_its_words(text, entry):
    assert apply_judge.entry_worded(text) is entry


def test_a_recaptcha_notice_is_no_bot_check():
    # M1: the invisible check's notice asks nothing of the person
    signin = FormDigest(url_host="x", title="Sign in",
                        text="Sign in. This site is protected by reCAPTCHA and the Google Privacy "
                             "Policy and Terms of Service apply.",
                        fields=LOGIN.fields, buttons=[_b(0, "Sign in")])
    facts = apply_judge.page_facts(signin)
    assert facts.captcha == ""
    assert apply_judge.structural_kind(facts, strict=True) == "login_wall"
    assert apply_run.unsure_step("login_wall", signin, facts) == ("login_wall", "guess")
    assert apply_judge.page_facts(CAPTCHA).captcha == "Verify you are human"


# --- NoisyJev misreads the read's Nouls ----------------------------------------------------------

class _Truth:
    """An inner judge that reads the page as `state` and answers every read
    Noul of that kind yes, every other no."""

    def __init__(self, state):
        self.state = state

    def judge(self, state, questions):
        out = {}
        for qid, q in questions.items():
            if qid == "page_state":
                names = list(q["criteria"])
                out[qid] = jev.Answer(kind="choice", choice=self.state, confidence=1.0,
                                      probabilities={n: float(n == self.state) for n in names})
            else:
                yes = qid in jev.PAGE_KIND_NOULS.get(self.state, ())
                out[qid] = jev.Answer(kind="noul", noul=0.9 if yes else 0.1)
        return out


def _requests(n):
    for i in range(n):
        digest = FormDigest(url_host="x", title=f"Page {i}", text="t", fields=[], buttons=[])
        yield apply_judge.read_questions(digest)


def test_the_read_nouls_are_flipped_at_least_as_often_as_the_page_state_is_swapped():
    judge = jev.NoisyJev(_Truth("application_form"), 3)
    runs = swaps = flips = nouls = 0
    for state, q in _requests(400):
        out = judge.judge(state, q)
        runs += 1
        swaps += out["page_state"].choice != "application_form"
        for qid in apply_judge.READ_NOUL_IDS:
            truth = qid in jev.PAGE_KIND_NOULS["application_form"]
            nouls += 1
            flips += (out[qid].noul >= 0.5) != truth
    assert swaps / runs >= 0.12, swaps
    assert flips / nouls >= judge.swap_p * 0.8, (flips, nouls)


def test_a_flipped_noul_lands_on_the_wrong_side_with_room_to_spare():
    judge = jev.NoisyJev(_Truth("login_wall"), 5, swap_p=0.0, noul_p=1.0)
    for state, q in _requests(30):
        out = judge.judge(state, q)
        assert 0.10 <= out["page_sign_in"].noul <= 0.40
        assert 0.60 <= out["page_code"].noul <= 0.90


def test_a_misread_page_state_takes_its_nouls_along():
    judge = jev.NoisyJev(_Truth("signup_form"), 7, swap_p=1.0, noul_p=0.0, coherent_p=1.0)
    for state, q in _requests(30):
        out = judge.judge(state, q)
        misread = out["page_state"].choice
        assert misread != "signup_form"
        for qid in jev.PAGE_KIND_NOULS.get(misread, ()):
            assert 0.60 <= out[qid].noul <= 0.85
        assert 0.15 <= out["page_create_account"].noul <= 0.40


def test_no_noul_noise_without_swaps_or_flips_is_a_pull_toward_the_middle():
    judge = jev.NoisyJev(_Truth("code_gate"), 9, swap_p=0.0, noul_p=0.0, conf_scale=0.75)
    for state, q in _requests(20):
        out = judge.judge(state, q)
        assert 0.80 <= out["page_code"].noul <= 0.90
        assert 0.10 <= out["page_sign_in"].noul <= 0.20


def test_the_old_noise_is_unchanged_where_the_read_nouls_are_not_asked():
    # a request of the old shape (no read Noul) gets the same answers as before
    state = {"page": {"title": "Apply"}, "buttons": [{"n": 0, "text": "Continue"}]}
    questions = {"page_state": {"type": "choice", "instructions": "Which kind of screen?",
                                "criteria": {s: s for s in jev.PAGE_STATE_NEIGHBOURS}},
                 "button_0_role": {"type": "choice", "instructions": "Which role?",
                                   "criteria": {"advance": "", "other": ""}}}
    a = jev.NoisyJev(jev.FakeJev(), 11).judge(state, questions)
    b = jev.NoisyJev(jev.FakeJev(), 11, noul_p=0.9, coherent_p=0.9).judge(state, questions)
    assert a == b
