"""Tests for local/resume_tailor/apply_answers.py, the master answer store.

Version 2 (cycle 18, spec ST-1 to ST-7): every answer has a type (yes_no,
number, choice, text) and a confirmed flag the run obeys; `fact_value` is the
one reader; a damaged file raises `AnswerStoreError` and never falls back to
defaults; a version 1 file migrates in memory with a review list; `validate`
blocks a value the run could read more than one way and `warnings` never
blocks; `find_collision` stops a custom question a built-in already covers.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import apply_answers as aa  # noqa: E402
from resume_tailor import apply_config  # noqa: E402


def _v1_file(path, entries):
    """A version 1 store on disk: no "version" key, free-text answers."""
    path.write_text(json.dumps({"answers": entries}), encoding="utf-8")
    return path


def _v1(eid, answer, question=None, **extra):
    """A version 1 entry (no type, no confirmed flag)."""
    if question is None:
        question = aa.BUILTINS[eid].question if eid in aa.BUILTINS else eid
    return {"id": eid, "question": question, "answer": answer, "kind": "open-ended",
            "status": "active", **extra}


def _v2(eid, answer, *, confirmed=True, note="", question=None, type_=None):
    """A version 2 entry; a built-in takes its question and type from BUILTINS."""
    spec = aa.BUILTINS.get(eid)
    return {"id": eid,
            "question": question or (spec.question if spec else eid.replace("_", " ") + "?"),
            "type": type_ or (spec.type if spec else "text"),
            "answer": answer, "note": note, "confirmed": confirmed, "status": "active"}


def _by_id(entries):
    return {e["id"]: e for e in entries}


_OLD_QUESTIONS = {
    "work_authorized": "Are you legally authorized to work in the US?",
    "requires_sponsorship": "Will you now or in the future require visa sponsorship?",
    "years_experience": "How many years of relevant experience do you have?",
    "willing_to_relocate": "Are you willing to relocate?",
    "authorization_statement": "Work-authorization statement (free text).",
    "gender": "Gender (EEO self-identification).",
    "race_ethnicity": "Race / ethnicity (EEO self-identification).",
    "veteran_status": "Veteran status (EEO self-identification).",
    "disability_status": "Disability status (EEO self-identification).",
    "how_did_you_hear": "How did you hear about us?",
    "address_street": "Street address (line 1).",
    "address_city": "City.",
    "address_state": "State / province.",
    "address_zip": "ZIP / postal code.",
    "address_country": "Country.",
}


def test_all_exports_the_documented_constants():
    for name in ("VERSION", "YES_NO", "TEXT_MAX", "NOTE_MAX", "STATE_TEXT_MAX", "NUMBER_MAX"):
        assert name in aa.__all__, name
        assert hasattr(aa, name), name


# --- ST-1 / ST-2: the built-ins, their options and the seed -------------------------------

def test_builtins_are_ordered_and_typed_as_the_spec_lists_them():
    assert list(aa.BUILTINS) == [
        "work_authorized", "requires_sponsorship", "willing_to_relocate", "onsite_ok",
        "years_experience", "authorization_statement", "gender", "race_ethnicity",
        "veteran_status", "disability_status", "how_did_you_hear", "address_street",
        "address_city", "address_state", "address_zip", "address_country"]
    types = {k: b.type for k, b in aa.BUILTINS.items()}
    assert [k for k, t in types.items() if t == "yes_no"] == [
        "work_authorized", "requires_sponsorship", "willing_to_relocate", "onsite_ok"]
    assert [k for k, t in types.items() if t == "number"] == ["years_experience"]
    assert [k for k, t in types.items() if t == "choice"] == [
        "gender", "race_ethnicity", "veteran_status", "disability_status",
        "address_state", "address_country"]
    assert [k for k, t in types.items() if t == "text"] == [
        "authorization_statement", "how_did_you_hear", "address_street", "address_city",
        "address_zip"]
    assert set(aa.BOOL_IDS) == {"work_authorized", "requires_sponsorship",
                                "willing_to_relocate", "onsite_ok"}


def test_existing_ids_keep_their_question_text_and_onsite_ok_is_new():
    questions = {k: b.question for k, b in aa.BUILTINS.items()}
    for key, text in _OLD_QUESTIONS.items():
        assert questions[key] == text, key
    assert questions["onsite_ok"] == "Are you willing to work on-site (in the office)?"
    assert apply_config.DEFAULTS["onsite_ok"] is True
    assert set(apply_config.DEFAULTS) == set(aa.BUILTINS)


def test_choice_builtins_carry_the_spec_option_lists():
    assert aa.BUILTINS["gender"].options == (
        "Male", "Female", "Non-binary", "Decline to self-identify")
    assert aa.BUILTINS["race_ethnicity"].options == (
        "American Indian or Alaska Native", "Asian", "Black or African American",
        "Hispanic or Latino", "Native Hawaiian or Other Pacific Islander", "White",
        "Two or more races", "Decline to self-identify")
    assert aa.BUILTINS["veteran_status"].options == (
        "I am not a protected veteran",
        "I identify as one or more of the classifications of protected veteran",
        "Decline to self-identify")
    assert aa.BUILTINS["disability_status"].options == (
        "Yes, I have a disability, or have had one in the past",
        "No, I do not have a disability and have not had one in the past",
        "Decline to self-identify")
    for key in ("work_authorized", "years_experience", "how_did_you_hear"):
        assert aa.BUILTINS[key].options == ()


def test_the_state_options_are_the_list_apply_judge_matches_against():
    import apply_judge
    from resume_tailor import answer_tables
    assert apply_judge._US_STATES is answer_tables.US_STATES       # one shared list
    assert len(answer_tables.US_STATES) == 51
    assert answer_tables.US_STATES["DC"] == "District of Columbia"
    names = aa.BUILTINS["address_state"].options
    assert set(names) == set(answer_tables.US_STATES.values())
    assert list(names) == sorted(names)                             # full names, A to Z


def test_the_country_options_are_a_bundled_list_with_the_united_states():
    countries = aa.BUILTINS["address_country"].options
    assert "United States" in countries and "Canada" in countries
    assert "United Kingdom" in countries and "India" in countries
    assert len(countries) == len(set(countries)) > 150


def test_seed_defaults_are_typed_unconfirmed_and_in_builtin_order():
    seeded = aa.seed_defaults()
    assert [e["id"] for e in seeded] == list(aa.BUILTINS)
    by = _by_id(seeded)
    assert by["work_authorized"]["answer"] == "Yes"
    assert by["requires_sponsorship"]["answer"] == "No"
    assert by["willing_to_relocate"]["answer"] == "Yes"
    assert by["onsite_ok"]["answer"] == "Yes"
    assert by["years_experience"]["answer"] == "0"
    assert by["gender"]["answer"] == "Decline to self-identify"
    assert by["address_street"]["answer"] == ""
    assert by["address_country"]["answer"] == "United States"
    for e in seeded:
        spec = aa.BUILTINS[e["id"]]
        assert (e["question"], e["type"]) == (spec.question, spec.type)
        assert e["confirmed"] is False and e["status"] == "active" and e["note"] == ""


def test_validate_passes_seed():
    assert aa.validate(aa.seed_defaults()) == []


def test_seed_reproduces_defaults():
    # the seed carries every DEFAULTS value, a bool shown as Yes or No
    by = _by_id(aa.seed_defaults())
    for key, value in apply_config.DEFAULTS.items():
        shown = ("Yes" if value else "No") if isinstance(value, bool) else str(value)
        assert by[key]["answer"] == shown, key


def test_defaults_include_structured_address():
    for key in ("address_street", "address_city", "address_state", "address_zip",
                "address_country"):
        assert key in apply_config.DEFAULTS
    assert apply_config.DEFAULTS["address_country"] == "United States"
    assert apply_config.DEFAULTS["address_street"] == ""


# --- ST-3: fact_value, the one reader -----------------------------------------------------

def test_fact_value_reads_a_confirmed_set_answer():
    assert aa.fact_value(_v2("work_authorized", "Yes")) == "Yes"
    assert aa.fact_value(_v2("requires_sponsorship", "No")) == "No"
    assert aa.fact_value(_v2("years_experience", "2.5")) == "2.5"
    assert aa.fact_value(_v2("veteran_status", "I am not a protected veteran")) == \
        "I am not a protected veteran"
    assert aa.fact_value(_v2("how_did_you_hear", "  A friend  ")) == "A friend"
    assert aa.fact_value(_v2("address_state", "Ontario")) == "Ontario"   # a province abroad


def test_match_option_strips_and_matches_case_aside():
    assert aa.match_option("Yes ", aa.YES_NO) == "Yes"
    assert aa.match_option(" no", aa.YES_NO) == "No"
    assert aa.match_option("female ", aa.BUILTINS["gender"].options) == "Female"
    assert aa.match_option("maybe", aa.YES_NO) is None
    assert aa.match_option("", aa.YES_NO) is None
    assert aa.match_option(None, aa.YES_NO) is None


def test_fact_value_reads_a_spaced_or_cased_option_the_way_the_editor_shows_it():
    # final review UI I2: one normalizer, so the run and the editor agree
    assert aa.fact_value(_v2("work_authorized", "Yes ")) == "Yes"
    assert aa.fact_value(_v2("work_authorized", "yes")) == "Yes"
    assert aa.fact_value(_v2("gender", "Female ")) == "Female"
    assert aa.fact_value(_v2("address_state", "massachusetts")) == "Massachusetts"
    assert aa.fact_value(_v2("work_authorized", "maybe")) == ""


def test_fact_value_gives_nothing_when_unset_or_unconfirmed():
    assert aa.fact_value(_v2("work_authorized", "Yes", confirmed=False)) == ""
    assert aa.fact_value(_v2("work_authorized", "")) == ""
    assert aa.fact_value(_v2("how_did_you_hear", "   ")) == ""
    assert aa.fact_value({**_v2("work_authorized", "Yes"), "confirmed": "true"}) == ""
    assert aa.fact_value({**_v2("work_authorized", "Yes"), "status": "needs-review"}) == ""
    assert aa.fact_value({}) == "" and aa.fact_value(None) == ""


def test_fact_value_gives_nothing_for_an_answer_that_does_not_fit_its_type():
    # a hand-edited file: an answer that no longer fits its type reads as unset
    assert aa.fact_value(_v2("work_authorized", "Yes, I am a US citizen")) == ""
    assert aa.fact_value(_v2("years_experience", "3+")) == ""
    assert aa.fact_value(_v2("gender", "Man")) == ""
    assert aa.fact_value(_v2("work_authorized", "Yes", type_="text")) == ""


# --- ST-4: load, load_store, load_with_defaults, save -------------------------------------

def test_answer_store_error_names_the_path_and_reason(tmp_path):
    path = tmp_path / "apply_answers.json"
    err = aa.AnswerStoreError(path, "it is not valid JSON")
    assert not isinstance(err, ValueError)          # `except ValueError` never swallows it
    assert err.path == path and err.reason == "it is not valid JSON"
    assert str(err) == f"{path}: it is not valid JSON"


@pytest.mark.parametrize("text, reason", [
    ("not json{", "not valid JSON"),
    ("[1, 2]", "not a JSON object"),
    ('{"version": 2}', "no answers list"),
    ('{"version": 2, "answers": "x"}', "no answers list"),
    ('{"version": 2, "answers": [1]}', "answer 1 is not a record"),
    ('{"answers": [{"id": "a"}, 3]}', "answer 2 is not a record"),
    ('{"version": 9, "answers": []}', "version"),
    ('{"version": true, "answers": []}', "version"),
    ('{"version": 2, "answers": [], "review": {}}', "review"),
    ('{"version": 2, "answers": [], "review": ["x"]}', "review item 1"),
    ('{"version": 2, "answers": [], "review": '
     '[{"id": "a", "question": "Q", "before": "x"}]}', "review item 1"),
    ('{"version": 2, "answers": [], "review": '
     '[{"id": 1, "question": "Q", "before": "x", "after": "y"}]}', "review item 1"),
])
def test_a_damaged_file_raises_and_never_falls_back_to_defaults(tmp_path, text, reason):
    path = tmp_path / "apply_answers.json"
    path.write_text(text, encoding="utf-8")
    for reader in (aa.load, aa.load_store, aa.load_with_defaults):
        with pytest.raises(aa.AnswerStoreError) as info:
            reader(path)
        assert info.value.path == path
        assert reason in info.value.reason, info.value.reason
        assert not info.value.reason.endswith(".")


def test_a_file_that_is_not_utf8_raises(tmp_path):
    path = tmp_path / "apply_answers.json"
    path.write_bytes(b'\xff\xfe{"answers": []}')
    with pytest.raises(aa.AnswerStoreError):
        aa.load(path)


def test_a_bom_prefixed_file_reads_and_is_not_damaged(tmp_path):
    path = tmp_path / "apply_answers.json"
    payload = json.dumps({"version": 2, "answers": [], "review": []})
    path.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
    assert aa.load(path) == []


def test_load_absent_file_seeds_and_migrates(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    store = aa.load_store(tmp_path / "missing_answers.json")
    assert store["disk_version"] is None and store["review"] == []
    assert [e["id"] for e in store["answers"]] == list(aa.BUILTINS)
    assert all(e["confirmed"] is False for e in store["answers"])
    assert aa.load(tmp_path / "missing_answers.json") == store["answers"]


def test_migrate_applies_overrides(tmp_path, monkeypatch):
    cfg = tmp_path / "apply_config.json"
    cfg.write_text(json.dumps({"how_did_you_hear": "Referral", "willing_to_relocate": False}),
                   encoding="utf-8")
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", cfg)
    merged = aa.migrate_from_apply_config(aa.seed_defaults())
    by = _by_id(merged)
    assert by["how_did_you_hear"]["answer"] == "Referral"
    assert by["willing_to_relocate"]["answer"] == "No"
    assert all(e["confirmed"] is False for e in merged)
    monkeypatch.setattr(aa, "STORE_PATH", tmp_path / "absent.json")
    assert _by_id(aa.load())["willing_to_relocate"]["answer"] == "No"


def test_a_v1_file_migrates_in_memory_and_the_next_save_writes_v2(tmp_path):
    path = _v1_file(tmp_path / "apply_answers.json",
                    [_v1("work_authorized", "Yes, I am a US citizen")])
    before = path.read_bytes()
    store = aa.load_store(path)
    assert path.read_bytes() == before                  # a load never writes
    assert store["disk_version"] == 1 and len(store["review"]) == 1
    entry = store["answers"][0]
    assert (entry["type"], entry["answer"], entry["note"]) == ("yes_no", "Yes",
                                                            "I am a US citizen")
    aa.save(store["answers"], path, review=store["review"])
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"version": 2, "answers": store["answers"], "review": store["review"]}
    assert path.with_name(path.name + ".bak").read_bytes() == before
    again = aa.load_store(path)
    assert again["disk_version"] == 2
    assert again["answers"] == store["answers"] and again["review"] == store["review"]


def test_save_then_load_roundtrips_and_makes_bak(tmp_path):
    path = tmp_path / "apply_answers.json"
    ans = aa.seed_defaults()
    aa.save(ans, path)
    aa.save(ans, path)                          # the second write makes a .bak
    assert path.with_name(path.name + ".bak").exists()
    assert aa.load(path) == ans
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2


def test_save_without_a_review_keeps_the_list_a_v2_file_has(tmp_path):
    path = tmp_path / "apply_answers.json"
    items = [{"id": "work_authorized", "question": "Q", "before": "yes sir", "after": "Yes"}]
    aa.save([_v2("work_authorized", "Yes")], path, review=items)
    aa.save([_v2("work_authorized", "No")], path)
    assert json.loads(path.read_text(encoding="utf-8"))["review"] == items
    aa.save([_v2("work_authorized", "No")], path, review=[])
    assert json.loads(path.read_text(encoding="utf-8"))["review"] == []
    # over a version 1 file there is no stored list to keep
    _v1_file(path, [_v1("work_authorized", "Yes, I am a US citizen")])
    aa.save([_v2("work_authorized", "Yes")], path)
    assert json.loads(path.read_text(encoding="utf-8"))["review"] == []


def test_save_refuses_to_write_over_a_damaged_file(tmp_path):
    path = tmp_path / "apply_answers.json"
    path.write_text("not json{", encoding="utf-8")
    bak = path.with_name(path.name + ".bak")
    bak.write_text("the good copy", encoding="utf-8")
    with pytest.raises(aa.AnswerStoreError):
        aa.save(aa.seed_defaults(), path)
    assert path.read_text(encoding="utf-8") == "not json{"
    assert bak.read_text(encoding="utf-8") == "the good copy"


def test_restore_bytes_keeps_a_good_bak_when_the_current_file_is_damaged(tmp_path):
    # SP1 fix round 1, item 1: reverting a damaged store used to copy the
    # damaged bytes over the one good backup, destroying it.
    path = tmp_path / "apply_answers.json"
    bak = path.with_name(path.name + ".bak")
    bak.write_text("the good copy", encoding="utf-8")
    path.write_text("not json{", encoding="utf-8")
    good_snapshot = json.dumps({"version": 2, "answers": [], "review": []}).encode("utf-8")
    aa.restore_bytes(good_snapshot, path)
    assert bak.read_text(encoding="utf-8") == "the good copy"
    assert path.read_bytes() == good_snapshot


def test_restore_bytes_still_backs_up_a_readable_current_file(tmp_path):
    path = tmp_path / "apply_answers.json"
    bak = path.with_name(path.name + ".bak")
    current = json.dumps({"version": 2, "answers": [], "review": []}).encode("utf-8")
    path.write_bytes(current)
    snapshot = json.dumps({"version": 2, "answers": [_v2("how_did_you_hear", "LinkedIn")],
                          "review": []}).encode("utf-8")
    aa.restore_bytes(snapshot, path)
    assert bak.read_bytes() == current
    assert path.read_bytes() == snapshot


def test_save_rejects_invalid(tmp_path):
    path = tmp_path / "apply_answers.json"
    with pytest.raises(ValueError):
        aa.save([_v2("work_authorized", "yes please")], path)
    assert not path.exists()


def test_load_stays_pure_no_merge(tmp_path):
    kept = [e for e in aa.seed_defaults() if e["id"] not in ("address_country", "onsite_ok")]
    path = tmp_path / "apply_answers.json"
    aa.save(kept, path)
    assert aa.load(path) == kept


def test_load_with_defaults_brings_a_missing_builtin_back_unset_and_unconfirmed(tmp_path):
    kept = [e for e in aa.seed_defaults() if e["id"] not in ("address_country", "onsite_ok")]
    kept.append(_v2("github", "https://github.com/x", question="What is your GitHub?"))
    path = tmp_path / "apply_answers.json"
    aa.save(kept, path)
    by = _by_id(aa.load_with_defaults(path))
    for key in ("address_country", "onsite_ok"):
        assert by[key]["answer"] == "", key            # never the seed value
        assert by[key]["confirmed"] is False
        assert by[key]["type"] == aa.BUILTINS[key].type
        assert by[key]["question"] == aa.BUILTINS[key].question
    assert by["github"]["answer"] == "https://github.com/x"      # custom entries stay
    assert aa.validate(list(by.values())) == []


# --- ST-5: validate and warnings ----------------------------------------------------------

@pytest.mark.parametrize("eid, answer, extra, fragment", [
    ("work_authorized", "yes", {}, "Yes, No or not set"),
    ("work_authorized", "Yes, I am a US citizen", {}, "Yes, No or not set"),
    ("years_experience", "3+", {}, "0 to 60"),
    ("years_experience", "61", {}, "0 to 60"),
    ("years_experience", "60.5", {}, "0 to 60"),
    ("years_experience", "2.25", {}, "0 to 60"),
    ("years_experience", "100", {}, "0 to 60"),
    ("gender", "Man", {}, "listed options"),
    ("address_country", "USA", {}, "listed options"),
    ("how_did_you_hear", "x" * 1001, {}, "1000 characters"),
    ("work_authorized", "Yes", {"note": "n" * 301}, "300 characters"),
    ("years_experience", "3", {"note": "n" * 301}, "300 characters"),
    ("address_zip", "0210", {}, "ZIP"),
    ("address_zip", "02100-12", {}, "ZIP"),
    ("address_state", "MA", {}, "state"),
    ("work_authorized", "Yes", {"question": "Can you work here?"}, "question"),
    ("work_authorized", "Yes", {"type": "text"}, "type"),
    ("salary", "a lot", {"type": "choice"}, "type"),
    ("salary", "a lot", {"type": "essay"}, "type"),
    ("work_authorized", "Yes", {"confirmed": "yes"}, "confirmed"),
    ("work_authorized", "Yes", {"status": "needs-review"}, "status"),
    ("salary", "a lot", {"question": ""}, "question is required"),
    ("", "a lot", {"question": "Q?"}, "id is required"),
])
def test_validate_blocks_a_value_the_run_could_misread(eid, answer, extra, fragment):
    entry = {**_v2(eid, answer), **extra}
    errs = aa.validate([entry])
    assert any(fragment in e for e in errs), errs


def test_validate_accepts_each_types_valid_shapes():
    store = aa.seed_defaults()
    by = _by_id(store)
    by["work_authorized"]["answer"] = "No"
    by["work_authorized"]["note"] = "n" * 300
    by["years_experience"]["answer"] = "60"
    by["onsite_ok"]["answer"] = ""                       # not set is always allowed
    by["authorization_statement"]["answer"] = "x" * 1000
    by["address_state"]["answer"] = "District of Columbia"
    by["address_zip"]["answer"] = "02100-1234"
    store.append(_v2("custom_years", "2.5", type_="number", question="Years managing people?"))
    store.append(_v2("custom_yes", "Yes", type_="yes_no", question="Are you over 18?"))
    assert aa.validate(store) == []
    by["years_experience"]["answer"] = "0"
    by["address_zip"]["answer"] = "02100"
    assert aa.validate(store) == []


def test_outside_the_us_the_state_is_text_and_the_zip_is_free():
    store = [_v2("address_country", "Canada"), _v2("address_state", "Ontario"),
             _v2("address_zip", "K1A 0B1")]
    assert aa.validate(store) == []
    store[1]["answer"] = "x" * 61
    assert any("60 characters" in e for e in aa.validate(store))
    # the United States (or no country yet) holds the state and ZIP to the US rules
    for country in ("United States", ""):
        store = [_v2("address_country", country), _v2("address_state", "Ontario"),
                 _v2("address_zip", "K1A 0B1")]
        errs = aa.validate(store)
        assert any("state" in e for e in errs) and any("ZIP" in e for e in errs), errs


def test_validate_does_not_limit_a_choice_entrys_note():
    # the 300-character cap applies only to yes_no and number (a migrated
    # unmatched choice keeps its full original text as the note)
    entry = _v2("gender", "Male", note="n" * 400)
    assert aa.validate([entry]) == []


def test_a_missing_address_country_entry_still_applies_the_us_rules():
    store = [_v2("address_state", "Ontario"), _v2("address_zip", "K1A 0B1")]
    errs = aa.validate(store)
    assert any("state" in e for e in errs) and any("ZIP" in e for e in errs), errs


def test_validate_ignores_the_legacy_kind_and_flags_duplicate_ids():
    entry = {**_v2("how_did_you_hear", "LinkedIn"), "kind": "weird"}
    errs = aa.validate([entry, dict(entry)])
    assert not any("kind" in e for e in errs)
    assert any("duplicate" in e.lower() for e in errs)


def test_validate_blocks_a_custom_question_worded_as_a_builtin():
    entry = _v2("auth_q", "Yes", type_="yes_no",
                question="are you legally authorized to work in the US")
    errs = aa.validate(aa.seed_defaults() + [entry])
    assert errs == ["answer 'auth_q': the built-in answer 'Are you legally authorized to work "
                    "in the US?' already answers this question, and the run fills it from "
                    "that answer"], errs
    assert not any("edit that answer" in e for e in errs)


def test_validate_names_a_custom_question_saved_twice():
    first = _v2("github", "x", question="What is your GitHub?")
    second = _v2("github_2", "y", question="what is your github")
    errs = aa.validate([first, second])
    assert errs == ["answer 'github_2': the custom answer 'What is your GitHub?' already has "
                    "this question; keep one of the two"], errs


@pytest.mark.parametrize("question", [
    "Are you authorized to work in the US?",
    "Are you authorized to work in Canada?",
    "Will you require H-1B visa sponsorship?",
])
def test_validate_accepts_a_custom_question_in_other_words(question):
    entry = _v2("custom", "No", type_="yes_no", question=question)
    assert aa.validate(aa.seed_defaults() + [entry]) == []


def test_validate_counts_a_long_note_in_its_message():
    entry = _v2("work_authorized", "Yes", note="n" * 467)
    assert aa.validate([entry]) == ["answer 'work_authorized': the note is 467 of 300 "
                                    "characters"]


def test_append_needs_review_is_retired():
    assert not hasattr(aa, "append_needs_review")


def test_warnings_flag_the_authorization_contradiction():
    store = aa.seed_defaults()
    by = _by_id(store)
    by["work_authorized"]["answer"] = "No"
    by["requires_sponsorship"]["answer"] = "No"
    msgs = aa.warnings(store)
    assert any("these two disagree: someone not authorized to work usually needs sponsorship"
               in w for w in msgs), msgs
    assert aa.validate(store) == []                     # a warning never blocks the save
    by["requires_sponsorship"]["answer"] = "Yes"
    assert not any("disagree" in w for w in aa.warnings(store))


def test_warnings_list_unset_builtins_and_count_unconfirmed_ones():
    store = aa.seed_defaults()      # street, city, state and zip start unset
    msgs = aa.warnings(store)
    unset = [w for w in msgs if "Street address (line 1)." in w]
    assert len(unset) == 1
    assert "City." in unset[0] and "ZIP / postal code." in unset[0]
    assert "Country." not in unset[0]
    assert any("12 answers are not confirmed" in w for w in msgs), msgs
    done = [dict(e, confirmed=True, answer=e["answer"] or "x") for e in store]
    assert aa.warnings(done) == []
    # a built-in missing from the list counts as not set
    assert any("Country." in w for w in aa.warnings([e for e in done
                                                      if e["id"] != "address_country"]))


# --- ST-6: migrating a version 1 store ----------------------------------------------------

def test_yes_no_reads_the_answers_first_word():
    # a worded answer is read by its first word, never turned to "No" (the
    # 2026-09-26 Contoso run: "Yes, I am a US citizen" went on the form as "No")
    for text in ("true", "Yes", "yes.", "Yes, I am a US citizen",
                 "Yes willing to relocate and open to on-site", "1"):
        assert aa.yes_no(text) == "Yes", text
    for text in ("false", "No", "No, I am a US citizen", "no.", "0"):
        assert aa.yes_no(text) == "No", text
    for text in ("US citizen", "Not at this time", "N/A", "Y", "", None):
        assert aa.yes_no(text) == "", text


def test_migrate_reads_a_worded_yes_no_answer_as_yes_plus_a_note():
    out, review = aa.migrate_v1([_v1("work_authorized", "Yes, I am a US citizen"),
                                 _v1("requires_sponsorship", "No, I am a US citizen")])
    auth, sponsor = out
    assert (auth["type"], auth["answer"], auth["note"]) == ("yes_no", "Yes", "I am a US citizen")
    assert (sponsor["answer"], sponsor["note"]) == ("No", "I am a US citizen")
    # the words after Yes or No can change what it means, so the user confirms it
    assert auth["confirmed"] is False and sponsor["confirmed"] is False
    assert review[0] == {"id": "work_authorized",
                         "question": "Are you legally authorized to work in the US?",
                         "before": "Yes, I am a US citizen",
                         "after": "Yes, note 'I am a US citizen'"}
    assert [r["id"] for r in review] == ["work_authorized", "requires_sponsorship"]


def test_migrate_leaves_a_seed_answer_unconfirmed_and_off_the_review():
    out, review = aa.migrate_v1([
        _v1("work_authorized", "true"), _v1("requires_sponsorship", "No"),
        _v1("willing_to_relocate", "True"), _v1("years_experience", "0"),
        _v1("gender", "Decline to self-identify"), _v1("how_did_you_hear", "LinkedIn"),
        _v1("address_street", ""), _v1("address_country", "United States")])
    assert [e["answer"] for e in out] == ["Yes", "No", "Yes", "0", "Decline to self-identify",
                                          "LinkedIn", "", "United States"]
    assert all(e["confirmed"] is False for e in out)
    assert review == []


@pytest.mark.parametrize("eid, text, answer", [
    ("requires_sponsorship", "No, but I will need H-1B sponsorship after my OPT ends", "No"),
    ("willing_to_relocate", "No problem relocating for the right role", "No"),
    ("work_authorized", "Yes, but only on OPT until May 2027", "Yes"),
    ("years_experience", "2 years in Python, 5 total", "2"),
    ("years_experience", "None professionally, 2 years of research", "0"),
    ("veteran_status", "Yes",
     "I identify as one or more of the classifications of protected veteran"),
    ("disability_status", "No",
     "No, I do not have a disability and have not had one in the past"),
])
def test_migrate_leaves_a_guessed_meaning_unconfirmed_and_on_the_review(eid, text, answer):
    # final review C1: the rest of the sentence can reverse the word the
    # migration read, and a Yes or No names no veteran or disability option
    out, review = aa.migrate_v1([_v1(eid, text)])
    assert (out[0]["answer"], out[0]["confirmed"]) == (answer, False)
    assert [r["id"] for r in review] == [eid]


def test_a_migrated_reversed_sponsorship_answer_reaches_no_form(tmp_path):
    # the drain loads a version 1 file in memory and leaves the file as it is
    path = _v1_file(tmp_path / "apply_answers.json", [
        _v1("work_authorized", "Yes, I am on OPT"),
        _v1("requires_sponsorship", "No, but I will need H-1B sponsorship after my OPT ends")])
    by = _by_id(aa.load(path))
    assert aa.fact_value(by["requires_sponsorship"]) == ""
    assert aa.fact_value(by["work_authorized"]) == ""


@pytest.mark.parametrize("eid, text, answer", [
    ("requires_sponsorship", "Yes", "Yes"),
    ("requires_sponsorship", "true.", "Yes"),
    ("years_experience", "5", "5"),
    ("gender", "female", "Female"),
    ("how_did_you_hear", "A friend", "A friend"),
])
def test_migrate_confirms_an_answer_that_reads_the_same_as_its_value(eid, text, answer):
    out, _ = aa.migrate_v1([_v1(eid, text)])
    assert (out[0]["answer"], out[0]["note"], out[0]["confirmed"]) == (answer, "", True)


def test_migrate_keeps_an_unreadable_yes_no_as_a_note_for_review():
    out, review = aa.migrate_v1([_v1("willing_to_relocate", "Open to NYC")])
    assert (out[0]["answer"], out[0]["note"], out[0]["confirmed"]) == ("", "Open to NYC", False)
    assert review == [{"id": "willing_to_relocate", "question": "Are you willing to relocate?",
                       "before": "Open to NYC", "after": "Not set, note 'Open to NYC'"}]


@pytest.mark.parametrize("text, answer, note", [
    ("3+ years", "3", "+ years"),
    ("2.5", "2.5", ""),
    ("10", "10", ""),
    ("5 years", "5", "years"),
    ("About 3 years", "", "About 3 years"),
    ("3-5 years", "", "3-5 years"),
    ("2.75", "", "2.75"),
    ("100", "", "100"),
    ("75", "", "75"),
    ("3 1/2 years", "", "3 1/2 years"),          # a fraction after a space is unreadable
    # SP1 fix round 2: a years answer starting with 0 and naming a range stays unreadable
    ("0 to 5 years", "", "0 to 5 years"),
    ("0-1 years", "", "0-1 years"),
    ("0 or 1 years", "", "0 or 1 years"),
    ("0.5 to 1 years", "", "0.5 to 1 years"),
    # words for zero years of experience migrate to "0", the full text kept as the note
    ("less than 1 year", "0", "less than 1 year"),
    ("Less than one year", "0", "Less than one year"),
    ("under 1 year", "0", "under 1 year"),
    ("<1", "0", "<1"),
    ("< 1 year", "0", "< 1 year"),
    ("None", "0", "None"),
    ("no experience", "0", "no experience"),
])
def test_migrate_reads_a_leading_number(text, answer, note):
    out, review = aa.migrate_v1([_v1("years_experience", text)])
    assert (out[0]["type"], out[0]["answer"], out[0]["note"]) == ("number", answer, note)
    assert out[0]["confirmed"] is (answer == text)     # only a bare number confirms
    assert bool(review) is (text != answer)


@pytest.mark.parametrize("eid, text, want", [
    ("veteran_status", "I am not a veteran", "I am not a protected veteran"),
    ("gender", "Decline", "Decline to self-identify"),
    ("race_ethnicity", "Prefer not to say", "Decline to self-identify"),
    ("disability_status", "I don't wish to answer", "Decline to self-identify"),
    ("gender", "female", "Female"),
    ("address_country", "USA", "United States"),
    ("address_country", "US", "United States"),
    ("address_country", "U.S.", "United States"),
    ("address_state", "MA", "Massachusetts"),
    ("address_state", "new york", "New York"),
    # SP1 fix round 1, item 3: short EEO answer aliases, applied per id
    ("gender", "Man", "Male"),
    ("gender", "M", "Male"),
    ("gender", "Woman", "Female"),
    ("gender", "F", "Female"),
    ("race_ethnicity", "Black", "Black or African American"),
    ("race_ethnicity", "African American", "Black or African American"),
    ("race_ethnicity", "Hispanic", "Hispanic or Latino"),
    ("race_ethnicity", "Latino", "Hispanic or Latino"),
    ("race_ethnicity", "Latina", "Hispanic or Latino"),
    ("race_ethnicity", "Latinx", "Hispanic or Latino"),
    ("race_ethnicity", "Native American", "American Indian or Alaska Native"),
    ("race_ethnicity", "Pacific Islander", "Native Hawaiian or Other Pacific Islander"),
    ("race_ethnicity", "Caucasian", "White"),
    ("race_ethnicity", "Multiracial", "Two or more races"),
    ("race_ethnicity", "Mixed", "Two or more races"),
    ("race_ethnicity", "Two or more", "Two or more races"),
    ("veteran_status", "No", "I am not a protected veteran"),
    ("veteran_status", "Not a veteran", "I am not a protected veteran"),
    ("veteran_status", "Non-veteran", "I am not a protected veteran"),
    ("veteran_status", "Yes",
     "I identify as one or more of the classifications of protected veteran"),
    ("veteran_status", "Protected veteran",
     "I identify as one or more of the classifications of protected veteran"),
    ("disability_status", "No",
     "No, I do not have a disability and have not had one in the past"),
    ("disability_status", "No disability",
     "No, I do not have a disability and have not had one in the past"),
    ("disability_status", "I do not have a disability",
     "No, I do not have a disability and have not had one in the past"),
    ("disability_status", "Yes", "Yes, I have a disability, or have had one in the past"),
    ("disability_status", "I have a disability",
     "Yes, I have a disability, or have had one in the past"),
])
def test_migrate_matches_a_choice_by_case_and_the_alias_table(eid, text, want):
    # final review C1: a case match, a state or country spelling and a decline
    # form confirm; every other alias is a reading the user confirms by hand
    out, _ = aa.migrate_v1([_v1(eid, text)])
    confirmed = (eid, text) in _SPELLING_ALIASES
    assert (out[0]["answer"], out[0]["note"], out[0]["confirmed"]) == (want, "", confirmed)


_SPELLING_ALIASES = {
    ("gender", "Decline"), ("race_ethnicity", "Prefer not to say"),
    ("disability_status", "I don't wish to answer"), ("gender", "female"),
    ("address_country", "USA"), ("address_country", "US"), ("address_country", "U.S."),
    ("address_state", "MA"), ("address_state", "new york"),
}


def test_migrate_confirms_a_trailing_period_on_an_exact_option():
    out, _ = aa.migrate_v1([_v1("gender", "Female.")])
    assert (out[0]["answer"], out[0]["confirmed"]) == ("Female", True)


def test_a_trailing_period_or_extra_spaces_never_blocks_an_exact_match():
    for text in ("Female.", "  Female  ", "female."):
        out, _ = aa.migrate_v1([_v1("gender", text)])
        assert out[0]["answer"] == "Female", text


def test_a_state_code_alias_applies_only_to_address_state():
    # SP1 fix round 1, item 6: "Georgia" is both a US state and a country, so a
    # v1 country answer of "GA" must not become the country Georgia.
    out, review = aa.migrate_v1([_v1("address_country", "GA")])
    assert out[0]["answer"] == ""
    assert review == [{"id": "address_country",
                       "question": aa.BUILTINS["address_country"].question,
                       "before": "GA", "after": "Not set, note 'GA'"}]


def test_migrate_keeps_an_unmatched_choice_as_a_note_for_review():
    out, review = aa.migrate_v1([_v1("disability_status", "No, I do not have a disability"),
                                 _v1("address_country", "Decline")])
    assert [(e["answer"], e["confirmed"]) for e in out] == [("", False), ("", False)]
    assert out[0]["note"] == "No, I do not have a disability"
    assert [r["id"] for r in review] == ["disability_status", "address_country"]


def test_migrate_reads_the_address_by_its_country():
    us, _ = aa.migrate_v1([_v1("address_state", "Ontario"), _v1("address_zip", "2100"),
                           _v1("address_country", "United States")])
    assert [(e["answer"], e["note"]) for e in us[:2]] == [("", "Ontario"), ("", "2100")]
    ca, review = aa.migrate_v1([_v1("address_state", "Ontario"), _v1("address_zip", "K1A 0B1"),
                                _v1("address_country", "Canada")])
    assert [(e["answer"], e["confirmed"]) for e in ca] == [
        ("Ontario", True), ("K1A 0B1", True), ("Canada", True)]
    assert review == []
    assert aa.validate(ca) == []


def test_migrate_turns_needs_review_active_and_unconfirmed():
    out, _ = aa.migrate_v1([_v1("salary", "Open", question="Desired salary?",
                                status="needs-review")])
    assert (out[0]["status"], out[0]["confirmed"], out[0]["answer"]) == ("active", False, "Open")


def test_migrate_keeps_custom_answers_as_confirmed_text_with_their_other_keys():
    out, review = aa.migrate_v1([_v1("github", "https://github.com/x",
                                     question="What is your GitHub?", extra_field=7)])
    assert out == [{"id": "github", "question": "What is your GitHub?", "type": "text",
                    "answer": "https://github.com/x", "note": "", "confirmed": True,
                    "status": "active", "kind": "open-ended", "extra_field": 7}]
    assert review == []


def test_migrate_restores_a_builtins_own_question_text():
    out, _ = aa.migrate_v1([_v1("how_did_you_hear", "LinkedIn", question="How?")])
    assert out[0]["question"] == "How did you hear about us?"


def test_a_migrated_test_bank_validates():
    values = {"work_authorized": "true", "requires_sponsorship": "false",
              "years_experience": "2", "willing_to_relocate": "true",
              "gender": "Decline to self-identify", "race_ethnicity": "Decline",
              "veteran_status": "I am not a veteran",
              "disability_status": "No, I do not have a disability",
              "how_did_you_hear": "LinkedIn", "address_street": "123 Main Street",
              "address_city": "Anytown", "address_state": "CA", "address_zip": "12345",
              "address_country": "USA"}
    out, _ = aa.migrate_v1([_v1(k, v) for k, v in values.items()]
                           + [_v1("salary", "Open", question="What is your desired salary?")])
    assert aa.validate(out) == []
    by = _by_id(out)
    assert by["address_state"]["answer"] == "California"
    assert by["address_country"]["answer"] == "United States"


# --- ST-7: collisions ---------------------------------------------------------------------

_Q = {k: v for k, v in _OLD_QUESTIONS.items()}
_Q["onsite_ok"] = "Are you willing to work on-site (in the office)?"


@pytest.mark.parametrize("question, want", [
    ("Are you legally authorized to work in the US?", "work_authorized"),
    ("are you legally  authorized to work in the us", "work_authorized"),
    ("Will you now or in the future require visa sponsorship?", "requires_sponsorship"),
    ("How many years of relevant experience do you have?", "years_experience"),
    ("Country.", "address_country"),
    ("State / province", "address_state"),
    # final review UI C1: a question the run hands to a custom answer saves;
    # the own-question check lives in the Add answer dialog (local/qt)
    ("Are you authorized to work in Canada?", None),
    ("Will you require H-1B visa sponsorship?", None),
    ("Are you willing to relocate to Austin, TX?", None),
    ("How many years of experience do you have with Python?", None),
    ("Work authorization", None),
    ("State your desired salary", None),
    ("Country of citizenship", None),
    ("What is your GitHub?", None),
])
def test_find_collision_names_only_a_builtins_own_wording(question, want):
    expected = _Q[want] if want else None
    assert aa.find_collision(question, aa.seed_defaults()) == expected


def test_validate_lets_a_version_1_store_keep_a_narrower_custom_question(tmp_path):
    path = _v1_file(tmp_path / "apply_answers.json", [
        _v1("work_authorized", "Yes"),
        _v1("canada", "No", question="Are you authorized to work in Canada?")])
    answers = aa.load(path)
    assert aa.validate(answers) == []
    aa.save(answers, path)
    assert _by_id(aa.load(path))["canada"]["answer"] == "No"


def test_find_collision_between_custom_questions_uses_normalised_text():
    entries = aa.seed_defaults() + [_v2("github", "x", question="What is your GitHub?")]
    assert aa.find_collision("what is your  github", entries) == "What is your GitHub?"
    assert aa.find_collision("What is your GitHub?", entries, own_id="github") is None
    assert aa.find_collision("What is your GitLab?", entries) is None
    assert aa.find_collision("", entries) is None


def test_find_collision_names_the_authorization_statement_by_its_own_text():
    question = aa.BUILTINS["authorization_statement"].question
    assert aa.find_collision(question, aa.seed_defaults()) == question


def test_every_built_in_and_custom_type_is_a_store_type():
    assert {b.type for b in aa.BUILTINS.values()} <= set(aa.TYPES)
    assert set(aa.CUSTOM_TYPES) <= set(aa.TYPES)
    assert aa.STATUSES == ("active",)
