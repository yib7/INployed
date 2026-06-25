"""osopen.open_path dispatches to the right per-OS file-manager command."""
import osopen


def test_windows_uses_startfile(monkeypatch, tmp_path):
    monkeypatch.setattr(osopen.sys, "platform", "win32")
    calls = []
    # os.startfile only exists on Windows; set it unconditionally for the test.
    monkeypatch.setattr(osopen.os, "startfile", lambda p: calls.append(p), raising=False)
    ran = []
    monkeypatch.setattr(osopen.subprocess, "run", lambda *a, **k: ran.append(a))

    osopen.open_path(tmp_path / "x")

    assert calls == [str(tmp_path / "x")]   # startfile called with the stringified path
    assert ran == []                        # no subprocess on Windows


def test_macos_uses_open(monkeypatch, tmp_path):
    monkeypatch.setattr(osopen.sys, "platform", "darwin")
    ran = []
    monkeypatch.setattr(osopen.subprocess, "run", lambda argv, **k: ran.append(argv))

    osopen.open_path(tmp_path / "x")

    assert ran == [["open", str(tmp_path / "x")]]


def test_linux_uses_xdg_open(monkeypatch, tmp_path):
    monkeypatch.setattr(osopen.sys, "platform", "linux")
    ran = []
    monkeypatch.setattr(osopen.subprocess, "run", lambda argv, **k: ran.append(argv))

    osopen.open_path(tmp_path / "x")

    assert ran == [["xdg-open", str(tmp_path / "x")]]
