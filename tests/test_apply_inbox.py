"""Inbox DOM fixtures and synthetic codes; no external inbox or keyring."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))
import jev  # noqa: E402

pytest_plugins = ["conftest_browser"]


def _inbox():
    assert importlib.util.find_spec("apply_inbox") is not None, "inbox implementation missing"
    import apply_inbox
    return apply_inbox


@pytest.mark.parametrize("provider", ["outlook", "gmail"])
def test_fetch_code_keeps_application_tab_and_ignores_the_decoys(
        browser_page, fixtures_server, provider, caplog):
    """The inbox carries an order number and another site's code as well."""
    inbox = _inbox()
    browser_page.goto(fixtures_server + "/forms/code_gate.html")
    original = browser_page.url
    # a second poll with no wait: a list read before the inbox page rendered
    # (a busy parallel run) is read again; an error that ended the polls is
    # shown by its type
    errors: list = []
    code = inbox.fetch_code(browser_page, "greenhouse.io",
                            fixtures_server + f"/inbox/{provider}_list.html",
                            jev=jev.FakeJev(), polls=2, sleep=lambda s: None, errors=errors)
    assert code == "MKPZ3QRA", errors  # not Q7R2XK (Ashby) and not 48213 (the order)
    assert browser_page.url == original
    assert len(browser_page.context.pages) == 1
    assert code not in caplog.text


def test_a_poll_that_times_out_leaves_the_next_poll_to_run(
        browser_page, fixtures_server, monkeypatch):
    """SP5 round 2, Minor 7: a timeout on one poll (a busy machine's 5 s cap
    on the list read) is that poll's error; the next poll reads the inbox."""
    inbox = _inbox()
    real = inbox.list_messages
    calls: list = []

    def _first_times_out(tab, url, *a, **kw):
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError("the list did not render in time")
        return real(tab, url, *a, **kw)
    monkeypatch.setattr(inbox, "list_messages", _first_times_out)
    browser_page.goto(fixtures_server + "/forms/code_gate.html")
    errors: list = []
    slept: list = []
    code = inbox.fetch_code(browser_page, "greenhouse.io",
                            fixtures_server + "/inbox/outlook_list.html", jev=jev.FakeJev(),
                            polls=2, sleep=slept.append, wait_s=7, errors=errors)
    assert code == "MKPZ3QRA", errors
    assert len(calls) == 2 and slept == [7]
    assert errors == ["TimeoutError"]          # the failed poll, by its type alone
    assert len(browser_page.context.pages) == 1


def test_the_polls_end_when_the_same_error_comes_back(browser_page, fixtures_server, monkeypatch):
    """Review round 3, M3: a failure that repeats (a provider outage) ends
    the polls at its second time, never spending every poll and its waits."""
    inbox = _inbox()
    calls: list = []

    def _always_times_out(tab, url, *a, **kw):
        calls.append(url)
        raise TimeoutError("the list did not render in time")
    monkeypatch.setattr(inbox, "list_messages", _always_times_out)
    browser_page.goto(fixtures_server + "/forms/code_gate.html")
    errors: list = []
    slept: list = []
    code = inbox.fetch_code(browser_page, "greenhouse.io",
                            fixtures_server + "/inbox/outlook_list.html", jev=jev.FakeJev(),
                            polls=3, sleep=slept.append, wait_s=7, errors=errors)
    assert code is None
    assert len(calls) == 2 and slept == [7]
    assert errors == ["TimeoutError", "TimeoutError"]


def test_the_polls_end_on_a_navigation_the_host_guard_stopped(browser_page, fixtures_server,
                                                               monkeypatch):
    """Review round 3, M3: a signed-out inbox redirects to its provider's
    sign-in, off the inbox host: the guard stops it, and no poll follows."""
    inbox = _inbox()
    calls: list = []

    def _redirected(tab, url, *a, **kw):
        calls.append(url)
        tab.goto("http://login.signin.invalid/sign-in", timeout=5_000)
        return []
    monkeypatch.setattr(inbox, "list_messages", _redirected)
    browser_page.goto(fixtures_server + "/forms/code_gate.html")
    errors: list = []
    slept: list = []
    code = inbox.fetch_code(browser_page, "greenhouse.io",
                            fixtures_server + "/inbox/outlook_list.html", jev=jev.FakeJev(),
                            polls=3, sleep=slept.append, wait_s=7, errors=errors)
    assert code is None
    assert len(calls) == 1 and slept == []
    assert len(errors) == 1
    assert len(browser_page.context.pages) == 1


