"""osopen.open_path dispatches to the right per-OS file-manager command."""
import osopen


def test_windows_uses_startfile(monkeypatch, tmp_path):
    monkeypatch.setattr(osopen.sys, "platform", "win32")
    calls = []
    # os.startfile only exists on Windows; set it unconditionally for the test.
    monkeypatch.setattr(osopen.os, "startfile", lambda p: calls.append(p), raising=False)
    ran = []
    monkeypatch.setattr(osopen.subprocess, "run", lambda *a, **k: ran.append(a))

    (tmp_path / "x").mkdir()
    osopen.open_path(tmp_path / "x")

    assert calls == [str(tmp_path / "x")]   # startfile called with the stringified path
    assert ran == []                        # no subprocess on Windows


def test_macos_uses_open(monkeypatch, tmp_path):
    monkeypatch.setattr(osopen.sys, "platform", "darwin")
    ran = []
    monkeypatch.setattr(osopen.subprocess, "run", lambda argv, **k: ran.append(argv))

    (tmp_path / "x").mkdir()
    osopen.open_path(tmp_path / "x")

    assert ran == [["open", str(tmp_path / "x")]]


def test_linux_uses_xdg_open(monkeypatch, tmp_path):
    monkeypatch.setattr(osopen.sys, "platform", "linux")
    ran = []
    monkeypatch.setattr(osopen.subprocess, "run", lambda argv, **k: ran.append(argv))

    (tmp_path / "x").mkdir()
    osopen.open_path(tmp_path / "x")

    assert ran == [["xdg-open", str(tmp_path / "x")]]


def _no_shell(monkeypatch):
    calls = []
    monkeypatch.setattr(osopen.sys, "platform", "win32")
    monkeypatch.setattr(osopen.os, "startfile", lambda p: calls.append(p), raising=False)
    return calls


def test_documents_and_folders_open(monkeypatch, tmp_path):
    calls = _no_shell(monkeypatch)
    for name in ("Jane_Doe_Resume.pdf", "apply.md", "interview_prep.md", "notes.TXT"):
        (tmp_path / name).write_text("x", encoding="utf-8")
        osopen.open_path(tmp_path / name)
    osopen.open_path(tmp_path)
    assert len(calls) == 5


def test_a_program_or_shortcut_is_never_opened(monkeypatch, tmp_path):
    """A path from the tracker database (Import tracker merges any file in) or
    the queue: the shell's open verb on an .exe, .lnk or .bat would run it."""
    calls = _no_shell(monkeypatch)
    for name in ("setup.exe", "Resume.pdf.lnk", "run.bat", "x.ps1", "page.html"):
        (tmp_path / name).write_text("x", encoding="utf-8")
        assert osopen.can_open(tmp_path / name) is False
        try:
            osopen.open_path(tmp_path / name)
        except OSError:
            pass
        else:
            raise AssertionError(name)
    assert calls == []


def test_a_network_path_is_refused_before_the_disk_is_asked(monkeypatch):
    r"""Even `exists()` on \\host\share connects to that host with the
    user's Windows sign-in, so a UNC or device path is refused unlooked-up."""
    calls = _no_shell(monkeypatch)
    looked = []
    monkeypatch.setattr(osopen.Path, "is_dir", lambda self: looked.append(self) or True)
    for p in (r"\\attacker.example\share\Resumes", "//attacker.example/share",
              r"\\?\C:\Users", r"\\.\pipe\x"):
        assert osopen.can_open(p) is False
    assert looked == [] and calls == []
    assert osopen.can_open("") is False and osopen.can_open(None) is False
