"""Tests for the Playwright modern-board driver's PURE bits (apply_playwright).

Exercises apply.md parsing, name splitting, and folder artifact discovery — no
browser. The Playwright driving (fill_identity, upload_files, run) is validated in
live runs, not here, exactly like apply_verify's locator wrappers. The one exception
is ``run``'s report-before-hold ordering (test_run_report_written_before_hold),
which stubs the whole ``playwright.sync_api`` module via ``sys.modules`` so it
never launches a real browser either.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_playwright  # noqa: E402

# A representative slice of a real apply.md (Gotion/CITGO shape).
_APPLY_MD = """\
# Apply sheet — Associate Business Analyst @ CITGO
Generated 2026-07-05.

## Instructions for the form-filler (read first)
- **Never click the final Submit / Apply / Send / Finish button.** Stop at review.

## Candidate
- **Name:** Jane Doe
- **Email:** jane.doe@example.com
- **Phone:** 555-555-0100
- **Location:** Anytown, VA
- **LinkedIn:** https://linkedin.com/in/Jane
- **GitHub / Portfolio:** https://github.com/yib7

### Address
- **Full:** 123 Main Street, Anytown, Virginia ST 00000, United States
- **Street:** 123 Main Street
- **City:** Anytown
- **State / Province:** Virginia
- **ZIP / Postal:** ST 00000
- **Country:** United States

## Education
- College of William & Mary — B.S. Computer Science

## Standard answers
- **Are you legally authorized to work in the US?** Yes
- **Will you now or in the future require visa sponsorship?** No
- **Work-authorization statement (free text).** Authorized to work in the US; no sponsorship.
- **Gender (EEO self-identification).** Male

