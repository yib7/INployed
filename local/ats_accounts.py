"""ATS-account ledger + master-password clipboard transit.

Job portals (Workday, iCIMS, ...) force per-company accounts. The design here
keeps ONE master password in the Windows Credential Manager (service
"inployed-ats", via keyring) and a JSON ledger of which domains have accounts:
the ledger records email + method + timestamps and NEVER a password (enforced
in code: a password-shaped field name is rejected on write).

The password's ONLY exit from the keyring is the clipboard
(`copy_password_to_clipboard`), so an agent can tell a human (or a signup
form) to paste it without the secret ever appearing in chat logs, stdout, or
a file. `clear_clipboard_if_password` wipes it afterwards, and only when the
clipboard still holds the password, so unrelated user clipboard content is
never clobbered. No function in this module returns or prints the password;
the getter is module-private.

keyring is imported lazily so this module (and the dashboard importing it)
still loads where keyring isn't installed; password_exists() just reports
False there.
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from jsonutil import atomic_write_json  # noqa: E402  (needs HERE on sys.path)

__all__ = [
    "SERVICE", "ledger_path", "record", "lookup", "list_accounts", "tenant_key",
    "password_exists", "has_password", "fill_password", "set_master_password",
    "unmet_rules", "copy_password_to_clipboard", "clear_clipboard_if_password", "main",
]

SERVICE = "inployed-ats"          # keyring service name (Windows Credential Manager)
_MASTER_USER = "master"           # single shared master-password slot

# Field names that must never land in the ledger: the ledger is plaintext JSON.
_FORBIDDEN_KEY_RE = re.compile(r"pass|pwd|secret|token|credential", re.IGNORECASE)

_getpass = getpass.getpass        # test seam (monkeypatched to feed answers)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── ledger (never contains a password) ───────────────────────────────────────

def ledger_path(path: Optional[Path] = None) -> Path:
    """The ledger file: explicit arg > ATS_ACCOUNTS_PATH env (read at call time)
    > beside the apply queue in the linkedin_watcher appdata dir."""
    if path is not None:
        return Path(path)
    env = os.environ.get("ATS_ACCOUNTS_PATH", "").strip()
    if env:
        return Path(env)
    appdata = Path(os.environ.get("LOCALAPPDATA",
                                  str(Path.home() / "AppData" / "Local")))
    return appdata / "linkedin_watcher" / "ats_accounts.json"


def _netloc(domain_or_url: str) -> str:
    """Lowercased netloc key: accepts a full URL or a bare host."""
    from urllib.parse import urlsplit
    raw = str(domain_or_url or "").strip()
    host = urlsplit(raw).netloc if "://" in raw else raw.split("/")[0]
    return host.strip().lower()


def _load_ledger(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    lp = ledger_path(path)
    try:
        data = json.loads(lp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _assert_no_password_keys(rec: Dict[str, Any]) -> None:
    for key in rec:
        if _FORBIDDEN_KEY_RE.search(str(key)):
            raise ValueError(
                f"refusing to store field {key!r} in the ATS ledger: "
                "the ledger is plaintext JSON and never carries credentials "
                f"(the master password lives in keyring service {SERVICE!r})")


def record(domain_or_url: str, email: str, method: str = "master_password",
           path: Optional[Path] = None, **extra: Any) -> Dict[str, Any]:
    """Upsert one ledger entry keyed by lowercased netloc. `extra` may carry
    descriptive fields (note, username, ...); a password-shaped field name
    raises ValueError, since this file is plaintext and holds no credentials."""
    key = _netloc(domain_or_url)
    if not key:
        raise ValueError("a domain or URL is required")
    _assert_no_password_keys(dict(extra))
    ledger = _load_ledger(path)
    rec = ledger.get(key) or {"created_at": _now()}
    rec.update({"email": str(email), "method": str(method),
                "updated_at": _now(), **extra})
    _assert_no_password_keys(rec)  # belt-and-braces before it hits disk
    ledger[key] = rec
    lp = ledger_path(path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(lp, ledger)
    return dict(rec)


# Multi-tenant ATS hosts (ACC-13): the tenant a host names, with its site. A
# tenant's sign-in can sit on another host of the same site than its careers
# pages (iCIMS: careers-<tenant>.icims.com, <tenant>.icims.com), and one site
# holds every company's tenant, so the site alone never finds an account.
_TENANT_HOSTS = (
    ("myworkdayjobs.com", re.compile(r"^([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$")),
    ("myworkdaysite.com", re.compile(r"^([a-z0-9-]+)\.wd\d+\.myworkdaysite\.com$")),
    ("icims.com", re.compile(r"^(?:(?:careers|jobs|uscareers|external|internal|campus)-)?"
                             r"([a-z0-9-]+)\.icims\.com$")),
)
# host labels every tenant of a site shares: they name no tenant
_SHARED_LABELS = frozenset(("login", "www", "api", "cdn", "static", "auth", "sso", "accounts",
                            "secure", "app", "apps", "mail"))


def tenant_key(domain_or_url: str) -> str:
    """"<site>/<tenant>" for a host of a multi-tenant ATS that names its
    tenant (`cboe.wd1.myworkdayjobs.com` -> "myworkdayjobs.com/cboe",
    `careers-gtsx.icims.com` -> "icims.com/gtsx"); "" for any other host and
    for a host every tenant shares (`login.icims.com`)."""
    host = _netloc(domain_or_url).split(":")[0]
    for site, pattern in _TENANT_HOSTS:
        m = pattern.match(host)
        if m and m.group(1) not in _SHARED_LABELS:
            return f"{site}/{m.group(1)}"
    return ""


def _find(ledger: Dict[str, Dict[str, Any]], host: str) -> Optional[Dict[str, Any]]:
    """The entry for `host`: by its netloc, else by its tenant (ACC-13)."""
    rec = ledger.get(_netloc(host))
    if rec:
        return rec
    key = tenant_key(host)
    if key:
        for stored, entry in ledger.items():
            if tenant_key(stored) == key and entry:
                return entry
    return None


def lookup(domain_or_url: str, path: Optional[Path] = None, *,
           related: Any = ()) -> Optional[Dict[str, Any]]:
    """The ledger entry for a domain/URL, or None. A host of a multi-tenant
    ATS finds the account of its tenant on any host of that tenant
    (`tenant_key`). `related`: the job's own hosts on the same site (its
    careers host beside a shared sign-in host, `login.icims.com`), looked up
    the same way when the host itself finds nothing; one that names another
    tenant than the host never lends its account (ACC-13)."""
    ledger = _load_ledger(path)
    rec = _find(ledger, domain_or_url)
    if rec is None:
        own = tenant_key(domain_or_url)
        for other in related or ():
            theirs = tenant_key(other)
            if own and theirs != own:
                continue
            rec = _find(ledger, other)
            if rec is not None:
                break
    return dict(rec) if rec else None


def list_accounts(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """The whole ledger map (lowercased netloc -> record)."""
    return _load_ledger(path)


# ── keyring master password ──────────────────────────────────────────────────

def _keyring():
    """The keyring module, or None where it isn't installed / importable:
    lazy so this module always imports."""
    try:
        import keyring
        return keyring
    except Exception:
        return None


def password_exists() -> bool:
    """Whether a master password is stored (False too when keyring is missing)."""
    kr = _keyring()
    if kr is None:
        return False
    try:
        return bool(kr.get_password(SERVICE, _MASTER_USER))
    except Exception:
        return False


def set_master_password(password: Optional[str] = None) -> bool:
    """Store the master password in the Credential Manager.

    password=None is the interactive path (getpass twice, must match: the CLI);
    a str is the programmatic path (the dashboard QInputDialog hands the typed value in,
    no prompt ever fires). A programmatic empty/whitespace-only value raises
    ValueError. The value is never echoed, printed, or returned either way."""
    kr = _keyring()
    if kr is None:
        print("keyring is not installed; run: pip install keyring",
              file=sys.stderr)
        return False
    if password is None:
        first = _getpass("New master password: ")
        second = _getpass("Repeat to confirm: ")
        if not first or first != second:
            print("passwords empty or did not match; nothing stored.",
                  file=sys.stderr)
            return False
        password = first
    elif not str(password).strip():
        raise ValueError("master password must not be empty or whitespace-only")
    kr.set_password(SERVICE, _MASTER_USER, password)
    return True


def _get_master_password() -> Optional[str]:
    """Module-PRIVATE. The only reader of the stored secret. Its legitimate
    consumers are `has_password` (which keeps only the boolean),
    `fill_password` (which types it into a password field in this process) and
    the clipboard transit below. Never export, log, or print."""
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(SERVICE, _MASTER_USER)
    except Exception:
        return None


def has_password() -> bool:
    """Report availability without exposing the stored password."""
    return bool(_get_master_password())


def unmet_rules(rules: Dict[str, Any]) -> Optional[List[str]]:
    """The password rules a site states (ACC-04: `min_length`, `max_length`,
    `upper`, `lower`, `digit`, `special`, `forbidden` characters) that the
    stored master password does not meet, each in words ("at least 12
    characters"); [] when it meets them all, None when no password is stored.
    Counted here from the password's length and character classes: the
    value, and anything read from it but those words, never leaves this
    module."""
    password = _get_master_password()
    if not password:
        return None
    out: List[str] = []
    low, high = rules.get("min_length"), rules.get("max_length")
    if low and len(password) < int(low):
        out.append(f"at least {int(low)} characters")
    if high and len(password) > int(high):
        out.append(f"at most {int(high)} characters")
    if rules.get("upper") and not any(c.isupper() for c in password):
        out.append("an uppercase letter")
    if rules.get("lower") and not any(c.islower() for c in password):
        out.append("a lowercase letter")
    if rules.get("digit") and not any(c.isdigit() for c in password):
        out.append("a digit")
    if rules.get("special") and all(c.isalnum() for c in password):
        out.append("a special character")
    banned = str(rules.get("forbidden") or "")
    if banned and any(c in banned for c in password):
        out.append(f"none of these characters: {' '.join(banned)}")
    return out


def fill_password(page_or_frame, locator) -> bool:
    """Move the stored password directly into a field, keeping errors private.

    The read-back compares lengths, so the value is never read back into
    Python. A field that truncated or ignored the fill (a maxlength, a widget
    that rewrites what it was given) reports False, so no half password is left
    behind."""
    password = _get_master_password()
    if not password:
        return False
    try:
        target = page_or_frame.locator(locator) if isinstance(locator, str) else locator
        target.fill(password, timeout=5_000)
        # the length is counted in the page, so the value itself never crosses
        # back into this process (`input_value()` would hand it over)
        landed = int(target.evaluate("el => (el.value || '').length"))
    except Exception:  # noqa: BLE001  (Playwright errors may include the fill value)
        return False
    if landed != len(password):
        logging.getLogger(__name__).warning(
            "password field kept %d of %d characters; treating the fill as failed",
            landed, len(password))
        return False
    logging.getLogger(__name__).info("password filled (%d chars hidden)", len(password))
    return True


# ── clipboard transit (ctypes, CF_UNICODETEXT) ───────────────────────────────

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def _win_clip():
    """(user32, kernel32) with 64-bit-safe handle signatures. Without explicit
    c_void_p restype/argtypes, ctypes truncates HANDLEs to 32-bit ints and the
    clipboard calls crash or corrupt on 64-bit Python."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    return ctypes, user32, kernel32


def _clip_set(text: str) -> None:
    """Put `text` on the Windows clipboard as CF_UNICODETEXT (test seam:
    monkeypatched by the suite; never called with real secrets in tests)."""
    if os.name != "nt":
        raise RuntimeError("clipboard transit is Windows-only (ctypes/user32)")
    ctypes, user32, kernel32 = _win_clip()
    if not user32.OpenClipboard(None):
        raise OSError("OpenClipboard failed")
    try:
        user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
        if not handle:
            raise OSError("GlobalAlloc failed")
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            raise OSError("GlobalLock failed")
        try:
            ctypes.memmove(ptr, buf, size)
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)  # ownership only passes on success
            raise OSError("SetClipboardData failed")
    finally:
        user32.CloseClipboard()


