"""Tests for the ATS-account ledger + master-password clipboard transit (SP2).

local/ats_accounts.py keeps a JSON ledger of which ATS domains the candidate has
accounts on (NEVER any password) and moves the single master password from the
Windows Credential Manager (service "inployed-ats") to the clipboard so a human
or agent can paste it — the password itself is never printed, returned by a
public function, or written to disk. Hermetic: the ledger goes to tmp_path, the
keyring is a fake object, and the ctypes clipboard layer is monkeypatched.
"""
import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import ats_accounts  # noqa: E402

pytest_plugins = ["conftest_browser"]

SECRET = "sekrit-hunter2-XYZZY"


def test_direct_password_fill_is_private(browser_page, kr, caplog):
    kr.set_password(ats_accounts.SERVICE, "master", SECRET)
    browser_page.set_content('<input id="password" type="password">')
    with caplog.at_level("INFO"):
        assert ats_accounts.has_password()
        assert ats_accounts.fill_password(browser_page, "#password") is True
    assert browser_page.locator("#password").input_value() == SECRET
    assert SECRET not in caplog.text
    assert "chars hidden" in caplog.text


def test_direct_password_truncated_by_the_field_reports_failure(browser_page, kr, caplog):
    # a field that silently keeps only part of the value is not a filled password
    kr.set_password(ats_accounts.SERVICE, "master", SECRET)
    browser_page.set_content('<input id="password" type="password" maxlength="4">')
    with caplog.at_level("INFO"):
        assert ats_accounts.fill_password(browser_page, "#password") is False
    assert SECRET not in caplog.text
    assert "password filled" not in caplog.text


def test_direct_password_failure_hides_playwright_error(browser_page, kr, caplog, monkeypatch):
    kr.set_password(ats_accounts.SERVICE, "master", SECRET)

    def fail(*args, **kwargs):
        raise RuntimeError("fill call included " + SECRET)

    monkeypatch.setattr(browser_page, "locator", fail)
    assert ats_accounts.fill_password(browser_page, "#password") is False
    assert SECRET not in caplog.text


def test_direct_password_missing_does_not_touch_field(browser_page, kr):
    browser_page.set_content('<input id="password" type="password" value="existing">')
    assert ats_accounts.has_password() is False
    assert ats_accounts.fill_password(browser_page, "#password") is False
    assert browser_page.locator("#password").input_value() == "existing"


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, user, pw):
        self.store[(service, user)] = pw

    def get_password(self, service, user):
        return self.store.get((service, user))


class FakeClipboard:
    def __init__(self):
        self.text = "unrelated user text"

    def set(self, text):
        self.text = text

    def get(self):
        return self.text

    def clear(self):
        self.text = ""


