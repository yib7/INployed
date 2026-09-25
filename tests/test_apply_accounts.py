"""SP7: accounts and email.

- The ledger's account for a sign-in host that names no tenant, found by
  the job's own hosts (ACC-13).

Headless Chromium through the module-scoped test browser for the flows; no
network, no judge but `FakeJev`, `NoisyJev` or a scripted one; the master
password is the harness's synthetic one."""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_run  # noqa: E402
import ats_accounts  # noqa: E402
import jev  # noqa: E402

pytest_plugins = ["conftest_browser"]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_ACCOUNTS_PATH", str(tmp_path / "accounts.json"))
    return tmp_path / "accounts.json"


def _job_run(tmp_path, *hosts: str) -> "apply_run._JobRun":
    """A `_JobRun` with no page, its admitted ATS hosts `hosts`."""
    runner = apply_run.Runner(jev=jev.FakeJev(), profile_dir=tmp_path / "profile",
                              settings={}, context=object(),
                              run_context={"signup_email": "jane.doe@example.com",
                                           "inbox_url": "https://mail.example.com/inbox"})
    run = apply_run._JobRun(runner, None, {"job_posting_id": "42"})
    run.ats_hosts.update(hosts)
    return run


# === the ledger by tenant (ACC-13) =======================================================================

def test_a_shared_sign_in_host_finds_the_account_made_on_the_jobs_careers_host(ledger, tmp_path):
    ats_accounts.record("careers-gtsx.icims.com", "jane.doe@example.com")
    run = _job_run(tmp_path, "careers-gtsx.icims.com")
    assert run._account_for("login.icims.com")["email"] == "jane.doe@example.com"
    # another tenant's job never takes this tenant's account
    other = _job_run(tmp_path, "careers-other.icims.com")
    assert other._account_for("login.icims.com") is None


def test_an_account_made_on_a_shared_sign_in_host_is_kept_under_the_jobs_tenant(ledger, tmp_path):
    run = _job_run(tmp_path, "careers-gtsx.icims.com")
    run._record_account("login.icims.com", "jane.doe@example.com")
    assert list(ats_accounts.list_accounts()) == ["careers-gtsx.icims.com"]
    # a host that names its tenant keeps the account under its own name
    run._record_account("cboe.wd1.myworkdayjobs.com", "jane.doe@example.com")
    assert "cboe.wd1.myworkdayjobs.com" in ats_accounts.list_accounts()