def test_a_frame_the_guard_stops_leaves_the_polls_running(browser_page, fixtures_server,
                                                          monkeypatch):
    """Review round 4, M3: a webmail loads frames on other hosts (a token
    renewal, a cookie rotation), which the guard stops; that is no sign-in
    redirect of the inbox itself, and a later failed poll still leaves the
    next poll to run."""
    inbox = _inbox()
    real = inbox.list_messages
    calls: list = []

    def _framed_then_times_out(tab, url, *a, **kw):
        calls.append(url)
        if len(calls) == 1:
            tab.set_content('<iframe src="http://login.signin.invalid/renew"></iframe>')
            tab.wait_for_timeout(300)
            raise TimeoutError("the list did not render in time")
        return real(tab, url, *a, **kw)
    monkeypatch.setattr(inbox, "list_messages", _framed_then_times_out)
    browser_page.goto(fixtures_server + "/forms/code_gate.html")
    errors: list = []
    code = inbox.fetch_code(browser_page, "greenhouse.io",
                            fixtures_server + "/inbox/outlook_list.html", jev=jev.FakeJev(),
                            polls=2, sleep=lambda s: None, errors=errors)
    assert code == "MKPZ3QRA", errors
    assert len(calls) == 2 and errors == ["TimeoutError"]


@pytest.mark.parametrize("provider", ["outlook", "gmail"])
def test_list_messages_reads_every_row_of_the_provider_shape(
        browser_page, fixtures_server, provider):
    inbox = _inbox()
    rows = inbox.list_messages(browser_page,
                               fixtures_server + f"/inbox/{provider}_list.html")
    assert [r.n for r in rows] == [0, 1, 2, 3, 4, 5]
    assert [r.subject for r in rows] == [
        "Order #48213 confirmed", "This week in analytics", "Verify your email for Ashby",
        "Dinner on Friday", "Your Greenhouse security code", "Your statement is ready"]
    assert [r.sender for r in rows] == [
        "orders@shopfront.example", "newsletter@weekly.example", "no-reply@ashbyhq.com",
        "nina@friends.example", "no-reply@greenhouse.io", "billing@utilities.example"]
    assert "MKPZ3QRA" in rows[4].preview


def test_list_honours_the_limit_and_open_message_reads_the_body(browser_page, fixtures_server):
    inbox = _inbox()
    rows = inbox.list_messages(browser_page, fixtures_server + "/inbox/outlook_list.html", limit=1)
    assert len(rows) == 1
    assert rows[0].subject == "Order #48213 confirmed"
    assert "48213" in inbox.open_message(browser_page, rows[0])


def test_open_message_waits_for_the_clicked_rows_body(browser_page, fixtures_server):
    inbox = _inbox()
    rows = inbox.list_messages(browser_page, fixtures_server + "/inbox/spa_stale_body.html")

    body = inbox.open_message(browser_page, rows[1])

    assert "NEW-CODE-2468" in body
    assert "STALE-CODE-1357" not in body


def test_fetch_code_hands_the_ats_and_company_to_the_from_site_question(browser_page,
                                                                          fixtures_server):
    """The site is the form's host; the mail comes from the ATS. Both names
    reach the judge's from-site question."""
    inbox = _inbox()
    seen = []

    class Capturing(jev.FakeJev):
        def judge(self, state, questions):
            seen.append(questions)
            return super().judge(state, questions)

    code = inbox.fetch_code(browser_page, "127.0.0.1",
                            fixtures_server + "/inbox/outlook_list.html",
                            jev=Capturing(), polls=1, ats="greenhouse", company="Fabrikam")
    assert code == "MKPZ3QRA"
    from_site = [q for qs in seen for qid, q in qs.items() if qid.endswith("_from_site")]
    assert from_site
    blob = json.dumps(from_site[0]["instructions"])
    assert "Greenhouse" in blob and "Fabrikam" in blob and "127.0.0.1" in blob


def test_no_code_polls_are_bounded(browser_page, fixtures_server, monkeypatch):
    inbox = _inbox()
    # an inbox with no row at all: each poll's wait for rows (ACC-08) is cut
    # short here, the polls and their waits are what this test counts
    monkeypatch.setattr(inbox, "ROWS_WAIT_MS", 200)
    class Clock:
        now = 0
        waits = []

        def __call__(self):
            return self.now

        def sleep(self, seconds):
            self.waits.append(seconds)
            self.now += seconds

    timer = Clock()
    result = inbox.fetch_code(browser_page, "127.0.0.1",
                             fixtures_server + "/inbox/empty.html", jev=jev.FakeJev(),
                             clock=timer, sleep=timer.sleep, polls=3, wait_s=60)
    assert result is None
    assert timer.waits == [60, 60]
    assert len(browser_page.context.pages) == 1


