"""The experience level picks the title words every search leaves out.

Bright Data's experience_level input does nothing: LinkedIn's logged-out search
ignores it (2026-10-14 A/B: Entry level, Director and no filter returned 24-25 of
the same 25 jobs). A NOT in the keyword does work, on titles:
`"Software Engineer" NOT Senior NOT Sr NOT Lead NOT Principal` came back with no
senior titles. So the level now decides which title words each keyword NOTs
(TITLE_EXCLUDE_BY_LEVEL, a user override in title_exclude_terms, a hand-edit map
in title_exclude_by_level), build_inputs sends the expanded keywords, and the
skip lists follow them.
"""
import asyncio
import json

import pandas as pd
import pytest

import scraper

ENTRY = ["Senior", "Sr", "Lead", "Principal"]
SE_NOT = '"Software Engineer" NOT Senior NOT Sr NOT Lead NOT Principal'


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Every scraper data path in tmp_path, no Drive master, no env overrides, and a
    fresh warn_once memory (it is per process)."""
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(scraper, "MASTER_CSV", tmp_path / "linkedin_jobs_master.csv")
    monkeypatch.setattr(scraper, "PREVIOUS_IDS_FILE", tmp_path / "last_run_job_ids.json")
    monkeypatch.setattr(scraper, "EXTERNAL_EXCLUDE_FILE", tmp_path / "external_exclude_ids.json")
    monkeypatch.setattr(scraper, "LEDGER_CSV", tmp_path / "collected_ids_ledger.csv")
    monkeypatch.setattr(scraper, "BLOCKLIST_FILE", tmp_path / "company_blocklist.txt")
    monkeypatch.setattr(scraper, "_WARNED", set())
    monkeypatch.delenv(scraper.EXTRA_MASTER_ENV, raising=False)
    monkeypatch.delenv(scraper.EXPECT_DRIVE_MASTER_ENV, raising=False)
    monkeypatch.delenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, raising=False)
    monkeypatch.delenv(scraper.MAX_EXCLUDE_IDS_ENV, raising=False)
    return tmp_path


def _config(sandbox, **values):
    (sandbox / "search_config.json").write_text(json.dumps(values), encoding="utf-8")


def _known(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["job_posting_id", "job_title", "keywords"])


def _terms(**cfg):
    """title_exclude_terms_for on a config built the way load_search_config builds it."""
    return scraper.title_exclude_terms_for(cfg)


def _days_ago(n: int) -> str:
    return (pd.Timestamp.now() - pd.Timedelta(days=n)).strftime("%Y-%m-%d")


# --- the built-in map ---------------------------------------------------------------

def test_the_built_in_map():
    m = scraper.TITLE_EXCLUDE_BY_LEVEL
    assert m["Internship"] == ENTRY and m["Entry level"] == ENTRY
    for level in ("Associate", "Mid-Senior level", "Director", "Executive"):
        assert m[level] == []
    assert all("Staff" not in v for v in m.values())       # Member of Technical Staff


# --- load_search_config -------------------------------------------------------------

def test_no_file_is_entry_level(sandbox):
    cfg = scraper.load_search_config()
    assert cfg["experience_levels"] == ["Entry level"]
    assert cfg["experience_level"] == "Entry level"
    assert cfg["title_exclude_terms"] is None
    assert cfg["title_exclude_by_level"] == {}


@pytest.mark.parametrize("raw, levels, sent", [
    ("Entry level", ["Entry level"], "Entry level"),
    (" Director ", ["Director"], "Director"),
    (["Entry level"], ["Entry level"], "Entry level"),
    (["Internship", "Entry level"], ["Internship", "Entry level"], ""),
    (["Entry level", 7, None, "", "  ", "Entry level"], ["Entry level"], "Entry level"),
    ([], [], ""),
    ("", [], ""),
    (None, ["Entry level"], "Entry level"),
])
def test_experience_level_is_a_string_or_a_list(sandbox, raw, levels, sent):
    _config(sandbox, experience_level=raw)
    cfg = scraper.load_search_config()
    assert cfg["experience_levels"] == levels
    assert cfg["experience_level"] == sent


def test_a_junk_experience_level_falls_back_to_entry_level(sandbox, capsys):
    _config(sandbox, experience_level={"x": 1})
    cfg = scraper.load_search_config()
    assert cfg["experience_levels"] == ["Entry level"]
    assert cfg["experience_level"] == "Entry level"


def test_the_raw_exclude_keys_are_passed_through(sandbox):
    _config(sandbox, title_exclude_terms="Senior, Lead",
            title_exclude_by_level={"Associate": ["Senior"]})
    cfg = scraper.load_search_config()
    assert cfg["title_exclude_terms"] == "Senior, Lead"
    assert cfg["title_exclude_by_level"] == {"Associate": ["Senior"]}


@pytest.mark.parametrize("junk", [["Senior"], "Senior", 3, None])
def test_a_junk_by_level_map_is_empty(sandbox, junk):
    _config(sandbox, title_exclude_by_level=junk)
    assert scraper.load_search_config()["title_exclude_by_level"] == {}


# --- the resolver -------------------------------------------------------------------

@pytest.mark.parametrize("auto", [None, "", "auto", "AUTO", " Auto "])
def test_auto_follows_the_level(auto):
    assert _terms(experience_levels=["Entry level"], title_exclude_terms=auto) == ENTRY


def test_absent_terms_are_auto():
    assert _terms(experience_levels=["Internship"]) == ENTRY


def test_auto_for_a_level_that_leaves_nothing_out():
    assert _terms(experience_levels=["Director"], title_exclude_terms="auto") == []


def test_an_unknown_level_leaves_nothing_out():
    assert _terms(experience_levels=["Wizard"]) == []
    assert _terms(experience_levels=["Entry level", "Wizard"]) == []


def test_no_levels_leaves_nothing_out():
    assert _terms(experience_levels=[]) == []


def test_several_levels_leave_out_only_what_every_one_does():
    assert _terms(experience_levels=["Internship", "Entry level"]) == ENTRY
    assert _terms(experience_levels=["Entry level", "Mid-Senior level"]) == []


def test_the_intersection_keeps_the_first_levels_order():
    by_level = {"Internship": ["Principal", "Lead", "Senior"]}
    assert _terms(experience_levels=["Entry level", "Internship"],
                  title_exclude_by_level=by_level) == ["Senior", "Lead", "Principal"]
    assert _terms(experience_levels=["Internship", "Entry level"],
                  title_exclude_by_level=by_level) == ["Principal", "Lead", "Senior"]


def test_the_by_level_map_is_merged_over_the_built_ins():
    by_level = {"Associate": ["Principal", "Director"], "Entry level": ["Senior", "Staff"]}
    assert _terms(experience_levels=["Associate"], title_exclude_by_level=by_level) == [
        "Principal", "Director"]
    assert _terms(experience_levels=["Entry level"], title_exclude_by_level=by_level) == [
        "Senior", "Staff"]
    # Internship is not in the map, so it keeps the built-in list
    assert _terms(experience_levels=["Internship"], title_exclude_by_level=by_level) == ENTRY
    # a level only the map knows
    assert _terms(experience_levels=["Wizard"],
                  title_exclude_by_level={"Wizard": "Archmage; Elder"}) == ["Archmage", "Elder"]


@pytest.mark.parametrize("off", ["none", "None", " NONE ", []])
def test_none_or_an_empty_list_leaves_nothing_out(off):
    assert _terms(experience_levels=["Entry level"], title_exclude_terms=off) == []


@pytest.mark.parametrize("value, terms", [
    (["Senior", " Staff ", "", "staff", "Tech Lead"], ["Senior", "Staff", "Tech Lead"]),
    ("Senior, Staff,,  Tech Lead ", ["Senior", "Staff", "Tech Lead"]),
    ("Senior; staff ;Staff", ["Senior", "staff"]),
    ("Senior,Lead;Principal", ["Senior", "Lead", "Principal"]),
])
def test_the_users_own_terms(value, terms):
    # the levels do not matter once the user names the words
    assert _terms(experience_levels=["Director"], title_exclude_terms=value) == terms


@pytest.mark.parametrize("junk", [7, {"a": 1}, True])
def test_a_junk_terms_value_falls_back_to_auto_with_one_warning(sandbox, capsys, junk):
    for _ in range(3):
        assert _terms(experience_levels=["Entry level"], title_exclude_terms=junk) == ENTRY
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("WARNING: ")]
    assert len(lines) == 1, out
    assert "title_exclude_terms" in lines[0]


def test_a_bare_config_still_resolves():
    """A caller's narrowed config (tests patch load_search_config) may lack the
    new keys; the resolver reads experience_level then."""
    assert _terms(experience_level="Entry level") == ENTRY
    assert _terms() == []


# --- with_title_excludes ------------------------------------------------------------

@pytest.mark.parametrize("keyword, terms, out", [
    ('"Software Engineer"', ENTRY, SE_NOT),
    ("Data Engineer", ["Senior"], "Data Engineer NOT Senior"),
    # the keyword already holds the word (whole word): a "Senior Data Engineer" search keeps Senior
    ('"Senior Data Engineer"', ENTRY, '"Senior Data Engineer" NOT Sr NOT Lead NOT Principal'),
    # a substring is not the word
    ('"Leadership Coach"', ["Lead"], '"Leadership Coach" NOT Lead'),
    # already NOTs it
    ('"Data Engineer" NOT Senior', ["Senior", "Sr"], '"Data Engineer" NOT Senior NOT Sr'),
    ('"Data Engineer" NOT sr.', ["Sr"], '"Data Engineer" NOT sr.'),
    # a phrase term is quoted
    ('"Data Engineer"', ["Tech Lead", "Sr"], '"Data Engineer" NOT "Tech Lead" NOT Sr'),
    ('"Data Engineer"', ["  Tech   Lead "], '"Data Engineer" NOT "Tech Lead"'),
    # quotes and parentheses in a term are stripped; one that is left empty is dropped
    ('"Data Engineer"', ['"Senior"', "(Lead)", '""', "()", "---"],
     '"Data Engineer" NOT Senior NOT Lead'),
    # boolean words are not terms
    ('"Data Engineer"', ["OR", "and", "Not", "Senior"], '"Data Engineer" NOT Senior'),
    # one spelling of a word once
    ('"Data Engineer"', ["Sr", "Sr.", "SR"], '"Data Engineer" NOT Sr'),
    # an OR / AND keyword is wrapped once, and stays whole-list
    ('"A" OR "B"', ["Senior"], '("A" OR "B") NOT Senior'),
    ("Data AND Science", ["Senior"], "(Data AND Science) NOT Senior"),
    ('("A" OR "B")', ["Senior"], '("A" OR "B") NOT Senior'),
    ('(Data Scientist)', ["Senior"], '(Data Scientist) NOT Senior'),
    ('"ML" OR ("AI" AND "LLM")', ["Lead"], '("ML" OR ("AI" AND "LLM")) NOT Lead'),
    # OR inside quotes is not an operator
    ('"Research OR Science"', ["Senior"], '"Research OR Science" NOT Senior'),
])
def test_with_title_excludes(keyword, terms, out):
    assert scraper.with_title_excludes(keyword, terms) == out


@pytest.mark.parametrize("keyword", ["", "   ", None, 7, ["a"]])
def test_a_blank_or_odd_keyword_is_unchanged(keyword):
    assert scraper.with_title_excludes(keyword, ENTRY) == keyword


@pytest.mark.parametrize("terms", [[], ["", "  ", "OR"]])
def test_no_usable_terms_leave_the_keyword_unchanged(terms):
    assert scraper.with_title_excludes('"Data Engineer"', terms) == '"Data Engineer"'


def test_an_unclosed_quote_is_left_alone():
    """Appending inside an open quote would turn the NOTs into words of a phrase."""
    assert scraper.with_title_excludes('"Data Engineer', ENTRY) == '"Data Engineer'


@pytest.mark.parametrize("keyword", ['"Software Engineer"', "Data Engineer",
                                     '"Data Engineer" NOT Manager', "(Data Scientist)",
                                     "\u201cQuant\u201d"])
def test_a_plain_keyword_round_trips_through_the_not_parser(keyword):
    expanded = scraper.with_title_excludes(keyword, ["Senior", "Tech Lead", "Sr."])
    split = scraper._split_not_keyword(expanded)
    assert split is not None, expanded
    base, banned = split
    assert banned[-3:] == ["senior", "tech lead", "sr"]


def test_an_expanded_or_keyword_stays_whole_list():
    expanded = scraper.with_title_excludes('"A" OR "B"', ENTRY)
    assert scraper._split_not_keyword(expanded) is None
    assert scraper._keyword_rule(expanded) is None


# --- build_inputs -------------------------------------------------------------------

SE_KNOWN = _known([
    ("se1", "Software Engineer", ""),
    ("senior", "Senior Software Engineer", ""),
    ("sr", "Sr. Software Engineer", ""),
    ("lead", "Lead Software Engineer", ""),
    ("principal", "Principal Software Engineer", ""),
    ("leadership", "Software Engineer, Leadership Team", ""),
    ("untitled", "", ""),
    ("nurse", "Nurse", ""),
])


def test_build_inputs_sends_the_not_keyword_and_its_skip_list(sandbox):
    _config(sandbox, keywords=['"Software Engineer"'], remote_types=[],
            experience_level="Entry level", limit_per_input=300)
    (inp,) = scraper.build_inputs(SE_KNOWN)
    assert inp["keyword"] == SE_NOT
    assert inp["experience_level"] == "Entry level"
    assert inp["jobs_to_not_include"] == ["se1", "leadership", "untitled"]


def test_none_sends_the_plain_keyword_and_the_whole_title_list(sandbox):
    _config(sandbox, keywords=['"Software Engineer"'], remote_types=[],
            experience_level="Entry level", title_exclude_terms="none", limit_per_input=300)
    (inp,) = scraper.build_inputs(SE_KNOWN)
    assert inp["keyword"] == '"Software Engineer"'
    assert inp["jobs_to_not_include"] == ["se1", "senior", "sr", "lead", "principal",
                                          "leadership", "untitled"]


def test_several_levels_send_no_experience_filter(sandbox):
    _config(sandbox, keywords=['"Software Engineer"'], remote_types=[],
            experience_level=["Internship", "Entry level"], limit_per_input=300)
    (inp,) = scraper.build_inputs(SE_KNOWN)
    assert inp["experience_level"] == ""
    assert inp["keyword"] == SE_NOT


def test_entry_plus_mid_senior_leaves_nothing_out(sandbox):
    _config(sandbox, keywords=['"Software Engineer"'], remote_types=[],
            experience_level=["Entry level", "Mid-Senior level"], limit_per_input=300)
    (inp,) = scraper.build_inputs(SE_KNOWN)
    assert inp["experience_level"] == ""
    assert inp["keyword"] == '"Software Engineer"'


def test_the_vm_with_no_config_file_sends_the_not_keywords(sandbox):
    assert not (sandbox / "search_config.json").exists()
    inputs = scraper.build_inputs(SE_KNOWN)
    assert {i["experience_level"] for i in inputs} == {"Entry level"}
    sent = [i["keyword"] for i in inputs]
    assert SE_NOT in sent
    assert all(" NOT Senior NOT Sr NOT Lead NOT Principal" in kw for kw in sent)
    (se,) = {json.dumps(i["jobs_to_not_include"]) for i in inputs if i["keyword"] == SE_NOT}
    assert json.loads(se) == ["se1", "leadership", "untitled"]


def test_the_plain_list_path_expands_too(sandbox):
    _config(sandbox, keywords=['"Software Engineer"'], remote_types=["Hybrid", "On-site"])
    inputs = scraper.build_inputs(["1", "2"])
    assert [i["keyword"] for i in inputs] == [SE_NOT, SE_NOT]
    assert all(i["jobs_to_not_include"] == ["1", "2"] for i in inputs)


def test_two_keywords_that_expand_alike_are_one_search(sandbox):
    _config(sandbox, keywords=['"Software Engineer" NOT Senior', '"Software Engineer"',
                               '"Data Scientist"'],
            remote_types=["Hybrid", "On-site"], limit_per_input=300)
    inputs = scraper.build_inputs(SE_KNOWN)
    assert [(i["keyword"], i["remote"]) for i in inputs] == [
        (SE_NOT, "Hybrid"), (SE_NOT, "On-site"),
        ('"Data Scientist" NOT Senior NOT Sr NOT Lead NOT Principal', "Hybrid"),
        ('"Data Scientist" NOT Senior NOT Sr NOT Lead NOT Principal', "On-site")]
    assert inputs[0]["jobs_to_not_include"] == ["se1", "leadership", "untitled"]


def test_max_keywords_caps_before_the_expansion(sandbox):
    _config(sandbox, keywords=['"Software Engineer"', '"Data Scientist"'], remote_types=[])
    inputs = scraper.build_inputs(SE_KNOWN, max_keywords=1)
    assert [i["keyword"] for i in inputs] == [SE_NOT]


def test_an_odd_keyword_still_does_not_raise(sandbox):
    _config(sandbox, keywords=[7, '"Quant"'], remote_types=[], limit_per_input=300)
    inputs = scraper.build_inputs(SE_KNOWN)
    assert [i["keyword"] for i in inputs] == [
        7, '"Quant" NOT Senior NOT Sr NOT Lead NOT Principal']


# --- the run-log line ---------------------------------------------------------------

def _run_main(sandbox, monkeypatch, capsys) -> tuple[list[dict], str]:
    label = scraper.RUN_LABELS[0]
    pd.DataFrame([("se1", _days_ago(1), "Software Engineer")],
                 columns=["job_posting_id", "extracted_date", "job_title"]).to_csv(
        sandbox / "linkedin_jobs_master.csv", index=False)
    monkeypatch.setattr(scraper, "require_credentials", lambda: None)
    monkeypatch.setattr(scraper, "append_to_master", lambda df: len(df))

    async def no_preflight(session):
        return None

    sent = []

    async def fake_trigger(session, payload, limit_per_input=None):
        sent.append(payload)
        return scraper.Collection(None, [])

    monkeypatch.setattr(scraper, "preflight", no_preflight)
    monkeypatch.setattr(scraper, "trigger", fake_trigger)
    asyncio.run(scraper.main(run_label=label))
    return sent[0]["input"], capsys.readouterr().out


def _exclude_lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if "leaves out job titles with" in ln]


def test_main_says_once_which_title_words_it_leaves_out(sandbox, monkeypatch, capsys):
    _config(sandbox, keywords=['"Software Engineer"', '"Data Scientist"'], remote_types=[],
            experience_level="Entry level", limit_per_input=300)
    inputs, out = _run_main(sandbox, monkeypatch, capsys)
    assert inputs[0]["keyword"] == SE_NOT
    (line,) = _exclude_lines(out)
    assert line == ("Experience level Entry level: every search leaves out job titles "
                    "with Senior, Sr, Lead or Principal (change this in Settings > "
                    "Leave out job titles with).")
    assert "WARNING" not in out


def test_main_names_the_users_own_words(sandbox, monkeypatch, capsys):
    _config(sandbox, keywords=['"Software Engineer"'], remote_types=[],
            title_exclude_terms="Senior; Tech Lead", limit_per_input=300)
    _, out = _run_main(sandbox, monkeypatch, capsys)
    (line,) = _exclude_lines(out)
    assert line == ("Every search leaves out job titles with Senior or Tech Lead "
                    "(change this in Settings > Leave out job titles with).")


def test_main_names_several_levels(sandbox, monkeypatch, capsys):
    _config(sandbox, keywords=['"Software Engineer"'], remote_types=[],
            experience_level=["Internship", "Entry level"], limit_per_input=300)
    _, out = _run_main(sandbox, monkeypatch, capsys)
    (line,) = _exclude_lines(out)
    assert line.startswith("Experience levels Internship and Entry level: every search "
                           "leaves out job titles with Senior, Sr, Lead or Principal")


@pytest.mark.parametrize("values", [{"title_exclude_terms": "none"},
                                    {"experience_level": "Director"},
                                    {"experience_level": ["Entry level", "Mid-Senior level"]}])
def test_main_says_nothing_when_nothing_is_left_out(sandbox, monkeypatch, capsys, values):
    _config(sandbox, keywords=['"Software Engineer"'], remote_types=[],
            limit_per_input=300, **values)
    inputs, out = _run_main(sandbox, monkeypatch, capsys)
    assert inputs[0]["keyword"] == '"Software Engineer"'
    assert _exclude_lines(out) == []
    assert "NOT" not in out
