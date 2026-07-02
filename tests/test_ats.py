"""Tests for the anchored skill_aliases layer in the ATS matcher + gap-finder.

The ATS check is deterministic (no LLM). These prove the alias layer:
  - anchoring: an alias group survives only if its canonical is a REAL skill;
  - extract_keywords surfaces the JD's own alias spelling and sums the group's
    frequency without double-listing canonical + alias;
  - coverage marks a keyword present iff ANY spelling in its group is literally on
    the page (never fabricates presence);
  - find_gap_keywords does not propose a JD synonym of an owned concept as a gap.

A synthetic master is swapped in so the behaviour holds for any user.
"""
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import assets, ats, config, master_gaps  # noqa: E402

_MASTER = textwrap.dedent("""
    basics:
      name: Test User
      email: test@example.com
    skills:
      languages: [Python, SQL]
      developer_tools: [Docker, PostgreSQL]
      concepts_and_methodologies:
        - "A/B Testing"
        - "Exploratory Data Analysis (EDA)"
        - "Feature Engineering"
        - "Data Cleaning & Preprocessing"
    skill_aliases:
      "A/B Testing": ["Experimentation", "Split Testing"]
      "Data Cleaning & Preprocessing": ["Data Wrangling", "Data Preparation"]
      "PostgreSQL": ["Postgres"]
      "Nonexistent Concept": ["Bogus Alias"]
    skill_aliases_match_only:
      "Docker": ["Containerization", "Containers"]
      "Nonexistent Tool": ["Ghost Term"]
""")


def _clear():
    for name in ("load_master", "skill_aliases", "skill_aliases_match_only",
                 "atoms_by_id", "blocks"):
        fn = getattr(assets, name, None)
        if fn is not None and hasattr(fn, "cache_clear"):
            fn.cache_clear()


@pytest.fixture()
def synthetic_master(tmp_path, monkeypatch):
    p = tmp_path / "master.yaml"
    p.write_text(_MASTER, encoding="utf-8")
    monkeypatch.setattr(config, "MASTER_YAML", p)
    _clear()
    yield p
    _clear()


# ── anchoring ─────────────────────────────────────────────────────────────────
def test_anchored_alias_groups_keeps_real_drops_unanchored(synthetic_master):
    groups = dict(ats.anchored_alias_groups())
    assert "A/B Testing" in groups
    assert "Data Cleaning & Preprocessing" in groups
    # the canonical that is NOT a real skill is dropped (no untethered keyword dump)
    assert "Nonexistent Concept" not in groups
    assert groups["A/B Testing"] == ["Experimentation", "Split Testing"]


def test_alias_index_maps_every_spelling_to_its_group(synthetic_master):
    idx = ats.alias_index()
    assert idx.get("experimentation") == ("A/B Testing", "Experimentation", "Split Testing")
    assert idx.get("data wrangling") == (
        "Data Cleaning & Preprocessing", "Data Wrangling", "Data Preparation")
    # the canonical's own spelling also resolves to the group
    assert idx.get("a/b testing")[0] == "A/B Testing"
    # an unanchored alias is absent
    assert "bogus alias" not in idx


# ── extract_keywords ──────────────────────────────────────────────────────────
def test_extract_surfaces_alias_spelling_when_only_alias_in_jd(synthetic_master):
    jd = "Heavy experimentation culture. We value Experimentation across teams."
    kws = ats.extract_keywords(jd)
    assert "Experimentation" in kws            # the JD's own spelling is surfaced
    assert "A/B Testing" not in kws            # not double-listed under the canonical


def test_extract_groups_canonical_and_alias_no_double_listing(synthetic_master):
    # canonical once + alias twice -> ONE grouped entry, surfaced as the more frequent spelling
    jd = "We do A/B Testing. But mostly Experimentation, lots of experimentation."
    kws = ats.extract_keywords(jd)
    assert "Experimentation" in kws            # higher JD frequency -> surfaced spelling
    assert "A/B Testing" not in kws            # collapsed into the same group


# ── coverage ──────────────────────────────────────────────────────────────────
def test_coverage_present_when_alias_on_page(synthetic_master):
    frac, present, missing = ats.coverage(["Experimentation"], "Ran Experimentation on the funnel.")
    assert "Experimentation" in present and not missing


def test_coverage_present_when_canonical_on_page_for_alias_keyword(synthetic_master):
    # keyword is the alias, but the page prints the canonical -> still covered (same concept)
    frac, present, missing = ats.coverage(["Experimentation"], "Designed an A/B Testing framework.")
    assert "Experimentation" in present


def test_coverage_absent_when_no_group_spelling_on_page(synthetic_master):
    # never fabricates: nothing from the group is printed -> stays MISSING
    frac, present, missing = ats.coverage(["Experimentation"], "Completely unrelated resume text.")
    assert "Experimentation" in missing and not present


# ── find_gap_keywords ─────────────────────────────────────────────────────────
def test_gap_excludes_jd_synonym_of_owned_concept(synthetic_master):
    jd = "Seeking strength in Data Wrangling and Kubernetes. Kubernetes a must."
    gaps = [g.lower() for g in master_gaps.find_gap_keywords(jd)]
    assert "data wrangling" not in gaps        # owned via alias of Data Cleaning & Preprocessing
    assert "kubernetes" in gaps                 # genuinely missing


# ── match-only alias map (broader synonyms: match, never print) ───────────────
def test_match_only_groups_anchored_and_separate_from_printable(synthetic_master):
    printable = dict(ats.anchored_alias_groups())
    match_only = dict(ats.match_only_alias_groups())
    all_groups = dict(ats.all_alias_groups())
    # a match-only canonical is a real skill -> kept; its unanchored sibling -> dropped
    assert match_only.get("Docker") == ["Containerization", "Containers"]
    assert "Nonexistent Tool" not in match_only
    # the two maps are distinct: a match-only term is NOT in the printable (swap) source
    assert "Docker" not in printable
    # ...but the union (used for matching) carries both
    assert "Docker" in all_groups and "A/B Testing" in all_groups


def test_alias_index_includes_match_only_for_matching(synthetic_master):
    idx = ats.alias_index()
    assert idx.get("containerization") == ("Docker", "Containerization", "Containers")
    # an unanchored match-only alias never enters the index
    assert "ghost term" not in idx


def test_extract_surfaces_match_only_synonym(synthetic_master):
    jd = "Strong Containerization background. Containerization at scale required."
    kws = ats.extract_keywords(jd)
    assert "Containerization" in kws            # the JD's broader term surfaces as a keyword
    assert "Docker" not in kws                  # collapsed into the same group


def test_coverage_match_only_present_when_canonical_on_page(synthetic_master):
    # the JD's broader term counts as covered because the candidate's canonical is printed
    frac, present, missing = ats.coverage(["Containerization"], "Shipped services in Docker.")
    assert "Containerization" in present


def test_gap_excludes_match_only_synonym_of_owned_skill(synthetic_master):
    jd = "We need Containerization and Kubernetes. Kubernetes is essential."
    gaps = [g.lower() for g in master_gaps.find_gap_keywords(jd)]
    assert "containerization" not in gaps       # owned via match-only alias of Docker
    assert "kubernetes" in gaps
