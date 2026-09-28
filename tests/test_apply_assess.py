"""The auto-apply difficulty check (cycle 19, SP6: DF-1 to DF-6).

DF-3's score is pure code over counts the check reads from the first
application page: a base by application system plus fixed steps, rounded half
up and clamped to 1-10, with Easy Apply, a closed or dead posting and a payment
page at 10 at once. DF-1 and DF-2: the gate, the profile and the walk to the
first application page on the local test pages, where the only click is an
Apply entry. DF-5: the cached page and "Check again with my answers" with no
browser. DF-6: the running Jev total.

Hermetic: FakeJev and NoisyJev, local pages and routed hosts in an offline
browser context, stores and the page cache in tmp_path. No network.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

import apply_assess as aa
import jev
import jev_switch

# The browser tests take the module's test browser (they skip where Playwright
# is missing; CI's browser step runs them); the score tables need neither.
pytest_plugins = ["conftest_browser", "conftest_jev"]

# --- DF-3: the score ---------------------------------------------------------------------


@pytest.mark.parametrize("system, base", [
    ("greenhouse", 2), ("lever", 2), ("ashby", 2),
    ("workable", 3), ("smartrecruiters", 3), ("jobvite", 3), ("bamboohr", 3),
    ("workday", 6), ("icims", 6),
    ("taleo", 7), ("successfactors", 7), ("oracle", 7),
    ("other", 4), ("", 4), ("brassring", 4), ("adp", 4),
])
def test_the_base_follows_the_application_system(system, base):
    assert aa.score(system=system)["score"] == base


def test_the_constants_are_the_specs():
    assert aa.SYSTEM_BASE == {
        "greenhouse": 2, "lever": 2, "ashby": 2,
        "workable": 3, "smartrecruiters": 3, "jobvite": 3, "bamboohr": 3,
        "workday": 6, "icims": 6,
        "taleo": 7, "successfactors": 7, "oracle": 7,
    }
    assert aa.UNKNOWN_BASE == 4
    assert (aa.PER_UNANSWERED, aa.UNANSWERED_CAP) == (1.5, 5)
    assert (aa.PER_ESSAY, aa.ESSAY_CAP) == (0.5, 2)
    assert (aa.SENSITIVE, aa.CAPTCHA, aa.ACCOUNT_WALL) == (3, 2, 1)
    assert (aa.HISTORY_EASIER, aa.HISTORY_HARDER, aa.HISTORY_MIN) == (-1, 1, 2)
    assert (aa.SCORE_MIN, aa.SCORE_MAX) == (1, 10)


def test_each_unanswerable_required_question_adds_one_and_a_half_up_to_five():
    got = [aa.score(system="greenhouse", unanswered=n)["score"] for n in range(6)]
    assert got == [2, 4, 5, 7, 7, 7]      # 2, 3.5, 5, 6.5, then 2 + 5


def test_each_required_essay_adds_a_half_up_to_two():
    got = [aa.score(system="greenhouse", essays=n)["score"] for n in range(6)]
    assert got == [2, 3, 3, 4, 4, 4]      # 2, 2.5, 3, 3.5, then 2 + 2


def test_a_half_rounds_up():
    assert aa.score(system="workable", essays=1)["score"] == 4          # 3.5
    assert aa.score(system="greenhouse", essays=1)["score"] == 3        # 2.5
    assert aa.score(system="workday", unanswered=1)["score"] == 8       # 7.5


def test_a_sensitive_field_a_captcha_and_an_account_wall_add_their_steps():
    assert aa.score(system="greenhouse", sensitive=True)["score"] == 5
    assert aa.score(system="greenhouse", captcha=True)["score"] == 4
    assert aa.score(system="greenhouse", account_wall=True)["score"] == 3
    assert aa.score(system="greenhouse", sensitive=True, captcha=True,
                    account_wall=True)["score"] == 8


def test_past_drains_move_the_score_by_one():
    assert aa.score(system="workday", past_submits=2)["score"] == 5
    assert aa.score(system="workday", past_submits=1)["score"] == 6
    assert aa.score(system="workday", past_parks=2)["score"] == 7
    assert aa.score(system="workday", past_parks=1)["score"] == 6


def test_one_park_takes_away_the_easier_step_and_two_parks_win():
    """"Submitted at least twice without a park": one park on that system
    keeps the easier step off, and two parks add the harder step."""
    assert aa.score(system="workday", past_submits=5, past_parks=1)["score"] == 6
    assert aa.score(system="workday", past_submits=5, past_parks=2)["score"] == 7


def test_the_score_is_clamped_to_one_through_ten():
    most = aa.score(system="oracle", unanswered=9, essays=9, sensitive=True, captcha=True,
                    account_wall=True, past_parks=3)
    assert most["score"] == 10
    least = aa.score(system="greenhouse", past_submits=9)
    assert least["score"] == 1


@pytest.mark.parametrize("stop", ["easy_apply", "closed", "dead", "payment"])
def test_easy_apply_a_closed_or_dead_posting_and_payment_are_ten_at_once(stop):
    got = aa.score(system="greenhouse", stop=stop)
    assert got["score"] == 10
    assert got["band"] == "Do it yourself"
    assert got["reasons"] == [aa.STOP_REASONS[stop]]


def test_a_stop_ignores_every_other_count():
    got = aa.score(system="greenhouse", stop="payment", past_submits=9, unanswered=0)
    assert got["score"] == 10


def test_an_unknown_stop_is_a_bug_and_raises():
    with pytest.raises(ValueError):
        aa.score(system="greenhouse", stop="gone")


@pytest.mark.parametrize("value, band", [
    (1, "Queue it"), (2, "Queue it"), (3, "Queue it"),
    (4, "May need an answer or two"), (5, "May need an answer or two"),
    (6, "May need an answer or two"),
    (7, "Do it yourself"), (8, "Do it yourself"), (9, "Do it yourself"),
    (10, "Do it yourself"),
])
def test_the_bands(value, band):
    assert aa.band_for(value) == band


def test_the_score_carries_its_band():
    assert aa.score(system="greenhouse")["band"] == "Queue it"
    assert aa.score(system="workday")["band"] == "May need an answer or two"
    assert aa.score(system="taleo")["band"] == "Do it yourself"


def test_the_reasons_name_each_step_the_score_took():
    got = aa.score(system="workday", unanswered=2, essays=1, sensitive=True, captcha=True,
                   account_wall=True, past_parks=2)
    assert got["reasons"] == [
        "Application system: Workday (base 6)",
        "2 required questions your answers cannot fill (+3)",
        "1 required essay the run would draft (+0.5)",
        "A required sensitive field, always yours to type (+3)",
        "A CAPTCHA or bot check (+2)",
        "An account wall with no saved account (+1)",
        "Past runs on Workday parked at least twice (+1)",
    ]
    assert got["score"] == 10


def test_the_reasons_for_an_unknown_system_and_a_smooth_history():
    got = aa.score(system="other", unanswered=1, past_submits=3)
    assert got["reasons"] == [
        "Application system: not one the check knows (base 4)",
        "1 required question your answers cannot fill (+1.5)",
        "Past runs on this system reached the end at least twice with no park (-1)",
    ]
    assert got["score"] == 5       # 4 + 1.5 - 1 = 4.5, rounded up


def test_the_capped_steps_say_so():
    got = aa.score(system="lever", unanswered=5, essays=6)
    assert got["reasons"][1:] == [
        "5 required questions your answers cannot fill (+5, the most this step adds)",
        "6 required essays the run would draft (+2, the most this step adds)",
    ]


# --- DF-1: the gate ------------------------------------------------------------------------

ON = {"jev_enabled": True, "jev_scoring": True, "jev_tailor": True, "jev_difficulty": True}
KEY = {"TYPESAFE_API_KEY": "not-a-real-key"}


@pytest.fixture
def sdk(monkeypatch):
    monkeypatch.setattr(jev_switch, "sdk_installed", lambda: True)


def test_the_gate_names_a_test_judge_before_the_jev_switch(sdk):
    off = dict(ON, jev_enabled=False)
    assert aa.refusal(config=off, env=KEY, mode="fake") == jev_switch.FIXTURE_ONLY
    assert aa.refusal(config=off, env=KEY, mode="replay") == jev_switch.FIXTURE_ONLY


def test_the_gate_then_asks_the_difficulty_switch(sdk):
    assert aa.refusal(config=ON, env=KEY, mode="typesafe") == ""
    assert aa.refusal(config=dict(ON, jev_difficulty=False), env=KEY, mode="typesafe") == \
        jev_switch.DIFFICULTY_OFF
    assert aa.refusal(config=dict(ON, jev_enabled=False), env=KEY, mode="typesafe") == \
        jev_switch.difficulty_blocked(config=dict(ON, jev_enabled=False), env=KEY,
                                      mode="typesafe")


def test_the_gate_counts_a_key_saved_in_settings(sdk):
    assert aa.refusal(config=ON, env={}, mode="typesafe") != ""
    assert aa.refusal(config=ON, env={}, mode="typesafe", saved_key=True) == ""


def test_the_gate_reads_the_auto_apply_judge_setting(sdk):
    cfg = dict(ON, auto_apply_jev_mode="fake")
    assert aa.refusal(config=cfg, env=KEY) == jev_switch.FIXTURE_ONLY


# --- DF-1: the profile ---------------------------------------------------------------------

def test_the_profile_is_the_drains(monkeypatch, tmp_path):
    import apply_run
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert aa.default_profile_dir() == apply_run.default_profile_dir()


def test_a_missing_or_idle_profile_is_free(tmp_path):
    assert aa.profile_busy(tmp_path / "nowhere") is False
    (tmp_path / "idle").mkdir()
    (tmp_path / "idle" / "lockfile").write_text("", encoding="utf-8")
    assert aa.profile_busy(tmp_path / "idle") is False


def _hold(lock: Path):
    """Hold `lock` as a running Chrome does: on Windows open with no sharing,
    elsewhere a SingletonLock naming this live process. Returns the undo. (A
    real Chrome is left out: it calls Google's services as it starts, and the
    bundled headless shell the tests launch locks nothing.)"""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        create = ctypes.windll.kernel32.CreateFileW
        create.restype = wintypes.HANDLE
        handle = create(str(lock), 0x40000000, 0, None, 4, 0x80, None)
        assert handle != wintypes.HANDLE(-1).value
        return lambda: ctypes.windll.kernel32.CloseHandle(handle)
    os.symlink(f"testhost-{os.getpid()}", str(lock.parent / "SingletonLock"))
    return lambda: os.unlink(str(lock.parent / "SingletonLock"))


def test_a_held_lock_reads_busy_and_a_released_one_free(tmp_path):
    (tmp_path / "profile").mkdir()
    release = _hold(tmp_path / "profile" / "lockfile")
    try:
        assert aa.profile_busy(tmp_path / "profile") is True
    finally:
        release()
    assert aa.profile_busy(tmp_path / "profile") is False


@pytest.mark.skipif(os.name == "nt", reason="the SingletonLock is the POSIX lock")
def test_a_singleton_lock_of_a_gone_process_is_free(tmp_path):
    (tmp_path / "profile").mkdir()
    os.symlink("testhost-999999999", str(tmp_path / "profile" / "SingletonLock"))
    assert aa.profile_busy(tmp_path / "profile") is False


# --- DF-5: the cached page -------------------------------------------------------------------

@pytest.fixture
def appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    return tmp_path / "appdata"


def test_the_page_is_cached_per_job_under_linkedin_watcher(appdata):
    page = {"job_id": "42", "url": "https://jobs.lever.co/x", "stop": "", "digest": None}
    path = aa.save_page(page)
    assert path == appdata / "linkedin_watcher" / "apply_assess" / "42.json"
    assert aa.load_page("42") == page
    assert aa.load_page("43") is None


def test_an_odd_job_id_gets_a_safe_file_name(appdata):
    one, two = aa.cache_path("a/b"), aa.cache_path("a:b")
    assert one.parent == two.parent == appdata / "linkedin_watcher" / "apply_assess"
    assert one != two
    assert "/" not in one.name and ":" not in two.name


def test_a_cached_page_for_another_job_is_no_page(appdata):
    aa.save_page({"job_id": "42", "digest": None})
    aa.cache_path("42").rename(aa.cache_path("7"))
    assert aa.load_page("7") is None


def test_the_age_shows_past_seven_days():
    now = datetime(2026, 9, 27, 12, 0)
    assert aa.age_text("2026-09-20T12:00:00", now) == ""
    assert aa.age_text("2026-09-19T11:00:00", now) == "8 days old"
    assert aa.age_text("not a time", now) == ""
    assert aa.STALE_DAYS == 7


@pytest.mark.parametrize("value, family", [
    (1, "success"), (3, "success"), (4, "warning"), (6, "warning"), (7, "danger"),
    (10, "danger"), (None, "neutral"), ("", "neutral"), (0, "neutral"),
])
def test_the_band_colour(value, family):
    assert aa.band_family(value) == family


@pytest.mark.parametrize("url, system", [
    ("https://jobs.lever.co/fabrikam/1/apply", "lever"),
    ("https://boards.greenhouse.io/fabrikam/jobs/1", "greenhouse"),
    ("https://fabrikam.wd5.myworkdayjobs.com/en-US/careers/job/1", "workday"),
    ("https://fabrikam.bamboohr.com/careers/1", "bamboohr"),
    ("https://www.linkedin.com/jobs/view/1/", ""),
    ("https://careers.fabrikam.example/apply", ""),
    ("", ""),
])
def test_the_system_follows_the_host(url, system):
    assert aa.system_for(url) == system


def _ran(jid, status, domain="jobs.lever.co", attempts=1):
    return {"job_posting_id": jid, "status": status, "attempts": attempts,
            "ats": {"system": "", "domain": domain}}


def test_past_runs_count_the_other_jobs_the_drain_ran_on_the_system():
    entries = [_ran("1", "submitted"), _ran("2", "ready_to_submit"), _ran("3", "needs_human"),
               _ran("4", "submitted", attempts=0), _ran("5", "submitted", "boards.greenhouse.io"),
               _ran("42", "needs_human"), _ran("6", "failed")]
    assert aa.past_runs(entries, "lever", "42") == (2, 1)
    assert aa.past_runs(entries, "", "42") == (0, 0)


def test_a_stop_page_is_scored_again_with_no_browser(appdata):
    aa.save_page({"job_id": "42", "url": "https://x.example", "system": "", "stop": "closed",
                  "notes": [], "checked_at": "2026-09-01T10:00:00", "digest": None})
    got, why = aa.recheck_job({"job_posting_id": "42"}, judge=_NoJudge(), answers=[],
                              settings={}, entries=[])
    assert why == ""
    assert got["score"] == 10 and got["reasons"] == [aa.STOP_REASONS["closed"]]
    assert got["checked_at"] == "2026-09-01T10:00:00"


def test_a_recheck_with_no_saved_page_says_so(appdata):
    got, why = aa.recheck_job({"job_posting_id": "42"}, judge=_NoJudge(), answers=[],
                              settings={}, entries=[])
    assert got is None and why == aa.NO_SAVED_PAGE


class _NoJudge:
    def judge(self, state, questions):
        raise AssertionError("the judge was asked")


def test_select_jobs():
    entries = [{"job_posting_id": "1", "status": "queued"},
               {"job_posting_id": "2", "status": "needs_human"},
               {"job_posting_id": "3", "status": "in_progress"}]
    assert [e["job_posting_id"] for e in aa.select_jobs(entries, [], all_queued=True)[0]] == ["1"]
    chosen, unknown = aa.select_jobs(entries, ["2", "3", "9"], all_queued=False)
    assert [e["job_posting_id"] for e in chosen] == ["2"] and unknown == ["9"]


# --- DF-1: the command line ----------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch, tmp_path):
    import apply_run
    import settings
    monkeypatch.setattr(apply_run, "_load_env", lambda: None)
    monkeypatch.setattr(apply_run, "load_settings", lambda: dict(apply_run.DEFAULT_SETTINGS))
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(jev_switch, "sdk_installed", lambda: True)

    def _never(*a, **kw):
        raise AssertionError("the real settings store was read")
    monkeypatch.setattr(settings, "load", _never)
    monkeypatch.setattr(settings, "secret_status", _never)
    return monkeypatch


def _write_switch(cfg):
    path = jev_switch.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg), encoding="utf-8")


def test_main_refuses_a_test_judge_first(cli, capsys):
    _write_switch(dict(ON, jev_enabled=False, auto_apply_jev_mode="fake"))
    assert aa.main(["--all"]) == 2
    assert jev_switch.FIXTURE_ONLY in capsys.readouterr().err


def test_main_refuses_with_the_switch_off(cli, capsys):
    _write_switch(dict(ON, jev_difficulty=False, auto_apply_jev_mode="typesafe"))
    assert aa.main(["--all"]) == 2
    assert jev_switch.DIFFICULTY_OFF in capsys.readouterr().err


def test_main_loads_the_env_before_the_jev_gate(cli, capsys, monkeypatch):
    import apply_run
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.setattr(apply_run, "_load_env",
                        lambda: monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key"))
    monkeypatch.setattr(aa, "profile_busy", lambda profile=None: True)
    _write_switch(dict(ON, auto_apply_jev_mode="typesafe"))
    assert aa.main(["--all"]) == 2
    assert aa.PROFILE_BUSY in capsys.readouterr().err


def test_main_refuses_while_a_drain_holds_the_profile(cli, capsys, monkeypatch):
    monkeypatch.setattr(aa, "profile_busy", lambda profile=None: True)
    _write_switch(dict(ON, auto_apply_jev_mode="typesafe"))
    assert aa.main(["42"]) == 2
    assert aa.PROFILE_BUSY in capsys.readouterr().err


def test_main_needs_ids_or_all(cli, capsys):
    assert aa.main([]) == 2


# --- DF-2: the walk, on the local test pages ---------------------------------------------

CAREERS = "https://careers.fabrikam.example"
LINKEDIN_JOB = "https://www.linkedin.com/jobs/view/4438751519/"
FORMS = Path(__file__).resolve().parent / "fixtures" / "forms"
_READ_ONLY = {"click"}


@pytest.fixture
def walk_env(tmp_path, monkeypatch):
    import apply_harness as h
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    with h.hermetic(tmp_path), h.fast_timing():
        yield tmp_path


@pytest.fixture
def context(_browser, walk_env):
    import apply_harness as h
    ctx = _browser.new_context()
    h.offline(ctx)
    try:
        yield ctx
    finally:
        ctx.close()


def _serve(context, pages: dict, host=CAREERS):
    def _handle(route):
        path = "/" + route.request.url.split(host + "/", 1)[1].split("?")[0]
        route.fulfill(body=pages.get(path, "<body>gone</body>"), content_type="text/html")
    context.route(f"{host}/**", _handle)


def _routes(context, routes):
    import apply_harness as h
    for glob, body in routes.items():
        context.route(glob, h._fulfiller(body))


def _form(name):
    return (FORMS / name).read_text(encoding="utf-8")


def _check(context, tmp_path, url, *, judge=None, generate=True, answers=None, **kw):
    """One job at `url` through the check with the action recorder on:
    (the difficulty, why none, the recorder, the entry)."""
    import apply_harness as h
    import apply_queue
    folder = h.write_job_folder(tmp_path / "job")
    apply_queue.enqueue(apply_queue.new_entry("42", company="Fabrikam",
                                              title="Analytics Engineer", apply_url=url, **kw))
    apply_queue.set_artifacts("42", {"folder": str(folder), "apply_md": str(folder / "apply.md"),
                                     "resume_pdf": str(folder / "Jane_Doe_Resume.pdf")})
    entry = apply_queue.load()["jobs"][0]
    rec = h.Recorder(None)
    with rec.recording():
        got, why = aa.check_job(entry, context=context, judge=judge or jev.FakeJev(),
                                answers=h.bank() if answers is None else answers,
                                settings={"auto_apply_generate": generate}, entries=[entry])
    return got, why, rec, entry


def _only_entries(rec, clicks):
    """DF-2's pin: nothing typed, ticked, picked, uploaded, pressed or
    dispatched, and the only clicks are the Apply entries named."""
    assert {a.kind for a in rec.actions} <= _READ_ONLY, rec.actions
    assert [a.text.strip() for a in rec.actions] == clicks, rec.actions


def test_a_lever_form_answered_in_full_is_easy(context, tmp_path):
    _serve(context, {"/fabrikam/1/apply": _form("lever_single.html")},
           host="https://jobs.lever.co")
    got, why, rec, _ = _check(context, tmp_path, "https://jobs.lever.co/fabrikam/1/apply")
    assert why == ""
    assert (got["score"], got["band"], got["system"]) == (2, "Queue it", "lever")
    assert got["questions"] == []
    _only_entries(rec, [])


def test_a_posting_s_apply_is_the_one_click(context, tmp_path):
    _serve(context, {"/fabrikam/1": _form("job_posting.html"),
                     "/fabrikam/ashby_steps.html": _form("ashby_steps.html")},
           host="https://jobs.ashbyhq.com")
    got, why, rec, _ = _check(context, tmp_path, "https://jobs.ashbyhq.com/fabrikam/1")
    assert why == ""
    assert (got["score"], got["system"]) == (2, "ashby")
    _only_entries(rec, ["Apply now"])


def test_linkedins_apply_through_its_redirect(context, flow_server, tmp_path):
    import apply_harness as h
    _routes(context, h.linkedin_job_routes()(flow_server.base))
    got, why, rec, _ = _check(context, tmp_path, LINKEDIN_JOB)
    assert why == ""
    assert got["score"] == aa.UNKNOWN_BASE and got["questions"] == []
    _only_entries(rec, ["Apply"])


def test_linkedins_safety_reminder_is_passed_with_no_click(context, flow_server, tmp_path):
    import apply_harness as h
    _routes(context, h.linkedin_job_routes(hop="linkedin_safety_interstitial.html",
                                           target="lever_single.html")(flow_server.base))
    got, why, rec, _ = _check(context, tmp_path, LINKEDIN_JOB)
    assert why == ""
    assert got["score"] == aa.UNKNOWN_BASE
    _only_entries(rec, ["Apply"])


def test_a_job_boards_company_link_is_followed(context, flow_server, tmp_path):
    import apply_harness as h
    _routes(context, h.board_routes(flow_server.base))
    got, why, rec, _ = _check(context, tmp_path, LINKEDIN_JOB)
    assert why == ""
    assert got["score"] == aa.UNKNOWN_BASE
    _only_entries(rec, ["Apply", "Apply on company site"])


def test_a_job_board_with_no_company_link_is_a_dead_end(context, flow_server, tmp_path):
    import apply_harness as h
    _routes(context, h.board_routes(flow_server.base, company_link=False))
    got, why, rec, _ = _check(context, tmp_path, LINKEDIN_JOB)
    assert why == ""
    assert got["score"] == 10 and got["reasons"][0] == aa.STOP_REASONS["dead"]
    _only_entries(rec, ["Apply"])


def test_an_essay_the_run_drafts_adds_a_half(context, flow_server, tmp_path):
    got, why, rec, _ = _check(context, tmp_path, f"{flow_server.base}/forms/essay_required.html")
    assert why == ""
    assert got["score"] == 5 and got["questions"] == []
    assert any("1 required essay" in r for r in got["reasons"])
    _only_entries(rec, [])


def test_an_essay_with_drafting_off_is_a_question(context, flow_server, tmp_path):
    got, why, rec, _ = _check(context, tmp_path, f"{flow_server.base}/forms/essay_required.html",
                              generate=False)
    assert why == ""
    assert got["score"] == 6
    [q] = got["questions"]
    assert q["label"].startswith("Describe a project you are proud of")
    assert (q["required"], q["type"], q["options"]) == (True, "text", [])
    _only_entries(rec, [])


def test_a_login_wall_with_no_saved_account(context, flow_server, tmp_path):
    got, why, rec, _ = _check(context, tmp_path, f"{flow_server.base}/forms/login_wall.html")
    assert why == ""
    assert got["score"] == aa.UNKNOWN_BASE + aa.ACCOUNT_WALL
    assert aa.ACCOUNT_NOTE in got["reasons"]
    _only_entries(rec, [])


def test_a_bot_check(context, flow_server, tmp_path):
    got, why, rec, _ = _check(context, tmp_path, f"{flow_server.base}/forms/captcha.html")
    assert why == ""
    assert got["score"] == aa.UNKNOWN_BASE + aa.CAPTCHA
    assert aa.CHECK_NOTE in got["reasons"]
    _only_entries(rec, [])


def test_an_apply_that_opens_an_email_is_ten_with_no_click(context, flow_server, tmp_path):
    got, why, rec, _ = _check(context, tmp_path, f"{flow_server.base}/forms/mailto_apply.html")
    assert why == ""
    assert got["score"] == 10
    assert aa.MAILTO_NOTE.format(address="jobs@contoso.example") in got["reasons"]
    _only_entries(rec, [])


@pytest.mark.parametrize("page, stop", [("linkedin_easy_apply.html", "easy_apply"),
                                        ("linkedin_closed.html", "closed")])
def test_linkedins_easy_apply_and_closed_postings_are_ten(context, flow_server, tmp_path,
                                                        page, stop):
    import apply_harness as h
    _routes(context, h.linkedin_job_routes(page)(flow_server.base))
    got, why, rec, _ = _check(context, tmp_path, LINKEDIN_JOB)
    assert why == ""
    assert got["score"] == 10 and got["reasons"] == [aa.STOP_REASONS[stop]]
    _only_entries(rec, [])


def test_an_easy_apply_entry_is_ten_and_opens_no_page(context, tmp_path):
    requests = []
    context.on("request", lambda r: requests.append(r.url))
    got, why, rec, _ = _check(context, tmp_path, LINKEDIN_JOB, is_easy_apply=True)
    assert got["score"] == 10 and got["system"] == "linkedin"
    assert requests == [] and context.pages == []
    _only_entries(rec, [])


def test_a_page_that_does_not_load_gives_no_result(context, tmp_path):
    context.route(f"{CAREERS}/**", lambda route: route.abort())
    got, why, rec, _ = _check(context, tmp_path, f"{CAREERS}/apply")
    assert got is None and why.startswith("the posting did not load")
    assert not aa.cache_path("42").exists()


def test_check_again_with_my_answers_reads_the_saved_page(context, flow_server, tmp_path):
    got, why, rec, entry = _check(context, tmp_path,
                                  f"{flow_server.base}/forms/essay_required.html",
                                  generate=False)
    assert why == ""
    [q] = got["questions"]
    assert got["score"] == 6      # an unknown system (4) and one question (+1.5, half up)
    import apply_facts
    import apply_harness as h
    answers = h.bank() + [{"id": "custom_project", "type": "text",
                           "answer": "A dashboard for the county food bank.",
                           "question": apply_facts.saved_question(q["label"], q["help"]),
                           "note": "", "confirmed": True, "status": "active"}]
    context.close()             # no browser from here on
    again, why = aa.recheck_job(entry, judge=jev.FakeJev(), answers=answers,
                                settings={"auto_apply_generate": False}, entries=[entry])
    assert why == ""
    assert again["questions"] == [] and again["score"] == aa.UNKNOWN_BASE
    assert again["checked_at"] == got["checked_at"]


@pytest.mark.parametrize("seed", (1, 2, 3))
def test_a_noisy_judge_never_types_or_clicks_past_the_entry(context, flow_server, tmp_path,
                                                            seed):
    import apply_harness as h
    _routes(context, h.linkedin_job_routes(target="lever_single.html")(flow_server.base))
    got, why, rec, _ = _check(context, tmp_path, LINKEDIN_JOB,
                              judge=jev.NoisyJev(jev.FakeJev(), seed=seed))
    assert {a.kind for a in rec.actions} <= _READ_ONLY, rec.actions
    assert [a.text.strip() for a in rec.actions] in ([], ["Apply"]), rec.actions
    assert got is None or 1 <= got["score"] <= 10


# --- DF-6: the run and its running total ------------------------------------------------

def test_run_stores_the_difficulty_and_prints_the_running_total(context, tmp_path, capsys):
    import apply_harness as h
    import apply_queue
    _serve(context, {"/fabrikam/1/apply": _form("lever_single.html")},
           host="https://jobs.lever.co")
    folder = h.write_job_folder(tmp_path / "job")
    apply_queue.enqueue(apply_queue.new_entry("42", company="Fabrikam", title="Analyst",
                                              apply_url="https://jobs.lever.co/fabrikam/1/apply"))
    apply_queue.set_artifacts("42", {"folder": str(folder),
                                     "apply_md": str(folder / "apply.md"),
                                     "resume_pdf": str(folder / "Jane_Doe_Resume.pdf")})
    code = aa.run([], all_queued=True, judge=jev.FakeJev(),
                  settings={"auto_apply_generate": True}, context=context)
    assert code == 0
    entry = apply_queue.load()["jobs"][0]
    assert entry["difficulty"]["score"] == 2 and entry["difficulty"]["jev_usd"] == 0
    assert set(entry["difficulty"]) == set(apply_queue.DIFFICULTY_KEYS)
    assert entry["status"] == "queued" and entry["attempts"] == 0
    out = capsys.readouterr().out
    assert "Fabrikam / Analyst: 2/10, Queue it." in out
    assert "Jev so far:" in out and "request(s), $0.0000" in out


def _queue_lever(context, tmp_path):
    import apply_harness as h
    import apply_queue
    _serve(context, {"/fabrikam/1/apply": _form("lever_single.html")},
           host="https://jobs.lever.co")
    folder = h.write_job_folder(tmp_path / "job")
    apply_queue.enqueue(apply_queue.new_entry("42", company="Fabrikam", title="Analyst",
                                              apply_url="https://jobs.lever.co/fabrikam/1/apply"))
    apply_queue.set_artifacts("42", {"folder": str(folder),
                                     "apply_md": str(folder / "apply.md"),
                                     "resume_pdf": str(folder / "Jane_Doe_Resume.pdf")})


class _Billed(jev.FakeJev):
    """The fake judge, each request counted as a live one of a million tokens."""

    def judge(self, state, questions):
        jev.count_usage(1_000_000)
        return super().judge(state, questions)


def test_the_jobs_jev_cost_is_stored_and_the_total_printed(context, tmp_path, capsys):
    import apply_queue
    _queue_lever(context, tmp_path)
    jev.reset_usage()
    try:
        assert aa.run(["42"], judge=_Billed(), settings={}, context=context) == 0
        spent = jev.usage()
    finally:
        jev.reset_usage()
    got = apply_queue.load()["jobs"][0]["difficulty"]
    assert spent["requests"] >= 1
    assert got["jev_usd"] == pytest.approx(spent["usd"])
    assert f"Jev so far: {spent['requests']} request(s), ${spent['usd']:.4f}" in \
        capsys.readouterr().out


class _Down:
    def judge(self, state, questions):
        raise jev.JudgeOutage("ConnectError")


def test_the_check_stops_when_jev_is_down(context, tmp_path, capsys):
    import apply_queue
    _queue_lever(context, tmp_path)
    assert aa.run(["42"], judge=_Down(), settings={}, context=context) == 1
    assert apply_queue.load()["jobs"][0]["difficulty"] == {}
    assert "Jev is down (ConnectError)" in capsys.readouterr().out


def test_an_unknown_job_id_is_named(context, tmp_path, capsys):
    _queue_lever(context, tmp_path)
    assert aa.run(["7"], judge=jev.FakeJev(), settings={}, context=context) == 2
    assert "job 7 is not in the queue" in capsys.readouterr().out


def test_the_real_judge_reads_a_lever_form(context, tmp_path, jev_judge):
    _serve(context, {"/fabrikam/1/apply": _form("lever_single.html")},
           host="https://jobs.lever.co")
    got, why, rec, _ = _check(context, tmp_path, "https://jobs.lever.co/fabrikam/1/apply",
                              judge=jev_judge())
    assert why == "" and got["system"] == "lever"
    _only_entries(rec, [])


# === SP6 fix round 1 =======================================================================

UNKNOWN_JUDGE = ("Unknown Auto-apply judge 'typesaf'; tick \"Show advanced settings\" and pick "
                 "typesafe in Settings > Auto-apply.")


def test_the_gate_names_an_unknown_judge_before_the_jev_switch(sdk):
    # `mode_gate` asks `jev_switch.mode_refusal`, as the drain does
    for switch in (True, False):
        cfg = dict(ON, jev_enabled=switch, auto_apply_jev_mode=" TypeSaf ")
        for env in ({}, KEY):
            assert aa.refusal(config=cfg, env=env) == UNKNOWN_JUDGE, (switch, env)
    assert aa.mode_gate("typesaf") == jev_switch.mode_refusal("typesaf") == UNKNOWN_JUDGE
    assert aa.mode_gate("fake") == jev_switch.FIXTURE_ONLY
    assert aa.mode_gate("typesafe") == ""


def test_main_refuses_an_unknown_judge_first(cli, capsys):
    _write_switch(dict(ON, jev_enabled=False, auto_apply_jev_mode=" TypeSaf "))
    assert aa.main(["--all"]) == 2
    assert capsys.readouterr().err.strip() == UNKNOWN_JUDGE


# --- one browser on the profile ----------------------------------------------------------------

class _FakeCtx:
    def __init__(self):
        self.handlers = {}
        self.pages = []

    def on(self, name, fn):
        self.handlers.setdefault(name, []).append(fn)

    def close(self):
        for fn in self.handlers.get("close", []):
            fn(self)


class _FakeChromium:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def launch_persistent_context(self, user_data_dir, **kw):
        self.calls.append((user_data_dir, kw))
        if kw.get("channel") in self.fail:
            raise RuntimeError("Chromium distribution 'chrome' is not found")
        return _FakeCtx()


def _fake_playwright(monkeypatch, chromium):
    import types

    import playwright.sync_api as sync_api
    pw = types.SimpleNamespace(chromium=chromium)

    class _Starter:
        def __enter__(self):
            return pw

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _Starter())


@pytest.fixture
def browser_cli(cli, monkeypatch):
    pytest.importorskip("playwright")
    monkeypatch.setattr(jev, "get", lambda mode="": jev.FakeJev())
    _write_switch(dict(ON, auto_apply_jev_mode="typesafe"))
    return monkeypatch


@pytest.mark.parametrize("holder", ["chrome", "sentinel"])
def test_main_refuses_while_a_run_holds_the_profile_by_either_sign(browser_cli, capsys, holder):
    from test_profile_lock import hold_chrome_lock, hold_sentinel
    chromium = _FakeChromium()
    _fake_playwright(browser_cli, chromium)
    profile = aa.default_profile_dir()
    release = hold_chrome_lock(profile) if holder == "chrome" else hold_sentinel(profile).release
    try:
        assert aa.main(["42"]) == 2
    finally:
        release()
    assert capsys.readouterr().err.strip() == aa.PROFILE_BUSY
    assert chromium.calls == []


def test_main_holds_the_sentinel_while_it_checks(browser_cli):
    import profile_lock
    chromium = _FakeChromium()
    _fake_playwright(browser_cli, chromium)
    seen = []
    browser_cli.setattr(aa, "run", lambda job_ids, **kw: seen.append(
        profile_lock.sentinel_held(aa.default_profile_dir())) or 0)
    assert aa.main(["42"]) == 0
    assert seen == [True]                       # a drain started now sees the check
    assert profile_lock.sentinel_held(aa.default_profile_dir()) is False


def test_main_opens_the_bundled_browser_when_chrome_does_not_start(browser_cli):
    """Fix round 2: the sentinel covers the bundled browser too, so the check
    falls back to it as the drain does, holding the sentinel while it runs."""
    import profile_lock
    chromium = _FakeChromium(fail={"chrome"})
    _fake_playwright(browser_cli, chromium)
    seen = []
    browser_cli.setattr(aa, "run", lambda job_ids, **kw: seen.append(
        profile_lock.sentinel_held(aa.default_profile_dir())) or 0)
    assert aa.main(["42"]) == 0
    assert [kw.get("channel") for _, kw in chromium.calls] == ["chrome", None]
    assert seen == [True]
    assert profile_lock.sentinel_held(aa.default_profile_dir()) is False


def test_main_stops_in_a_sentence_when_no_browser_starts(browser_cli, capsys):
    import profile_lock
    chromium = _FakeChromium(fail={"chrome", None})
    _fake_playwright(browser_cli, chromium)
    browser_cli.setattr(aa, "run", lambda *a, **kw: pytest.fail("the check ran"))
    assert aa.main(["42"]) == 1
    assert capsys.readouterr().err.strip() == aa.NO_BROWSER.format(why="RuntimeError")
    assert profile_lock.sentinel_held(aa.default_profile_dir()) is False


def test_main_refuses_when_a_browser_takes_the_profile_as_it_starts(browser_cli, capsys):
    import profile_lock
    from test_profile_lock import hold_sentinel
    browser_cli.setattr(profile_lock, "HOLD_WAIT_S", 0)
    browser_cli.setattr(aa, "profile_busy", lambda profile=None: False)   # free when asked
    chromium = _FakeChromium()
    _fake_playwright(browser_cli, chromium)
    guard = hold_sentinel(aa.default_profile_dir())
    try:
        assert aa.main(["42"]) == 2
    finally:
        guard.release()
    assert chromium.calls == []
    assert capsys.readouterr().err.strip() == aa.PROFILE_BUSY


def test_the_checks_busy_sentence_names_every_holder():
    import profile_lock
    assert aa.PROFILE_BUSY == profile_lock.BUSY_LEAD + " Check difficulty once that window closes."


# --- minors 2 to 4 -----------------------------------------------------------------------------

def test_the_age_shows_past_seven_whole_days():
    now = datetime(2026, 9, 27, 12, 0)
    assert aa.age_text("2026-09-19T16:00:00", now) == "7 days old"      # 7 days 20 hours
    assert aa.age_text("2026-09-20T13:00:00", now) == ""                # 6 days 23 hours
    assert aa.age_text("2026-09-20T12:00:00", now) == ""                # 7 days to the minute


def test_a_required_upload_with_no_file_is_listed_with_the_questions():
    from apply_form import Field, FormDigest
    from apply_judge import FillPlan, PlannedField
    digest = FormDigest("jobs.lever.co", "Apply", "", fields=[
        Field(0, (0, "#cv"), "Resume/CV", "file", True, help="PDF only"),
        Field(1, (0, "#why"), "Why this team?", "text", True)])
    plan = FillPlan(fields=[
        PlannedField(0, (0, "#cv"), "Resume/CV", True, None, "", None, 0.0, "skip"),
        PlannedField(1, (0, "#why"), "Why this team?", True, None, "", None, 0.0, "skip")])
    got = aa.tally(digest, plan, generate=False)
    assert got.unanswered == 2 == len(got.questions)
    assert got.questions[0] == {"label": "Resume/CV", "help": "PDF only", "options": [],
                                "required": True, "type": "file"}
    assert got.notes == []


# --- minor 7: a check that reads nothing leaves a trace ------------------------------------

def test_a_failed_check_is_recorded_and_the_earlier_result_kept(context, tmp_path, capsys):
    import apply_queue
    _queue_lever(context, tmp_path)
    assert aa.run(["42"], judge=jev.FakeJev(), settings={}, context=context) == 0
    earlier = dict(apply_queue.load()["jobs"][0]["difficulty"])
    context.unroute("https://jobs.lever.co/**")
    context.route("https://jobs.lever.co/**", lambda route: route.abort())
    assert aa.run(["42"], judge=jev.FakeJev(), settings={}, context=context) == 0
    got = apply_queue.load()["jobs"][0]["difficulty"]
    assert {k: got[k] for k in earlier} == earlier
    assert got["last_failed_why"].startswith("the posting did not load")
    assert datetime.fromisoformat(got["last_failed_at"]) >= \
        datetime.fromisoformat(earlier["checked_at"])
    assert "not checked (the posting did not load" in capsys.readouterr().out


def test_a_recheck_with_no_saved_page_is_recorded(walk_env):
    import apply_queue
    apply_queue.enqueue(apply_queue.new_entry("42", company="Fabrikam", title="Analyst"))
    assert aa.run(["42"], recheck=True, judge=_NoJudge(), settings={}) == 0
    got = apply_queue.load()["jobs"][0]["difficulty"]
    assert got["last_failed_why"] == aa.NO_SAVED_PAGE and "score" not in got


def test_the_failure_line_names_its_age():
    now = datetime(2026, 9, 27, 12, 0)
    assert aa.failed_text({"last_failed_at": "2026-09-27T09:00:00", "last_failed_why": "x",
                           "score": 3}, now) == \
        "Last check failed today: x. Showing the earlier result."
    assert aa.failed_text({"last_failed_at": "2026-09-26T09:00:00", "last_failed_why": "x"},
                          now) == "Last check failed yesterday: x."
    assert aa.failed_text({"last_failed_at": "2026-09-20T09:00:00", "last_failed_why": "x"},
                          now) == "Last check failed 7 days ago: x."
    assert aa.failed_text({"score": 3}, now) == ""


# --- minors 5 and 6: the walker decides as the drain does ----------------------------------

class _ReadAs(jev.FakeJev):
    """The fake, reading every page as STATE at CONF."""
    STATE, CONF = "", 0.0

    def judge(self, state, questions):
        import apply_harness as h
        out = super().judge(state, questions)
        if "page_state" in out:
            h.read_as(out, self.STATE, self.CONF, {self.STATE: self.CONF})
        return out


def _read_as(state, conf):
    return type("J", (_ReadAs,), {"STATE": state, "CONF": conf})()


def _loop_category(said: str):
    import re
    m = re.search(r"click the Apply entry \[(\d+)\]", said)
    if m:
        return "entry", int(m.group(1))
    if "fill the page" in said:
        return "form", None
    if "the account step" in said or "the code step" in said:
        return "account", None
    if "wait for the person" in said:
        return "check", None
    assert "park:" in said, said
    return "end", None


def _walker_category(step):
    if step.kind == "entry":
        return "entry", step.button.n
    if step.kind in ("form", "account", "check"):
        return step.kind, None
    assert step.kind in ("stop", "unread"), step
    return "end", None


@pytest.mark.parametrize("where, read", [
    ("job_posting.html", None), ("ashby_steps.html", None), ("lever_single.html", None),
    ("login_wall.html", None), ("captcha.html", None), ("confirmation.html", None),
    ("review_with_next.html", None), ("code_gate.html", None), ("signup.html", None),
    ("job_posting.html", ("other", 0.30)), ("job_posting.html", ("other", 0.55)),
    ("job_posting.html", ("other", 0.9)), ("lever_single.html", ("login_wall", 0.90)),
    ("captcha.html", ("other", 0.55)), ("signup.html", ("login_wall", 0.55)),
    ("code_gate.html", ("login_wall", 0.35)),
    # a low-confidence confirmation read: the drain leaves it to the unsure rule
    ("lever_single.html", ("confirmation", 0.40)), ("confirmation.html", ("confirmation", 0.40)),
    ("job_posting.html", ("confirmation", 0.40)),
])
def test_the_walker_decides_each_page_as_the_drains_loop_does(context, flow_server, tmp_path,
                                                               where, read):
    """The walker's decision and `apply_run.loop_step` (what `probe` says the
    drain's loop does) over the same pages and reads, so a change to the
    loop's order shows here."""
    import io
    import re

    import apply_fill
    import apply_run
    judge = _read_as(*read) if read else jev.FakeJev()
    url = flow_server.url(where)
    page = context.new_page()
    page.goto(url)
    apply_fill.settle(page, 3)
    out = io.StringIO()
    apply_run._probe_page(page, 1, judge, out, park_mode=False)
    said = re.search(r"  the loop would: (.*)", out.getvalue()).group(1)
    page.close()
    walker = aa._Walker(context, {"job_posting_id": "42", "company": "", "title": ""}, judge,
                        __import__("logging").getLogger("test"))
    walker.page = context.new_page()
    walker.page.goto(url)
    apply_fill.settle(walker.page, 3)
    walker.hosts.add(apply_run._host(url))
    step = walker._decision(walker._digest())
    assert _walker_category(step) == _loop_category(said), (said, step)


