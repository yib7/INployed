"""Open job / resume links in Chrome under the configured Google profile.

Toolkit-agnostic (no Tk/Qt) so any UI can launch URLs through it. The account
comes from LINKEDIN_CHROME_ACCOUNT (loaded from scrape_data/.env by the app);
falls back to the default browser when Chrome or the profile can't be resolved.
"""
from __future__ import annotations

import json
import os
import subprocess
import webbrowser
from functools import lru_cache
from pathlib import Path

# Open job links in Chrome under this Google account's profile (falls back to the
# default browser if Chrome or the profile can't be resolved).
CHROME_ACCOUNT = os.environ.get("LINKEDIN_CHROME_ACCOUNT", "")


def _find_chrome() -> str | None:
    """Locate chrome.exe via the usual install dirs, then the registry App Paths."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    try:
        import winreg

        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                    root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
                ) as k:
                    path, _ = winreg.QueryValueEx(k, None)
                    if path and Path(path).is_file():
                        return path
            except OSError:
                continue
    except Exception:
        pass
    return None


def _chrome_profile_dir(account: str) -> str:
    """Profile directory whose signed-in account matches `account` (default 'Default').

    Matches user_name, then gaia_name, then the email local-part, so a profile that
    stores the address differently still resolves. An empty `account` short-circuits
    to 'Default' so it never matches a blank-user_name (signed-out) profile. Prints a
    warning when a non-empty account finds no match, so a silent fallback is visible.
    """
    if not account:
        return "Default"
    want = account.lower()
    want_local = want.split("@", 1)[0]
    local_state = Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data/Local State"
    try:
        info = json.loads(local_state.read_text(encoding="utf-8")).get("profile", {}).get("info_cache", {})
    except (OSError, ValueError):
        return "Default"
    for directory, meta in info.items():
        user_name = (meta.get("user_name") or "").lower()
        gaia_name = (meta.get("gaia_name") or "").lower()
        if user_name == want or gaia_name == want or (user_name and user_name.split("@", 1)[0] == want_local):
            return directory
    print(f"[chrome] no Chrome profile matched {account!r}; using Default profile")
    return "Default"


@lru_cache(maxsize=1)
def _chrome_launcher() -> tuple[str, str] | None:
    """(chrome_exe, profile_dir) for CHROME_ACCOUNT, or None if Chrome isn't found."""
    chrome = _find_chrome()
    if not chrome:
        return None
    return chrome, _chrome_profile_dir(CHROME_ACCOUNT)


def open_in_chrome(url: str) -> None:
    """Open `url` in Chrome under the configured profile; fall back to the default browser.

    With LINKEDIN_CHROME_ACCOUNT resolved, --profile-directory opens the URL in
    that profile's window. A profile cannot be force-switched inside an already-
    running Chrome via CLI; the resolved-Default case is the one that matters here.

    `url` originates from scraped job data, so guard the subprocess: (1) only open
    http(s) URLs — never file:/javascript:/etc. — and (2) pass the URL after a `--`
    end-of-options marker so a value starting with '-' can't be misread as a Chrome
    switch (Chromium argument-injection class).
    """
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        print(f"[chrome] refusing to open non-http(s) URL: {url!r}")
        return
    launcher = _chrome_launcher()
    if launcher:
        chrome, profile = launcher
        try:
            subprocess.Popen([chrome, f"--profile-directory={profile}", "--", url])
            return
        except OSError:
            pass
    webbrowser.open(url)
