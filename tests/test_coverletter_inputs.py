"""SP3 (cycle 15): the cover letter's two new inputs.

The letter used to be built from the tailored bullets alone. This pins the two
fact sources that widen it and the plumbing that carries them:

  * the voice seed (`letter.seed` in the master yaml): `assets.letter_seed`
    reads it (trimmed, capped, blank when malformed), `master_validate` accepts
    it and warns on a bad one, the example master carries a placeholder, and it
    rides in the generation prompt only when set;
  * the background block: `assets.flatten_entries` flattens, in full, every
    master entry that printed a bullet (every entry in the standalone path),
    bounded on a line boundary, and both `run.py` call sites hand it to
    `generate_body`, which threads it to the refine pass, the style gate and
    the grounding repair.

No real LLM ever runs: compose.call (the transport coverletter uses) is
monkeypatched everywhere, and every master read is a synthetic dict.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import assets, chat, compose, coverletter, master_validate  # noqa: E402
from resume_tailor import run as run_mod  # noqa: E402


BULLETS = {"a1": "Shipped the viewer with 178 tests",
           "a2": "Cut per-run cost by 65%"}

MASTER = {
    "basics": {"name": "Test User", "location": "NYC", "email": "t@example.com"},
    "letter": {"seed": "  I want work where the data pipeline is the product.  "},
    "experience": [
        {"org": "Example Corp", "title": "Intern", "dates": "2024-06 / 2024-08",
         "achievements": [
             {"id": "a1", "what": "rebuilt the nightly pipeline",
              "how": "batched the fetcher", "scope": "2M records a day",
              "impact": ["cut runtime from 6h to 90min"]},
             {"id": "a2", "what": "raised test coverage", "impact": "caught 3 regressions"},
         ]},
    ],
    "projects": [
        {"name": "ExampleApp", "dates": "2024-01 / 2024-05",
         "achievements": [{"id": "p1", "what": "built a query app"}]},
    ],
    "leadership": [
        {"org": "Coding Club", "title": "President", "dates": "2023-09 / 2024-05",
         "achievements": [{"id": "l1", "what": "grew membership 20 to 60"}]},
    ],
}


def _fake_master(monkeypatch, master=None):
    monkeypatch.setattr(assets, "load_master", lambda: master if master is not None else MASTER)


# ── the seed accessor ─────────────────────────────────────────────────────────
def test_letter_seed_is_trimmed(monkeypatch):
    _fake_master(monkeypatch)
    assert assets.letter_seed() == "I want work where the data pipeline is the product."


@pytest.mark.parametrize("master", [
    {"basics": {}},                                  # no letter block at all
    {"letter": None},                                # an empty block
    {"letter": {}},                                  # a block with no seed
    {"letter": {"seed": ""}},                        # blank
    {"letter": {"seed": "   \n  "}},                 # whitespace only
    {"letter": {"seed": 42}},                        # the wrong type
    {"letter": {"seed": ["a", "list"]}},             # the wrong type
    {"letter": "just a string"},                     # the block is the wrong type
])
def test_letter_seed_is_blank_when_absent_or_malformed(monkeypatch, master):
    _fake_master(monkeypatch, master)
    assert assets.letter_seed() == ""


def test_letter_seed_is_capped(monkeypatch):
    _fake_master(monkeypatch, {"letter": {"seed": "word " * 400}})
    seed = assets.letter_seed()
    assert len(seed) <= assets.LETTER_SEED_CAP == 1200
    assert seed.startswith("word word")
    assert not seed.endswith(" ")


def test_letter_seed_cut_never_lands_mid_word(monkeypatch):
    # 1,190 chars, then a word that straddles the cap: it goes, whole
    text = ("x" * 1190) + " straddling the cap " + "y" * 50
    _fake_master(monkeypatch, {"letter": {"seed": text}})
    assert assets.letter_seed() == "x" * 1190
    # the word ending exactly at the cap is kept whole
    exact = ("z" * 1195) + " abcd " + "w" * 40
    _fake_master(monkeypatch, {"letter": {"seed": exact}})
    assert assets.letter_seed() == ("z" * 1195) + " abcd"
    # a seed with no whitespace at all is cut at the cap
    _fake_master(monkeypatch, {"letter": {"seed": "q" * 1300}})
    assert assets.letter_seed() == "q" * 1200


# ── the background block ──────────────────────────────────────────────────────
def test_flatten_entries_lists_headers_and_atoms_in_section_order():
    out = assets.flatten_entries(MASTER)
    lines = out.splitlines()
    assert "- Example Corp, Intern (2024-06 / 2024-08)" in lines
    assert ("    - rebuilt the nightly pipeline; batched the fetcher; "
            "2M records a day; cut runtime from 6h to 90min") in lines
    assert "    - raised test coverage; caught 3 regressions" in lines
    assert "- ExampleApp (2024-01 / 2024-05)" in lines
    assert "- Coding Club, President (2023-09 / 2024-05)" in lines
    assert "    - grew membership 20 to 60" in lines
    order = [out.index(s) for s in ("EXPERIENCE:", "Example Corp", "PROJECTS:",
                                    "ExampleApp", "LEADERSHIP:", "Coding Club")]
    assert order == sorted(order)


def test_flatten_entries_lists_whole_entries_that_own_a_named_atom():
    """The filter is ENTRY-level: an entry that printed any bullet is listed in
    full, so the letter holds every note on that employer, and an entry that
    printed nothing drops out header and all."""
    out = assets.flatten_entries(MASTER, entry_atoms={"a2", "l1"})
    assert "raised test coverage" in out                  # the marked atom
    assert "rebuilt the nightly pipeline" in out          # its sibling, same entry
    assert "grew membership" in out
    assert "Example Corp, Intern" in out and "Coding Club, President" in out
    # a project owning none of the marked atoms drops out header and all
    assert "ExampleApp" not in out and "PROJECTS:" not in out
    assert "built a query app" not in out
    # an empty marker set lists nothing; None lists everything
    assert assets.flatten_entries(MASTER, entry_atoms=set()) == ""
    assert assets.flatten_entries(MASTER, entry_atoms=None) == assets.flatten_entries(MASTER)


def test_flatten_entries_is_bounded_with_the_truncation_marker():
    big = {"experience": [{"org": f"Org {i}", "title": "T", "dates": "2020",
                           "achievements": [{"id": f"x{i}{j}", "what": "w" * 200}
                                            for j in range(4)]}
                          for i in range(200)]}
    out = assets.flatten_entries(big)
    assert len(out) <= assets.LETTER_BACKGROUND_CAP == 30_000
    assert out.endswith(assets.TRUNCATED_MARKER)
    # the cut lands on a line boundary, so no atom line is sent half-finished
    body = out[:-len(assets.TRUNCATED_MARKER)]
    assert body.endswith("\n")
    whole = {"EXPERIENCE:", "    - " + "w" * 200}
    for ln in body.splitlines():
        assert ln in whole or (ln.startswith("- Org ") and ln.endswith(", T (2020)")), ln
    small = assets.flatten_entries(MASTER)
    assert assets.TRUNCATED_MARKER not in small
    assert assets.flatten_entries(MASTER, cap=80).endswith(assets.TRUNCATED_MARKER)


def test_flatten_entries_trims_atoms_evenly_and_keeps_every_entry():
    """Over the cap, atoms come off the longest entries first and every entry
    keeps its header: a rich history loses detail evenly, and no employer
    drops out of the letter (the 2026-09-20 report: 5 of 8 entries lost)."""
    big = {"experience": [{"org": f"Org {i}", "title": "T", "dates": "2020",
                           "achievements": [{"id": f"x{i}{j}", "what": f"o{i} " + "w" * 150}
                                            for j in range(6)]}
                          for i in range(8)]}
    full = assets.flatten_entries(big, cap=10 ** 9)
    out = assets.flatten_entries(big, cap=len(full) // 2)
    assert out.endswith(assets.TRUNCATED_MARKER)
    assert len(out) <= len(full) // 2
    headers = [ln for ln in out.splitlines() if ln.startswith("- Org ")]
    assert len(headers) == 8                       # every entry still listed
    per_entry = [sum(1 for ln in out.splitlines() if ln.startswith(f"    - o{i} "))
                 for i in range(8)]
    assert all(n >= 1 for n in per_entry)          # and every entry keeps a note
    assert max(per_entry) - min(per_entry) <= 1    # trimmed evenly
    # a longer entry gives first: three entries of six atoms and one of one
    lopsided = {"experience": [{"org": f"Org {i}", "title": "T",
                                "achievements": [{"id": f"y{i}{j}", "what": f"o{i} " + "w" * 150}
                                                 for j in range(6 if i else 1)]}
                               for i in range(4)]}
    out = assets.flatten_entries(lopsided, cap=len(assets.flatten_entries(lopsided, cap=10 ** 9)) // 2)
    assert sum(1 for ln in out.splitlines() if ln.startswith("    - o0 ")) == 1
    # headers alone over the cap fall back to a line-boundary cut
    tiny = assets.flatten_entries(big, cap=60)
    assert tiny.endswith(assets.TRUNCATED_MARKER) and len(tiny) <= 60
    assert tiny[:-len(assets.TRUNCATED_MARKER)].endswith("\n")


def test_flatten_entries_of_an_empty_master_is_blank():
    assert assets.flatten_entries({}) == ""
    assert assets.flatten_entries({"experience": "not a list"}) == ""


def test_chat_flattening_now_comes_from_assets():
    """chat's private helpers are the assets ones, so SP4 can reuse one copy."""
    assert chat._atom_line is assets.atom_line
    assert chat._entries is assets.entry_lines
    assert chat.TRUNCATED_MARKER == assets.TRUNCATED_MARKER
    # chat's own callers (no atom filter) see exactly what they saw before
    assert assets.entry_lines(MASTER, "education", "degree", "school") == []
    assert assets.entry_lines(MASTER, "projects", "name") == [
        "- ExampleApp (2024-01 / 2024-05)", "    - built a query app"]