class _GetStarted(jev.FakeJev):
    """The fake, reading the posting as a posting and its "Get started"
    button as the Apply entry, as the live judge reads such a posting."""

    def judge(self, state, questions):
        import apply_harness as h
        out = super().judge(state, questions)
        title = str((state.get("page") or {}).get("title") or "")
        if "page_state" in out and title.startswith("Data Analyst at"):
            h.read_as(out, "job_posting", 0.95, {"job_posting": 0.95})
        for b in state.get("buttons") or []:
            qid = f"button_{b['n']}_role"
            if qid in out and b.get("text") == "Get started":
                out[qid] = jev.Answer(kind="choice", choice="apply_entry", confidence=0.95,
                                      probabilities={"apply_entry": 0.95})
        return out


_GET_STARTED = ("<html><head><title>Data Analyst at Fabrikam</title></head><body><main>"
                "<h1>Data Analyst</h1><p>Fabrikam is hiring a data analyst to build the "
                "reports our teams read each week. You will own the pipeline and the "
                "dashboards.</p><h2>Requirements</h2><ul><li>SQL</li><li>Python</li></ul>"
                "<a class=\"btn\" href=\"/apply/form\">Get started</a></main></body></html>")


def test_the_walker_follows_the_judges_apply_entry(context, tmp_path):
    """Minor 6: the drain follows the judge's `apply_entry` on a posting
    (`posting_entry_choice`), so the check does too. "Get started" has no
    entry word (`apply_judge.entry_worded`), so the text match alone would
    find no Apply and score the posting a dead end at 10."""
    import apply_judge
    assert not apply_judge.entry_worded("Get started")
    _serve(context, {"/jobs/7": _GET_STARTED, "/apply/form": _form("lever_single.html")})
    got, why, rec, _ = _check(context, tmp_path, f"{CAREERS}/jobs/7", judge=_GetStarted())
    assert why == ""
    assert got["score"] < aa.STOP_SCORE, (got["reasons"], rec.actions)
    _only_entries(rec, ["Get started"])



