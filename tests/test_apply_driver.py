"""Tests for the file-driven Playwright REPL (apply_driver): serve + send protocol.

Fully hermetic — a FakePage stands in for Playwright's real page everywhere. No real
browser is ever launched in this file, and apply_driver only imports
`playwright.sync_api` lazily (inside the entry function that needs it), so importing
the module never requires Playwright to be installed.
"""
import io
import json
import os
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

    def set_input_files(self, selector, paths, timeout=None):
        self.calls.append(("set_input_files", selector, paths))

    def expect_file_chooser(self, timeout=None):
        return _FileChooserCtx(self)


class _FileChooser:
    def __init__(self, page):
        self.page = page

    def set_files(self, paths):
        self.page.calls.append(("chooser_set_files", paths))


class _FileChooserCtx:
    """Mimics Playwright's ``page.expect_file_chooser()`` context manager: yields an
    info object whose ``.value`` is the file chooser."""

    def __init__(self, page):
        self.page = page
        self.value = _FileChooser(page)

    def __enter__(self):
        self.page.calls.append(("expect_file_chooser",))
        return self

    def __exit__(self, *exc):
        return False


class _FrameFake:
    """A child frame that may or may not contain the requested file input."""

    def __init__(self, page, has_input):
        self.page = page
        self.has_input = has_input

    def set_input_files(self, selector, paths, timeout=None):
        self.page.calls.append(("frame_set_input_files", selector, paths, self.has_input))
        if not self.has_input:
            raise RuntimeError("waiting for locator " + selector)


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


def test_next_seq_recovers_from_corrupt_seq_file(tmp_path):
    """P2 #13: a hand-edited / corrupt seq.txt used to raise ValueError inside
    int(...) and kill the serve loop. _next_seq must now recover — treat an
    unparseable value as 0 and hand out 1 next — instead of crashing."""
    workdir = tmp_path
    (workdir / "seq.txt").write_text("not-a-number")
    assert apply_driver._next_seq(workdir) == 1
    assert (workdir / "seq.txt").read_text().strip() == "1"
    # And it keeps counting monotonically from the recovered value.
    assert apply_driver._next_seq(workdir) == 2


def test_next_seq_empty_file_starts_at_one(tmp_path):
    (tmp_path / "seq.txt").write_text("   ")
    assert apply_driver._next_seq(tmp_path) == 1


def test_next_seq_concurrent_no_duplicate(tmp_path):
    """P2 #13/#16: two callers sharing a workdir (orchestrator + a subagent, or a
    reopen racing a send) must never both get the same seq — a lost command. The
    byte-0 lock around the read-modify-write serializes them, so N threads each
    get a distinct seq and the file lands at N."""
    import threading

    workdir = tmp_path
    n = 20
    got: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait(timeout=10)
        s = apply_driver._next_seq(workdir)
        with lock:
            got.append(s)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(got) == n
    assert len(set(got)) == n                    # all distinct — no lost command
    assert sorted(got) == list(range(1, n + 1))  # a clean 1..N run
    assert int((workdir / "seq.txt").read_text().strip()) == n


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


# ── upload: file chooser + iframe fallback ───────────────────────────────────

def test_do_upload_uses_file_chooser(tmp_path):
    page = FakePage()
    apply_driver.do(page, {"seq": 20, "action": "upload", "trigger": "#add-doc",
                           "paths": ["C:/tmp/resume.pdf"]}, workdir=tmp_path)
    assert ("expect_file_chooser",) in page.calls
    assert any(c[0] == "click" and c[2] == "#add-doc" for c in page.calls)
    assert ("chooser_set_files", ["C:/tmp/resume.pdf"]) in page.calls


def test_do_upload_by_role_trigger_text(tmp_path):
    page = FakePage()
    apply_driver.do(page, {"seq": 21, "action": "upload", "trigger_text": "Upload a Resume",
                           "paths": ["r.pdf"]}, workdir=tmp_path)
    assert any(c[0] == "get_by_role" and c[2] == "Upload a Resume" for c in page.calls)
    assert ("chooser_set_files", ["r.pdf"]) in page.calls


