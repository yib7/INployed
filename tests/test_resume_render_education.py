"""The education block's ATS layout (render._education), entry by entry.

The template's \\resumeSubheading takes (school, location suffix, dates, degree)
and the GPA and honors sit on their own labelled \\small line beneath. The golden
pins one entry that has everything; these pin what an entry WITHOUT a field
renders, and the escaping of each field, so a user whose master lacks a GPA or a
location gets a well-formed block rather than a stray separator or an empty label.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import render  # noqa: E402

_FULL = {
    "school": "State University", "location": "City, ST",
    "degree": "Bachelor of Science in Computer Science",
    "gpa": 3.7, "dates": "2021-08 / 2025-05", "honors": ["Dean's List", "Honors College"],
}


def _entry(**overrides):
    e = dict(_FULL)
    e.update(overrides)
    for k, v in list(e.items()):
        if v is None:
            del e[k]
    return e


def _heading(tex: str) -> str:
    """The four-argument \\resumeSubheading line pair for the one entry."""
    start = tex.index("\\resumeSubheading\n") + len("\\resumeSubheading\n")
    return tex[start:tex.index("\\vspace{2pt}", start)]


def test_the_heading_arguments_are_school_location_suffix_dates_degree():
    tex = render._education([_entry()])
    assert _heading(tex) == ("{State University}{, City, ST}{August 2021 -- May 2025}\n"
                             "{Bachelor of Science in Computer Science}")


def test_the_gpa_and_honors_share_one_small_line():
    tex = render._education([_entry()])
    assert (r"\item \small{\textbf{GPA:} 3.7/4.0 $|$ "
            r"\textbf{Awards \& Honors:} Dean's List; Honors College}") in tex


def test_no_location_leaves_the_suffix_empty_not_a_dangling_comma():
    tex = render._education([_entry(location=None)])
    assert "{State University}{}{August 2021" in tex
    assert ", }" not in tex


def test_no_gpa_prints_honors_alone_without_a_separator():
    for gpa in (None, "", 0, 0.0, "0"):
        tex = render._education([_entry(gpa=gpa)])
        assert "GPA" not in tex, repr(gpa)
        assert r"\item \small{\textbf{Awards \& Honors:} Dean's List; Honors College}" in tex
        assert r"$|$ \textbf{Awards" not in tex, repr(gpa)


def test_no_honors_prints_the_gpa_alone():
    for honors in (None, []):
        tex = render._education([_entry(honors=honors)])
        assert r"\item \small{\textbf{GPA:} 3.7/4.0}" in tex
        assert "Honors" not in tex


def test_neither_gpa_nor_honors_prints_no_small_line_at_all():
    tex = render._education([_entry(gpa=None, honors=None)])
    assert r"\item \small" not in tex
    assert r"\vspace{2pt}" in tex, "the degree row's spacer stays"


def test_gpa_scale_overrides_the_printed_denominator():
    assert "GPA:} 8.9/10" in render._education([_entry(gpa=8.9, gpa_scale=10)])
    assert "GPA:} 3.7/4" in render._education([_entry(gpa_scale=4)])
    assert "GPA:} 3.7/4.0" in render._education([_entry(gpa_scale=4.0)])


def test_gpa_scale_blank_or_zero_falls_back_to_four_point_oh():
    for scale in (None, "", 0):
        assert "GPA:} 3.7/4.0" in render._education([_entry(gpa_scale=scale)]), repr(scale)


def test_a_non_numeric_gpa_scale_is_printed_as_written():
    assert "GPA:} 3.7/four" in render._education([_entry(gpa_scale="four")])


def test_every_field_is_latex_escaped():
    tex = render._education([_entry(
        school="A&M University", location="Research Triangle, NC #1",
        degree="B.S. in Math_Stats", gpa="95%", gpa_scale="100%",
        honors=["Top 5% of class", "Dean's #1"],
    )])
    assert r"{A\&M University}{, Research Triangle, NC \#1}" in tex
    assert r"{B.S. in Math\_Stats}" in tex
    assert r"GPA:} 95\%/100\%" in tex
    assert r"Top 5\% of class; Dean's \#1" in tex


def test_no_entries_renders_no_section():
    assert render._education([]) == ""
    assert render._education(None) == ""
