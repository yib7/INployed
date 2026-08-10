"""P2-7: the Projects section's repo link must not double the scheme.

render._projects builds an inline "Name | Link" href from the block's `repo`
field. If master_experience.yaml stores a full URL (https://github.com/x/y)
rather than a bare host+path (github.com/x/y), naively prefixing "https://"
produces "https://https://..." -- a broken link in the compiled resume.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import assets, render  # noqa: E402
from resume_tailor.latexutil import escape_url  # noqa: E402

_SEL = {"projects": [{"name": "ProjX", "groups": [["a1"]]}]}
_BULLETS = {"a1": "Built a thing that did a thing."}


def _blocks_with_repo(repo: str) -> dict:
    return {
        "projects": [{
            "name": "ProjX", "dates": "2024",
            "live_url": None, "repo": repo,
            "atoms": ["a1"],
        }],
    }


def test_full_url_repo_renders_one_https_prefix(monkeypatch):
    monkeypatch.setattr(assets, "blocks", lambda: _blocks_with_repo("https://github.com/x/y"))
    tex = render._projects(_SEL, _BULLETS)
    assert "https://github.com/x/y" in tex
    assert "https://https://" not in tex


def test_bare_host_repo_still_gets_https_prefix(monkeypatch):
    monkeypatch.setattr(assets, "blocks", lambda: _blocks_with_repo("github.com/x/y"))
    tex = render._projects(_SEL, _BULLETS)
    assert "https://github.com/x/y" in tex
    assert "https://https://" not in tex


def test_http_url_repo_normalizes_to_https(monkeypatch):
    monkeypatch.setattr(assets, "blocks", lambda: _blocks_with_repo("http://github.com/x/y"))
    tex = render._projects(_SEL, _BULLETS)
    assert "https://github.com/x/y" in tex
    assert "http://https://" not in tex
    assert "https://http://" not in tex


# P2-2: an href target is not body text. escape_latex would backslash '_' and '~',
# which is right in a printed line and wrong in an address.


def test_escape_url_leaves_underscore_and_tilde_alone():
    assert escape_url("https://github.com/foo_bar/~x") == "https://github.com/foo_bar/~x"


def test_escape_url_backslashes_percent_and_hash():
    assert escape_url("https://x.dev/a#b") == r"https://x.dev/a\#b"
    assert escape_url("https://x.dev/a%20b") == r"https://x.dev/a\%20b"


def test_escape_url_percent_encodes_non_ascii_and_spaces():
    out = escape_url("https://x.dev/a b/é")
    assert out == r"https://x.dev/a\%20b/\%C3\%A9"
    assert out.isascii()


def test_escape_url_empty_is_empty():
    assert escape_url("") == ""
    assert escape_url(None) == ""


def test_underscored_repo_href_target_has_no_backslash_escape(monkeypatch):
    monkeypatch.setattr(assets, "blocks", lambda: _blocks_with_repo("github.com/x/my_repo"))
    tex = render._projects(_SEL, _BULLETS)
    assert r"\href{https://github.com/x/my_repo}" in tex
    assert r"my\_repo" not in tex