# ── the generation prompt carries both ────────────────────────────────────────
def _capture_generate(monkeypatch, **kwargs):
    seen = {}

    def fake_call(system, user, *a, **k):
        seen.setdefault("system", system)
        seen.setdefault("user", user)
        return "I shipped the viewer with 178 tests."

    monkeypatch.setattr(compose, "call", fake_call)
    coverletter.generate_body("jd", "Engineer", "Acme", BULLETS, **kwargs)
    return seen


def test_seed_rides_in_the_prompt_when_set(monkeypatch):
    _fake_master(monkeypatch)
    seen = _capture_generate(monkeypatch, seed="I want work where the pipeline is the product.")
    assert "IN THE CANDIDATE'S OWN WORDS" in seen["user"]
    assert "I want work where the pipeline is the product." in seen["user"]
    assert "quote at most a fragment of it" in seen["user"]
    # the system prompt uses the block's own label ("seed" is a yaml key)
    assert "candidate's own words" in seen["system"] and "seed" not in seen["system"]


@pytest.mark.parametrize("seed", ["", "   ", None])
def test_nothing_seed_shaped_appears_when_blank(monkeypatch, seed):
    _fake_master(monkeypatch)
    seen = _capture_generate(monkeypatch, seed=seed) if seed is not None else \
        _capture_generate(monkeypatch)
    assert "OWN WORDS" not in seen["user"]
    assert "seed" not in seen["user"].lower()


