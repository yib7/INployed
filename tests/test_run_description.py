"""SP2 (cycle 15): resume_tailor.run._job_description_text's job_description_md
precedence (the tailor + Ask AI side of the same fix as jobsdata.job_detail_fields).

job_description_md is passed through UNCHANGED -- the tailor already reasons
over markdown, so running it through _to_plain (markdownify-on-HTML) here would
throw away structure the prompt can use, not add any.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor.run import _job_description_text  # noqa: E402


def test_prefers_job_description_md_over_formatted_html():
    job = {
        "job_description_md": "## Requirements\n\n- Own the pipeline end to end\n"
        "- Ship weekly",
        "job_description_formatted": "<p>The HTML posting, also long enough to "
        "qualify on its own merits here.</p>",
    }
    text = _job_description_text(job)
    assert text.startswith("## Requirements")
    assert "The HTML posting" not in text


def test_job_description_md_is_passed_through_unchanged_not_flattened():
    # The tailor reasons over markdown directly -- _to_plain (markdownify) must
    # never touch this column, or the structure it exists to preserve is lost.
    md = "## Requirements\n\n- **Own** the pipeline end to end\n- Ship weekly"
    job = {"job_description_md": md}
    assert _job_description_text(job) == md


def test_blank_md_falls_through_to_the_old_precedence():
    job = {
        "job_description_md": "   ",       # whitespace only -> "" via _field
        "job_description_formatted": "<p>The real posting, spelled out at "
        "sufficient length to clear the bar.</p>",
    }
    text = _job_description_text(job)
    assert text.startswith("The real posting")


def test_md_stub_falls_through_to_a_longer_summary():
    # job_description_md clears the same 40-char floor as the other three
    # columns -- a short md stub falls through exactly like a short
    # job_description_formatted always has.
    job = {
        "job_description_md": "## Requirements\n\n- go",     # well under 40 chars
        "job_summary": "A LinkedIn summary that is long enough to clear the "
        "forty-character bar on its own and describes the role, the team, "
        "the tech stack, and the day-to-day work in enough detail to read "
        "as a real, usable job description rather than a one-line teaser.",
    }
    text = _job_description_text(job)
    assert text.startswith("A LinkedIn summary")
    assert "Requirements" not in text


def test_without_a_md_column_keeps_the_old_precedence():
    job = {
        "job_description_formatted": "<p>Formatted wins over summary here, once "
        "it clears the same forty-character floor as everything else.</p>",
        "job_summary": "A summary that is also long enough to clear the forty "
        "character bar on its own, but formatted must still win.",
    }
    text = _job_description_text(job)
    assert text.startswith("Formatted wins")


def test_blank_row_yields_empty_string():
    assert _job_description_text({}) == ""