def test_inbox_error_does_not_log_email_body(browser_page, fixtures_server, caplog):
    inbox = _inbox()
    class FailedJudge:
        def judge(self, state, questions):
            raise ValueError("private code MKPZ3QRA")

    assert inbox.fetch_code(browser_page, "127.0.0.1",
                            fixtures_server + "/inbox/outlook_list.html",
                            jev=FailedJudge(), polls=1) is None
    assert "MKPZ3QRA" not in caplog.text
    assert len(browser_page.context.pages) == 1


@pytest.mark.parametrize("subject,body,expected", [
    ("Your security code", "Your security code is MKPZ3QRA\nThanks.", ["MKPZ3QRA"]),
    ("Code: Q7R2XK", "Use it within ten minutes.", ["Q7R2XK"]),
    ("Order receipt", "Your order number is 987654. Thank you for shopping.", []),
    ("Welcome", "Copyright 2026 Fabrikam. All rights reserved.", []),
    ("Verify", "Enter the code\n\nAB12CD\n\nThe older one\nZZ99YY\nstops working.",
     ["AB12CD", "ZZ99YY"]),
    # an all-letter code in lower case: alone on its own line, or labelled on one line
    ("Your sign-in code", "Your sign-in code\n\nqwertyz\n\nIt expires in ten minutes.",
     ["qwertyz"]),
    ("Sign in", "Enter code: hunterz to continue.", ["hunterz"]),
    # the same shape in prose, a line below the word code, stays out
    ("Your code", "Here is the code you asked for.\nThanks.", []),
    # a sign-off or a footer word alone on its own line is prose in title case
    ("Verify your email", "Your code is MKPZ3QRA\n\nRegards\n\nUnsubscribe\n", ["MKPZ3QRA"]),
    ("Sign in", "Enter code: hunterz to continue.\n\nBest\nThe team\n", ["hunterz"]),
    # the label may carry both "is" and a colon
    ("Sign in", "Your code is: hunterz\nIt expires soon.", ["hunterz"]),
])
def test_candidates_reads_the_subject_and_the_body(subject, body, expected):
    assert _inbox().candidates(body, subject) == expected


def test_a_stray_listbox_does_not_hide_the_gmail_rows(browser_page, fixtures_server):
    """Gmail renders its own `role=listbox` widgets (a label picker), and the
    Outlook shape is tried first."""
    inbox = _inbox()
    rows = inbox.list_messages(browser_page,
                               fixtures_server + "/inbox/gmail_with_listbox.html")
    assert [r.subject for r in rows] == [
        "Order #48213 confirmed", "This week in analytics", "Verify your email for Ashby",
        "Dinner on Friday", "Your Greenhouse security code", "Your statement is ready"]


def test_a_subject_selector_drift_keeps_the_rows_that_carry_a_sender(browser_page,
                                                                       fixtures_server):
    """Outlook renamed its subject hook; the rows still name a sender, so they
    are listed with an empty subject and the judge reads the preview."""
    inbox = _inbox()
    rows = inbox.list_messages(browser_page,
                               fixtures_server + "/inbox/outlook_list_drift.html")
    assert [r.n for r in rows] == [0, 1, 2, 3, 4, 5]
    assert [r.subject for r in rows] == [""] * 6
    assert rows[4].sender == "no-reply@greenhouse.io"
    assert "MKPZ3QRA" in rows[4].preview


@pytest.mark.parametrize("url,expected", [
    ("https://mail.google.com/mail/u/0/#inbox", "gmail"),
    ("https://outlook.office.com/mail/", "outlook"),
    ("https://outlook.live.com/mail/0/", "outlook"),
    ("https://mail.wm.edu/owa/", None),
    ("https://outlook.com.example.invalid/mail/", None),
    ("http://127.0.0.1:8000/inbox/gmail_list.html", None),
])
def test_provider_for_reads_the_inbox_host(url, expected):
    assert _inbox().provider_for(url) == expected


# === SP7: a slow inbox, stale and used codes, verification links (ACC-05, 07, 08) ==========

