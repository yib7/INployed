"""A skip list per search keyword.

Bright Data copies a search's jobs_to_not_include into each of its up to
limit_per_input child jobs, so the byte budget (MAX_EXCLUDE_PAYLOAD_BYTES) is per
input: ids x width x limit. One shared list fit only ~3,350 ids at limit 300
while the 30-day window holds ~20,000. With selective_search on, a search only
returns jobs whose TITLE contains its keyword, so each search only needs the
known jobs whose title matches its keyword (plus the ones the ledger says that
search returned before). Measured 2026-10-13: 130 to 3,060 ids per keyword,
except "Software Engineer" at 4,714.
"""
import asyncio
import json
import time

import pandas as pd
import pytest

import scraper


def _days_ago(n: int) -> str:
    return (pd.Timestamp.now() - pd.Timedelta(days=n)).strftime("%Y-%m-%d")


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Every scraper data path in tmp_path, no Drive master, no env overrides."""
    monkeypatch.setattr(scraper, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(scraper, "MASTER_CSV", tmp_path / "linkedin_jobs_master.csv")
    monkeypatch.setattr(scraper, "PREVIOUS_IDS_FILE", tmp_path / "last_run_job_ids.json")
    monkeypatch.setattr(scraper, "EXTERNAL_EXCLUDE_FILE", tmp_path / "external_exclude_ids.json")
    monkeypatch.setattr(scraper, "LEDGER_CSV", tmp_path / "collected_ids_ledger.csv")
    monkeypatch.setattr(scraper, "BLOCKLIST_FILE", tmp_path / "company_blocklist.txt")
    monkeypatch.delenv(scraper.EXTRA_MASTER_ENV, raising=False)
    monkeypatch.delenv(scraper.EXPECT_DRIVE_MASTER_ENV, raising=False)
    monkeypatch.delenv(scraper.EXCLUDE_WINDOW_DAYS_ENV, raising=False)
    monkeypatch.delenv(scraper.MAX_EXCLUDE_IDS_ENV, raising=False)
    return tmp_path


def _config(sandbox, **values):
    (sandbox / "search_config.json").write_text(json.dumps(values), encoding="utf-8")


def _csv(path, columns, rows):
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)
    return path


def _master(sandbox, rows):
    """rows: (id, extracted_date, job_title)."""
    return _csv(sandbox / "linkedin_jobs_master.csv",
                ["job_posting_id", "extracted_date", "job_title"], rows)


def _ledger(sandbox, rows):
    """rows: (id, collected_date, job_title, keywords)."""
    return _csv(sandbox / "collected_ids_ledger.csv",
                ["job_posting_id", "collected_date", "job_title", "keywords"], rows)


def _known(rows) -> pd.DataFrame:
    """A known-jobs frame as load_known_jobs returns it: (id, title, keywords)
    rows, oldest first."""
    return pd.DataFrame(rows, columns=["job_posting_id", "job_title", "keywords"])


def _problem_lines(out: str, kind: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.startswith(f"{kind}: ")]


# --- the title match rule ------------------------------------------------------

@pytest.mark.parametrize("keyword, title", [
    ('"Quant"', "Quantitative Researcher"),           # substring, not a whole word
    ('"LLM"', "Engineer, LLMs and Agents"),
    ('"GenAI"', "Gen AI Engineer"),                     # spaces removed on both sides
    ('"Gen AI"', "GenAI Platform Developer"),
    ('"Gen AI"', "Gen-AI Engineer"),                    # punctuation is a space
    ("Gen-AI", "gen ai engineer"),
    ('"Software Engineer"', "Senior SOFTWARE ENGINEER II"),   # case
    ('"Data Scientist"', "Data Scientist (Remote)"),
    ("Software Engineer", "Software-Engineer, Backend"),
    ('"AI Engineer"', "Applied AI Engineer"),
])
def test_a_title_holding_the_keyword_matches(keyword, title):
    assert scraper.title_matches(keyword, title)


@pytest.mark.parametrize("keyword, title", [
    ('"Data Scientist"', "Data Engineer"),
    ('"Quant"', "Software Engineer"),
    ('"Software Engineer"', "Software Developer"),
    ('"LLM"', ""),
    ('"LLM"', None),
    ('"LLM"', float("nan")),
])
def test_a_title_without_the_keyword_does_not_match(keyword, title):
    assert not scraper.title_matches(keyword, title)


@pytest.mark.parametrize("keyword", ["", "   ", '""', None])
def test_a_blank_keyword_restricts_nothing(keyword):
    """A search with no keyword is not title-restricted, so every title can come back."""
    assert scraper.title_matches(keyword, "Anything At All")


# --- the loader -----------------------------------------------------------------

def test_load_exclude_ids_is_unchanged_by_the_titles(sandbox, tmp_path, monkeypatch):
    """Same sources, same window, same order: oldest first by the newest date."""
    _master(sandbox, [("a", _days_ago(10), "Data Scientist"),
                      ("b", _days_ago(5), "Software Engineer"),
                      ("c", _days_ago(7), "Quant Analyst"),
                      ("stale", _days_ago(400), "Data Analyst")])
    _ledger(sandbox, [("a", _days_ago(1), "", "data scientist")])
    (sandbox / "external_exclude_ids.json").write_text(json.dumps(["ext"]), encoding="utf-8")
    drive = _csv(tmp_path / "drive.csv", ["job_posting_id", "extracted_date", "job_title"],
                 [("vm", _days_ago(3), "LLM Engineer")])
    monkeypatch.setenv(scraper.EXTRA_MASTER_ENV, str(drive))
    assert scraper.load_exclude_ids() == ["ext", "c", "b", "vm", "a"]
    known = scraper.load_known_jobs()
    assert known["job_posting_id"].tolist() == ["ext", "c", "b", "vm", "a"]


def test_titles_come_from_every_source_and_keywords_from_the_ledger(
        sandbox, tmp_path, monkeypatch):
    _master(sandbox, [("own", _days_ago(4), "Data Scientist"),
                      ("both", _days_ago(4), "Old Master Title"),
                      ("led", _days_ago(4), None)])
    _ledger(sandbox, [("both", _days_ago(2), "Ledger Title", "quant"),
                      ("led", _days_ago(2), "LLM Engineer", "gen ai|llm")])
    drive = _csv(tmp_path / "drive.csv", ["job_posting_id", "extracted_date", "job_title"],
                 [("vm", _days_ago(3), "Quant Researcher")])
    monkeypatch.setenv(scraper.EXTRA_MASTER_ENV, str(drive))
    known = scraper.load_known_jobs().set_index("job_posting_id")
    assert known.loc["own", "job_title"] == "Data Scientist"
    assert known.loc["both", "job_title"] == "Ledger Title"       # the ledger's is newest
    assert known.loc["led", "job_title"] == "LLM Engineer"
    assert known.loc["vm", "job_title"] == "Quant Researcher"     # the Drive master's
    assert known.loc["both", "keywords"] == "quant"
    assert known.loc["led", "keywords"] == "gen ai|llm"
    assert known.loc["own", "keywords"] == ""
    assert known.loc["vm", "keywords"] == ""


def test_an_id_no_source_titles_has_an_empty_title(sandbox):
    (sandbox / "external_exclude_ids.json").write_text(json.dumps(["ext"]), encoding="utf-8")
    known = scraper.load_known_jobs()
    assert known.to_dict("records") == [
        {"job_posting_id": "ext", "job_title": "", "keywords": ""}]


def test_no_known_jobs_is_an_empty_frame(sandbox):
    known = scraper.load_known_jobs()
    assert known.empty
    assert list(known.columns) == ["job_posting_id", "job_title", "keywords"]


# --- assembling the lists ---------------------------------------------------------

KNOWN = _known([
    ("untitled", "", ""),                         # cannot prove it irrelevant: every list
    ("ds", "Senior Data Scientist", ""),
    ("quant", "Quantitative Analyst", ""),
    ("recorded", "Platform Engineer", "data scientist"),   # LinkedIn's own match
    ("llm", "LLMs Engineer", ""),
    ("genai", "Gen AI Developer", ""),
    ("ds2", "Data Scientist II", ""),
])


def test_each_keyword_gets_the_ids_its_search_can_return():
    lists = scraper.exclude_ids_per_keyword(
        KNOWN, ['"Data Scientist"', '"Quant"', '"LLM"', '"GenAI"', '"Business Analyst"'])
    assert lists['"Data Scientist"'] == ["untitled", "ds", "recorded", "ds2"]
    assert lists['"Quant"'] == ["untitled", "quant"]
    assert lists['"LLM"'] == ["untitled", "llm"]
    assert lists['"GenAI"'] == ["untitled", "genai"]
    assert lists['"Business Analyst"'] == ["untitled"]


def test_a_recorded_keyword_puts_the_id_on_the_list_whatever_its_title():
    lists = scraper.exclude_ids_per_keyword(KNOWN, ["Data Scientist"])
    assert "recorded" in lists["Data Scientist"]


def test_an_untitled_unkeyworded_id_is_on_every_list():
    lists = scraper.exclude_ids_per_keyword(KNOWN, ['"Quant"', '"LLM"', '"Nothing"'])
    assert all("untitled" in ids for ids in lists.values())


def test_an_id_is_not_on_an_unrelated_keywords_list():
    lists = scraper.exclude_ids_per_keyword(KNOWN, ['"Quant"'])
    assert "ds" not in lists['"Quant"'] and "recorded" not in lists['"Quant"']


def test_a_blank_keyword_gets_the_whole_list():
    lists = scraper.exclude_ids_per_keyword(KNOWN, [""])
    assert lists[""] == KNOWN["job_posting_id"].tolist()


def test_lists_keep_the_global_oldest_first_order():
    known = _known([(f"id{n}", "Software Engineer" if n % 2 else "Data Analyst", "")
                    for n in range(50)])
    lists = scraper.exclude_ids_per_keyword(known, ['"Software Engineer"'])
    assert lists['"Software Engineer"'] == [f"id{n}" for n in range(50) if n % 2]


def test_the_vector_match_agrees_with_title_matches():
    titles = ["Quantitative Researcher", "Gen-AI Engineer", "GenAI Lead", "LLMs",
              "Data Scientist", "Software Engineer II", "", "Applied A.I. Engineer",
              "Business Analyst (Contract)", "Analytics Engineer"]
    keywords = ['"Quant"', '"Gen AI"', '"GenAI"', '"LLM"', '"Data Scientist"',
                '"Software Engineer"', '"Applied AI"', '"Analytics Engineer"', '"AI"']
    known = _known([(f"t{i}", t, "") for i, t in enumerate(titles)])
    lists = scraper.exclude_ids_per_keyword(known, keywords)
    for kw in keywords:
        expected = [f"t{i}" for i, t in enumerate(titles)
                    if t == "" or scraper.title_matches(kw, t)]
        assert lists[kw] == expected, kw


def test_assembly_is_fast_on_a_real_sized_window():
    """~20,000 known ids x ~20 keywords, the 2026-10-13 shape."""
    words = ["Data Scientist", "Software Engineer", "Quant Analyst", "LLM Engineer",
             "Business Analyst", "Gen AI Developer", "Data Engineer", "AI Engineer"]
    known = _known([(str(4_400_000_000 + n), f"Senior {words[n % len(words)]} {n}",
                     "software engineer" if n % 7 == 0 else "")
                    for n in range(20_000)])
    start = time.perf_counter()
    lists = scraper.exclude_ids_per_keyword(known, scraper.KEYWORDS)
    assert time.perf_counter() - start < 2.0
    assert len(lists) == len(scraper.KEYWORDS)


# --- build_inputs with the per-keyword lists ------------------------------------

def _ids(start, n):
    return [str(4_400_000_000 + start + i) for i in range(n)]


def _big_known():
    """5,000 Software Engineer jobs (more than limit 300 fits), 200 Data Scientist."""
    se = [(i, "Software Engineer", "") for i in _ids(0, 5_000)]
    ds = [(i, "Data Scientist", "") for i in _ids(10_000, 200)]
    # interleave so the order is shared, Software Engineer newest last
    return _known(ds[:100] + se + ds[100:])


def test_every_input_fits_the_byte_budget(sandbox):
    _config(sandbox, keywords=['"Software Engineer"', '"Data Scientist"'],
            remote_types=["Hybrid", "On-site"], limit_per_input=300)
    for inp in scraper.build_inputs(_big_known()):
        size = len(json.dumps(inp["jobs_to_not_include"]))
        assert size * 300 <= scraper.MAX_EXCLUDE_PAYLOAD_BYTES


def test_a_trimmed_keyword_keeps_its_newest_and_others_are_whole(sandbox):
    _config(sandbox, keywords=['"Software Engineer"', '"Data Scientist"'],
            remote_types=[], limit_per_input=300, title_exclude_terms="none")
    known = _big_known()
    inputs = {i["keyword"]: i["jobs_to_not_include"] for i in scraper.build_inputs(known)}
    se = inputs['"Software Engineer"']
    all_se = _ids(0, 5_000)
    assert 3_000 < len(se) < 5_000
    assert se == all_se[-len(se):]                    # the newest kept
    assert inputs['"Data Scientist"'] == _ids(10_000, 200)


def test_a_trim_is_one_info_line_and_never_a_warning(sandbox, capsys):
    _config(sandbox, keywords=['"Software Engineer"', '"Data Scientist"', '"Quant"'],
            remote_types=[], limit_per_input=300)
    scraper.build_inputs(_big_known())
    out = capsys.readouterr().out
    assert "WARNING" not in out
    lines = [ln for ln in out.splitlines() if "skip list capped" in ln]
    assert len(lines) == 1, out
    line = lines[0]
    assert "1 of 3" in line
    assert '"Software Engineer"' in line and "of 5,000" in line
    assert "billed again" in line
    assert "limit_per_input=300" in line and "search_config.json" in line


def test_a_trim_the_cap_forced_names_max_exclude_ids(sandbox, capsys):
    _config(sandbox, keywords=['"Software Engineer"'], remote_types=[],
            limit_per_input=100, max_exclude_ids=1_000)
    kept = scraper.build_inputs(_big_known())[0]["jobs_to_not_include"]
    assert len(kept) == 1_000
    out = capsys.readouterr().out
    assert "WARNING" not in out
    assert "max_exclude_ids in search_config.json" in out


def test_nothing_trimmed_prints_no_cap_line(sandbox, capsys):
    _config(sandbox, keywords=['"Data Scientist"'], remote_types=[], limit_per_input=300)
    scraper.build_inputs(_big_known())
    assert "skip list capped" not in capsys.readouterr().out


def test_below_the_floor_warns_once_per_run(sandbox, capsys):
    _config(sandbox, keywords=['"Software Engineer"', '"Data Scientist"', '"Quant"'],
            remote_types=["Hybrid", "On-site"], limit_per_input=30_000)
    known = _known([(i, "Software Engineer Data Scientist Quant", "")
                    for i in _ids(0, 500)])
    inputs = scraper.build_inputs(known)
    out = capsys.readouterr().out
    assert len(_problem_lines(out, "WARNING")) == 1, out
    assert "limit_per_input" in out
    for inp in inputs:
        assert 0 < len(inp["jobs_to_not_include"]) < scraper.MIN_EXCLUDE_IDS
        assert (len(json.dumps(inp["jobs_to_not_include"])) * 30_000
                <= scraper.MAX_EXCLUDE_PAYLOAD_BYTES)


def test_split_remote_types_share_the_keywords_list(sandbox):
    _config(sandbox, keywords=['"Software Engineer"', '"Data Scientist"'],
            remote_types=["Hybrid", "On-site"], limit_per_input=300,
            title_exclude_terms="none")
    inputs = scraper.build_inputs(_big_known())
    assert [(i["keyword"], i["remote"]) for i in inputs] == [
        ('"Software Engineer"', "Hybrid"), ('"Software Engineer"', "On-site"),
        ('"Data Scientist"', "Hybrid"), ('"Data Scientist"', "On-site")]
    assert inputs[0]["jobs_to_not_include"] == inputs[1]["jobs_to_not_include"]
    assert inputs[2]["jobs_to_not_include"] == inputs[3]["jobs_to_not_include"]
    assert inputs[0]["jobs_to_not_include"] != inputs[2]["jobs_to_not_include"]


def test_max_keywords_still_caps_the_searches(sandbox):
    _config(sandbox, keywords=['"Software Engineer"', '"Data Scientist"', '"Quant"'],
            remote_types=[], limit_per_input=300, title_exclude_terms="none")
    inputs = scraper.build_inputs(_big_known(), max_keywords=1)
    assert [i["keyword"] for i in inputs] == ['"Software Engineer"']


def test_a_mixed_width_tail_still_fits_the_budget(sandbox):
    """Newer ids are larger; the tail kept can be wider than the list's average."""
    _config(sandbox, keywords=['"Software Engineer"'], remote_types=[], limit_per_input=300)
    narrow = [(str(1_000 + n), "Software Engineer", "") for n in range(3_000)]
    wide = [(str(44_000_000_000_000 + n), "Software Engineer", "") for n in range(4_000)]
    kept = scraper.build_inputs(_known(narrow + wide))[0]["jobs_to_not_include"]
    assert len(json.dumps(kept)) * 300 <= scraper.MAX_EXCLUDE_PAYLOAD_BYTES


