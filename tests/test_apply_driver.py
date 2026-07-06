"""Tests for the file-driven Playwright REPL (apply_driver): serve + send protocol.

Fully hermetic — a FakePage stands in for Playwright's real page everywhere. No real
browser is ever launched in this file, and apply_driver only imports
`playwright.sync_api` lazily (inside the entry function that needs it), so importing
the module never requires Playwright to be installed.
"""
import io
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_driver  # noqa: E402


class FakePage:
    """Stands in for a Playwright Page. Records every call; lets tests script
    return values / exceptions per-call. No network, no browser, ever."""

    def __init__(self, url="https://example.com/apply", title="Apply"):
        self.url = url
        self._title = title
        self.calls = []
        self.closed = False
        self._eval_value = ""
        self._raise_on = None  # (method_name, exception) to raise once

    def title(self):
        self.calls.append(("title",))
        return self._title

    def eval_on_selector_all(self, selector, script):
        self.calls.append(("eval_on_selector_all", selector))
        if "input, textarea, select" in selector:
            return [{"tag": "input", "type": "text", "id": "first_name", "name": "",
                      "ph": "", "aria": "", "vis": True}]
        return [{"t": "Next", "id": "next-btn"}]

    def eval_on_selector(self, selector, script):
        self.calls.append(("eval_on_selector", selector))
        return len(self._eval_value)

    def inner_text(self, selector):
        self.calls.append(("inner_text", selector))
        return "Some page body text"

    def screenshot(self, path=None, full_page=False):
        self.calls.append(("screenshot", path))

    def goto(self, url, wait_until=None, timeout=None):
        self.calls.append(("goto", url))
        self.url = url

    def wait_for_timeout(self, ms):
        self.calls.append(("wait_for_timeout", ms))

    def fill(self, selector, value):
        self.calls.append(("fill", selector, value))

    def get_by_label(self, label, exact=False):
        self.calls.append(("get_by_label", label, exact))
        return _Locator(self, "fill_label", label)

    def locator(self, selector):
        self.calls.append(("locator", selector))
        return _Locator(self, "sel", selector)

    def get_by_role(self, role, name=None, exact=False):
        self.calls.append(("get_by_role", role, name, exact))
        return _Locator(self, "role", (role, name))

    @property
    def keyboard(self):
        return _Keyboard(self)

    def set_input_files(self, selector, paths):
        self.calls.append(("set_input_files", selector, paths))


class _Locator:
    def __init__(self, page, kind, key):
        self.page = page
        self.kind = kind
        self.key = key

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        self.page.calls.append(("click", self.kind, self.key, timeout))

    def fill(self, value):
        self.page.calls.append(("locator_fill", self.kind, self.key, value))


class _Keyboard:
    def __init__(self, page):
        self.page = page

    def type(self, value, delay=None):
        self.page.calls.append(("kbd_type", value))

    def press(self, key):
        self.page.calls.append(("kbd_press", key))


class ClosedPage(FakePage):
    """A page that raises the Playwright 'target closed' error on any op."""

    def _boom(self, *a, **k):
        raise RuntimeError("Target page, context or browser has been closed")

    def goto(self, *a, **k):
        self._boom()

    def wait_for_timeout(self, *a, **k):
        self._boom()

    def title(self):
        self._boom()


# ── do() action dispatch ─────────────────────────────────────────────────────

def test_do_actions_dispatch():
    page = FakePage()
    apply_driver.do(page, {"seq": 1, "action": "goto", "url": "https://x.test", "wait": 10})
    assert ("goto", "https://x.test") in page.calls

    page = FakePage()
    apply_driver.do(page, {"seq": 2, "action": "fill", "selector": "#a", "value": "hi"})
    assert ("fill", "#a", "hi") in page.calls

    page = FakePage()
    apply_driver.do(page, {"seq": 3, "action": "click", "selector": "#btn"})
    assert any(c[0] == "click" and c[2] == "#btn" for c in page.calls)

    page = FakePage()
    apply_driver.do(page, {"seq": 4, "action": "type", "selector": "#field", "value": "abc"})
    assert ("kbd_type", "abc") in page.calls

    page = FakePage()
    apply_driver.do(page, {"seq": 5, "action": "press", "key": "Enter"})
    assert ("kbd_press", "Enter") in page.calls

    page = FakePage()
    apply_driver.do(page, {"seq": 6, "action": "clear", "selector": "#c"})
    assert ("kbd_press", "Control+A") in page.calls
    assert ("kbd_press", "Delete") in page.calls


def test_do_paste_reports_length_only():
    page = FakePage()
    page._eval_value = "s3cret!"
    result = apply_driver.do(page, {"seq": 7, "action": "paste", "selector": "#pw"})
    assert result["pw_len"] == 7
    serialized = json.dumps(result)
    assert "s3cret!" not in serialized
    assert ("kbd_press", "Control+V") in page.calls


