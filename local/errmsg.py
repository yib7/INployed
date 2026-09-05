"""One place to render an exception for a message a USER will see.

The dashboard shows exception text in message boxes, the status bar and the
empty-state panel, because "something went wrong" is not actionable and the
parser's own reason usually is. The problem is what comes along with it: Python
puts the offending path into an OSError's own message, so

    PermissionError: [Errno 13] Permission denied:
    'C:\\Users\\<name>\\My Drive\\linkedin_jobs_master.csv.gz'

is what a user gets on screen the first time Excel or an antivirus scanner holds
the master open -- an ordinary Windows Tuesday. That string then travels: into a
screenshot, into a bug report, into a pasted stack of text on a public issue
tracker. The home directory names the person; the tree above it names their
employer often enough. None of it helps them fix the file that is locked.

So `for_user` keeps the diagnosis and drops the location: the exception class,
its message, and every absolute path in it reduced to a bare file name. The
caller already knows which file it was working on and can say so itself -- and
`local/qt/main_window.py`'s unreadable-sources panel does exactly that, naming
`Path(p).name` beside the reason.

This is NOT a log scrubber. A log file on the user's own disk is the right place
for a full path, and `watcher.log` / `scraper.log` keep theirs. This is only for
text that goes on screen.
"""
from __future__ import annotations

import re

__all__ = ["for_user", "scrub_paths"]

# A drive-letter path: C:\a\b\file.ext or C:/a/b/file.ext -> file.ext. The
# lookbehind is load-bearing: without it the "s:/" inside "https://host/p" is a
# drive-letter match, and an error naming an API endpoint came out as "httpp".
_WIN_PATH_RE = re.compile(
    r"""(?<![A-Za-z0-9])[A-Za-z]:[\\/](?:[^\\/\r\n<>|"']*[\\/])*([^\\/\r\n<>|"']*)""")
# A POSIX absolute path with at least one directory: /home/u/f -> f. The
# lookbehind keeps it off URLs (the "//" in "https://host/path") and off a path
# that is already only a leading slash.
_POSIX_PATH_RE = re.compile(r"""(?<![\w.:/])/(?:[^/\r\n<>|"' ]+/)+([^/\r\n<>|"' ]*)""")


def scrub_paths(text: str) -> str:
    """`text` with every absolute path reduced to its final component."""
    out = _WIN_PATH_RE.sub(lambda m: m.group(1) or "...", str(text))
    return _POSIX_PATH_RE.sub(lambda m: m.group(1) or "...", out)


def for_user(exc: BaseException, *, with_type: bool = False) -> str:
    """An exception as one line of user-facing text, carrying no absolute path.

    `with_type=True` prefixes the class name, for places where the message alone
    is often empty (a bare `KeyError('x')` reads as just `'x'`).
    """
    body = scrub_paths(str(exc)).strip()
    name = type(exc).__name__
    if not body:
        return name
    return f"{name}: {body}" if with_type else body
