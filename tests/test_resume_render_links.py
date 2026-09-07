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


def test_escape_url_percent_encodes_an_ampersand():
    r"""render.py drops the finished \href into argument #2 of
    \resumeProjectHeadingInline, and that macro puts #2 inside a tabular* row
    right before the column separator (resume_template.tex). TeX reads a raw `&`
    there as an alignment tab, so a repo URL carrying one failed the whole build
    with "Extra alignment tab has been changed to \cr"."""
    # the % of the escape is itself backslashed, the same as any other % in a URL
    assert escape_url("https://git.example/q?a=1&b=2") == r"https://git.example/q?a=1\%26b=2"
    assert "&" not in escape_url("https://x.dev/a&b&c")


def test_escape_url_never_emits_a_bare_brace():
    r"""`{` and `}` would close \href's argument early. They are percent-encoded
    by quote() before the backslash map is consulted, so the map needs no entry
    for them -- pinned here because the map USED to carry two that were dead."""
    out = escape_url("https://x.dev/a{b}c")
    assert "{" not in out and "}" not in out
    assert out == r"https://x.dev/a\%7Bb\%7Dc"


# 3A: a master entry with no `title:` / `location:` must not print "None".


_EXP_SEL = {"experience": [{"name": "Globex", "groups": [["a1"]]}]}


def test_experience_omits_a_missing_title_instead_of_printing_none(monkeypatch):
    """assets.blocks() builds every key with e.get(...), so a master entry that
    simply has no `title:` line arrives here as {"title": None} -- the key is
    PRESENT, so b.get('title', '') returns None and to_latex(None) renders the
    literal word "None" onto the PDF. master_validate.validate_master requires
    neither field, so a master that passes "Check setup" can ship it."""
    monkeypatch.setattr(assets, "blocks", lambda: {"experience": [
        {"name": "Globex", "title": None, "location": None, "dates": None,
         "atoms": ["a1"]}]})
    tex = render._experience(_EXP_SEL, _BULLETS)
    assert "Globex" in tex
    assert "None" not in tex


def test_leadership_and_projects_omit_a_missing_name_instead_of_printing_none(monkeypatch):
    """Same defect, same shape, in the other two block renderers."""
    monkeypatch.setattr(assets, "blocks", lambda: {"leadership": [
        {"name": None, "dates": None, "atoms": ["a1"]}]})
    assert "None" not in render._leadership(
        {"leadership": [{"name": None, "groups": [["a1"]]}]}, _BULLETS)
    monkeypatch.setattr(assets, "blocks", lambda: {"projects": [
        {"name": None, "dates": None, "live_url": None, "repo": None,
         "atoms": ["a1"]}]})
    assert "None" not in render._projects(
        {"projects": [{"name": None, "groups": [["a1"]]}]}, _BULLETS)


# The contact header's LinkedIn and GitHub entries were printed as plain text while
# the Projects section rendered its repo as a coloured \href. Same kind of address,
# two different treatments: on the compiled PDF the project link was blue and
# clickable and the header's own links were dead black text. hyperref is already
# configured in resume_template.tex (colorlinks=true, urlcolor=LinkColor), so the
# only thing missing was the \href itself.


def _header_of(**basics) -> str:
    return render._header(basics)


def test_header_linkedin_is_a_clickable_href():
    tex = _header_of(name="Jane Doe", linkedin="linkedin.com/in/janedoe")
    assert "\\href{https://linkedin.com/in/janedoe}" in tex


def test_header_github_is_a_clickable_href():
    tex = _header_of(name="Jane Doe", github="github.com/janedoe")
    assert "\\href{https://github.com/janedoe}" in tex


def test_header_link_stored_as_full_url_does_not_double_the_scheme():
    tex = _header_of(github="https://github.com/janedoe")
    assert "\\href{https://github.com/janedoe}" in tex
    assert "https://https://" not in tex


def test_header_href_target_keeps_underscores_unescaped():
    """escape_url, not escape_latex: a backslash in the target breaks the address."""
    tex = _header_of(github="github.com/foo_bar")
    assert "\\href{https://github.com/foo_bar}" in tex


def test_header_link_display_text_is_unchanged():
    """Only the colour and the clickability change; the printed line reads the same."""
    tex = _header_of(linkedin="linkedin.com/in/janedoe")
    assert "linkedin.com/in/janedoe" in tex


def test_header_non_link_fields_stay_plain_text():
    tex = _header_of(location="City, ST", phone="555-555-0100", email="jane@example.com")
    assert "\\href" not in tex
    for v in ("City, ST", "555-555-0100", "jane@example.com"):
        assert v in tex


def test_header_omits_missing_link_fields():
    tex = _header_of(name="Jane Doe", location="City, ST")
    assert "\\href" not in tex
    assert "$|$" not in tex          # a single contact bit needs no separator