@pytest.fixture
def kr(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(ats_accounts, "_keyring", lambda: fake)
    return fake


@pytest.fixture
def clip(monkeypatch):
    fake = FakeClipboard()
    monkeypatch.setattr(ats_accounts, "_clip_set", fake.set)
    monkeypatch.setattr(ats_accounts, "_clip_get", fake.get)
    monkeypatch.setattr(ats_accounts, "_clip_clear", fake.clear)
    return fake


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "ats_accounts.json"
    monkeypatch.setenv("ATS_ACCOUNTS_PATH", str(path))
    return path


# --- ledger ---------------------------------------------------------------------

def test_record_and_lookup_by_lowercased_netloc(ledger):
    rec = ats_accounts.record("https://Jobs.Example.COM/careers/apply?x=1",
                              email="me@example.com")
    assert rec["email"] == "me@example.com"
    assert rec["created_at"]
    stored = json.loads(ledger.read_text(encoding="utf-8"))
    assert list(stored) == ["jobs.example.com"]
    # lookup accepts a full URL or a bare domain, any case
    assert ats_accounts.lookup("jobs.example.com")["email"] == "me@example.com"
    assert ats_accounts.lookup("https://JOBS.example.com/other")["email"] == "me@example.com"
    assert ats_accounts.lookup("unknown.example.com") is None


def test_record_upsert_keeps_created_at(ledger):
    first = ats_accounts.record("acme.icims.com", email="a@x.com")
    second = ats_accounts.record("ACME.icims.com", email="b@x.com", method="google_sso")
    assert second["created_at"] == first["created_at"]
    assert second["email"] == "b@x.com"
    assert len(json.loads(ledger.read_text(encoding="utf-8"))) == 1


def test_record_rejects_password_like_keys(ledger):
    for bad in ("password", "Password", "pwd", "api_token", "client_secret"):
        with pytest.raises(ValueError):
            ats_accounts.record("x.example.com", email="a@x.com", **{bad: "v"})


def _all_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_keys(v)


def test_serialized_ledger_never_has_password_like_key(ledger):
    ats_accounts.record("a.example.com", email="a@x.com", note="uses master")
    ats_accounts.record("b.example.com", email="b@x.com")
    stored = json.loads(ledger.read_text(encoding="utf-8"))
    for key in _all_keys(stored):
        assert not ats_accounts._FORBIDDEN_KEY_RE.search(key), key


@pytest.mark.parametrize("host,key", [
    ("cboe.wd1.myworkdayjobs.com", "myworkdayjobs.com/cboe"),
    ("https://SPGI.wd5.myworkdayjobs.com/en-US/SPGI_Careers", "myworkdayjobs.com/spgi"),
    ("careers-gtsx.icims.com", "icims.com/gtsx"),
    ("uscareers-gdit.icims.com", "icims.com/gdit"),
    ("gdit.icims.com", "icims.com/gdit"),
    ("login.icims.com", ""),            # a sign-in host every tenant shares names none
    ("www.icims.com", ""),
    ("careers.fabrikam.example", ""),
    ("", ""),
])
def test_tenant_key_names_a_multi_tenant_ats_hosts_tenant(host, key):
    assert ats_accounts.tenant_key(host) == key


def test_lookup_finds_an_account_by_its_tenant_on_another_host_of_the_site(ledger):
    # ACC-13: the account made on the careers host of an iCIMS tenant is found
    # from that tenant's other hosts, never from another tenant's
    ats_accounts.record("careers-gtsx.icims.com", email="me@example.com")
    assert ats_accounts.lookup("gtsx.icims.com")["email"] == "me@example.com"
    assert ats_accounts.lookup("uscareers-gtsx.icims.com")["email"] == "me@example.com"
    assert ats_accounts.lookup("careers-other.icims.com") is None
    assert ats_accounts.lookup("login.icims.com") is None


def test_lookup_takes_the_jobs_own_hosts_for_a_shared_sign_in_host(ledger):
    # ACC-13: a sign-in on login.icims.com (no tenant in its name) for a job
    # whose careers host holds the account; a related host of another tenant
    # never lends its account to this one
    ats_accounts.record("careers-gtsx.icims.com", email="me@example.com")
    assert ats_accounts.lookup("login.icims.com",
                               related=["careers-gtsx.icims.com"])["email"] == "me@example.com"
    assert ats_accounts.lookup("careers-other.icims.com",
                               related=["careers-gtsx.icims.com"]) is None
    assert ats_accounts.lookup("login.icims.com", related=["careers-other.icims.com"]) is None


@pytest.mark.parametrize("rules,unmet", [
    ({}, []),
    ({"min_length": 8, "upper": True, "lower": True, "digit": True, "special": True}, []),
    ({"min_length": 30}, ["at least 30 characters"]),
    ({"max_length": 16}, ["at most 16 characters"]),
    ({"forbidden": "-"}, ["none of these characters: -"]),
    ({"forbidden": "<>&"}, []),
])
def test_unmet_rules_reads_the_stored_password_in_process(kr, rules, unmet):
    # ACC-04: the rules the stored password misses, in words; the value never
    # comes back (SECRET is 20 characters, upper, lower, digits, a hyphen)
    kr.set_password(ats_accounts.SERVICE, "master", SECRET)
    got = ats_accounts.unmet_rules(rules)
    assert got == unmet
    assert SECRET not in repr(got)


def test_unmet_rules_names_each_missing_class(kr):
    kr.set_password(ats_accounts.SERVICE, "master", "alllowercase")
    assert ats_accounts.unmet_rules({"upper": True, "lower": True, "digit": True,
                                     "special": True}) == [
        "an uppercase letter", "a digit", "a special character"]


@pytest.mark.parametrize("need, unmet", [
    (1, []), (2, []),
    (3, ["at least 3 of: an uppercase letter, a lowercase letter, a digit, a special character"]),
])
def test_unmet_rules_counts_the_classes_a_count_rule_asks_for(kr, need, unmet):
    # "3 of the following" (SP7 review I2): lowercase and a hyphen are two
    kr.set_password(ats_accounts.SERVICE, "master", "all-lowercase")
    assert ats_accounts.unmet_rules({"upper": True, "lower": True, "digit": True,
                                     "special": True, "classes_needed": need}) == unmet


def test_unmet_rules_without_a_stored_password_is_none(kr):
    assert ats_accounts.unmet_rules({"min_length": 8}) is None


def test_ledger_env_override_read_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_ACCOUNTS_PATH", str(tmp_path / "l.json"))
    assert ats_accounts.ledger_path() == tmp_path / "l.json"
    monkeypatch.delenv("ATS_ACCOUNTS_PATH")
    assert ats_accounts.ledger_path().name == "ats_accounts.json"


# --- keyring master password -------------------------------------------------------

def test_password_exists_false_then_true(kr, monkeypatch):
    assert ats_accounts.password_exists() is False
    answers = iter([SECRET, SECRET])
    monkeypatch.setattr(ats_accounts, "_getpass", lambda prompt: next(answers))
    assert ats_accounts.set_master_password() is True
    assert ats_accounts.password_exists() is True
    assert kr.store[(ats_accounts.SERVICE, ats_accounts._MASTER_USER)] == SECRET


def test_set_master_password_mismatch_stores_nothing(kr, monkeypatch, capsys):
    answers = iter([SECRET, "different"])
    monkeypatch.setattr(ats_accounts, "_getpass", lambda prompt: next(answers))
    assert ats_accounts.set_master_password() is False
    assert kr.store == {}
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


def test_set_master_password_programmatic_path_never_prompts(kr, monkeypatch, capsys):
    # SP3 wires this to QInputDialog: the dialog's value comes in as an argument
    # and getpass must never fire (it would hang a GUI process).
    def no_prompt(prompt):
        raise AssertionError("getpass must not be called on the programmatic path")

    monkeypatch.setattr(ats_accounts, "_getpass", no_prompt)
    assert ats_accounts.set_master_password(SECRET) is True
    assert kr.store[(ats_accounts.SERVICE, ats_accounts._MASTER_USER)] == SECRET
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err          # never echoed


def test_set_master_password_programmatic_rejects_blank(kr):
    for bad in ("", "   ", "\t\n"):
        with pytest.raises(ValueError):
            ats_accounts.set_master_password(bad)
    assert kr.store == {}                           # nothing stored


def test_password_exists_false_when_keyring_missing(monkeypatch):
    monkeypatch.setattr(ats_accounts, "_keyring", lambda: None)
    assert ats_accounts.password_exists() is False


def test_module_imports_without_keyring(monkeypatch):
    # the lazy accessor is the only touchpoint: simulate an ImportError inside it
    import builtins
    real_import = builtins.__import__

    def no_keyring(name, *a, **kw):
        if name == "keyring":
            raise ImportError("no keyring here")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_keyring)
    assert ats_accounts._keyring() is None
    assert ats_accounts.password_exists() is False


