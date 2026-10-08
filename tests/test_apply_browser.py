"""Which browser the auto-apply run launches: the installed Google Chrome on
its own persistent profile, with the bundled Playwright Chromium as the
fallback. Fake Playwright objects only; no browser starts here."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))

import apply_run  # noqa: E402
import jev  # noqa: E402
import setup_check  # noqa: E402


class _Page:
    def __init__(self):
        self.urls = []

    def goto(self, url, **kw):
        self.urls.append(url)


class _Ctx:
    def __init__(self):
        self.pages = []
        self.opened = []

    def new_page(self):
        page = _Page()
        self.opened.append(page)
        return page

    def close(self):
        pass


class _Chromium:
    """Records every launch; `fail_channels` names the channels that raise."""

    def __init__(self, fail_channels=()):
        self.calls = []
        self.fail_channels = set(fail_channels)
        self.ctx = _Ctx()

    def launch_persistent_context(self, user_data_dir, **kw):
        self.calls.append((user_data_dir, kw))
        if kw.get("channel") in self.fail_channels:
            raise RuntimeError("Chromium distribution 'chrome' is not found at C:/x/chrome.exe")
        return self.ctx


class _PW:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_launch_profile_starts_the_installed_chrome_first(tmp_path):
    chromium = _Chromium()
    ctx = apply_run.launch_profile(_PW(chromium), tmp_path / "profile", headless=True)
    assert ctx is chromium.ctx
    assert len(chromium.calls) == 1
    user_data_dir, kw = chromium.calls[0]
    assert user_data_dir == str(tmp_path / "profile")
    assert kw["channel"] == "chrome" == apply_run.BROWSER_CHANNEL
    assert kw["headless"] is True
    assert kw["viewport"] == apply_run.VIEWPORT


def test_launch_profile_falls_back_to_the_bundled_chromium_and_says_so(tmp_path, caplog):
    chromium = _Chromium(fail_channels={"chrome"})
    with caplog.at_level("WARNING", logger="apply_run"):
        ctx = apply_run.launch_profile(_PW(chromium), tmp_path / "profile", headless=False)
    assert ctx is chromium.ctx
    assert [kw.get("channel") for _, kw in chromium.calls] == ["chrome", None]
    assert chromium.calls[1][1]["headless"] is False
    assert "bundled Chromium" in caplog.text


def test_launch_profile_raises_when_neither_browser_starts(tmp_path):
    chromium = _Chromium(fail_channels={"chrome", None})
    with pytest.raises(RuntimeError):
        apply_run.launch_profile(_PW(chromium), tmp_path / "profile", headless=False)


def _fake_playwright(monkeypatch, chromium):
    import playwright.sync_api as sync_api
    pw = _PW(chromium)

    class _Starter:
        def start(self):
            return pw

        def __enter__(self):
            return pw

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _Starter())
    return pw


def test_the_drain_launches_through_launch_profile(tmp_path, monkeypatch):
    pytest.importorskip("playwright")
    chromium = _Chromium()
    _fake_playwright(monkeypatch, chromium)
    runner = apply_run.Runner(jev=jev.FakeJev(), profile_dir=tmp_path / "profile",
                              settings=dict(apply_run.DEFAULT_SETTINGS))
    _, ctx = runner._launch()
    assert ctx is chromium.ctx
    assert chromium.calls[0][1]["channel"] == "chrome"
    assert (tmp_path / "profile").is_dir()


def test_login_opens_linkedin_and_the_inbox_in_chrome(tmp_path, monkeypatch):
    pytest.importorskip("playwright")
    chromium = _Chromium()
    _fake_playwright(monkeypatch, chromium)
    monkeypatch.setattr(apply_run.apply_queue, "build_context",
                        lambda: {"inbox_url": "https://mail.example.com/inbox"})
    monkeypatch.setattr(apply_run, "hold_until_closed", lambda ctx, **kw: None)
    assert apply_run.login(tmp_path / "profile") == 0
    assert chromium.calls[0][1]["channel"] == "chrome"
    assert chromium.calls[0][1]["headless"] is False
    urls = [u for page in chromium.ctx.opened for u in page.urls]
    assert urls == [apply_run.LINKEDIN_LOGIN_URL, "https://mail.example.com/inbox"]


def test_chrome_installed_reads_the_known_install_paths(tmp_path):
    exe = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    assert setup_check.chrome_installed(paths=[exe]) is False
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert setup_check.chrome_installed(paths=[tmp_path / "missing.exe", exe]) is True


def test_chrome_install_paths_cover_the_windows_locations(monkeypatch):
    monkeypatch.setattr(setup_check.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMFILES", r"C:\Program Files")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    paths = [str(p) for p in setup_check.chrome_install_paths()]
    assert any(p.startswith(r"C:\Program Files") and p.endswith("chrome.exe") for p in paths)
    assert any(p.startswith(r"C:\Users\x\AppData\Local") for p in paths)


# --- one browser on the profile -------------------------------------------------------------

class _EventCtx(_Ctx):
    """A context double with the close event Playwright's has."""

    def __init__(self):
        super().__init__()
        self.handlers = {}

    def on(self, name, fn):
        self.handlers.setdefault(name, []).append(fn)

    def close(self):
        for fn in self.handlers.get("close", []):
            fn(self)


