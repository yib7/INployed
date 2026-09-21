"""Browser fixtures for the auto-apply tests (Playwright, headless Chromium).

A browser test module opts in with

    pytest_plugins = ["conftest_browser"]

at module scope (pytest registers the plugin once, so the session fixtures are
shared across every module that names it, and the non-browser suite never
loads it). Fixtures:

- `fixtures_server` (session): a `http.server` thread serving `tests/fixtures/`
  on a free localhost port; yields the base URL. The forms fixtures embed an
  iframe, and Chromium refuses a `file://` iframe, so they are served over http.
- `fixture_url` (session): `fixture_url(name) -> str` for a page under
  `tests/fixtures/forms/`.
- `browser_page` (function): a fresh page in a fresh context of one shared
  headless Chromium. Skips with "Chromium not installed" when the launch fails
  and via `importorskip` when Playwright itself is absent.
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
def fixture_url(fixtures_server):
    def _url(name: str) -> str:
        return f"{fixtures_server}/forms/{name}"
    return _url


def _point_at_installed_browsers() -> None:
    """`tests/conftest.py` redirects LOCALAPPDATA to a scratch dir for the whole
    session, and on Windows Playwright looks for its browsers under
    `%LOCALAPPDATA%\\ms-playwright`, so under pytest the installed Chromium
    would read as missing. Name the real directory through the env var the
    driver honours, unless the caller already did."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    real = Path.home() / "AppData" / "Local" / "ms-playwright"
    if real.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(real)


@pytest.fixture(scope="session")
def _browser():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    _point_at_installed_browsers()
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as e:    # noqa: BLE001  (Playwright raises its own Error class)
            pytest.skip(f"Chromium not installed: {e}")
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def browser_page(_browser):
    context = _browser.new_context()
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()