def test_a_plain_list_is_still_one_list_for_every_search(sandbox):
    _config(sandbox, keywords=['"Software Engineer"', '"Data Scientist"'],
            remote_types=["Hybrid", "On-site"], limit_per_input=100)
    ids = _ids(0, 40)
    inputs = scraper.build_inputs(ids)
    assert len(inputs) == 4
    assert all(i["jobs_to_not_include"] == ids for i in inputs)


def test_cap_exclude_ids_keeps_its_message(capsys):
    ids = _ids(0, 12_000)
    assert len(scraper.cap_exclude_ids(ids, 100)) == 10_000
    out = capsys.readouterr().out
    assert "skip list capped at 10000 of 12000 known jobs" in out
    assert "WARNING" not in out


# --- main() ---------------------------------------------------------------------

def test_main_sends_each_search_its_own_list_and_says_so(sandbox, monkeypatch, capsys):
    label = scraper.RUN_LABELS[0]
    _config(sandbox, keywords=['"Data Scientist"', '"Quant"'], remote_types=[],
            limit_per_input=300, exclude_window_days=30, title_exclude_terms="none")
    _master(sandbox, [("ds", _days_ago(3), "Data Scientist"),
                      ("q1", _days_ago(2), "Quant Researcher"),
                      ("q2", _days_ago(1), "Quantitative Analyst"),
                      ("other", _days_ago(1), "Nurse")])
    monkeypatch.setattr(scraper, "require_credentials", lambda: None)
    monkeypatch.setattr(scraper, "append_to_master", lambda df: len(df))

    async def no_preflight(session):
        return None

    monkeypatch.setattr(scraper, "preflight", no_preflight)
    sent = []

    async def fake_trigger(session, payload, limit_per_input=None):
        sent.append(payload)
        return scraper.Collection(None, [])

    monkeypatch.setattr(scraper, "trigger", fake_trigger)
    asyncio.run(scraper.main(run_label=label))
    inputs = {i["keyword"]: i["jobs_to_not_include"] for i in sent[0]["input"]}
    assert inputs == {'"Data Scientist"': ["ds"], '"Quant"': ["q1", "q2"]}
    out = capsys.readouterr().out
    run_lines = [ln for ln in out.splitlines() if ln.startswith(f"Run: {label} | ")]
    assert len(run_lines) == 1, out
    assert "4 known jobs from the last 30 days" in run_lines[0]
    assert "1 to 2 ids per search" in run_lines[0]
    assert "WARNING" not in out