def test_a_slow_inbox_is_read_once_its_rows_render(browser_page, fixtures_server):
    """ACC-08: the list renders 1.5 s after the page loads; the one poll
    waits for its rows instead of reading an empty list."""
    inbox = _inbox()
    code = inbox.fetch_code(browser_page, "127.0.0.1", fixtures_server + "/inbox/slow_list.html",
                            jev=jev.FakeJev(), polls=1, ats="greenhouse")
    assert code == "MKPZ3QRA"


def test_a_code_older_than_the_job_is_never_used(browser_page, fixtures_server):
    """ACC-07: an earlier Greenhouse code sits above the fresh one; the rows
    whose time is before the job's start are never offered."""
    from datetime import datetime, timedelta
    inbox = _inbox()
    url = fixtures_server + "/inbox/stale_list.html"
    since = datetime.now() - timedelta(minutes=1)
    assert inbox.fetch_code(browser_page, "127.0.0.1", url, jev=jev.FakeJev(), polls=1,
                            ats="greenhouse", since=since) == "MKPZ3QRA"
    # with no start time nothing is ruled stale, and the top row's code wins
    assert inbox.fetch_code(browser_page, "127.0.0.1", url, jev=jev.FakeJev(), polls=1,
                            ats="greenhouse") == "OLD7C0DE"


def test_a_code_used_once_in_the_job_is_never_offered_again(browser_page, fixtures_server):
    """ACC-07: a code the run typed (by its hash) is never picked again: a
    gate that comes back wants a new code."""
    inbox = _inbox()
    used = {inbox.code_hash("MKPZ3QRA")}
    assert inbox.fetch_code(browser_page, "127.0.0.1",
                            fixtures_server + "/inbox/outlook_list.html", jev=jev.FakeJev(),
                            polls=1, ats="greenhouse", used=used) is None


@pytest.mark.parametrize("text,expected", [
    ("Mon 1/5/2026 9:12 AM", (2026, 1, 5, 9, 12)),
    ("Thu, Sep 24, 2026, 10:42 PM", (2026, 9, 24, 22, 42)),
    ("2026-09-24T10:42:00", (2026, 9, 24, 10, 42)),
    ("1/5/2026", (2026, 1, 5, 0, 0)),
    ("Sep 3", (2026, 9, 3, 0, 0)),
    ("10:42 AM", (2026, 9, 24, 10, 42)),
    ("22:05", (2026, 9, 24, 22, 5)),
    ("Tue", (2026, 9, 22, 0, 0)),
    ("Yesterday", (2026, 9, 23, 0, 0)), ("", None), ("soon", None),
])
def test_parse_when_reads_a_rows_time(text, expected):
    from datetime import datetime
    now = datetime(2026, 9, 24, 23, 0)      # a Thursday
    got = _inbox().parse_when(text, now)
    assert (got.timetuple()[:5] if got else None) == expected


