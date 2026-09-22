"""Regression checks for runner boundaries using synthetic pages and judges."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import apply_form
import apply_run
import jev_harness

pytest_plugins = ["conftest_jev"]


@pytest.fixture(autouse=True)
def _judge_selector(jev_judge):
    """Every runner here takes the harness judge (fake unless AUTO_APPLY_TEST_JEV says otherwise)."""


def _job():
    context = Mock()
    runner = apply_run.Runner(jev=jev_harness.judge(), context=context, run_context={},
                              sleep=lambda seconds: None)
    entry = {"job_posting_id": "synthetic", "apply_url": "https://www.linkedin.com/jobs/1",
             "ats": {"domain": "www.linkedin.com", "system": "linkedin"}}
    job = apply_run._JobRun(runner, context, entry)
    job._build_allowlist()
    job.page = Mock(url=entry["apply_url"])
    return job


@pytest.mark.parametrize("mode", ["fake", "replay"])
def test_cli_fixture_judges_refuse_before_queue_or_browser(monkeypatch, mode, capsys):
    monkeypatch.setattr(apply_run, "_load_env", lambda: None)
    monkeypatch.setattr(apply_run, "_settings_from_args", lambda args: {
        **apply_run.DEFAULT_SETTINGS, "auto_apply_jev_mode": mode})
    constructor = Mock(side_effect=AssertionError("must not construct a production runner"))
    monkeypatch.setattr(apply_run, "Runner", constructor)
    assert apply_run.main(["drain", "--jev", mode]) == 2
    constructor.assert_not_called()
    assert "fixture" in capsys.readouterr().err.lower()


def test_native_continue_is_an_advance_while_final_submit_stays_guarded():
    digest = apply_form.FormDigest("jobs.example", "Apply", "", buttons=[
        apply_form.Button(0, (0, "#next"), "Continue", "submit"),
        apply_form.Button(1, (0, "#submit"), "Submit application", "submit"),
    ])
    assert not apply_run._submit_shaped(digest, 0)
    assert apply_run._submit_shaped(digest, 1)


def test_generic_ats_cannot_extend_allowlist_twice(monkeypatch):
    job = _job()
    monkeypatch.setattr(apply_run.apply_fill, "settle", lambda *a: None)
    monkeypatch.setattr(apply_run.apply_queue, "update", lambda *a, **kw: None)
    job._follow_popup(Mock(url="https://careers.example/apply"))
    assert "careers.example" in job.allowed
    with pytest.raises(apply_run._Parked, match="allowed"):
        job._follow_popup(Mock(url="https://unrelated.example/collect"))
    assert "unrelated.example" not in job.allowed


class _RedirectingTab:
    """A popup on LinkedIn's `/safety/go/` hop whose script moves on to
    `dest` only once waited on, as the real one does a few seconds in."""

    def __init__(self, dest):
        self.url = "https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fcareers.example"
        self.dest = dest
        self.waited = []

    def wait_for_url(self, predicate, timeout):
        self.waited.append(timeout)
        if self.dest is None:
            raise TimeoutError("still on the hop")
        self.url = self.dest
        assert predicate(self.url)


def test_popup_on_the_linkedin_redirector_admits_the_destination(monkeypatch):
    job = _job()
    monkeypatch.setattr(apply_run.apply_fill, "settle", lambda *a: None)
    monkeypatch.setattr(apply_run.apply_queue, "update", lambda *a, **kw: None)
    tab = _RedirectingTab("https://careers-gtsx.icims.com/jobs/1605/job")
    job._follow_popup(tab, source_url=job.page.url)
    assert tab.waited, "the hop was not waited on"
    assert job.page is tab and "careers-gtsx.icims.com" in job.allowed
    job._check_host(job.page.url)       # the loop's next check passes


def test_a_tab_that_stays_on_the_redirector_admits_nothing(monkeypatch):
    job = _job()
    monkeypatch.setattr(apply_run.apply_fill, "settle", lambda *a: None)
    monkeypatch.setattr(apply_run.apply_queue, "update", lambda *a, **kw: None)
    before = set(job.allowed)
    job._follow_popup(_RedirectingTab(None), source_url=job.page.url)
    assert job.allowed == before and not job.ats_transition_used


def test_same_tab_linkedin_transition_admits_one_ats_host(monkeypatch):
    job = _job()
    source = job.page.url
    button = apply_form.Button(0, (0, "#apply"), "Apply", "")
    digest = apply_form.FormDigest("www.linkedin.com", "Job", "", buttons=[button])
    plan = apply_run.FillPlan(buttons={"apply_entry": (0, 0.99)})
    rec = {"clicked": []}
    job.pages.append(rec)

    class _Locator:
        first = None

        def __init__(self):
            self.first = self

        def click(self, **kwargs):
            job.page.url = "https://careers.example/apply"

    monkeypatch.setattr(apply_run.apply_form, "resolve", lambda *a: _Locator())
    monkeypatch.setattr(apply_run.apply_fill, "settle", lambda *a: None)
    monkeypatch.setattr(apply_run.apply_queue, "update", lambda *a, **kw: None)

    class _NoPopup:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            raise TimeoutError("same tab")

    job.page.expect_popup = Mock(return_value=_NoPopup())

    job._job_posting(digest, {}, plan, rec)

    assert source.startswith("https://www.linkedin.com/")
    assert "careers.example" in job.allowed
    with pytest.raises(apply_run._Parked, match="allowed"):
        job._follow_popup(Mock(url="https://unrelated.example/collect"))


def test_post_submit_redirect_is_checked_before_reading_or_filling(monkeypatch):
    job = _job()
    job.page = Mock(url="https://unrelated.example/code")
    job.submit_clicked = True
    extract = Mock(side_effect=AssertionError("untrusted destination must not be extracted"))
    monkeypatch.setattr(apply_run.apply_form, "extract", extract)
    with pytest.raises(apply_run._Parked, match="allowed"):
        job._after_submit()
    extract.assert_not_called()


def test_post_submit_actionable_frame_violation_keeps_no_retry_outcome(monkeypatch):
    job = _job()
    main = SimpleNamespace(url=job.page.url, parent_frame=None)
    job.page.frames = [main, SimpleNamespace(url="https://unrelated.example/code",
                                             parent_frame=main)]
    job.submit_clicked = True
    digest = apply_form.FormDigest("www.linkedin.com", "Verify", "", fields=[
        apply_form.Field(0, (1, "#code"), "Security code", "text", True)])
    monkeypatch.setattr(apply_run.apply_form, "extract", lambda page: digest)
    monkeypatch.setattr(job, "_judge_page",
                        Mock(side_effect=AssertionError("untrusted frame was judged")))

    with pytest.raises(apply_run._Parked, match="allowed") as parked:
        job._after_submit()

    assert parked.value.status == "submitted"


def test_actionable_cross_origin_frame_is_rejected():
    job = _job()
    job.page.frames = [SimpleNamespace(url=job.page.url),
                       SimpleNamespace(url="https://unrelated.example/form")]
    digest = apply_form.FormDigest("www.linkedin.com", "Apply", "", fields=[
        apply_form.Field(0, (1, "#email"), "Email", "email", True)])
    with pytest.raises(apply_run._Parked, match="allowed"):
        job._check_frames(digest)


@pytest.mark.parametrize("url", ["about:blank", "about:srcdoc"])
def test_blank_frame_inherits_allowed_parent(url):
    job = _job()
    main = SimpleNamespace(url=job.page.url, parent_frame=None)
    job.page.frames = [main, SimpleNamespace(url=url, parent_frame=main)]
    digest = apply_form.FormDigest("www.linkedin.com", "Apply", "", fields=[
        apply_form.Field(0, (1, "#email"), "Email", "email", True)])
    job._check_frames(digest)


@pytest.mark.parametrize("role,confidence", [
    ("advance", apply_run.apply_judge.BUTTON_ADVANCE_MIN_CONF - 0.01),
    ("submit", apply_run.apply_judge.BUTTON_SUBMIT_MIN_CONF - 0.01),
])
def test_code_gate_refuses_low_confidence_continue(monkeypatch, role, confidence):
    job = _job()
    job.inbox = SimpleNamespace(fetch_code=lambda *a: "SYNTHETIC-CODE")
    code = apply_form.Field(0, (0, "#code"), "Security code", "text", True,
                            id_or_name="code")
    digest = apply_form.FormDigest("www.linkedin.com", "Verify", "", fields=[code])
    plan = apply_run.FillPlan(buttons={role: (0, confidence)})
    locator = SimpleNamespace(first=SimpleNamespace(fill=lambda *a, **kw: None))
    monkeypatch.setattr(apply_run.apply_form, "resolve", lambda *a: locator)
    monkeypatch.setattr(job, "_click",
                        Mock(side_effect=AssertionError("low-confidence button was clicked")))

    with pytest.raises(apply_run._Parked, match="confidence"):
        job._code_gate(digest, plan, {"filled": []})


def test_code_gate_submit_uses_submit_no_retry_path(monkeypatch):
    job = _job()
    job.inbox = SimpleNamespace(fetch_code=lambda *a: "SYNTHETIC-CODE")
    code = apply_form.Field(0, (0, "#code"), "Security code", "text", True,
                            id_or_name="code")
    digest = apply_form.FormDigest("www.linkedin.com", "Verify", "", fields=[code])
    plan = apply_run.FillPlan(buttons={
        "submit": (0, apply_run.apply_judge.BUTTON_SUBMIT_MIN_CONF)})
    locator = SimpleNamespace(first=SimpleNamespace(fill=lambda *a, **kw: None))
    monkeypatch.setattr(apply_run.apply_form, "resolve", lambda *a: locator)
    clicked = Mock()
    monkeypatch.setattr(job, "_click", clicked)

    job._code_gate(digest, plan, {"filled": []})

    clicked.assert_called_once_with(digest, 0, "submit", {"filled": [{
        "n": 0, "label": "Security code", "value": apply_run.HIDDEN,
        "type": "text", "id_or_name": "code", "upload": False, "hidden": True}]})