def test_no_known_jobs_gives_every_search_an_empty_list(sandbox):
    _config(sandbox, keywords=['"Data Scientist"', '"Quant"'], remote_types=[],
            limit_per_input=300)
    inputs = scraper.build_inputs(scraper.load_known_jobs())
    assert [i["jobs_to_not_include"] for i in inputs] == [[], []]
    assert scraper._skip_list_summary("x", 0, inputs).startswith(
        "Run: x | no known jobs from the last ")


def test_the_cap_fits_exactly_the_most_ids_the_budget_allows():
    """2026-10-14 real-data check: 4,098 "Software Engineer" ids at limit 300 kept
    3,133 (13.16 MB) when ~3,357 fit under 14.1 MB, because the width estimate was
    rounded up. The size is exact now: the largest k that fits, and k + 1 would not."""
    ids = _ids(0, 4_098)
    kept = scraper.cap_exclude_ids(ids, 300)
    k = len(kept)
    assert kept == ids[-k:]
    assert len(json.dumps(ids[-k:])) * 300 <= scraper.MAX_EXCLUDE_PAYLOAD_BYTES
    assert len(json.dumps(ids[-(k + 1):])) * 300 > scraper.MAX_EXCLUDE_PAYLOAD_BYTES


def test_a_wide_tail_is_sized_exactly_too(sandbox):
    """Mixed widths: the kept tail is the largest that fits as sent."""
    narrow = [str(1_000 + n) for n in range(3_000)]
    wide = [str(44_000_000_000_000 + n) for n in range(4_000)]
    ids = narrow + wide
    k = len(scraper.cap_exclude_ids(ids, 300))
    assert len(json.dumps(ids[-k:])) * 300 <= scraper.MAX_EXCLUDE_PAYLOAD_BYTES
    assert len(json.dumps(ids[-(k + 1):])) * 300 > scraper.MAX_EXCLUDE_PAYLOAD_BYTES


