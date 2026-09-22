"""Inbox DOM fixtures and synthetic codes; no external inbox or keyring."""
import importlib.util
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
    code = inbox.fetch_code(browser_page, "greenhouse.io",
                            fixtures_server + f"/inbox/{provider}_list.html",
                            jev=jev.FakeJev(), polls=1)
    assert code == "MKPZ3QRA"          # not Q7R2XK (Ashby) and not 48213 (the order)
    assert browser_page.url == original
    assert len(browser_page.context.pages) == 1
    assert code not in caplog.text


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


def test_no_code_polls_are_bounded(browser_page, fixtures_server):
    inbox = _inbox()
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
])
def test_candidates_reads_the_subject_and_the_body(subject, body, expected):
    assert _inbox().candidates(body, subject) == expected