class _AccountEntry(jev.FakeJev):
    """A noisy read of a posting: the posting read as a posting and the
    account link in its body (`account`) read as the Apply entry at 0.99,
    every other button read as the fake reads it, with any other Apply entry
    read as `other`."""

    def __init__(self, account: str):
        super().__init__()
        self.account = account

    def judge(self, state, questions):
        import apply_harness as h
        out = super().judge(state, questions)
        title = str((state.get("page") or {}).get("title") or "")
        if "page_state" in out and title.startswith("Data Analyst at"):
            h.read_as(out, "job_posting", 0.95, {"job_posting": 0.95})
        for b in state.get("buttons") or []:
            qid = f"button_{b['n']}_role"
            if qid not in out:
                continue
            if b.get("text") == self.account:
                out[qid] = jev.Answer(kind="choice", choice="apply_entry", confidence=0.99,
                                      probabilities={"apply_entry": 0.99})
            elif out[qid].choice == "apply_entry":
                out[qid] = jev.Answer(kind="choice", choice="other", confidence=0.95,
                                      probabilities={"other": 0.95})
        return out


def _account_posting(account: str, *, apply: bool = True) -> str:
    entry = "<a class=\"btn\" href=\"/apply/form\">Apply now</a>" if apply else ""
    return ("<html><head><title>Data Analyst at Fabrikam</title></head><body><main>"
            "<h1>Data Analyst</h1><p>Fabrikam is hiring a data analyst to build the "
            "reports our teams read each week. Talent network members hear first: "
            f"<a class=\"btn\" href=\"/signup\">{account}</a></p><h2>Requirements</h2>"
            f"<ul><li>SQL</li><li>Python</li></ul>{entry}</main></body></html>")