def test_background_rides_in_the_prompt_after_the_bullets(monkeypatch):
    _fake_master(monkeypatch)
    bg = "- Example Corp, Intern (2024)\n    - rebuilt the nightly pipeline"
    seen = _capture_generate(monkeypatch, background=bg)
    user = seen["user"]
    assert "BACKGROUND (the candidate's own notes behind those bullets" in user
    assert bg in user
    assert user.index("Shipped the viewer with 178 tests") < user.index("BACKGROUND")
    assert "every employer, number, tool, date, school and credential must already appear" in user


def test_no_background_block_when_blank(monkeypatch):
    _fake_master(monkeypatch)
    seen = _capture_generate(monkeypatch, background="")
    assert "BACKGROUND" not in seen["user"]


def test_prompt_blocks_carry_no_banned_styling(monkeypatch):
    """The new blocks must not model what the prompt forbids."""
    _fake_master(monkeypatch)
    seen = _capture_generate(monkeypatch, background="- notes", seed="my seed")
    assert compose.style_violations(seen["user"]) == []


def _capture_refine(monkeypatch, **kwargs):
    seen = {}

    def fake_call(system, user, *a, **k):
        seen["system"], seen["user"] = system, user
        return "ok"

    monkeypatch.setattr(compose, "call", fake_call)
    coverletter.refine_body("Engineer", "Acme", "the draft body", BULLETS, **kwargs)
    return seen