# --- unquoted, boolean and odd keywords (2026-10-14 review) ----------------------

def test_an_unquoted_phrase_matches_its_words_in_any_order():
    """Settings says quotes make a phrase, so an unquoted keyword is separate words."""
    assert scraper.title_matches("Data Scientist", "Scientist, Data Platform")
    assert scraper.title_matches("data  scientist", "Lead Scientist - Big Data")


def test_a_quoted_phrase_still_needs_the_phrase():
    assert not scraper.title_matches('"Data Scientist"', "Scientist, Data Platform")
    assert not scraper.title_matches("\u201cData Scientist\u201d", "Scientist, Data Platform")


def test_an_unquoted_phrase_needs_every_word():
    assert not scraper.title_matches("Data Scientist", "Data Engineer")


def test_the_lists_follow_the_unquoted_rule():
    known = _known([("phrase", "Data Scientist", ""), ("words", "Scientist, Data Platform", ""),
                    ("one", "Data Engineer", "")])
    lists = scraper.exclude_ids_per_keyword(known, ["Data Scientist", '"Data Scientist"'])
    assert lists["Data Scientist"] == ["phrase", "words"]
    assert lists['"Data Scientist"'] == ["phrase"]


@pytest.mark.parametrize("keyword", ['"A" OR "B"', "Data AND Science", "AI NOT (Sales)",
                                     "(Data Scientist)", '"ML" OR ("AI" AND "LLM")'])