def test_do_set_files_falls_back_to_child_frame(tmp_path):
    class FramePage(FakePage):
        def __init__(self):
            super().__init__()
            self._main = object()
            self._frame = _FrameFake(self, has_input=True)

        def set_input_files(self, selector, paths, timeout=None):
            self.calls.append(("set_input_files", selector, paths))
            raise RuntimeError("waiting for locator input[type=file]")

        @property
        def frames(self):
            return [self._main, self._frame]

        @property
        def main_frame(self):
            return self._main

    page = FramePage()
    apply_driver.do(page, {"seq": 22, "action": "set_files", "selector": "#resume",
                           "paths": ["C:/tmp/r.pdf"]})
    # main frame missed; the child frame carrying the input succeeded.
    assert ("set_input_files", "#resume", ["C:/tmp/r.pdf"]) in page.calls
    assert ("frame_set_input_files", "#resume", ["C:/tmp/r.pdf"], True) in page.calls


# ── persistence: liveness, detached launch, reopen ───────────────────────────

def test_driver_alive_detects_live_and_dead_pid(tmp_path):
    assert apply_driver._driver_alive(tmp_path) is None                 # no pidfile
    (tmp_path / "driver.pid").write_text(str(os.getpid()), encoding="utf-8")
    assert apply_driver._driver_alive(tmp_path) == os.getpid()          # our pid is alive
    (tmp_path / "driver.pid").write_text("999999999", encoding="utf-8")
    assert apply_driver._driver_alive(tmp_path) is None                 # unused pid is dead


def test_launch_spawns_detached_and_writes_pid(tmp_path, monkeypatch):
    seen = {}

    class FakeProc:
        pid = 4242

    def fake_popen(args, stdout=None, stderr=None, stdin=None, **kwargs):
        seen["args"] = list(args)
        seen["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(apply_driver.subprocess, "Popen", fake_popen)
    rc = apply_driver.launch(str(tmp_path))
    assert rc == 0
    assert (tmp_path / "driver.pid").read_text().strip() == "4242"
    assert (tmp_path / "serve.log").exists()
    assert "serve" in seen["args"] and "--workdir" in seen["args"]
    if os.name == "nt":
        assert seen["kwargs"].get("creationflags")            # detached creation flags
    else:
        assert seen["kwargs"].get("start_new_session") is True


def test_launch_noops_when_driver_already_alive(tmp_path, monkeypatch):
    (tmp_path / "driver.pid").write_text(str(os.getpid()), encoding="utf-8")
    called = []
    monkeypatch.setattr(apply_driver.subprocess, "Popen", lambda *a, **k: called.append(a))
    rc = apply_driver.launch(str(tmp_path))
    assert rc == 0
    assert called == []   # an already-alive driver is not relaunched (avoids profile lock)


def test_reopen_noop_when_already_alive(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(apply_driver, "_driver_alive", lambda wd: 1234)
    launched = []
    monkeypatch.setattr(apply_driver, "launch", lambda *a, **k: launched.append(a))
    rc = apply_driver.reopen(str(tmp_path))
    assert rc == 0 and launched == []
    assert "already open" in capsys.readouterr().out


def test_reopen_relaunches_and_navigates_when_dead(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_driver, "_driver_alive", lambda wd: None)
    (tmp_path / "parked.json").write_text(
        json.dumps({"url": "https://ats.example/apply/99", "title": "Review"}),
        encoding="utf-8")
    launched, sent = [], []
    monkeypatch.setattr(apply_driver, "launch",
                        lambda wd, headless=False: launched.append(wd) or 0)
    monkeypatch.setattr(apply_driver, "send",
                        lambda wd, cmd, timeout=60.0: sent.append(cmd) or {"ok": True})
    rc = apply_driver.reopen(str(tmp_path))
    assert rc == 0
    assert launched                                    # relaunched the detached driver
    assert sent and sent[0]["action"] == "goto"
    assert sent[0]["url"] == "https://ats.example/apply/99"


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


def test_cli_launch_help_exits_zero():
    r = subprocess.run(
        [sys.executable, str(REPO / "local" / "apply_driver.py"), "launch", "--help"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0


def test_cli_reopen_help_exits_zero():
    r = subprocess.run(
        [sys.executable, str(REPO / "local" / "apply_driver.py"), "reopen", "--help"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