def test_refine_carries_the_background_as_an_allowed_source(monkeypatch):
    seen = _capture_refine(monkeypatch, background="- Example Corp\n    - notes")
    assert "BACKGROUND" in seen["user"] and "- notes" in seen["user"]
    assert "any BACKGROUND notes below" in seen["system"]
    assert "the draft body" in seen["user"]
    assert "Shipped the viewer with 178 tests" in seen["user"]


def test_refine_without_background_carries_no_background_block(monkeypatch):
    seen = _capture_refine(monkeypatch)
    assert "BACKGROUND" not in seen["user"]


def test_gate_repair_carries_the_background(monkeypatch):
    seen = {}

    def fake_call(system, user, *a, **k):
        seen["system"], seen["user"] = system, user
        return "I cut latency by 30% so responses stay fast."

    monkeypatch.setattr(compose, "call", fake_call)
    coverletter.enforce_body_style("Engineer", "Acme",
                                   "I cut latency by 30%, ensuring fast responses.",
                                   BULLETS, background="- Example Corp\n    - notes")
    assert "BACKGROUND" in seen["user"] and "- notes" in seen["user"]
    assert "any BACKGROUND notes below" in seen["system"]


def test_grounding_repair_carries_the_background(monkeypatch):
    seen = {}

    def fake_call(system, user, *a, **k):
        seen["system"], seen["user"] = system, user
        return "I shipped the viewer with 178 tests."

    monkeypatch.setattr(compose, "call", fake_call)
    coverletter._repair_ungrounded_body("Engineer", "Acme", "I led the Zorblatt migration.",
                                        BULLETS, ["Zorblatt"], "professional",
                                        background="- Example Corp\n    - notes")
    assert "BACKGROUND" in seen["user"] and "- notes" in seen["user"]
    assert "Zorblatt" in seen["user"]
    assert "any BACKGROUND notes below" in seen["system"]


def test_generate_body_hands_the_background_to_refine_and_gate(monkeypatch):
    _fake_master(monkeypatch)
    seen = {}
    monkeypatch.setattr(compose, "call", lambda *a, **k: "I shipped the viewer with 178 tests.")

    def spy(name):
        def stub(jt, co, body, bullets, **k):
            seen[name] = k
            return body
        return stub

    monkeypatch.setattr(coverletter, "refine_body", spy("refine"))
    monkeypatch.setattr(coverletter, "enforce_body_style", spy("gate"))
    coverletter.generate_body("jd", "Engineer", "Acme", BULLETS, background="- bg", seed="s")
    assert seen["refine"]["background"] == "- bg"
    assert seen["gate"]["background"] == "- bg"


# ── master_validate ───────────────────────────────────────────────────────────
_OK = {"basics": {"name": "A", "email": "a@b.c"}}


def test_validate_accepts_a_letter_seed():
    m = dict(_OK, letter={"seed": "Two sentences in my own words. That is all."})
    assert master_validate.validate_master(m) == []


def test_validate_accepts_an_empty_letter_block():
    assert master_validate.validate_master(dict(_OK, letter={})) == []
    assert master_validate.validate_master(dict(_OK, letter=None)) == []


def test_validate_warns_on_a_long_seed():
    m = dict(_OK, letter={"seed": "x" * 1201})
    out = master_validate.validate_master(m)
    assert len(out) == 1 and out[0].startswith("warning:")
    assert "letter.seed" in out[0] and "1201" in out[0] and "1200" in out[0]
    assert master_validate.validate_master(dict(_OK, letter={"seed": "x" * 1200})) == []


@pytest.mark.parametrize("letter", [{"seed": 42}, {"seed": ["a"]}, "a bare string", 7])
def test_validate_warns_on_a_malformed_letter_block(letter):
    out = master_validate.validate_master(dict(_OK, letter=letter))
    assert len(out) == 1 and out[0].startswith("warning:")
    assert "letter" in out[0]


def test_the_example_master_carries_the_letter_block():
    import yaml
    path = Path(__file__).resolve().parents[1] / "resume_tailor_files" / "master_experience.example.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data.get("letter"), dict)
    seed = data["letter"].get("seed")
    assert isinstance(seed, str) and 20 < len(seed) <= 1200
    assert master_validate.validate_master(data) == []


# ── run.py: both call sites build background + seed ───────────────────────────
def _fake_render(pdf):
    """A render stub that writes the .tex (the run ships it) and reports ok."""
    def render(body, company, tex_path, work_dir):
        Path(tex_path).write_text("x", encoding="utf-8")
        return types.SimpleNamespace(ok=True, pdf_path=pdf, error=""), ""
    return render