_ACCOUNT_LINKS = ("Create an account", "Sign in", "Sign up", "Log in", "Next")


@pytest.mark.parametrize("account", _ACCOUNT_LINKS)
def test_a_judged_entry_that_reads_as_an_account_link_is_never_clicked(context, tmp_path,
                                                                       account):
    """DF-2: a confident judged `apply_entry` on a sign-in, sign-up, log-in
    or Next link in the posting body is refused, and the walker takes the
    text choice, the posting's own "Apply now"."""
    _serve(context, {"/jobs/7": _account_posting(account),
                     "/apply/form": _form("lever_single.html")})
    got, why, rec, _ = _check(context, tmp_path, f"{CAREERS}/jobs/7",
                              judge=_AccountEntry(account))
    assert why == ""
    assert got["score"] < aa.STOP_SCORE, (got["reasons"], rec.actions)
    _only_entries(rec, ["Apply now"])


@pytest.mark.parametrize("account", ("Create an account", "Sign in", "Log in"))
def test_an_account_link_alone_on_a_posting_is_never_clicked(context, tmp_path, account):
    """DF-2: with no Apply beside it, the refused account link leaves the
    walk with no click at all."""
    _serve(context, {"/jobs/7": _account_posting(account, apply=False)})
    _got, _why, rec, _ = _check(context, tmp_path, f"{CAREERS}/jobs/7",
                                judge=_AccountEntry(account))
    _only_entries(rec, [])


