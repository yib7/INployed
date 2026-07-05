"""Tests for the ATS-account ledger + master-password clipboard transit (SP2).

local/ats_accounts.py keeps a JSON ledger of which ATS domains the candidate has
accounts on (NEVER any password) and moves the single master password from the
Windows Credential Manager (service "inployed-ats") to the clipboard so a human
or agent can paste it — the password itself is never printed, returned by a
public function, or written to disk. Hermetic: the ledger goes to tmp_path, the
keyring is a fake object, and the ctypes clipboard layer is monkeypatched.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import ats_accounts  # noqa: E402

SECRET = "sekrit-hunter2-XYZZY"


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


def test_cli_clip_password_prints_copy_line_only(kr, clip, capsys):
    kr.set_password(ats_accounts.SERVICE, ats_accounts._MASTER_USER, SECRET)
    assert ats_accounts.main(["clip-password"]) == 0
    out = _assert_no_secret(capsys)
    assert "copied to clipboard" in out.out.lower()
    assert clip.text == SECRET


def test_cli_clip_password_without_stored_password_exits_1(kr, clip, capsys):
    assert ats_accounts.main(["clip-password"]) == 1
    _assert_no_secret(capsys)


def test_cli_clip_clear(kr, clip, capsys):
    kr.set_password(ats_accounts.SERVICE, ats_accounts._MASTER_USER, SECRET)
    clip.text = SECRET
    assert ats_accounts.main(["clip-clear"]) == 0
    _assert_no_secret(capsys)
    assert clip.text == ""


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


def test_public_api_never_returns_the_password(kr, clip):
    """No exported callable hands the password back to a caller."""
    kr.set_password(ats_accounts.SERVICE, ats_accounts._MASTER_USER, SECRET)
    assert ats_accounts.copy_password_to_clipboard() is True     # -> bool, not str
    assert ats_accounts.password_exists() is True                # -> bool
    assert ats_accounts.clear_clipboard_if_password() in (True, False)
    assert "_get_master_password" not in getattr(ats_accounts, "__all__", ())