def test_standalone_path_passes_the_whole_master_and_the_seed(monkeypatch, tmp_path):
    monkeypatch.setattr(run_mod.assets, "load_master", lambda: MASTER)
    monkeypatch.setattr(run_mod, "pdflatex_available", lambda: True)
    monkeypatch.setattr(run_mod.llm, "reset_usage", lambda: None)
    monkeypatch.setattr(run_mod.llm, "usage_summary", lambda: "0 tokens")
    monkeypatch.setattr(run_mod.research, "company_blurb", lambda *a, **k: "")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "apply.md").write_text(
        "# Apply sheet\n\n## Work experience\n\n**X** Intern\n\n- Built it.\n\n"
        "<!-- inployed-apply-meta: {\"job_posting_id\": \"1\"} -->\n", encoding="utf-8")
    rec = {}

    def fake_body(jd, job_title, company, bullets, research="", tone="professional",
                  background="", seed=""):
        rec["background"], rec["seed"] = background, seed
        return "body"

    monkeypatch.setattr(run_mod.coverletter, "generate_body", fake_body)
    pdf = tmp_path / "c.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(run_mod.coverletter, "render_cover_letter", _fake_render(pdf))
    monkeypatch.setattr(run_mod.coverletter, "cover_letter_text", lambda b, c: "txt")
    job = {"company_name": "BigCo", "job_title": "Engineer", "job_description": "x" * 200}
    run_mod.generate_cover_letter(job, out_dir)
    assert rec["background"] == assets.flatten_entries(MASTER)
    assert "rebuilt the nightly pipeline" in rec["background"]     # every atom, no filter
    assert rec["seed"] == "I want work where the data pipeline is the product."


def test_letter_atom_ids_follow_the_bullets_that_made_the_page():
    sel = {"experience": [{"name": "Example Corp", "groups": [["a1"], ["a2"]]}],
           "projects": [{"name": "ExampleApp", "groups": [["p1"]]}],
           "leadership": [{"name": "Coding Club",
                           "groups": [["__verbatim__/Coding Club/0"]]}]}
    # one-page enforcement dropped the project bullet; the leadership block is verbatim
    final = {"a1+a2": "merged", "__verbatim__/Coding Club/0": "Exact words."}
    ids = run_mod._letter_atom_ids(sel, final, MASTER)
    assert ids == {"a1", "a2", "l1"}      # l1: the verbatim entry's own master atoms
    assert "p1" not in ids
    # ...and those ids mark whole entries: the project that printed nothing is out
    background = assets.flatten_entries(MASTER, entry_atoms=ids)
    assert "Example Corp" in background and "Coding Club" in background
    assert "ExampleApp" not in background


def test_letter_inputs_warn_when_the_background_is_truncated(monkeypatch):
    """The cap is 30,000; hitting it is said out loud (log + advisory) with the
    count of achievement notes that never reached the prompt (entries are all
    kept, so the count is atoms)."""
    big = {"experience": [{"org": f"Org {i}", "title": "T", "dates": "2020",
                           "achievements": [{"id": f"x{i}{j}", "what": "w" * 200}
                                            for j in range(5)]}
                          for i in range(60)]}
    monkeypatch.setattr(run_mod.assets, "load_master", lambda: big)
    logs, warns = [], []
    background, seed = run_mod._letter_inputs(None, {}, logs.append, warn=warns.append)
    assert background.endswith(assets.TRUNCATED_MARKER) and seed == ""
    assert warns == logs and len(logs) == 1
    kept = sum(1 for ln in background.splitlines() if ln.startswith("    - "))
    assert logs[0] == (f"cover letter background truncated at 30,000 characters "
                       f"({300 - kept} of 300 achievement notes left out, every entry kept)")
    assert 0 < kept < 300
    assert sum(1 for ln in background.splitlines() if ln.startswith("- Org ")) == 60
    # a master that fits says nothing
    logs.clear()
    monkeypatch.setattr(run_mod.assets, "load_master", lambda: MASTER)
    run_mod._letter_inputs(None, {}, logs.append, warn=warns.append)
    assert logs == []