# --- clipboard transit ---------------------------------------------------------------

def test_copy_password_to_clipboard(kr, clip):
    kr.set_password(ats_accounts.SERVICE, ats_accounts._MASTER_USER, SECRET)
    assert ats_accounts.copy_password_to_clipboard() is True
    assert clip.text == SECRET


def test_copy_without_stored_password_is_refused(kr, clip):
    assert ats_accounts.copy_password_to_clipboard() is False
    assert clip.text == "unrelated user text"


def test_clear_clipboard_only_if_password(kr, clip):
    kr.set_password(ats_accounts.SERVICE, ats_accounts._MASTER_USER, SECRET)
    clip.text = "the user's own clipboard content"
    assert ats_accounts.clear_clipboard_if_password() is False
    assert clip.text == "the user's own clipboard content"   # never clobbered
    clip.text = SECRET
    assert ats_accounts.clear_clipboard_if_password() is True
    assert clip.text == ""


# --- CLI: output discipline (the password NEVER appears on stdout/stderr) -------------

def _assert_no_secret(capsys):
    out = capsys.readouterr()
    assert SECRET not in out.out
    assert SECRET not in out.err
    return out


def test_cli_set_password_and_status(kr, monkeypatch, capsys):
    assert ats_accounts.main(["password-status"]) == 0
    assert capsys.readouterr().out.strip() == "not set"
    answers = iter([SECRET, SECRET])
    monkeypatch.setattr(ats_accounts, "_getpass", lambda prompt: next(answers))
    assert ats_accounts.main(["set-password"]) == 0
    _assert_no_secret(capsys)
    assert ats_accounts.main(["password-status"]) == 0
    assert capsys.readouterr().out.strip() == "set"


