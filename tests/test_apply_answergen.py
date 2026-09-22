"""`apply_answergen`: one flash-lite draft per free-text field, gated by a Jev
grounding request sentence by sentence. `llm.call` is mocked in every test
(the module imports `resume_tailor` lazily, so `import apply_answergen` costs
nothing) and the judge is `FakeJev`, which grounds a sentence at 0.9 when two
of its content words appear in the sheet and at 0.1 otherwise."""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_answergen  # noqa: E402
import apply_form  # noqa: E402
import apply_judge  # noqa: E402
import jev  # noqa: E402
from resume_tailor import aiwriting, llm  # noqa: E402  (pytest only; config loads .env)

SHEET = ("## Candidate\n\n- Name: Jane Doering\n- Location: Anytown, CA\n\n"
         "## Work experience\n\n### Acme Corp, Software Engineer (2024-06 / present)\n"
         "- Built the ingestion pipeline that loads nightly postings into the warehouse.\n"
         "- Cut the scoring cost per run by 65% with a two-stage model chain.\n")
QUESTION = "Describe a project you are proud of and your motivation for this role"
GROUNDED = ("Built the ingestion pipeline at Acme Corp that loads nightly postings. "
            "Cut the scoring cost per run by 65% with a two-stage model chain.")
UNGROUNDED = ("Built the ingestion pipeline at Acme Corp that loads nightly postings. "
              "Baking sourdough bread relaxes me on weekends.")

# The prompt, frozen. A silent edit to the shipped text fails here on purpose:
# the wording is what the hygiene census passed and what the grounding gate
# was tuned against, so a change to it is a reviewed change.
FROZEN_SYSTEM = (
    "You write one answer to a job application question on behalf of the candidate, in "
    "the first person.\n"
    "The only source of facts is the SHEET that follows the question. Select the facts "
    "that answer the question and rephrase them in plain sentences. Never add an "
    "employer, a project, a number, a date, a skill or a motive the sheet does not state. "
    "When the sheet holds nothing that answers the question, return an empty response.\n"
    "Plain text only: no markdown, no headings, no bullet points, no greeting, no sign-off, "
    "no preamble. Whole sentences, each one a claim the sheet supports. Stay within the "
    "character limit given with the question."
)
FROZEN_USER = (
    "QUESTION: Q\n"
    "CHARACTER LIMIT: 500\n\n"
    "SHEET (the only source of facts):\n"
    "S"
)


class _Counting(jev.FakeJev):
    def __init__(self):
        self.requests = []

    def judge(self, state, questions):
        self.requests.append((state, questions))
        return super().judge(state, questions)


def _field(label=QUESTION, required=True, help="Max 1500 characters.", n=3):
    return apply_form.Field(n=n, locator=(0, f"#f{n}"), label=label, type="textarea",
                            required=required, help=help)


class _Catalog:
    def __init__(self, sheet=SHEET):
        self._sheet = sheet

    def sheet_excerpt(self, max_chars=6000):
        return self._sheet


# --- (a) draft: one llm.call, tier flash_lite, the sheet, question, limit and rules ---

def test_draft_calls_llm_once_with_the_sheet_the_question_the_limit_and_the_rules(monkeypatch):
    calls = []

    def fake_call(system, user, tier, **kw):
        calls.append((system, user, tier, kw))
        return "  Built the ingestion pipeline at Acme Corp.  "
    monkeypatch.setattr(llm, "call", fake_call)

    out = apply_answergen.draft(QUESTION, SHEET, 500)

    assert out == "Built the ingestion pipeline at Acme Corp."
    assert len(calls) == 1
    system, user, tier, kw = calls[0]
    assert tier == "flash_lite"
    assert kw.get("json_out", False) is False
    assert system.endswith("\n\n" + aiwriting.RULES_PROMPT)
    assert system.startswith(FROZEN_SYSTEM)
    assert SHEET in user and QUESTION in user and "CHARACTER LIMIT: 500" in user


def test_draft_uses_the_injected_llm_call_and_never_imports_the_tailor(monkeypatch):
    def _never(*a, **kw):
        raise AssertionError("resume_tailor.llm.call was reached")
    monkeypatch.setattr(llm, "call", _never)
    seen = []

    def injected(system, user, tier, **kw):
        seen.append(tier)
        return GROUNDED
    assert apply_answergen.draft(QUESTION, SHEET, 500, llm_call=injected) == GROUNDED
    assert seen == ["flash_lite"]


def test_the_prompt_text_is_frozen():
    system, user = apply_answergen.build_prompt("Q", "S", 500, rules_prompt="R")
    assert system == FROZEN_SYSTEM + "\n\nR"
    assert user == FROZEN_USER
    assert apply_answergen.SYSTEM_PROMPT == FROZEN_SYSTEM
    assert apply_answergen.TIER == "flash_lite"


