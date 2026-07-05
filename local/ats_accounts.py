"""ATS-account ledger + master-password clipboard transit (SP2).

Job portals (Workday, iCIMS, ...) force per-company accounts. The design here
keeps ONE master password in the Windows Credential Manager (service
"inployed-ats", via keyring) and a JSON ledger of which domains have accounts —
the ledger records email + method + timestamps and NEVER a password (enforced
in code: a password-shaped field name is rejected on write).

The password's ONLY exit from the keyring is the clipboard
(`copy_password_to_clipboard`), so an agent can tell a human — or a signup
form — to paste it without the secret ever appearing in chat logs, stdout, or
a file. `clear_clipboard_if_password` wipes it afterwards, and only when the
clipboard still holds the password, so unrelated user clipboard content is
never clobbered. No function in this module returns or prints the password;
the getter is module-private.

keyring is imported lazily so this module (and the dashboard importing it)
still loads where keyring isn't installed — password_exists() just reports
False there.
"""
from __future__ import annotations

import argparse
import getpass
import json
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
    "SERVICE", "ledger_path", "record", "lookup", "list_accounts",
    "password_exists", "set_master_password",
    "copy_password_to_clipboard", "clear_clipboard_if_password", "main",
]

SERVICE = "inployed-ats"          # keyring service name (Windows Credential Manager)
_MASTER_USER = "master"           # single shared master-password slot

# Field names that must never land in the ledger — the ledger is plaintext JSON.
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
                f"refusing to store field {key!r} in the ATS ledger — "
                "the ledger is plaintext JSON and never carries credentials "
                f"(the master password lives in keyring service {SERVICE!r})")


def record(domain_or_url: str, email: str, method: str = "master_password",
           path: Optional[Path] = None, **extra: Any) -> Dict[str, Any]:
    """Upsert one ledger entry keyed by lowercased netloc. `extra` may carry
    descriptive fields (note, username, ...) but any password-shaped field name
    raises ValueError — credentials never enter this file."""
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


def lookup(domain_or_url: str, path: Optional[Path] = None
           ) -> Optional[Dict[str, Any]]:
    """The ledger entry for a domain/URL, or None."""
    rec = _load_ledger(path).get(_netloc(domain_or_url))
    return dict(rec) if rec else None


def list_accounts(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """The whole ledger map (lowercased netloc -> record)."""
    return _load_ledger(path)


# ── keyring master password ──────────────────────────────────────────────────

def _keyring():
    """The keyring module, or None where it isn't installed / importable —
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


def set_master_password() -> bool:
    """Prompt (getpass, twice, must match) and store the master password in the
    Credential Manager. The typed value is never echoed or printed."""
    kr = _keyring()
    if kr is None:
        print("keyring is not installed — run: pip install keyring",
              file=sys.stderr)
        return False
    first = _getpass("New master password: ")
    second = _getpass("Repeat to confirm: ")
    if not first or first != second:
        print("passwords empty or did not match — nothing stored.",
              file=sys.stderr)
        return False
    kr.set_password(SERVICE, _MASTER_USER, first)
    return True


def _get_master_password() -> Optional[str]:
    """Module-PRIVATE. The only reader of the stored secret; its only legitimate
    consumer is the clipboard transit below. Never export, log, or print."""
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(SERVICE, _MASTER_USER)
    except Exception:
        return None


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
    """Clear the clipboard ONLY if it still holds the master password — a user's
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

def main(argv: Optional[List[str]] = None) -> int:
    """Exit codes: 0 ok · 1 refused/unavailable (no password stored, mismatch,
    keyring missing) · 2 lookup miss. NO verb ever outputs the password."""
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
    lp = Path(args.ledger) if getattr(args, "ledger", None) else None

    if args.verb == "set-password":
        return 0 if set_master_password() else 1
    if args.verb == "password-status":
        print("set" if password_exists() else "not set")
        return 0
    if args.verb == "clip-password":
        if not copy_password_to_clipboard():
            print("no master password stored — run set-password first.",
                  file=sys.stderr)
            return 1
        print("master password copied to clipboard — paste it, then run "
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