def test_cli_set_password_mismatch_exits_nonzero(kr, monkeypatch, capsys):
    answers = iter([SECRET, "nope"])
    monkeypatch.setattr(ats_accounts, "_getpass", lambda prompt: next(answers))
    assert ats_accounts.main(["set-password"]) == 1
    _assert_no_secret(capsys)


def test_cli_record_lookup_list_json(ledger, kr, capsys):
    kr.set_password(ats_accounts.SERVICE, ats_accounts._MASTER_USER, SECRET)
    assert ats_accounts.main(["record", "--domain", "a.example.com",
                              "--email", "a@x.com", "--method", "master_password"]) == 0
    _assert_no_secret(capsys)
    assert ats_accounts.main(["lookup", "a.example.com", "--json"]) == 0
    out = _assert_no_secret(capsys)
    assert json.loads(out.out)["email"] == "a@x.com"
    assert ats_accounts.main(["lookup", "missing.example.com", "--json"]) == 2
    capsys.readouterr()
    assert ats_accounts.main(["list", "--json"]) == 0
    out = _assert_no_secret(capsys)
    assert "a.example.com" in out.out


def test_cli_json_output_survives_cp1252_pipe(ledger, monkeypatch):
    # Piped stdout on this machine defaults to cp1252; a ledger note with an
    # emoji/arrow must not crash record/lookup/list (json ensure_ascii=False).
    note = "✅ prêt → go"
    ats_accounts.record("a.example.com", email="a@x.com", note=note)
    buf = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(buf, encoding="cp1252"))
    assert ats_accounts.main(["list", "--json"]) == 0
    sys.stdout.flush()
    data = json.loads(buf.getvalue().decode("utf-8"))
    assert data["a.example.com"]["note"] == note
    assert ats_accounts.main(["lookup", "a.example.com", "--json"]) == 0
    sys.stdout.flush()          # second verb through the same cp1252 pipe: fine


def test_cli_unexpected_error_secret_verb_prints_class_only(kr, monkeypatch, capsys):
    # Verbs that touch the secret must never interpolate str(e): an exception
    # message can carry the password (e.g. a keyring backend error). set-password
    # is the only verb in _SECRET_VERBS; feed it a correct, matching password so it
    # reaches the actual keyring write, then make that write blow up the way a real
    # backend would.
    answers = iter([SECRET, SECRET])
    monkeypatch.setattr(ats_accounts, "_getpass", lambda prompt: next(answers))

    def boom(service, user, pw):
        raise RuntimeError(f"keyring backend exploded holding {SECRET}")

    monkeypatch.setattr(kr, "set_password", boom)
    assert ats_accounts.main(["set-password"]) == 1
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    assert "RuntimeError" in out.err
    assert "exploded" not in out.err                # class name ONLY
    assert len(out.err.strip().splitlines()) == 1


def test_cli_unexpected_error_ledger_verb_prints_message(ledger, monkeypatch,
                                                         capsys):
    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(ats_accounts, "record", boom)
    assert ats_accounts.main(["record", "--domain", "x.example.com",
                              "--email", "a@x.com"]) == 1
    err = capsys.readouterr().err
    assert "ats_accounts: error: OSError: disk full" in err
    assert "Traceback" not in err
    assert len(err.strip().splitlines()) == 1


def test_public_api_never_returns_the_password(kr, clip):
    """No exported callable hands the password back to a caller."""
    kr.set_password(ats_accounts.SERVICE, ats_accounts._MASTER_USER, SECRET)
    assert ats_accounts.copy_password_to_clipboard() is True     # -> bool, not str
    assert ats_accounts.password_exists() is True                # -> bool
    assert ats_accounts.clear_clipboard_if_password() in (True, False)
    assert "_get_master_password" not in getattr(ats_accounts, "__all__", ())