def test_launch_profile_holds_the_sentinel_until_the_context_closes(tmp_path):
    import profile_lock
    chromium = _Chromium()
    chromium.ctx = _EventCtx()
    profile = tmp_path / "profile"
    ctx = apply_run.launch_profile(_PW(chromium), profile, headless=True)
    assert profile_lock.sentinel_held(profile) is True
    ctx.close()
    assert profile_lock.sentinel_held(profile) is False


def test_launch_profile_holds_the_sentinel_on_the_bundled_fallback(tmp_path):
    # the bundled Chromium leaves no Chrome lock: the sentinel is what the
    # difficulty check sees
    import profile_lock
    chromium = _Chromium(fail_channels={"chrome"})
    chromium.ctx = _EventCtx()
    profile = tmp_path / "profile"
    ctx = apply_run.launch_profile(_PW(chromium), profile, headless=True)
    assert [kw.get("channel") for _, kw in chromium.calls] == ["chrome", None]
    assert profile_lock.busy(profile) is True
    ctx.close()
    assert profile_lock.busy(profile) is False


def test_launch_profile_opens_nothing_while_another_browser_holds_the_sentinel(tmp_path,
                                                                               monkeypatch):
    import profile_lock
    from test_profile_lock import hold_sentinel
    monkeypatch.setattr(profile_lock, "HOLD_WAIT_S", 0)
    chromium = _Chromium()
    guard = hold_sentinel(tmp_path / "profile")
    try:
        with pytest.raises(profile_lock.ProfileBusy) as err:
            apply_run.launch_profile(_PW(chromium), tmp_path / "profile", headless=True)
    finally:
        guard.release()
    assert chromium.calls == []
    assert str(err.value) == profile_lock.RUN_BUSY


def test_launch_profile_never_falls_back_onto_a_profile_chrome_holds(tmp_path):
    # Chrome refuses a profile another Chrome holds; the bundled build would
    # open it all the same
    import profile_lock
    from test_profile_lock import hold_chrome_lock
    chromium = _Chromium(fail_channels={"chrome"})
    release = hold_chrome_lock(tmp_path / "profile")
    try:
        with pytest.raises(profile_lock.ProfileBusy):
            apply_run.launch_profile(_PW(chromium), tmp_path / "profile", headless=True)
    finally:
        release()
    assert [kw.get("channel") for _, kw in chromium.calls] == ["chrome"]
    assert profile_lock.sentinel_held(tmp_path / "profile") is False


def test_launch_profile_gives_the_sentinel_back_when_no_browser_starts(tmp_path):
    import profile_lock
    chromium = _Chromium(fail_channels={"chrome", None})
    with pytest.raises(RuntimeError) as err:
        apply_run.launch_profile(_PW(chromium), tmp_path / "profile", headless=True)
    assert not isinstance(err.value, profile_lock.ProfileBusy)
    assert [kw.get("channel") for _, kw in chromium.calls] == ["chrome", None]
    assert profile_lock.sentinel_held(tmp_path / "profile") is False


def test_login_refuses_in_a_sentence_while_another_browser_holds_the_profile(tmp_path,
                                                                             monkeypatch,
                                                                             capsys):
    pytest.importorskip("playwright")
    import profile_lock
    from test_profile_lock import hold_sentinel
    monkeypatch.setattr(profile_lock, "HOLD_WAIT_S", 0)
    chromium = _Chromium()
    _fake_playwright(monkeypatch, chromium)
    monkeypatch.setattr(apply_run.apply_queue, "build_context",
                        lambda: {"inbox_url": "https://mail.example.com/inbox"})
    guard = hold_sentinel(tmp_path / "profile")
    try:
        assert apply_run.main(["login", "--profile", str(tmp_path / "profile")]) == 2
    finally:
        guard.release()
    assert chromium.calls == []
    assert capsys.readouterr().err.strip() == profile_lock.RUN_BUSY