def test_a_rows_time_with_a_zone_is_read_in_local_time():
    """M1 (SP7 review): a `<time datetime>` in UTC ("...Z") or with an
    offset is turned to local time; read as local, a code from before the
    job's start looked newer than the start (ACC-07)."""
    from datetime import datetime, timedelta, timezone
    inbox = _inbox()
    since = datetime.now().replace(second=0, microsecond=0)
    before = (since - timedelta(hours=1)).astimezone()      # an hour before the start, local

    def row(when: str):
        return inbox.Message(0, "Greenhouse", "Your security code", "", "#m0", when=when)
    utc = before.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert inbox.parse_when(utc) == since - timedelta(hours=1)
    assert inbox._stale(row(utc), since)
    # a zone three hours ahead of this machine's: its own clock reads two
    # hours after the start, and the moment is an hour before it
    ahead = before.astimezone(timezone(before.utcoffset() + timedelta(hours=3)))
    assert inbox._stale(row(ahead.isoformat(timespec="seconds")), since)
    assert inbox._stale(row(ahead.strftime("%Y-%m-%d %H:%M%z")), since)
    # the same moments an hour after the start are fresh
    later = before + timedelta(hours=2)
    assert not inbox._stale(row(later.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")), since)


def test_fetch_link_takes_the_verification_link_on_the_application_site(
        browser_page, fixtures_server):
    """ACC-05: the account check's message among decoys; of its links only
    the one that verifies, on the application's own host, is handed back
    (never the careers link, the privacy page or the unsubscribe)."""
    inbox = _inbox()
    browser_page.goto(fixtures_server + "/forms/code_gate.html")
    refused: list = []
    link = inbox.fetch_link(browser_page, "127.0.0.1", fixtures_server + "/inbox/link_list.html",
                            jev=jev.FakeJev(), allowed=lambda host: host == "127.0.0.1",
                            polls=1, ats="workday", company="Fabrikam", refused=refused)
    assert link == fixtures_server + "/forms/workday_account_verified.html?token=6f1c2e9a0b7d4e35"
    assert refused == []
    assert len(browser_page.context.pages) == 1     # the inbox tab is gone, the job's stays


def test_fetch_link_never_opens_a_link_outside_the_allowed_hosts(browser_page, fixtures_server):
    """ACC-05: a verification link through a mail tracker's host is named
    and never requested."""
    inbox = _inbox()
    asked: list = []
    browser_page.context.on("request", lambda r: asked.append(r.url))
    refused: list = []
    link = inbox.fetch_link(browser_page, "127.0.0.1",
                            fixtures_server + "/inbox/link_outside_list.html", jev=jev.FakeJev(),
                            allowed=lambda host: host == "127.0.0.1", polls=1, ats="workday",
                            company="Fabrikam", refused=refused)
    assert link is None
    assert refused == ["click.mailtrack.invalid"]
    assert not any("mailtrack" in u or "mailer.example" in u for u in asked), asked


def test_verification_links_keep_the_allowed_verify_links_and_name_the_rest():
    inbox = _inbox()
    refused: list = []
    links = [("Verify Account", "https://acme.wd5.myworkdayjobs.com/verify?t=1"),
             ("Fabrikam Careers", "https://acme.wd5.myworkdayjobs.com/careers"),
             ("Confirm your email", "https://click.tracker.invalid/c?u=2"),
             ("here", "https://acme.wd5.myworkdayjobs.com/activate/3"),
             ("Verify Account", "https://acme.wd5.myworkdayjobs.com/verify?t=1"),
             ("Verify", "mailto:help@acme.example")]
    got = inbox.verification_links(links, lambda host: host.endswith("myworkdayjobs.com"),
                                   refused)
    assert got == [("Verify Account", "https://acme.wd5.myworkdayjobs.com/verify?t=1"),
                   ("here", "https://acme.wd5.myworkdayjobs.com/activate/3")]
    assert refused == ["click.tracker.invalid"]


@pytest.mark.parametrize("text, path", [
    ("Confirm", "/confirm-unsubscribe"),
    ("Verify", "/verify/decline"),
    ("Confirm unsubscribe", "/c/1"),
    ("Not you? Confirm here", "/confirm/2"),
    ("Verify", "/account/not-you"),
    ("Confirm password reset", "/confirm/3"),
    ("Verify", "/reset-password/verify"),
    ("Report this email", "/verify/report"),
    ("Confirm your email preferences", "/confirm/4"),
    ("Verify", "/email_preferences/confirm"),
])
def test_verification_links_never_take_an_unsubscribe_a_reset_or_a_not_you(text, path):
    """M2 (SP7 review): a link beside the check that carries a verify word
    in its text or its path is never it, and one alone is never opened."""
    inbox = _inbox()
    host = "https://acme.wd5.myworkdayjobs.com"
    assert inbox.verification_links([(text, host + path)], lambda h: True) == []
    got = inbox.verification_links([(text, host + path), ("Verify Account", host + "/verify?t=1")],
                                   lambda h: True)
    assert got == [("Verify Account", host + "/verify?t=1")]


def test_the_link_pick_names_each_link_by_its_text_and_host_never_its_url():
    import apply_judge
    state, questions = apply_judge.link_pick_questions(
        [("Verify Account", "acme.wd5.myworkdayjobs.com"), ("here", "acme.wd5.myworkdayjobs.com")],
        "Open the link below to verify your email address.")
    assert set(questions["link_pick"]["criteria"]) == {"link_0", "link_1", "none"}
    assert "?" not in json.dumps(state)          # no URL, so no token, reaches the judge
    pick = jev.Answer(kind="choice", choice="link_1", probabilities={"link_1": 1.0},
                      confidence=1.0)
    assert apply_judge.read_link_pick({"link_pick": pick}, 2) == 1
    assert apply_judge.read_link_pick({"link_pick": pick}, 1) is None
    none = jev.Answer(kind="choice", choice="none", probabilities={"none": 1.0}, confidence=1.0)
    assert apply_judge.read_link_pick({"link_pick": none}, 2) is None