@pytest.mark.parametrize("raw, expected", [
    ("```text\nBuilt the pipeline.\n```", "Built the pipeline."),
    ("**Built** the _pipeline_.", "Built the pipeline."),
    ("Answer: Built the pipeline.", "Built the pipeline."),
    ("- Built the pipeline.\n- Cut the cost.", "Built the pipeline.\nCut the cost."),
    ("Built the pipeline.\n\n\n\nCut the cost.", "Built the pipeline.\n\nCut the cost."),
    ("\"Built the pipeline.\"", "Built the pipeline."),
    ("“Built the pipeline.”", "Built the pipeline."),
    ("'Built the pipeline.”", "Built the pipeline."),
    ("\"Built the pipeline.", "\"Built the pipeline."),
    ("Maintained score_jobs.py and apply_run.py.", "Maintained score_jobs.py and apply_run.py."),
    ("_Built_ the *pipeline* in snake_case_names.", "Built the pipeline in snake_case_names."),
    ('{"answer": "Built the pipeline."}', "Built the pipeline."),
    ('["Built the pipeline.", "Cut the cost."]', "Built the pipeline."),
    ('{"n": 3}', ""),
    ("{not json", ""),
    ("", ""),
    ("   \n  ", ""),
])
def test_draft_strips_the_model_output_to_plain_text(raw, expected):
    assert apply_answergen.draft(QUESTION, SHEET, 500, llm_call=lambda *a, **k: raw) == expected


def test_draft_respects_the_char_limit_at_a_sentence_end():
    long = "Built the pipeline. Cut the cost by half. Wrote the scorer. Shipped the dashboard."
    out = apply_answergen.draft(QUESTION, SHEET, 45, llm_call=lambda *a, **k: long)
    assert out == "Built the pipeline. Cut the cost by half."
    assert len(out) <= 45
    one = "x" * 30 + " " + "y" * 30
    cut = apply_answergen.draft(QUESTION, SHEET, 40, llm_call=lambda *a, **k: one)
    assert cut == "x" * 30
    assert apply_answergen.draft(QUESTION, SHEET, 0, llm_call=lambda *a, **k: long) == long


def test_draft_returns_an_empty_string_when_the_model_returns_nothing_useful():
    assert apply_answergen.draft(QUESTION, SHEET, 500, llm_call=lambda *a, **k: None) == ""
    assert apply_answergen.draft(QUESTION, SHEET, 500, llm_call=lambda *a, **k: {"x": 1}) == ""


# --- grounding: one Jev request, a sentence below 0.7 drops the draft ----------------

def test_sentences_split_on_end_punctuation_and_newlines_and_keep_abbreviations():
    text = "Built the pipeline. Earned a B.S. in CS.\nCut the cost by 65%! Why? Because."
    assert apply_answergen.sentences(text) == [
        "Built the pipeline.", "Earned a B.S. in CS.", "Cut the cost by 65%!", "Why?",
        "Because."]
    assert apply_answergen.sentences("") == []
    assert apply_answergen.sentences("no end punctuation") == ["no end punctuation"]


@pytest.mark.parametrize("text, expected", [
    ("Wrote tooling, e.g. a scraper. Then shipped it.",
     ["Wrote tooling, e.g. a scraper.", "Then shipped it."]),
    ("Cut cost, i.e. the bill. Then more.", ["Cut cost, i.e. the bill.", "Then more."]),
    ("Used pandas, numpy, etc. for the work. Done.",
     ["Used pandas, numpy, etc. for the work.", "Done."]),
    ("Joined the Ops. Then led it.", ["Joined the Ops.", "Then led it."]),
    ("Reported to Dr. Lee at Acme Inc. in May.", ["Reported to Dr. Lee at Acme Inc. in May."]),
    ("Earned a B.S. in CS. Then an M.S. at MIT.", ["Earned a B.S. in CS.", "Then an M.S. at MIT."]),
])
def test_sentences_keep_common_abbreviations_and_split_ordinary_short_words(text, expected):
    assert apply_answergen.sentences(text) == expected


def test_grounded_asks_one_request_with_one_noul_per_sentence():
    judge = _Counting()
    ok, weakest = apply_answergen.grounded(GROUNDED, SHEET, judge)
    assert (ok, weakest) == (True, 0.9)
    assert len(judge.requests) == 1
    state, questions = judge.requests[0]
    assert state == {"sheet_excerpt": SHEET, "sentences": apply_answergen.sentences(GROUNDED)}
    assert set(questions) == {"grounded_0", "grounded_1"}
    assert all(q["type"] == "noul" for q in questions.values())


def test_grounded_rejects_a_draft_with_one_sentence_below_the_minimum():
    judge = _Counting()
    ok, weakest = apply_answergen.grounded(UNGROUNDED, SHEET, judge)
    assert ok is False
    assert weakest == 0.1 < apply_judge.GROUNDING_MIN
    assert len(judge.requests) == 1


def test_grounded_accepts_an_empty_draft_without_a_request():
    judge = _Counting()
    assert apply_answergen.grounded("", SHEET, judge) == (True, 1.0)
    assert judge.requests == []