def _clip_get() -> str:
    """Current clipboard text ("" when empty / not text)."""
    if os.name != "nt":
        raise RuntimeError("clipboard transit is Windows-only (ctypes/user32)")
    ctypes, user32, kernel32 = _win_clip()
    if not user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
        return ""
    if not user32.OpenClipboard(None):
        raise OSError("OpenClipboard failed")
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _clip_clear() -> None:
    if os.name != "nt":
        raise RuntimeError("clipboard transit is Windows-only (ctypes/user32)")
    _, user32, _ = _win_clip()
    if not user32.OpenClipboard(None):
        raise OSError("OpenClipboard failed")
    try:
        user32.EmptyClipboard()
    finally:
        user32.CloseClipboard()


def copy_password_to_clipboard() -> bool:
    """Move the master password keyring -> clipboard. Returns True on success,
    False when no password is stored. Never returns or prints the secret."""
    pw = _get_master_password()
    if not pw:
        return False
    _clip_set(pw)
    return True


def clear_clipboard_if_password() -> bool:
    """Clear the clipboard ONLY if it still holds the master password; a user's
    unrelated clipboard content is never clobbered. True when cleared."""
    pw = _get_master_password()
    if not pw:
        return False
    try:
        current = _clip_get()
    except Exception:
        return False
    if current == pw:
        _clip_clear()
        return True
    return False


