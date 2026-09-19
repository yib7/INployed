"""SP2 (cycle 15): jobsdata.md_to_text() and job_detail_fields' job_description_md
precedence.

Root cause (measured by the controller): pipeline/score_jobs.py drops the HTML
job_description_formatted and keeps LinkedIn's job_summary (a single ~5,000-char
line with no newlines), but ALSO writes job_description_md (markdownify, ATX
headings, **bold**, lists) into every scored row -- and job_detail_fields never
read it. md_to_text is job_detail_fields' markdown counterpart to html_to_text
above it: same pure-regex, no-dependency contract, so a scored row's card text
gets its structure back.
"""
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import jobsdata  # noqa: E402


# ---- md_to_text ---------------------------------------------------------------------

def test_md_to_text_empty_string_passes_through():
    assert jobsdata.md_to_text("") == ""


def test_md_to_text_plain_text_passes_through_unchanged():
    plain = "No markup here.\n\nJust two paragraphs of ordinary text."
    assert jobsdata.md_to_text(plain) == plain


def test_md_to_text_heading_becomes_its_own_line_with_marker_stripped():
    out = jobsdata.md_to_text("## Requirements\n\n- a\n- b")
    assert out.startswith("Requirements")
    assert "## " not in out


def test_md_to_text_inserts_a_blank_line_before_a_heading_even_without_one():
    out = jobsdata.md_to_text("Intro paragraph.\n## Section\nBody text.")
    assert out == "Intro paragraph.\n\nSection\nBody text."


def test_md_to_text_keeps_the_blank_line_a_heading_already_had():
    out = jobsdata.md_to_text("Intro.\n\n## Section\n\nBody.")
    assert out == "Intro.\n\nSection\n\nBody."


def test_md_to_text_strips_bold_markers_both_styles():
    out = jobsdata.md_to_text("**Bold text** and __also bold__ here.")
    assert out == "Bold text and also bold here."


def test_md_to_text_strips_italic_markers_both_styles():
    out = jobsdata.md_to_text("*Italic* and _also italic_ here.")
    assert out == "Italic and also italic here."


def test_md_to_text_dash_list_items_become_bullets():
    out = jobsdata.md_to_text("- alpha\n- beta")
    assert out == "• alpha\n• beta"


def test_md_to_text_star_list_items_become_bullets():
    # markdownify's default top-level bullet marker is "*", not "-".
    out = jobsdata.md_to_text("* alpha\n* beta")
    assert out == "• alpha\n• beta"


def test_md_to_text_plus_list_items_become_bullets():
    out = jobsdata.md_to_text("+ alpha\n+ beta")
    assert out == "• alpha\n• beta"


def test_md_to_text_numbered_lists_keep_their_numbers():
    out = jobsdata.md_to_text("1. first\n2. second")
    assert out == "1. first\n2. second"


def test_md_to_text_unescapes_punctuation():
    out = jobsdata.md_to_text(r"full\-stack, e\.g\. Python, a \* symbol")
    assert out == "full-stack, e.g. Python, a * symbol"


def test_md_to_text_does_not_corrupt_a_star_bullet_next_to_real_italics():
    # markdownify's list marker AND its emphasis marker are the same "*"
    # character -- the bullet at line-start must not pair with a later,
    # genuinely-emphasised word on the same line.
    out = jobsdata.md_to_text("* Requires *Python* experience")
    assert out == "• Requires Python experience"


def test_md_to_text_leaves_an_escaped_asterisk_or_underscore_unpaired():
    # markdownify escapes a LITERAL asterisk/underscore in prose (escape_asterisks
    # / escape_underscores default True) -- two escaped stars/underscores must
    # never be mistaken for a real emphasis pair once unescaped.
    out = jobsdata.md_to_text(r"full\_time and part\_time")
    assert out == "full_time and part_time"
    out = jobsdata.md_to_text(r"a \* b \* c")
    assert out == "a * b * c"


def test_md_to_text_collapses_three_or_more_newlines_to_two():
    assert jobsdata.md_to_text("a\n\n\n\nb") == "a\n\nb"


def test_md_to_text_strips_each_line():
    out = jobsdata.md_to_text("  leading and trailing  \n   another line   ")
    assert out == "leading and trailing\nanother line"


def test_md_to_text_realistic_markdownify_output():
    # The actual shape score_jobs.py's html_to_md produces (markdownify,
    # heading_style="ATX", default bullets/emphasis options).
    md = ("We need **5+ years** of *Python* experience.\n\n"
          "### Nice to have\n\n1. AWS\n2. Docker")
    out = jobsdata.md_to_text(md)
    assert out == ("We need 5+ years of Python experience.\n\n"
                   "Nice to have\n\n1. AWS\n2. Docker")


# ---- job_detail_fields: job_description_md precedence -------------------------------

def _row(**over):
    base = {
        "job_posting_id": "1", "job_title": "AI Engineer",
        "company_name": "Riverstone", "job_location": "Boston, MA",
        "url": "https://x/1", "job_summary": "A summary long enough to clear the "
        "40-character bar, so it is the card's JD when nothing richer survives.",
    }
    base.update(over)
    return pd.Series(base)


def test_job_detail_fields_prefers_job_description_md_over_everything():
    f = jobsdata.job_detail_fields(_row(
        job_description_md="## Requirements\n\n- Own the pipeline end to end\n"
        "- Ship weekly",
        job_description_formatted="<p>The HTML posting, also long enough to "
        "qualify on its own merits here.</p>"))
    assert f["jd"].startswith("Requirements")
    assert "• Own the pipeline end to end" in f["jd"]
    assert "The HTML posting" not in f["jd"]


def test_job_detail_fields_blank_md_falls_through_to_formatted_html():
    # job_description_md wins on ANY non-empty result (no 40-char floor: a
    # non-empty value IS the real posting, not a truncatable teaser) -- only a
    # column that comes back blank after md_to_text falls through.
    f = jobsdata.job_detail_fields(_row(
        job_description_md="   \n\n   ",       # whitespace only -> "" after strip
        job_description_formatted="<p>The real posting, spelled out at "
        "sufficient length to clear the bar.</p>"))
    assert f["jd"].startswith("The real posting")


def test_job_detail_fields_short_but_real_md_still_wins_over_a_longer_summary():
    # The checkpoint case: a short, clearly-structured job_description_md
    # beats a long job_summary -- there is no length floor to clear.
    f = jobsdata.job_detail_fields(_row(job_description_md="## Requirements\n\n- a\n- b"))
    assert f["jd"].startswith("Requirements")
    assert "• a" in f["jd"]
    assert "A summary" not in f["jd"]


def test_job_detail_fields_without_md_column_keeps_the_old_precedence():
    f = jobsdata.job_detail_fields(_row(
        job_description_formatted="<p>Formatted still wins over summary, once "
        "it clears the same forty-character floor as everything else.</p>"))
    assert f["jd"].startswith("Formatted still wins")


def test_job_detail_fields_md_renders_through_md_to_text_not_html_to_text():
    # "<...>" in markdown prose (a value range, not markup) must survive --
    # html_to_text would read it as a tag and strip it; md_to_text must not.
    f = jobsdata.job_detail_fields(_row(
        job_description_md="## Requirements\n\nMust support values <5 and "
        ">10 in the same query."))
    assert "<5 and >10" in f["jd"]
