"""Checks over the real-page captures, which stay on this machine
(`tests/fixtures/local_captures/`, git-excluded). Every test here skips when
the folder is absent (a fresh clone, CI).

- The consent pre-step's control (`apply_form.consent_control`) on every
  capture that shows a cookie banner: the reject or close control the
  captures' expectations file names (`_consent_expected.json`, beside the
  captures), never an accept.

Each capture's `page.html` is loaded offline (the browser tests' guard
aborts every request off the local machine), so nothing reaches the site
it came from."""
import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_form  # noqa: E402

pytest_plugins = ["conftest_browser"]

CAPTURES = REPO / "tests" / "fixtures" / "local_captures"
EXPECTED = CAPTURES / "_consent_expected.json"


def _expected() -> dict:
    if not EXPECTED.is_file():
        return {}
    return {k: v for k, v in json.loads(EXPECTED.read_text(encoding="utf-8")).items()
            if not k.startswith("_")}


@pytest.mark.skipif(not EXPECTED.is_file(), reason="no local captures on this machine")
@pytest.mark.parametrize("capture", sorted(_expected()))
def test_the_consent_control_on_each_capture_with_a_banner(_browser, capture):
    folder = CAPTURES / capture
    if not (folder / "page.html").is_file():
        pytest.skip(f"{capture} is not on this machine")
    want = _expected()[capture]
    ctx = _browser.new_context(viewport={"width": 1400, "height": 1000})
    try:
        page = ctx.new_page()
        page.set_content((folder / "page.html").read_text(encoding="utf-8", errors="replace"),
                         wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(300)
        found = apply_form.consent_control(page)
    finally:
        ctx.close()
    got = [found[1]["kind"], found[1]["text"]] if found else None
    assert got == want, (capture, found)
    if found:
        assert not re.search(r"accept|allow|agree", found[1]["text"], re.I)
