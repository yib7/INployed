"""SP7: local_task — the pure layer that keeps the local LinkedInJobsWatcher
Task-Scheduler task in step with the VM schedule. No real schtasks anywhere:
`register` takes an injectable runner, and every test uses a fake."""
import os
from pathlib import Path

import local_task


# --- parse_offsets ----------------------------------------------------------------

def test_parse_offsets_plain():
    assert local_task.parse_offsets("30,50,70") == (30, 50, 70)


def test_parse_offsets_spaces():
    assert local_task.parse_offsets(" 15 , 45 ") == (15, 45)


def test_parse_offsets_junk_tolerated():
    # non-numeric and negative entries are dropped; the valid one survives
    assert local_task.parse_offsets("abc, -5, 20") == (20,)


def test_parse_offsets_empty_falls_back_to_default():
    assert local_task.parse_offsets("") == (30, 50, 70)
    assert local_task.parse_offsets(None) == (30, 50, 70)
    assert local_task.parse_offsets(" , ,junk") == (30, 50, 70)


def test_parse_offsets_custom_default():
    assert local_task.parse_offsets("", default=(10,)) == (10,)


# --- watcher_times ----------------------------------------------------------------

def test_watcher_times_vm_noon_evening():
    # the live scenario: VM runs at 12:00/20:00, watcher checks +30/+50/+70
    assert local_task.watcher_times(["12:00", "20:00"], (30, 50, 70)) == [
        "12:30", "12:50", "13:10", "20:30", "20:50", "21:10"]


def test_watcher_times_midnight_wraparound():
    assert local_task.watcher_times(["23:50"], (30,)) == ["00:20"]


def test_watcher_times_dedups():
    # 10:00+50 == 10:20+30 -> one 10:50 entry
    assert local_task.watcher_times(["10:00", "10:20"], (30, 50)) == [
        "10:30", "10:50", "11:10"]


def test_watcher_times_sorted():
    out = local_task.watcher_times(["20:00", "08:00"], (30,))
    assert out == sorted(out) == ["08:30", "20:30"]


# --- build_task_xml ---------------------------------------------------------------

_SUBS = dict(pythonw=r"C:\Py\pythonw.exe", script=r"C:\repo\local\watcher.py",
             workdir=r"C:\repo\local", user_id=r"BOX\jane")


def test_build_task_xml_no_placeholders_left():
    xml = local_task.build_task_xml(["12:30", "20:30"], **_SUBS)
    assert "{{" not in xml and "}}" not in xml
    assert r"C:\Py\pythonw.exe" in xml
    assert r"BOX\jane" in xml


def test_build_task_xml_one_calendar_trigger_per_time():
    times = ["12:30", "12:50", "13:10", "20:30", "20:50", "21:10"]
    xml = local_task.build_task_xml(times, **_SUBS)
    assert xml.count("<CalendarTrigger>") == len(times)
    assert xml.count("</CalendarTrigger>") == len(times)
    for t in times:
        assert f"T{t}:00</StartBoundary>" in xml


def test_build_task_xml_keeps_event_triggers_verbatim():
    template = (Path(local_task.__file__).parent / "task.xml").read_text(encoding="utf-8")
    xml = local_task.build_task_xml(["09:00"], **_SUBS)
    for tag in ("<LogonTrigger>", "<SessionStateChangeTrigger>", "<EventTrigger>"):
        assert tag in xml
    # the wake-from-sleep subscription survives byte-for-byte
    sub = next(line for line in template.splitlines() if "Power-Troubleshooter" in line)
    assert sub in xml


def test_build_task_xml_strips_boot_trigger():
    xml = local_task.build_task_xml(["09:00"], **_SUBS)
    assert "<BootTrigger>" not in xml and "</BootTrigger>" not in xml


# --- register ---------------------------------------------------------------------

class _Res:
    def __init__(self, rc=0, out="ok"):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def test_register_builds_schtasks_argv_and_utf16_file(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["raw"] = Path(argv[-1]).read_bytes()
        return _Res(0)

    ok, msg = local_task.register(["12:30", "20:30"], runner=fake_run)
    assert ok
    argv = seen["argv"]
    assert argv[0] == "schtasks"
    assert argv[1:5] == ["/Create", "/F", "/TN", local_task.TASK_NAME]
    assert argv[5] == "/XML"
    assert seen["raw"][:2] in (b"\xff\xfe", b"\xfe\xff")   # UTF-16 BOM
    body = seen["raw"].decode("utf-16")
    assert "12:30" in body and "{{" not in body
    assert not os.path.exists(argv[-1])                    # temp file cleaned up


def test_register_task_name_overridable():
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return _Res(0)

    ok, _ = local_task.register(["09:00"], task_name="TestWatcherDryRun", runner=fake_run)
    assert ok
    i = seen["argv"].index("/TN")
    assert seen["argv"][i + 1] == "TestWatcherDryRun"


def test_register_failure_reports_not_ok_and_cleans_up():
    seen = {}

    def fake_run(argv, **kw):
        seen["xml"] = argv[-1]
        return _Res(1, "Access is denied.")

    ok, msg = local_task.register(["09:00"], runner=fake_run)
    assert not ok
    assert "denied" in msg
    assert not os.path.exists(seen["xml"])


def test_register_launch_error_is_not_ok(monkeypatch):
    def boom(argv, **kw):
        raise OSError("no schtasks here")

    ok, msg = local_task.register(["09:00"], runner=boom)
    assert not ok and "no schtasks" in msg


def test_default_times_are_the_live_registered_schedule():
    assert local_task.DEFAULT_TIMES == ["10:10", "10:20", "10:30",
                                        "19:10", "19:20", "19:30"]
