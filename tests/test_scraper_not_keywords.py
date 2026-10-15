"""Skip lists understand NOT keywords (cycle 29, SP1).

LinkedIn's logged-out search ignores Bright Data's experience_level input, but a
NOT in the keyword works on TITLES: `"Software Engineer" NOT Senior NOT Sr` came
back with no senior titles (2026-10-14 A/B). Before this, any keyword with a NOT
got the WHOLE known-id list (a boolean keyword), which at limit 300 trims to the
newest ~3,350 ids. A keyword `BASE NOT a NOT "b c"` now parses to its base and
its banned terms: an id is on its list when the base would put it there AND its
title holds no banned term as whole word(s). Whole-word on purpose: dropping an
id LinkedIn would still return makes Bright Data bill it again, so the ban is the
strict side.
"""
import pandas as pd
import pytest

import scraper

# Curly double quotes, built from code points so the source stays ASCII.
_LQ, _RQ = chr(0x201C), chr(0x201D)


def _known(rows) -> pd.DataFrame:
    """A known-jobs frame as load_known_jobs returns it: (id, title, keywords)."""
    return pd.DataFrame(rows, columns=["job_posting_id", "job_title", "keywords"])


# --- parsing ---------------------------------------------------------------------

@pytest.mark.parametrize("keyword, base, banned", [
    ('"Software Engineer" NOT Senior NOT Sr', '"Software Engineer"', ["senior", "sr"]),
    ("Data Engineer NOT Senior", "Data Engineer", ["senior"]),
    ('"Data Engineer" NOT "Member of Technical Staff" NOT Lead', '"Data Engineer"',
     ["member of technical staff", "lead"]),
    ('("Data Scientist") NOT Principal', '"Data Scientist"', ["principal"]),
    ("(Data Scientist) NOT Principal", "Data Scientist", ["principal"]),
    ("  " + _LQ + "Quant" + _RQ + "  NOT  Sr.  ", _LQ + "Quant" + _RQ, ["sr"]),
    ("AI NOT Sales", "AI", ["sales"]),
])
def test_a_not_keyword_parses_to_its_base_and_banned_terms(keyword, base, banned):
    assert scraper._split_not_keyword(keyword) == (base, banned)


@pytest.mark.parametrize("keyword", [
    '"A" OR "B" NOT Senior',              # OR next to NOT: whole list, as today
    '("A" OR "B") NOT Senior',
    "Data AND Science NOT Senior",
    "(Data Scientist NOT Senior)",        # parens around more than the base
    "((Data Scientist)) NOT Senior",      # one wrapping pair only
    "Data NOT (Senior)",                  # a term is one word or one quoted phrase
    'Data NOT "Senior',                   # an unclosed quote
    "Data NOT OR",                        # an operator is not a term
    "Data NOT",                           # NOT with nothing after it
    "NOT Senior",                         # no base
    '"" NOT Senior',                      # a base that normalizes to nothing
    "Data NOT ---",                       # a term that normalizes to nothing
    "(Data Scientist)",                   # boolean, but no NOT
])
def test_any_other_boolean_keyword_does_not_parse(keyword):
    assert scraper._split_not_keyword(keyword) is None


@pytest.mark.parametrize("keyword", ['"Data Engineer"', "Data Engineer", "", None, 7,
                                     "data not senior"])
def test_a_plain_keyword_is_not_a_not_keyword(keyword):
    assert scraper._split_not_keyword(keyword) is None


def test_lower_case_not_is_a_plain_word():
    """LinkedIn's operator is capital NOT; "not" is one more word of the search."""
    assert scraper.title_matches("Data not Senior", "Senior Data Engineer, not remote")
    assert not scraper.title_matches("Data not Senior", "Data Engineer")


def test_or_with_not_still_gets_the_whole_list():
    known = _known([("a", "Senior Nurse", ""), ("b", "", "")])
    kw = '("A" OR "B") NOT Senior'
    assert scraper.exclude_ids_per_keyword(known, [kw])[kw] == ["a", "b"]
    assert scraper.title_matches(kw, "Senior Nurse")


# --- the banned whole-word rule ------------------------------------------------------

@pytest.mark.parametrize("keyword, title, matches", [
    ('"Software Engineer" NOT Lead', "Lead Software Engineer", False),
    ('"Software Engineer" NOT Lead', "Software Engineer, Leadership Team", True),
    ('"Software Engineer" NOT Sr', "Sr. Software Engineer", False),
    ('"Software Engineer" NOT Sr', "Software Engineer (SRE)", True),
    ('"Software Engineer" NOT Senior', "Software Engineer, Seniors Care", True),
    ('"Software Engineer" NOT Senior', "Senior Software Engineer", False),
    ('"Software Engineer" NOT Senior', "Software Engineer - SENIOR", False),
    ('"Software Engineer" NOT "Tech Lead"', "Software Engineer, Tech Lead", False),
    ('"Software Engineer" NOT "Tech Lead"', "Tech Software Engineer Lead", True),
    ('"Software Engineer" NOT Senior', "Data Analyst", False),     # base still applies
    ("Software Engineer NOT Senior", "Engineer, Software Platform", True),  # words rule
    ("Software Engineer NOT Senior", "Senior Engineer, Software", False),
])
def test_a_banned_term_rules_out_a_title_only_as_whole_words(keyword, title, matches):
    assert scraper.title_matches(keyword, title) is matches