def test_a_sign_in_to_apply_link_is_an_account_step_not_a_click(context, tmp_path):
    """DF-2: "Sign in to apply" is Apply-worded, so the text choice finds it;
    it signs in all the same, so the walk notes the account step and does
    not click it."""
    _serve(context, {"/jobs/7": _account_posting("Sign in to apply", apply=False)})
    got, why, rec, _ = _check(context, tmp_path, f"{CAREERS}/jobs/7",
                              judge=_AccountEntry("Sign in to apply"))
    _only_entries(rec, [])
    assert why == ""
    assert aa.ACCOUNT_NOTE in got["reasons"], got["reasons"]


@pytest.mark.parametrize("seed", (1, 2, 3, 4))
def test_a_noisy_judge_never_clicks_an_account_link_in_the_posting(context, tmp_path, seed):
    _serve(context, {"/jobs/7": _account_posting("Create an account"),
                     "/apply/form": _form("lever_single.html")})
    got, _why, rec, _ = _check(context, tmp_path, f"{CAREERS}/jobs/7",
                               judge=jev.NoisyJev(_AccountEntry("Create an account"),
                                                  seed=seed))
    assert {a.kind for a in rec.actions} <= _READ_ONLY, rec.actions
    assert [a.text.strip() for a in rec.actions] in ([], ["Apply now"]), rec.actions
    assert got is None or 1 <= got["score"] <= 10