# --- a sentinel the caller holds; the old parallel check's copies ---------------------------

class _ClosingCtx(_Ctx):
    """A context that fires its close handlers, as Playwright's does."""

    def __init__(self):
        super().__init__()
        self.handlers = []

    def on(self, name, fn):
        if name == "close":
            self.handlers.append(fn)

    def close(self):
        for fn in self.handlers:
            fn(self)


def test_launch_profile_uses_a_sentinel_its_caller_holds_and_leaves_it_held(tmp_path):
    """The difficulty check holds the sentinel across its jobs: each job's
    browser opens under it, and closing that browser keeps it held."""
    import profile_lock
    profile = tmp_path / "profile"
    guard = profile_lock.hold(profile)
    chromium = _Chromium()
    chromium.ctx = _ClosingCtx()
    try:
        ctx = apply_run.launch_profile(_PW(chromium), profile, headless=True, guard=guard)
        assert ctx is chromium.ctx
        ctx.close()
        assert profile_lock.sentinel_held(profile) is True
        # a failed start leaves the caller's sentinel alone too
        dead = _Chromium(fail_channels={"chrome", None})
        with pytest.raises(RuntimeError):
            apply_run.launch_profile(_PW(dead), profile, headless=True, guard=guard)
        assert profile_lock.sentinel_held(profile) is True
    finally:
        guard.release()
    assert profile_lock.sentinel_held(profile) is False


def test_launch_profile_without_a_guard_gives_the_sentinel_back_on_close(tmp_path):
    import profile_lock
    profile = tmp_path / "profile"
    chromium = _Chromium()
    chromium.ctx = _ClosingCtx()
    ctx = apply_run.launch_profile(_PW(chromium), profile, headless=True)
    assert profile_lock.sentinel_held(profile) is True
    ctx.close()
    assert profile_lock.sentinel_held(profile) is False


def _old_copy(name="slot-3"):
    import profile_lock
    folder = profile_lock.old_check_copies_dir() / name
    (folder / "Default" / "Network").mkdir(parents=True)
    (folder / "Default" / "Network" / "Cookies").write_text("old", encoding="utf-8")
    return folder


def test_launch_profile_deletes_the_old_parallel_checks_copies(tmp_path, monkeypatch):
    import profile_lock
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _old_copy()
    root = profile_lock.old_check_copies_dir()
    assert root == tmp_path / "linkedin_watcher" / "assess_profiles"
    apply_run.launch_profile(_PW(_Chromium()), tmp_path / "profile", headless=True)
    assert not root.exists()


def test_launch_profile_names_old_copies_it_could_not_delete(tmp_path, monkeypatch, caplog):
    import profile_lock
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(profile_lock, "drop_old_check_copies", lambda: False)
    with caplog.at_level("WARNING"):
        ctx = apply_run.launch_profile(_PW(_Chromium()), tmp_path / "profile", headless=True)
    assert ctx is not None
    assert apply_run.OLD_COPIES_LEFT.format(folder=profile_lock.old_check_copies_dir()) \
        in caplog.text


def test_dropping_the_old_copies_never_raises(tmp_path, monkeypatch):
    import shutil

    import profile_lock
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert profile_lock.drop_old_check_copies() is True            # nothing there
    folder = _old_copy()

    def stuck(*a, **kw):
        raise PermissionError("a browser has a file open")
    monkeypatch.setattr(shutil, "rmtree", stuck)
    assert profile_lock.drop_old_check_copies() is False
    assert folder.exists()
    monkeypatch.undo()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert profile_lock.drop_old_check_copies() is True
    assert not profile_lock.old_check_copies_dir().exists()


def test_dropping_the_old_copies_clears_read_only_files(tmp_path, monkeypatch):
    import os
    import stat

    import profile_lock
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    folder = _old_copy()
    cookies = folder / "Default" / "Network" / "Cookies"
    os.chmod(cookies, stat.S_IREAD)
    assert profile_lock.drop_old_check_copies() is True
    assert not profile_lock.old_check_copies_dir().exists()