# --- answer: budget, empty draft, grounding, one attempt per field ----------------------

def test_answer_returns_none_with_no_budget_and_calls_nothing():
    calls = []
    judge = _Counting()
    out = apply_answergen.answer(_field(), _Catalog(), judge, budget=0,
                                 llm_call=lambda *a, **k: calls.append(a) or GROUNDED)
    assert out is None
    assert calls == [] and judge.requests == []


def test_answer_returns_the_grounded_draft_and_records_the_attempt():
    judge = _Counting()
    calls = []

    def fake_call(system, user, tier, **kw):
        calls.append(user)
        return GROUNDED
    out = apply_answergen.attempt(_field(), _Catalog(), judge, budget=3, llm_call=fake_call)
    assert out.text == GROUNDED
    assert out.ok is True
    assert out.weakest == 0.9
    assert out.sentences == 2
    assert "generated" in out.note and "0.90" in out.note
    assert len(calls) == 1 and "CHARACTER LIMIT: 1500" in calls[0]
    assert len(judge.requests) == 1
    assert apply_answergen.answer(_field(), _Catalog(), judge, budget=3,
                                  llm_call=fake_call) == GROUNDED


def test_answer_rejects_an_ungrounded_draft_after_one_attempt():
    judge = _Counting()
    calls = []

    def fake_call(system, user, tier, **kw):
        calls.append(user)
        return UNGROUNDED
    out = apply_answergen.attempt(_field(), _Catalog(), judge, budget=3, llm_call=fake_call)
    assert out.text is None and out.ok is False
    assert out.weakest == 0.1
    assert out.note == "draft rejected: weakest sentence grounded 0.10, below 0.70"
    assert len(calls) == 1 and len(judge.requests) == 1


def test_answer_returns_none_on_an_empty_draft_without_a_grounding_request():
    judge = _Counting()
    out = apply_answergen.attempt(_field(), _Catalog(), judge, budget=3,
                                  llm_call=lambda *a, **k: "")
    assert out.text is None and out.note == "empty draft"
    assert judge.requests == []


def test_answer_returns_none_when_the_model_call_fails_and_names_the_error_type():
    def boom(*a, **kw):
        raise RuntimeError("the key AQ.secret is invalid")
    out = apply_answergen.attempt(_field(), _Catalog(), _Counting(), budget=3, llm_call=boom)
    assert out.text is None
    assert out.note == "draft failed: RuntimeError"
    assert "AQ.secret" not in out.note


def test_the_generator_hook_keeps_the_last_attempt():
    gen = apply_answergen.Generator(llm_call=lambda *a, **k: UNGROUNDED)
    assert gen.last is None
    assert gen.answer(_field(), _Catalog(), _Counting(), budget=3) is None
    assert gen.last.note.startswith("draft rejected")
    gen = apply_answergen.Generator(llm_call=lambda *a, **k: GROUNDED)
    assert gen.answer(_field(), _Catalog(), _Counting(), budget=1) == GROUNDED
    assert gen.last.ok is True


@pytest.mark.parametrize("help, placeholder, expected", [
    ("Max 1500 characters.", "", 1500),
    ("Keep it under 300 characters", "", 300),
    ("", "up to 800 chars", 800),
    ("", "", apply_answergen.DEFAULT_CHAR_LIMIT),
    ("Max 5 characters.", "", apply_answergen.DEFAULT_CHAR_LIMIT),
])
def test_char_limit_for_reads_the_help_or_placeholder(help, placeholder, expected):
    f = apply_form.Field(n=0, locator=(0, "#x"), label="Q", type="textarea", required=False,
                         help=help, placeholder=placeholder)
    assert apply_answergen.char_limit_for(f) == expected


def test_the_question_carries_the_label_and_the_help_text():
    f = _field(help="Tell us in a few lines. Max 1500 characters.")
    seen = []
    apply_answergen.attempt(f, _Catalog(), _Counting(), budget=1,
                            llm_call=lambda s, u, t, **k: seen.append(u) or GROUNDED)
    assert f"QUESTION: {QUESTION} (Tell us in a few lines. Max 1500 characters.)" in seen[0]


# --- (d) the prompt passes the hygiene census ---------------------------------------

def test_the_prompt_passes_the_hygiene_census():
    import test_prompt_hygiene as hygiene
    assert REPO / "local" / "apply_answergen.py" in hygiene.EXTRA_MODULES
    hits = [h for h in hygiene.scan() if h["module"] == "apply_answergen.py"]
    assert not hits, hygiene._report(hits)
    reached = {mod for mod, _ in hygiene._reachable_constants(hygiene._Index(hygiene.PKG))}
    assert "apply_answergen.py" in reached
    # the frozen copy itself is clean, so the census and the freeze agree
    for label, pattern in hygiene.BANNED:
        assert not pattern.search(FROZEN_SYSTEM), label
        assert not pattern.search(FROZEN_USER), label