## Electronic signature (use at the end, where the form asks — do not submit)
- **Signature (type):** Jane Doe
- **Date:** use today's date (the day you apply)
"""


def test_split_name_two_parts():
    assert apply_playwright.split_name("Jane Doe") == ("Jane", "Doe")


def test_split_name_single_and_empty():
    assert apply_playwright.split_name("Cher") == ("Cher", "")
    assert apply_playwright.split_name("") == ("", "")
    assert apply_playwright.split_name("   ") == ("", "")


def test_split_name_multiword_surname():
    # Three+ tokens: first token is the first name, the rest is the surname.
    assert apply_playwright.split_name("Ana Maria de la Cruz") == ("Ana", "Maria de la Cruz")


def test_parse_candidate_block():
    p = apply_playwright.parse_apply_md(_APPLY_MD)
    c = p["candidate"]
    assert c["name"] == "Jane Doe"
    assert c["email"] == "jane.doe@example.com"
    assert c["phone"] == "555-555-0100"
    assert c["linkedin"] == "https://linkedin.com/in/Jane"
    assert c["github / portfolio"] == "https://github.com/yib7"


def test_parse_address_block():
    p = apply_playwright.parse_apply_md(_APPLY_MD)
    a = p["address"]
    assert a["country"] == "United States"
    assert a["street"] == "123 Main Street"
    assert a["zip / postal"] == "ST 00000"


def test_parse_standard_answers_keep_question_text():
    p = apply_playwright.parse_apply_md(_APPLY_MD)
    sa = dict(p["standard_answers"])
    assert sa["Are you legally authorized to work in the US?"] == "Yes"
    assert sa["Will you now or in the future require visa sponsorship?"] == "No"
    assert sa["Gender (EEO self-identification)."] == "Male"


def test_parse_signature_name():
    p = apply_playwright.parse_apply_md(_APPLY_MD)
    assert p["signature_name"] == "Jane Doe"


def test_parse_ignores_playbook_instruction_bullets():
    # The form-filler instructions are bold bullets too, but they live in the
    # Instructions section — they must NOT leak into candidate/standard answers.
    p = apply_playwright.parse_apply_md(_APPLY_MD)
    assert "Never click the final Submit / Apply / Send / Finish button." \
        not in dict(p["standard_answers"])
    assert all("Never click" not in k for k in p["candidate"])


def test_parse_empty_returns_empty_shape():
    p = apply_playwright.parse_apply_md("")
    assert p == {"candidate": {}, "address": {}, "standard_answers": [],
                 "signature_name": ""}


def test_load_folder_discovers_pdfs(tmp_path):
    (tmp_path / "apply.md").write_text(_APPLY_MD, encoding="utf-8")
    (tmp_path / "Jane_Doe_Resume.pdf").write_bytes(b"%PDF-1.4 resume")
    (tmp_path / "Jane_Doe_Cover_Letter.pdf").write_bytes(b"%PDF-1.4 cover")
    p = apply_playwright._load_folder(tmp_path)
    assert p["resume"].endswith("Jane_Doe_Resume.pdf")
    assert p["cover"].endswith("Jane_Doe_Cover_Letter.pdf")
    assert p["candidate"]["name"] == "Jane Doe"


def test_load_folder_missing_applymd_is_tolerant(tmp_path):
    p = apply_playwright._load_folder(tmp_path)
    assert p["candidate"] == {} and p["resume"] == "" and p["cover"] == ""


def test_write_report_roundtrip(tmp_path):
    rd = tmp_path / ".apply_run"
    report = {"url": "https://x.test", "status": "PARKED at review", "filled": {"email": "a@b.c"}}
    out = apply_playwright._write_report(rd, report)
    assert out == rd / "report.json"
    assert json.loads(out.read_text(encoding="utf-8")) == report


class _FakeLocator:
    def __init__(self, count=0):
        self._count = count

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def is_visible(self):
        return False

    def click(self, timeout=None):
        pass

    def fill(self, value):
        pass


class _FakePage:
    def __init__(self):
        self.url = "https://boards.greenhouse.io/example/jobs/1"

    def goto(self, url, wait_until=None, timeout=None):
        pass

    def wait_for_timeout(self, ms):
        pass

    def query_selector(self, selector):
        return None

    def get_by_label(self, label, exact=False):
        return _FakeLocator(count=0)

    def get_by_role(self, role, name=None, exact=False):
        return _FakeLocator(count=0)

    def set_input_files(self, selector, path):
        pass

    def inner_text(self, selector):
        return "body text"


class _FakeContext:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


class _FakeBrowser:
    def __init__(self, page):
        self._ctx = _FakeContext(page)
        self._connected = True

    def new_context(self, viewport=None):
        return self._ctx

    def is_connected(self):
        return self._connected

    def close(self):
        self._connected = False


class _FakeChromium:
    def __init__(self, page):
        self._page = page

    def launch(self, headless=False):
        return _FakeBrowser(self._page)


class _FakePlaywrightCM:
    """Stands in for the ``with sync_playwright() as p:`` context manager."""

    def __init__(self, page):
        self._page = page

    def __enter__(self):
        return _SimpleNamespace(chromium=_FakeChromium(self._page))

    def __exit__(self, *exc):
        return False


class _SimpleNamespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_run_report_written_before_hold(tmp_path, monkeypatch):
    """Stub the whole ``playwright.sync_api`` module (no real browser anywhere) and
    confirm ``run(..., submit=False)`` writes report.json BEFORE the hold — verified
    by making ``_hold`` itself assert the report already exists on disk."""
    import types

    folder = tmp_path / "job"
    folder.mkdir()
    (folder / "apply.md").write_text(
        "## Candidate\n- **Name:** Jane Doe\n- **Email:** jane@example.com\n",
        encoding="utf-8")

    fake_page = _FakePage()
    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: _FakePlaywrightCM(fake_page)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    run_dir = tmp_path / "rundir"
    hold_calls = []

    def fake_hold(browser):
        # By the time _hold is called, report.json must already exist.
        assert (run_dir / "report.json").exists()
        hold_calls.append(True)

    monkeypatch.setattr(apply_playwright, "_hold", fake_hold)

    report = apply_playwright.run(
        "https://boards.greenhouse.io/example/jobs/1", str(folder),
        submit=False, run_dir=str(run_dir), hold=True, headless=True)

    assert report["status"].startswith("PARKED at review")
    assert hold_calls == [True]
    written = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert written["status"] == report["status"]


class _CrashingPage(_FakePage):
    """A page whose goto() blows up — models a page-load timeout / launch failure
    in the fill/upload phase that has no per-branch try/except of its own."""

    def goto(self, url, wait_until=None, timeout=None):
        raise RuntimeError("Timeout 60000ms exceeded while navigating")


def test_run_crash_in_fill_phase_writes_failed_report(tmp_path, monkeypatch):
    """P1: a crash during page-load/fill/upload (goto timeout, launch failure, an
    uncaught fill/upload error) must STILL write report.json with a failed status —
    the orchestrator polls report.json, and a claimed entry with no terminal signal
    silently stalls the FIFO queue. The exception re-raises so the CLI exits nonzero,
    but report.json is on disk first, before any hold."""
    import types

    folder = tmp_path / "job"
    folder.mkdir()
    (folder / "apply.md").write_text(
        "## Candidate\n- **Name:** Jane Doe\n- **Email:** jane@example.com\n",
        encoding="utf-8")

    fake_page = _CrashingPage()
    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: _FakePlaywrightCM(fake_page)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    run_dir = tmp_path / "rundir"
    hold_calls = []

    def fake_hold(browser):
        # The failed report must already be on disk by the time we hold the window.
        assert (run_dir / "report.json").exists()
        hold_calls.append(True)

    monkeypatch.setattr(apply_playwright, "_hold", fake_hold)

    import pytest
    with pytest.raises(RuntimeError):
        apply_playwright.run(
            "https://boards.greenhouse.io/example/jobs/1", str(folder),
            submit=False, run_dir=str(run_dir), hold=True, headless=True)

    written = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert written["status"].startswith("failed:")
    assert "Timeout" in written["status"]
    assert hold_calls == [True]        # window held open after the failed report


def test_run_crash_writes_report_even_without_hold(tmp_path, monkeypatch):
    """The failed-report write does not depend on hold=True: a crash with hold=False
    still records report.json and re-raises."""
    import types

    folder = tmp_path / "job"
    folder.mkdir()
    (folder / "apply.md").write_text(
        "## Candidate\n- **Name:** Jane Doe\n", encoding="utf-8")

    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: _FakePlaywrightCM(_CrashingPage())
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    run_dir = tmp_path / "rundir"
    import pytest
    with pytest.raises(RuntimeError):
        apply_playwright.run(
            "https://boards.greenhouse.io/example/jobs/1", str(folder),
            submit=False, run_dir=str(run_dir), hold=False, headless=True)
    written = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert written["status"].startswith("failed:")


def test_detach_spawns_child_without_detach_and_writes_pid(tmp_path, monkeypatch):
    """--detach re-spawns this one-shot as a DETACHED child (so the parked window
    outlives the calling agent), writes driver.pid + serve.log, and returns without
    launching a browser itself. The child command must drop --detach (else it would
    re-detach forever) and keep --url/--folder."""
    seen = {}

    class FakeProc:
        pid = 7777

    def fake_popen(args, stdout=None, stderr=None, stdin=None, **kwargs):
        seen["args"] = list(args)
        seen["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(apply_playwright.subprocess, "Popen", fake_popen)
    rc = apply_playwright.main(
        ["--url", "https://boards.greenhouse.io/x/jobs/1", "--folder", str(tmp_path),
         "--detach"])
    assert rc == 0
    rd = tmp_path / ".apply_run"
    assert (rd / "driver.pid").read_text().strip() == "7777"
    assert (rd / "serve.log").exists()
    assert "--detach" not in seen["args"]       # child actually runs the browser
    assert "--url" in seen["args"] and "--folder" in seen["args"]
