"""Tests for the self-contained apply.md sheet (cycle 13: résumé-rich).

apply_data.write() renders one apply.md per tailored folder: the no-submit
fill-it-out playbook at the top, the candidate basics + structured address, the
active standard answers (work auth / EEO / how-did-you-hear), THIS JOB'S TAILORED
RÉSUMÉ translated into markdown (Work experience / Projects / Leadership /
Technical skills — built deterministically from the tailor's own selection +
surviving bullets, NO LLM call), and a hidden meta marker carrying the job
identity. The résumé sections include ONLY the blocks the tailor selected for this
job, with each block's surviving bullet text verbatim. No apply_data.json is
written. The write tests pin STORE_PATH + APPLY_CONFIG to temp paths so they never
read the developer's real files.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import apply_answers, apply_config, apply_data, assets, output  # noqa: E402


_MASTER = {
    "basics": {"name": "Test Person", "email": "t@example.com", "phone": "555",
               "location": "NYC", "linkedin": "li", "github": "gh"},
    "education": [{"school": "Uni", "degree": "BS", "dates": "2020-2024"}],
    "experience": [
        {"org": "Acme Corp", "title": "SWE Intern", "location": "NYC",
         "dates": "2024-06 / 2024-08",
         "achievements": [{"id": "a1"}, {"id": "a2"}]},
        {"org": "Other Co", "title": "Analyst", "location": "Remote",
         "dates": "2023", "achievements": [{"id": "z1"}]},
    ],
    "projects": [
        {"name": "CoolApp", "dates": "2024", "repo": "gh/cool",
         "live_url": "http://cool", "achievements": [{"id": "p1"}]},
    ],
    "leadership": [
        {"org": "Coding Club", "title": "President", "dates": "2023-2024",
         "achievements": [{"id": "l1"}]},
    ],
}

_JOB = {"job_posting_id": "42", "company_name": "Acme", "job_title": "Engineer",
        "url": "https://example.com/job/42"}

# What the tailor would hand apply_data.write(): the selection, the bullets that
# survived one-page enforcement (keyed by group key = "+".join(atom_ids)), and the
# compressed skill lines.
_SEL = {
    "experience": [{"name": "Acme Corp", "groups": [["a1"], ["a2"]]}],
    "projects": [{"name": "CoolApp", "groups": [["p1"]]}],
    "leadership": [{"name": "Coding Club", "groups": [["l1"]]}],
}
_BULLETS = {"a1": "Built the ingestion pipeline fast.", "a2": "Cut cloud spend 40%.",
            "p1": "Shipped CoolApp end to end.", "l1": "Grew membership threefold."}
_SKILLS = [{"label": "Languages", "items": "Python, SQL"}]


@pytest.fixture(autouse=True)
def _stub_master(monkeypatch):
    monkeypatch.setattr(apply_data.assets, "load_master", lambda: _MASTER)


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    # Never read the developer's real apply config / answer store.
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")


# --- load_apply_config -------------------------------------------------------

def test_load_apply_config_defaults_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "missing.json")
    cfg = apply_config.load_apply_config()
    assert cfg["work_authorized"] is True
    assert cfg["requires_sponsorship"] is False
    assert cfg["willing_to_relocate"] is True
    assert cfg["years_experience"] == "0"
    assert cfg["how_did_you_hear"] == "LinkedIn"
    assert cfg["gender"] == "Decline to self-identify"
    assert cfg["disability_status"] == "Decline to self-identify"
    assert cfg["address_country"] == "United States"
    assert "no visa sponsorship" in cfg["authorization_statement"].lower()


def test_load_apply_config_override_is_honored(tmp_path, monkeypatch):
    path = tmp_path / "apply_config.json"
    path.write_text(json.dumps({"willing_to_relocate": False,
                                "how_did_you_hear": "Referral"}), encoding="utf-8")
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", path)
    cfg = apply_config.load_apply_config()
    assert cfg["willing_to_relocate"] is False
    assert cfg["how_did_you_hear"] == "Referral"
    assert cfg["work_authorized"] is True
    assert cfg["requires_sponsorship"] is False


def test_load_apply_config_ignores_unreadable_file(tmp_path, monkeypatch):
    path = tmp_path / "apply_config.json"
    path.write_text("not json{", encoding="utf-8")
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", path)
    cfg = apply_config.load_apply_config()
    assert cfg["work_authorized"] is True


# --- apply_data.write produces a self-contained apply.md ---------------------

def test_write_creates_apply_md_not_json(tmp_path):
    out = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS, skill_lines=_SKILLS)

    assert out.name == "apply.md"
    assert out.exists()
    assert not (tmp_path / "apply_data.json").exists()  # JSON is gone

    text = out.read_text(encoding="utf-8")
    assert "Test Person" in text and "t@example.com" in text      # candidate
    assert "Uni" in text                                          # education


def test_write_embeds_no_submit_playbook(tmp_path):
    text = apply_data.write(_JOB, tmp_path).read_text(encoding="utf-8").lower()
    assert "never" in text and "submit" in text          # never-submit contract
    assert "electronic signature" in text                # sign with name + date
    assert "xxxxx" in text                               # placeholder-for-blocking-required
    assert "captcha" in text and "password" in text      # safety walls preserved


def test_write_playbook_notes_optional_unknowns_for_review(tmp_path):
    text = apply_data.write(_JOB, tmp_path).read_text(encoding="utf-8")
    # Cycle 13 copy tweak: optional unknowns are now flagged for review too.
    assert "note them for review too" in text
    assert "Leave optional unknowns blank." not in text  # the bare old wording is gone


def test_write_marker_roundtrips_job_identity(tmp_path):
    text = apply_data.write(_JOB, tmp_path).read_text(encoding="utf-8")
    meta = apply_data.parse_marker(text)
    assert meta["job_posting_id"] == "42"
    assert meta["url"] == "https://example.com/job/42"
    assert meta["company"] == "Acme"


def test_write_includes_structured_address(tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    monkeypatch.setattr(apply_answers, "STORE_PATH", store)
    ans = apply_answers.seed_defaults()
    by = {e["id"]: e for e in ans}
    by["address_street"]["answer"] = "1 Main St"
    by["address_city"]["answer"] = "Boston"
    by["address_state"]["answer"] = "MA"
    by["address_zip"]["answer"] = "02100"
    apply_answers.save(ans, store)
    text = apply_data.write(_JOB, tmp_path).read_text(encoding="utf-8")
    for part in ("1 Main St", "Boston", "MA", "02100", "United States"):
        assert part in text


def test_write_standard_answers_render_bools_and_exclude_address(tmp_path):
    text = apply_data.write(_JOB, tmp_path).read_text(encoding="utf-8")
    assert "Yes" in text and "No" in text                 # work auth Yes / sponsorship No
    assert "Street address (line 1)." not in text         # address not repeated here


def test_write_has_no_documents_or_upload_language(tmp_path):
    # apply.md is for portals that DON'T auto-fill from a résumé upload, so it no
    # longer carries a Documents section or any directive to upload files.
    text = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                            skill_lines=_SKILLS).read_text(encoding="utf-8")
    assert "## Documents" not in text                  # the upload section is gone
    assert "Upload the résumé PDF" not in text         # the upload directive is gone
    assert output.resume_filename() not in text        # no document path is listed in the sheet
    assert "## Work experience" in text                # ...but the résumé itself still renders


def test_write_explains_when_to_use_as_fallback(tmp_path):
    text = apply_data.write(_JOB, tmp_path).read_text(encoding="utf-8")
    assert "When to use this sheet" in text            # purpose is spelled out
    low = text.lower()
    assert "auto-fill" in low and "by hand" in low     # fallback framing: normal=auto-fill, this=by hand


# --- résumé sections mirror this job's tailored résumé (deterministic) --------

def test_write_renders_selected_experience_with_headers_and_bullets(tmp_path):
    text = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                            skill_lines=_SKILLS).read_text(encoding="utf-8")
    assert "## Work experience" in text
    assert "Acme Corp" in text and "SWE Intern" in text          # org + title
    assert "2024-06 / 2024-08" in text                           # dates
    assert "Built the ingestion pipeline fast." in text          # bullet 1 verbatim
    assert "Cut cloud spend 40%." in text                        # bullet 2 verbatim


def test_write_includes_only_selected_blocks(tmp_path):
    # "Other Co" is in the master but NOT in the selection for this job.
    text = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                            skill_lines=_SKILLS).read_text(encoding="utf-8")
    assert "Other Co" not in text
    assert "Analyst" not in text


def test_write_renders_projects_and_leadership(tmp_path):
    text = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                            skill_lines=_SKILLS).read_text(encoding="utf-8")
    assert "## Projects" in text and "CoolApp" in text
    assert "Shipped CoolApp end to end." in text
    assert "## Leadership" in text and "Coding Club" in text and "President" in text
    assert "Grew membership threefold." in text


def test_write_renders_skills_from_skill_lines(tmp_path):
    text = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                            skill_lines=_SKILLS).read_text(encoding="utf-8")
    assert "## Technical skills" in text
    assert "Languages" in text and "Python, SQL" in text


def test_write_skips_groups_whose_bullet_didnt_survive(tmp_path):
    # a2's bullet was trimmed away on one-page enforcement -> not in the dict.
    bullets = {"a1": "Built the ingestion pipeline fast.", "p1": "Shipped CoolApp.",
               "l1": "Grew membership threefold."}
    text = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=bullets,
                            skill_lines=_SKILLS).read_text(encoding="utf-8")
    assert "Built the ingestion pipeline fast." in text
    assert "Cut cloud spend 40%." not in text   # dropped group is absent


def test_write_without_selection_renders_note_not_crash(tmp_path):
    # Backfill / CLI path: no tailoring data available.
    text = apply_data.write(_JOB, tmp_path).read_text(encoding="utf-8")
    assert "## Work experience" not in text
    assert "Re-tailor" in text                  # the placeholder note
    # the rest of the sheet is still valid
    assert "Test Person" in text and apply_data.parse_marker(text)["job_posting_id"] == "42"


def test_write_from_folder_backfills_without_resume_sections(tmp_path):
    out = apply_data.write_from_folder(tmp_path, _JOB)
    assert out.name == "apply.md"
    text = out.read_text(encoding="utf-8")
    assert "## Work experience" not in text and "Re-tailor" in text


# --- assets.full_url ----------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "https://linkedin.com/in/t",
    "http://cool.dev",
    "HTTPS://GitHub.com/t",     # scheme check is case-insensitive
    "HTTP://x.io",
])
def test_full_url_is_idempotent_on_scheme_prefixed_values(raw):
    assert assets.full_url(raw) == raw


def test_full_url_prepends_https_to_bare_values():
    assert assets.full_url("linkedin.com/in/t") == "https://linkedin.com/in/t"
    assert assets.full_url("  github.com/t  ") == "https://github.com/t"  # stripped first


def test_full_url_empty_values_stay_blank():
    assert assets.full_url("") == ""
    assert assets.full_url(None) == ""
    assert assets.full_url("   ") == ""


# --- item 3: contact block carries full https:// links -------------------------

def test_write_contact_links_are_full_https_urls(tmp_path):
    text = apply_data.write(_JOB, tmp_path).read_text(encoding="utf-8")
    assert "- **LinkedIn:** https://li\n" in text
    assert "- **GitHub / Portfolio:** https://gh\n" in text


# --- item 6: project entry headers carry no dates ------------------------------

def test_write_project_headers_carry_no_dates(tmp_path):
    text = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                            skill_lines=_SKILLS).read_text(encoding="utf-8")
    assert "**CoolApp**\n" in text            # bare header
    assert "**CoolApp** — " not in text       # no " — 2024" suffix
    # experience / leadership / education dates are untouched
    assert "2024-06 / 2024-08" in text        # experience
    assert "2023-2024" in text                # leadership
    assert "2020-2024" in text                # education


# --- item 7: honors/awards sub-bullet under education ---------------------------

def _master_with_honors(honors):
    m = json.loads(json.dumps(_MASTER))
    m["education"][0]["honors"] = honors
    return m


def test_education_honors_list_renders_awards_sub_bullet():
    md = apply_data.build_markdown(
        _master_with_honors(["Dean's List", "Honors College"]), _JOB, [])
    assert "  - Awards & Honors: Dean's List; Honors College\n" in md


def test_education_honors_scalar_is_coerced_to_one_item():
    md = apply_data.build_markdown(_master_with_honors("Dean's List"), _JOB, [])
    assert "  - Awards & Honors: Dean's List\n" in md


def test_education_without_honors_has_no_awards_line():
    assert "Awards & Honors" not in apply_data.build_markdown(dict(_MASTER), _JOB, [])
    assert "Awards & Honors" not in apply_data.build_markdown(_master_with_honors([]), _JOB, [])


# --- parse_resume_bullets: apply.md -> the tailored résumé bullets --------------

def test_parse_resume_bullets_roundtrip_from_build_markdown():
    # Feed a full generated sheet through the parser: exactly the work /
    # projects / leadership bullets come back, in document order.
    md = apply_data.build_markdown(
        _master_with_honors(["Dean's List"]), _JOB, [],
        sel=_SEL, bullets=_BULLETS, skill_lines=_SKILLS)
    got = apply_data.parse_resume_bullets(md)
    assert got == [
        "Built the ingestion pipeline fast.",
        "Cut cloud spend 40%.",
        "Shipped CoolApp end to end.",
        "Grew membership threefold.",
    ]


def test_parse_resume_bullets_excludes_skills_education_and_answers():
    md = apply_data.build_markdown(
        _master_with_honors(["Dean's List"]), _JOB,
        [{"id": "how_did_you_hear", "question": "How did you hear?",
          "answer": "LinkedIn", "status": "active"}],
        sel=_SEL, bullets=_BULLETS, skill_lines=_SKILLS)
    got = apply_data.parse_resume_bullets(md)
    joined = "\n".join(got)
    assert "Python, SQL" not in joined          # Technical skills excluded
    assert "Dean's List" not in joined       # education honors sub-bullet
    assert "Uni" not in joined                  # education entry line
    assert "LinkedIn" not in joined             # standard answers
    assert "**" not in joined                   # no entry headers leak in


def test_parse_resume_bullets_placeholder_returns_empty():
    # Backfilled / untailored sheet: the re-tailor note stands in for the résumé.
    md = apply_data.build_markdown(dict(_MASTER), _JOB, [])
    assert "Re-tailor" in md
    assert apply_data.parse_resume_bullets(md) == []


def test_parse_resume_bullets_tolerates_hand_edits():
    md = (
        "# Apply sheet — Engineer @ Acme\n\n"
        "## Work experience\n\n"
        "**Acme Corp** — SWE · NYC\n"
        "a stray sentence the user typed by hand\n"
        "- Built a pipeline.\n"
        "  - an indented sub-note must not count\n\n"
        "## Projects\n\n"
        "**CoolApp**\n"
        "*https://cool · gh/cool*\n\n"
        "- Shipped CoolApp.\n\n"
        "## Leadership\n\n"
        "**Coding Club** — President\n\n"
        "- Grew membership.\n\n"
        "## Technical skills\n"
        "- **Languages:** Python\n"
    )
    assert apply_data.parse_resume_bullets(md) == [
        "Built a pipeline.", "Shipped CoolApp.", "Grew membership."]


def test_parse_resume_bullets_empty_and_sectionless_input():
    assert apply_data.parse_resume_bullets("") == []
    # bullets outside the three résumé sections never count
    assert apply_data.parse_resume_bullets("## Education\n- Uni — BS\n") == []


# --- refresh_standard_answers: splice ONLY the Standard-answers section ----------

def _seed_store(tmp_path, **overrides):
    store = tmp_path / "apply_answers.json"
    ans = apply_answers.seed_defaults()
    by = {e["id"]: e for e in ans}
    for key, val in overrides.items():
        by[key]["answer"] = val
    apply_answers.save(ans, store)
    return store


def _spans(raw: bytes):
    """(start-of-Standard-answers, start-of-Electronic-signature) byte offsets."""
    start = raw.index(b"## Standard answers")
    sig = raw.index(b"## Electronic signature")
    return start, sig


def test_refresh_standard_answers_roundtrip_touches_only_the_span(tmp_path):
    _seed_store(tmp_path, how_did_you_hear="LinkedIn")
    out = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                           skill_lines=_SKILLS)
    before = out.read_bytes()
    assert b"LinkedIn" in before

    _seed_store(tmp_path, how_did_you_hear="Referral from a friend")
    got = apply_data.refresh_standard_answers(tmp_path)
    assert got == out

    after = out.read_bytes()
    text = after.decode("utf-8")
    assert "Referral from a friend" in text
    start_b, sig_b = _spans(before)
    start_a, sig_a = _spans(after)
    assert before[:start_b] == after[:start_a]     # everything above: byte-identical
    assert before[sig_b:] == after[sig_a:]         # signature + marker: byte-identical
    # the tailored résumé bullets survive the splice untouched
    assert apply_data.parse_resume_bullets(text) == \
        apply_data.parse_resume_bullets(before.decode("utf-8"))
    assert apply_data.parse_resume_bullets(text) == [
        "Built the ingestion pipeline fast.",
        "Cut cloud spend 40%.",
        "Shipped CoolApp end to end.",
        "Grew membership threefold.",
    ]
    # the meta marker still round-trips
    assert apply_data.parse_marker(text)["job_posting_id"] == "42"


def test_refresh_standard_answers_unchanged_store_is_byte_identical(tmp_path):
    _seed_store(tmp_path)
    out = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                           skill_lines=_SKILLS)
    before = out.read_bytes()
    assert apply_data.refresh_standard_answers(tmp_path) == out
    assert out.read_bytes() == before              # a no-change refresh is a no-op


def _rewrite_eol(path, eol: bytes):
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    if eol != b"\n":
        raw = raw.replace(b"\n", eol)
    path.write_bytes(raw)


def test_refresh_standard_answers_preserves_lf_endings(tmp_path):
    # A hand-curated apply.md saved with LF endings must come back LF: the
    # splice contract says every byte outside the span is identical, and
    # write_text's os.linesep translation used to CRLF-ify the whole file.
    _seed_store(tmp_path, how_did_you_hear="LinkedIn")
    out = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                           skill_lines=_SKILLS)
    _rewrite_eol(out, b"\n")
    before = out.read_bytes()
    assert b"\r" not in before
    _seed_store(tmp_path, how_did_you_hear="Referral from a friend")
    assert apply_data.refresh_standard_answers(tmp_path) == out
    after = out.read_bytes()
    assert b"\r" not in after                      # still LF everywhere
    assert b"Referral from a friend" in after
    start_b, sig_b = _spans(before)
    start_a, sig_a = _spans(after)
    assert before[:start_b] == after[:start_a]     # bytes outside span identical
    assert before[sig_b:] == after[sig_a:]


def test_refresh_standard_answers_lf_unchanged_store_is_byte_identical(tmp_path):
    _seed_store(tmp_path)
    out = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                           skill_lines=_SKILLS)
    _rewrite_eol(out, b"\n")
    before = out.read_bytes()
    assert apply_data.refresh_standard_answers(tmp_path) == out
    assert out.read_bytes() == before


def test_refresh_standard_answers_preserves_crlf_endings(tmp_path):
    # Platform-independent pin of the CRLF case (on Windows write() already
    # emits CRLF; on POSIX it wouldn't — force it so the pin holds everywhere).
    _seed_store(tmp_path, how_did_you_hear="LinkedIn")
    out = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                           skill_lines=_SKILLS)
    _rewrite_eol(out, b"\r\n")
    before = out.read_bytes()
    _seed_store(tmp_path, how_did_you_hear="Referral from a friend")
    assert apply_data.refresh_standard_answers(tmp_path) == out
    after = out.read_bytes()
    assert after.count(b"\n") == after.count(b"\r\n")   # no bare LF introduced
    assert b"Referral from a friend" in after
    start_b, sig_b = _spans(before)
    start_a, sig_a = _spans(after)
    assert before[:start_b] == after[:start_a]
    assert before[sig_b:] == after[sig_a:]


def test_refresh_standard_answers_missing_file_returns_none(tmp_path):
    assert apply_data.refresh_standard_answers(tmp_path) is None


def test_refresh_standard_answers_missing_headings_returns_none(tmp_path):
    md = tmp_path / "apply.md"
    md.write_text("# Apply sheet\n\n## Candidate\n- **Name:** X\n", encoding="utf-8")
    before = md.read_bytes()
    assert apply_data.refresh_standard_answers(tmp_path) is None
    assert md.read_bytes() == before               # untouched when it can't splice

    # Standard answers present but no signature heading -> still None, untouched
    md.write_text("## Standard answers\n- **Q** A\n", encoding="utf-8")
    before = md.read_bytes()
    assert apply_data.refresh_standard_answers(tmp_path) is None
    assert md.read_bytes() == before


def test_refresh_standard_answers_never_regenerates_tailored_content(tmp_path):
    # A hand-edited résumé bullet outside the span must survive a refresh —
    # proof the function splices instead of calling write_from_folder.
    _seed_store(tmp_path)
    out = apply_data.write(_JOB, tmp_path, sel=_SEL, bullets=_BULLETS,
                           skill_lines=_SKILLS)
    text = out.read_text(encoding="utf-8")
    text = text.replace("Built the ingestion pipeline fast.",
                        "Built the ingestion pipeline REALLY fast.")
    out.write_text(text, encoding="utf-8")
    _seed_store(tmp_path, how_did_you_hear="Referral")
    apply_data.refresh_standard_answers(tmp_path)
    refreshed = out.read_text(encoding="utf-8")
    assert "Built the ingestion pipeline REALLY fast." in refreshed
    assert "Referral" in refreshed