def test_letter_inputs_degrade_to_blank_and_say_so(monkeypatch):
    """A master the flattening cannot read leaves the letter to run from the
    bullets alone: it logs a line and an advisory, and stays an ordinary return."""
    def boom():
        raise ValueError("bad yaml")

    monkeypatch.setattr(run_mod.assets, "load_master", boom)
    logs, warns = [], []
    assert run_mod._letter_inputs(None, {"b0": "x"}, logs.append, warn=warns.append) == ("", "")
    assert logs and "background unavailable" in logs[0] and "bad yaml" in logs[0]
    assert warns == logs


def test_tailor_path_passes_selected_background_and_seed(monkeypatch, tmp_path):
    """The tailor run hands generate_body the notes behind the bullets that made
    the page, plus the seed. Everything else is stubbed as in test_resume_artifacts."""
    monkeypatch.setattr(run_mod.assets, "load_master", lambda: MASTER)
    sel = {"experience": [{"name": "Example Corp", "groups": [["a2"]]}],
           "projects": [], "leadership": []}
    bullets = {"a2": "Raised the coverage and caught three regressions."}
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.setattr(run_mod, "pdflatex_available", lambda: True)
    monkeypatch.setattr(run_mod.compose, "select", lambda *a, **k: sel)
    monkeypatch.setattr(run_mod.compose, "inject_verbatim", lambda *a, **k: {})
    monkeypatch.setattr(run_mod.compose, "block_briefs", lambda *a, **k: {})
    monkeypatch.setattr(run_mod, "_resolve_bullets", lambda *a, **k: dict(bullets))
    monkeypatch.setattr(run_mod, "_trim_to_caps", lambda *a, **k: None)
    monkeypatch.setattr(run_mod.sweep, "sweep_items",
                        lambda *a, **k: run_mod.sweep.SweepResult(
                            changed=(), rejected=(), reasked=(), unfixed_p2=(),
                            unfixed_phrasing=(), calls=0, items=0, failures=()))
    monkeypatch.setattr(run_mod.compose, "compress_skills", lambda *a, **k: ["Python"])
    monkeypatch.setattr(run_mod.output, "resolve_dir", lambda *a, **k: out_dir)
    monkeypatch.setattr(run_mod.output, "resume_filename", lambda: "resume.pdf")
    monkeypatch.setattr(run_mod.output, "cover_filename", lambda: "cover.pdf")
    monkeypatch.setattr(run_mod.output, "cover_tex_filename", lambda: "cover.tex")
    monkeypatch.setattr(run_mod.llm, "reset_usage", lambda: None)
    monkeypatch.setattr(run_mod.llm, "usage_summary", lambda: "0 tokens")
    pdf = tmp_path / "compiled.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    result = types.SimpleNamespace(ok=True, pdf_path=pdf, error="", log_tail="")

    def fake_enforce(sel_, bullets_, skills_, tex_path, tmp_, jd, on_status=None):
        Path(tex_path).write_text("x", encoding="utf-8")
        return result, dict(bullets_), "TEX"

    monkeypatch.setattr(run_mod, "enforce_one_page", fake_enforce)
    monkeypatch.setattr(run_mod.ats, "write_report", lambda *a, **k: 0.5)
    monkeypatch.setattr(run_mod.research, "company_blurb", lambda *a, **k: "")
    rec = {}

    def fake_body(jd, job_title, company, bullets, research="", tone="professional",
                  background="", seed=""):
        rec["background"], rec["seed"] = background, seed
        return "cover body"

    monkeypatch.setattr(run_mod.coverletter, "generate_body", fake_body)
    monkeypatch.setattr(run_mod.coverletter, "render_cover_letter", _fake_render(pdf))
    monkeypatch.setattr(run_mod.coverletter, "cover_letter_text", lambda b, c: "txt")
    monkeypatch.setattr(run_mod.apply_data, "write", lambda *a, **k: None)
    job = {"company_name": "BigCo", "job_title": "Engineer",
           "job_description": "x" * 200, "url": "http://x"}
    run_mod.tailor(job, cover_letter=True)
    assert rec["background"] == assets.flatten_entries(MASTER, entry_atoms={"a2"})
    # the whole entry that printed: the selected atom and its sibling
    assert "raised test coverage" in rec["background"]
    assert "rebuilt the nightly pipeline" in rec["background"]
    # entries that printed nothing stay out
    assert "ExampleApp" not in rec["background"] and "Coding Club" not in rec["background"]
    assert rec["seed"] == "I want work where the data pipeline is the product."