def test_a_boolean_keyword_gets_the_whole_list(keyword):
    lists = scraper.exclude_ids_per_keyword(KNOWN, [keyword])
    assert lists[keyword] == KNOWN["job_posting_id"].tolist()
    assert scraper.title_matches(keyword, "Nurse")


def test_lowercase_or_inside_a_word_is_not_boolean():
    """LinkedIn's operators are capitals and whole words; "Oracle" or "or" are not."""
    lists = scraper.exclude_ids_per_keyword(KNOWN, ['"Oracle"', "data or quant"])
    assert lists['"Oracle"'] == ["untitled"]
    assert len(lists["data or quant"]) < len(KNOWN)


@pytest.mark.parametrize("keyword", [["Data", "Scientist"], 7, None, {"k": "v"}])
def test_an_odd_keyword_does_not_raise_and_gets_the_whole_list(sandbox, keyword):
    _config(sandbox, keywords=[keyword, '"Quant"'], remote_types=["Hybrid", "On-site"],
            limit_per_input=300, title_exclude_terms="none")
    inputs = scraper.build_inputs(KNOWN)
    assert [i["keyword"] for i in inputs] == [keyword, keyword, '"Quant"', '"Quant"']
    assert inputs[0]["jobs_to_not_include"] == KNOWN["job_posting_id"].tolist()
    assert inputs[2]["jobs_to_not_include"] == ["untitled", "quant"]


