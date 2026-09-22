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
def test_fetch_code_keeps_application_tab_and_ignores_order_email(
        browser_page, fixtures_server, provider, caplog):
    inbox = _inbox()
    browser_page.goto(fixtures_server + "/forms/code_gate.html")
    original = browser_page.url
    code = inbox.fetch_code(browser_page, "127.0.0.1",
                            fixtures_server + f"/inbox/{provider}_list.html",
                            jev=jev.FakeJev(), polls=1)
    assert code == "MKPZ3QRA"
    assert browser_page.url == original
    assert len(browser_page.context.pages) == 1
    assert code not in caplog.text


def test_list_and_open_message(browser_page, fixtures_server):
    inbox = _inbox()
    rows = inbox.list_messages(browser_page, fixtures_server + "/inbox/outlook_list.html", limit=1)
    assert len(rows) == 1
    assert rows[0].subject == "Order receipt"
    assert "987654" in inbox.open_message(browser_page, rows[0])


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