# ── CLI ──────────────────────────────────────────────────────────────────────

# Verbs whose code path touches the secret: an unexpected exception there is
# reported by CLASS NAME ONLY: str(e) from a keyring/clipboard backend could
# carry the password itself.
_SECRET_VERBS = frozenset(("set-password", "clip-password", "clip-clear"))


def _force_utf8_stdio() -> None:
    """Piped stdout/stderr on Windows default to cp1252, so a ledger note or
    email with an emoji/arrow would UnicodeEncodeError mid-verb. Reconfigure
    both streams to UTF-8 up front; errors="replace" so printing never raises."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main(argv: Optional[List[str]] = None) -> int:
    """Exit codes: 0 ok · 1 refused/unavailable (no password stored, mismatch,
    keyring missing) or unexpected error (one line on stderr) · 2 lookup miss.
    NO verb ever outputs the password."""
    _force_utf8_stdio()
    ap = argparse.ArgumentParser(
        prog="ats_accounts",
        description="ATS account ledger + master-password clipboard transit "
                    "(the password itself is never printed).")
    sub = ap.add_subparsers(dest="verb", required=True)

    sub.add_parser("set-password", help="store the master password (prompts twice)")
    sub.add_parser("password-status", help="print exactly 'set' or 'not set'")
    sub.add_parser("clip-password", help="copy the master password to the clipboard")
    sub.add_parser("clip-clear", help="clear the clipboard if it holds the password")

    def add_ledger_flag(p):
        p.add_argument("--ledger", metavar="PATH", default=None,
                       help="ledger file (default: %%LOCALAPPDATA%%\\linkedin_watcher"
                            "\\ats_accounts.json, or ATS_ACCOUNTS_PATH)")
        return p

    p = add_ledger_flag(sub.add_parser("record", help="upsert one ledger entry"))
    p.add_argument("--domain", "--url", dest="domain", required=True,
                   help="ATS domain or any URL on it")
    p.add_argument("--email", required=True)
    p.add_argument("--method", default="master_password",
                   help="how the account signs in (e.g. master_password, google_sso)")
    p.add_argument("--note", default=None)

    p = add_ledger_flag(sub.add_parser("lookup", help="one entry by domain/URL"))
    p.add_argument("domain")
    p.add_argument("--json", action="store_true")

    p = add_ledger_flag(sub.add_parser("list", help="the whole ledger"))
    p.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    try:
        return _run_verb(args)
    except Exception as exc:
        # Anything unexpected: one line on stderr, exit 1; for verbs that
        # touch the secret, the exception CLASS name only (str(e) from a
        # keyring/clipboard backend could carry the password).
        detail = type(exc).__name__
        if args.verb not in _SECRET_VERBS:
            detail = f"{detail}: {exc}"
        print(f"ats_accounts: error: {detail}", file=sys.stderr)
        return 1


def _run_verb(args: argparse.Namespace) -> int:
    lp = Path(args.ledger) if getattr(args, "ledger", None) else None

    if args.verb == "set-password":
        return 0 if set_master_password() else 1
    if args.verb == "password-status":
        print("set" if password_exists() else "not set")
        return 0
    if args.verb == "clip-password":
        if not copy_password_to_clipboard():
            print("no master password stored; run set-password first.",
                  file=sys.stderr)
            return 1
        print("master password copied to clipboard; paste it, then run "
              "clip-clear.")
        return 0
    if args.verb == "clip-clear":
        if clear_clipboard_if_password():
            print("clipboard cleared.")
        else:
            print("clipboard left untouched (it does not hold the password).")
        return 0
    if args.verb == "record":
        extra = {"note": args.note} if args.note is not None else {}
        rec = record(args.domain, email=args.email, method=args.method,
                     path=lp, **extra)
        print(json.dumps({_netloc(args.domain): rec}, indent=2,
                         ensure_ascii=False))
        return 0
    if args.verb == "lookup":
        rec = lookup(args.domain, path=lp)
        if rec is None:
            print(f"no ledger entry for {_netloc(args.domain)!r}",
                  file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(rec, indent=2, ensure_ascii=False))
        else:
            print(f"{_netloc(args.domain)}: {rec.get('email', '')} "
                  f"({rec.get('method', '')})")
        return 0
    if args.verb == "list":
        ledger = list_accounts(path=lp)
        if args.json:
            print(json.dumps(ledger, indent=2, ensure_ascii=False))
        else:
            for key, rec in sorted(ledger.items()):
                print(f"{key}: {rec.get('email', '')} ({rec.get('method', '')})")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