def test_do_set_files_paths():
    page = FakePage()
    apply_driver.do(page, {"seq": 8, "action": "set_files", "selector": "#resume",
                           "paths": ["C:/tmp/resume.pdf"]})
    assert ("set_input_files", "#resume", ["C:/tmp/resume.pdf"]) in page.calls


def test_do_park_writes_parked_json_and_keeps_loop(tmp_path):
    page = FakePage(url="https://ats.example/review", title="Review your application")
    result = apply_driver.do(page, {"seq": 9, "action": "park"}, workdir=tmp_path)
    parked = tmp_path / "parked.json"
    assert parked.exists()
    data = json.loads(parked.read_text(encoding="utf-8"))
    assert data["url"] == "https://ats.example/review"
    assert data["title"] == "Review your application"
    assert "ts" in data
    # park does not signal quit — the serve loop must stay alive.
    assert not result.get("quit")


def test_do_quit_flag():
    page = FakePage()
    result = apply_driver.do(page, {"seq": 10, "action": "quit"})
    assert result["quit"] is True
    assert result["ok"] is True


def test_dump_shape():
    page = FakePage()
    info = apply_driver.dump(page, 1, workdir=None)
    assert "inputs" in info
    assert "clickables" in info
    assert "text" in info
    assert info["url"] == page.url


# ── send/seq protocol ────────────────────────────────────────────────────────

def test_send_monotonic_seq_and_result(tmp_path, monkeypatch):
    workdir = tmp_path
    # Simulate a serve loop by pre-seeding out.json once send writes cmd.json.
    def fake_wait(*a, **k):
        cmd = json.loads((workdir / "cmd.json").read_text(encoding="utf-8"))
        return {"seq": cmd["seq"], "ok": True, "url": "https://x.test", "title": "T"}

    monkeypatch.setattr(apply_driver, "_wait_for_result", fake_wait)
    r1 = apply_driver.send(workdir, {"action": "dump"})
    assert r1["seq"] == 1
    r2 = apply_driver.send(workdir, {"action": "dump"})
    assert r2["seq"] == 2
    assert (workdir / "seq.txt").read_text().strip() == "2"


def test_send_utf8_safe_output(tmp_path, monkeypatch):
    # SuccessFactors-style page titles carry private-use glyphs; a STRICT cp1252
    # stdout raises UnicodeEncodeError on them unless the CLI's reconfigure
    # guard (in main()) kicks in. Drive main() with strict cp1252 and prove the
    # summary still prints.
    workdir = tmp_path

    def fake_wait(*a, **k):
        cmd = json.loads((workdir / "cmd.json").read_text(encoding="utf-8"))
        return {"seq": cmd["seq"], "ok": True, "url": "https://x.test",
                "title": "Apply  now"}

    monkeypatch.setattr(apply_driver, "_wait_for_result", fake_wait)
    raw = io.BytesIO()
    strict = io.TextIOWrapper(raw, encoding="cp1252", errors="strict",
                              write_through=True)
    monkeypatch.setattr(sys, "stdout", strict)
    rc = apply_driver.main(["send", "--workdir", str(workdir), '{"action": "dump"}'])
    strict.flush()
    assert rc == 0
    printed = raw.getvalue().decode("utf-8", errors="replace")
    assert "seq=1" in printed and "title:" in printed and "Apply" in printed


# ── serve loop (injected page) ───────────────────────────────────────────────

def test_serve_step_processes_cmd_writes_out(tmp_path):
    page = FakePage()
    workdir = tmp_path
    (workdir / "cmd.json").write_text(
        json.dumps({"seq": 1, "action": "dump"}), encoding="utf-8")
    last = apply_driver._serve_step(page, workdir, last_seq=-1)
    assert last == 1
    out = json.loads((workdir / "out.json").read_text(encoding="utf-8"))
    assert out["seq"] == 1
    assert out["ok"] is True
    assert not (workdir / "cmd.json").exists()


def test_serve_exits_cleanly_on_closed_browser(tmp_path):
    page = ClosedPage()
    workdir = tmp_path
    (workdir / "cmd.json").write_text(
        json.dumps({"seq": 1, "action": "goto", "url": "https://x.test"}), encoding="utf-8")
    # The step must not raise — it should detect the closed-target error and
    # signal the caller (serve()) to exit the loop cleanly.
    last, should_exit = apply_driver._serve_step(page, workdir, last_seq=-1, return_exit=True)
    assert should_exit is True


def test_is_closed_error_matches_target_closed_message():
    assert apply_driver._is_closed_error(
        RuntimeError("Target page, context or browser has been closed"))
    assert not apply_driver._is_closed_error(RuntimeError("some other error"))


# ── CLI --help smoke (no browser, no workdir needed) ────────────────────────

def test_cli_serve_help_exits_zero():
    r = subprocess.run(
        [sys.executable, str(REPO / "local" / "apply_driver.py"), "serve", "--help"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0


def test_cli_send_help_exits_zero():
    r = subprocess.run(
        [sys.executable, str(REPO / "local" / "apply_driver.py"), "send", "--help"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