def test_an_odd_keyword_with_a_plain_list_does_not_raise(sandbox):
    _config(sandbox, keywords=[["a", "b"], "x"], remote_types=[], limit_per_input=300)
    inputs = scraper.build_inputs(["1", "2"])
    assert [i["jobs_to_not_include"] for i in inputs] == [["1", "2"], ["1", "2"]]


@pytest.mark.parametrize("recorded, search", [("genai", '"Gen AI"'), ("gen ai", '"GenAI"')])
def test_a_recorded_keyword_matches_with_spaces_ignored(recorded, search):
    known = _known([("rec", "Platform Engineer", recorded), ("other", "Nurse", "quant")])
    assert scraper.exclude_ids_per_keyword(known, [search])[search] == ["rec"]


def test_the_summary_lines_survive_an_ascii_stdout(sandbox, monkeypatch):
    """Both lines go through _print_line, which falls back to escapes."""
    import io
    import sys
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii"))
    _config(sandbox, keywords=['"Software Engineer\u2122"'], remote_types=[],
            limit_per_input=300)
    known = _known([(i, "Software Engineer\u2122", "") for i in _ids(0, 5_000)])
    scraper.build_inputs(known)
    sys.stdout.flush()
    assert b"skip list capped for 1 of 1 keywords" in raw.getvalue()