KNOWN = _known([
    ("untitled", "", ""),                                # nothing proves it irrelevant
    ("plain", "Data Engineer", ""),
    ("senior", "Senior Data Engineer", ""),
    ("sr", "Sr. Data Engineer", ""),
    ("leadership", "Data Engineer, Leadership Programs", ""),
    ("seniors", "Data Engineer for Seniors Services", ""),
    ("recorded", "Platform Engineer", "data engineer"),          # LinkedIn's own match
    ("recorded_senior", "Senior Platform Engineer", "data engineer"),
    ("recorded_untitled", "", "data engineer"),
    ("other", "Nurse", "quant"),
])


def test_the_list_drops_only_whole_word_banned_titles():
    kw = '"Data Engineer" NOT Senior NOT Sr NOT Lead'
    assert scraper.exclude_ids_per_keyword(KNOWN, [kw])[kw] == [
        "untitled", "plain", "leadership", "seniors", "recorded", "recorded_untitled"]


def test_a_blank_title_id_stays_on_the_list():
    kw = "Data Engineer NOT Senior"
    assert "untitled" in scraper.exclude_ids_per_keyword(KNOWN, [kw])[kw]


def test_the_recorded_keyword_leg_minus_banned():
    """The ledger says a "Data Engineer" search returned these; the NOT search
    cannot return the one whose title is senior, so it is off that list."""
    kw = '"Data Engineer" NOT Senior'
    ids = scraper.exclude_ids_per_keyword(KNOWN, [kw])[kw]
    assert "recorded" in ids and "recorded_untitled" in ids
    assert "recorded_senior" not in ids
    assert "recorded_senior" in scraper.exclude_ids_per_keyword(
        KNOWN, ['"Data Engineer"'])['"Data Engineer"']


def test_a_not_keyword_and_its_base_are_separate_lists():
    lists = scraper.exclude_ids_per_keyword(
        KNOWN, ['"Data Engineer"', '"Data Engineer" NOT Senior'])
    assert "senior" in lists['"Data Engineer"']
    assert "senior" not in lists['"Data Engineer" NOT Senior']


def test_the_vector_match_agrees_with_title_matches():
    titles = ["Senior Data Engineer", "Sr. Data Engineer", "Data Engineer, Leadership",
              "Lead Data Engineer", "Data Engineer for Seniors", "", "Data Engineer",
              "Engineer, Data (Principal)", "Staff Data Engineer", "Quant"]
    keywords = ['"Data Engineer" NOT Senior NOT Sr NOT Lead',
                "Data Engineer NOT Principal", '("Data Engineer") NOT Staff',
                '"Data Engineer" NOT "Staff Data"', "Quant NOT Senior"]
    known = _known([(f"t{i}", t, "") for i, t in enumerate(titles)])
    lists = scraper.exclude_ids_per_keyword(known, keywords)
    for kw in keywords:
        expected = [f"t{i}" for i, t in enumerate(titles)
                    if t == "" or scraper.title_matches(kw, t)]
        assert lists[kw] == expected, kw


# --- the ledger records the base ------------------------------------------------------

def test_the_ledger_records_the_base_of_a_not_keyword():
    df = pd.DataFrame({
        "job_posting_id": ["1", "2", "3", "3"],
        "discovery_input.keyword": ['"Data Engineer" NOT Senior NOT Sr',
                                    "(Machine Learning) NOT Lead",
                                    '"A" OR "B" NOT Senior',
                                    '"Data Engineer"'],
    })
    assert scraper._keywords_by_id(df) == {
        "1": {"data engineer"},
        "2": {"machine learning"},
        "3": {"a or b not senior", "data engineer"},   # other booleans: as today
    }


def test_the_ledger_records_the_base_from_a_whole_input_cell_too():
    df = pd.DataFrame({"job_posting_id": ["1"],
                       "discovery_input": ["{'keyword': 'Quant NOT Senior'}"]})
    assert scraper._keywords_by_id(df) == {"1": {"quant"}}


def test_a_recorded_base_counts_for_the_not_search_and_back():
    known = _known([("by_not", "Platform Engineer", "data engineer"),
                    ("other", "Nurse", "quant")])
    lists = scraper.exclude_ids_per_keyword(
        known, ['"Data Engineer"', '"Data Engineer" NOT Senior'])
    assert lists['"Data Engineer"'] == ["by_not"]
    assert lists['"Data Engineer" NOT Senior'] == ["by_not"]
