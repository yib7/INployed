"""Browser fixtures for the auto-apply tests (Playwright, headless Chromium).

A browser test module opts in with

    pytest_plugins = ["conftest_browser"]

at module scope (pytest registers the plugin once, so the session fixtures are
shared across every module that names it, and the rest of the suite leaves it
unloaded). Fixtures:

- `fixtures_server` (session): a `http.server` thread serving `tests/fixtures/`
  on a free localhost port; yields the base URL. The forms fixtures embed an
  iframe, and Chromium refuses a `file://` iframe, so they are served over http.
- `fixture_url` (session): `fixture_url(name) -> str` for a page under
  `tests/fixtures/forms/`.
- `browser_page` (function): a fresh page in a fresh context of one headless
  Chromium per test module. Skips with "Chromium not installed" when the
  launch fails and via `importorskip` when Playwright itself is absent.
- `flow_server` (session): `apply_harness.FlowServer`, the fixtures served
  with a counted `POST /submit/<name>` (the flow matrix and its invariants).

The browser is module-scoped because Playwright's sync API keeps an asyncio
loop running on the main thread for as long as the `sync_playwright()`
context is open, and any test that then calls `asyncio.run()` (the scraper
suite does) fails with "cannot be called from a running event loop". Closing
the context when each browser module ends keeps the rest of the suite
unaware of it. For the same reason a browser test module should leave
`asyncio.run()` alone.
"""
from __future__ import annotations

import functools
import http.server
import os
import threading
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):    # noqa: A002  (the base class's signature)
        pass


@pytest.fixture(scope="session")
def fixtures_server():
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="fixtures-http", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="session")
def flow_server():
    import apply_harness
    server = apply_harness.FlowServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def fixture_url(fixtures_server):
    def _url(name: str) -> str:
        return f"{fixtures_server}/forms/{name}"
    return _url


_UNSET = object()


def _installed_browsers_path() -> str | None:
    """`tests/conftest.py` redirects LOCALAPPDATA to a scratch dir for the whole
    session, and on Windows Playwright looks for its browsers under
    `%LOCALAPPDATA%\\ms-playwright`, so under pytest the installed Chromium
    would read as missing. The real directory, when it exists and the caller
    has not named one through the env var the driver honours."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return None
    real = Path.home() / "AppData" / "Local" / "ms-playwright"
    return str(real) if real.is_dir() else None


@pytest.fixture(scope="module")
def _browser():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    previous = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", _UNSET)
    real = _installed_browsers_path()
    if real:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = real
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as e:    # noqa: BLE001  (Playwright raises its own Error class)
                pytest.skip(f"Chromium not installed: {e}")
            try:
                yield browser
            finally:
                browser.close()
    finally:
        if previous is _UNSET:
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        else:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = previous


@pytest.fixture
def browser_page(_browser):
    context = _browser.new_context()
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()
