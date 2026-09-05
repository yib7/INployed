"""local/errmsg.py — exception text for a message a user will SEE.

The dashboard shows exception text because "something went wrong" is not
actionable. Python puts the offending path into an OSError's own message, so the
empty-state panel carefully named the file as `Path(p).name` and then printed
`[Errno 13] Permission denied: 'C:\\\\Users\\\\<name>\\\\...'` right after it.
That travels into screenshots and bug reports, and the home directory names the
person. These pin the scrub without breaking the diagnosis it carries.
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import errmsg  # noqa: E402

B = chr(92)


def test_a_windows_path_is_reduced_to_its_file_name():
    text = "cannot read C:" + B + "Users" + B + "someone" + B + "My Drive" + B + "master.csv.gz"
    assert errmsg.scrub_paths(text) == "cannot read master.csv.gz"


def test_a_posix_path_is_reduced_to_its_file_name():
    assert errmsg.scrub_paths("/home/someone/scraper/out.csv missing") == "out.csv missing"


def test_a_url_is_left_alone():
    """The drive-letter rule has to not fire on the "s:/" inside "https://" --
    without the lookbehind, an error naming an endpoint came out as "httpp"."""
    for url in ("https://api.brightdata.com/datasets/v3/progress",
                "http://127.0.0.1:8080/x"):
        assert errmsg.scrub_paths(f"see {url} for detail") == f"see {url} for detail"


def test_ordinary_text_is_left_alone():
    for text in ("expected 3.14.0, got 3.14.7", "column 'job_posting_id' missing",
                 "Error -3 while decompressing data: invalid stored block lengths",
                 "ratio 1/2 and a/b/c"):
        assert errmsg.scrub_paths(text) == text


def test_for_user_on_a_real_oserror_keeps_the_diagnosis_and_drops_the_location():
    """The concrete case: a master the user has open in Excel, or an antivirus
    scanner is mid-scan. An ordinary Windows Tuesday, and the raw message names
    the user's home directory."""
    d = tempfile.mkdtemp()
    try:
        open(os.path.join(d, "sub", "linkedin_jobs_master.csv.gz"), "rb")
    except OSError as exc:
        raw = str(exc)
        shown = errmsg.for_user(exc, with_type=True)
    # OSError.__str__ formats the filename with %R, so `raw` carries it in repr
    # form (doubled separators on Windows) -- compare on the directory NAME,
    # which is separator-free and present either way.
    leaf = os.path.basename(d)
    assert leaf in raw, "premise: the raw message carries the directory"
    assert leaf not in shown
    assert "Users" not in shown and "sub" not in shown
    assert "linkedin_jobs_master.csv.gz" in shown   # still says WHICH file
    assert "No such file" in shown                  # still says WHY
    assert shown.startswith("FileNotFoundError")


def test_for_user_falls_back_to_the_class_name_when_there_is_no_message():
    assert errmsg.for_user(ValueError()) == "ValueError"
