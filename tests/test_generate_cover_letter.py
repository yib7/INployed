"""run.generate_cover_letter — a cover letter for an ALREADY-tailored job.

The bullets come from parsing the folder's existing apply.md (written on every
tailor, deterministic), so no re-tailor is needed. Fully offline: the LLM body
generation, the LaTeX render, and the pdflatex check are all stubbed — the tests
pin the orchestration (guards, friendly errors, the copied output path) and the
exact bullets shape handed to coverletter.generate_body (a dict whose values are
the bullet texts, same shape tailor passes).
"""
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import run as run_mod  # noqa: E402

_JOB = {"company_name": "BigCo", "job_title": "Engineer",
        "job_description": "x" * 200, "url": "http://x"}

# A minimal but structurally-faithful apply.md: résumé sections + the decoys the
# parser must skip (Candidate contact bullets, Technical skills, meta marker).
_APPLY_MD = """# Apply sheet — Engineer @ BigCo

## Candidate
- **Name:** Test Person
- **LinkedIn:** https://li

## Work experience

**BigCo** — Intern · NYC

- Built the ingestion pipeline fast.
- Cut cloud spend 40%.

## Projects

**CoolApp**
*https://cool*

- Shipped CoolApp end to end.

## Technical skills
- **Languages:** Python, SQL

<!-- inployed-apply-meta: {"job_posting_id": "42"} -->
"""

_PLACEHOLDER_MD = """# Apply sheet — Engineer @ BigCo

## Résumé
_(Re-tailor this job to embed the résumé contents here.)_

<!-- inployed-apply-meta: {"job_posting_id": "42"} -->
"""


@pytest.fixture()
def offline_cover(monkeypatch, tmp_path):
    """Stub the LLM/compile/pdflatex touchpoints; return (rec, out_dir)."""
    rec = {"research": 0, "statuses": []}
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "apply.md").write_text(_APPLY_MD, encoding="utf-8")

    monkeypatch.setattr(run_mod, "pdflatex_available", lambda: True)
    monkeypatch.setattr(run_mod.llm, "reset_usage", lambda: None)
    monkeypatch.setattr(run_mod.llm, "usage_summary", lambda: "0 tokens")

    def fake_blurb(company, job_title):
        rec["research"] += 1
        return "a company blurb"

    monkeypatch.setattr(run_mod.research, "company_blurb", fake_blurb)

    def fake_body(jd, job_title, company, bullets, research="", tone="professional"):
        rec["jd"] = jd
        rec["bullets"] = bullets
        rec["research_text"] = research
        rec["tone"] = tone
        return "cover body"

    monkeypatch.setattr(run_mod.coverletter, "generate_body", fake_body)

    pdf = tmp_path / "compiled_cover.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake cover")
    cl_result = types.SimpleNamespace(ok=True, pdf_path=pdf, error="")
    rec["cl_result"] = cl_result

    def fake_render(body, company, tex_path, work_dir):
        # the real render writes the .tex before compiling; the run ships that file
        Path(tex_path).write_text("\\documentclass[11pt]{article}", encoding="utf-8")
        return cl_result, ""

    monkeypatch.setattr(run_mod.coverletter, "render_cover_letter", fake_render)
    monkeypatch.setattr(run_mod.output, "cover_filename", lambda: "cover.pdf")
    monkeypatch.setattr(run_mod.output, "cover_tex_filename", lambda: "cover.tex")
    monkeypatch.setattr(run_mod.coverletter, "cover_letter_text",
                        lambda body, company: f"TXT:{body}:{company}")
    return rec, out_dir


def test_happy_path_copies_pdf_and_returns_path(offline_cover):
    rec, out_dir = offline_cover
    statuses = []
    got = run_mod.generate_cover_letter(_JOB, out_dir, tone="professional",
                                        on_status=statuses.append)
    assert got == out_dir / "cover.pdf"
    assert got.read_bytes() == b"%PDF-1.4 fake cover"
    assert rec["research"] == 1
    assert statuses  # progress was reported


def test_writes_tex_sibling_and_no_txt(offline_cover):
    # The LaTeX source lands beside the PDF so a manual fix recompiles without
    # regenerating; the copy-paste text now lives in apply.md, not a .txt.
    rec, out_dir = offline_cover
    run_mod.generate_cover_letter(_JOB, out_dir, tone="professional")
    tex = out_dir / "cover.tex"
    assert tex.exists()
    assert "documentclass" in tex.read_text(encoding="utf-8")
    assert not (out_dir / "cover.txt").exists()
    assert list(out_dir.glob("*_Cover_Letter.txt")) == []


def test_regenerate_patches_the_letter_into_apply_md(offline_cover):
    # generate_cover_letter runs against a folder whose apply.md already exists,
    # so the letter is SPLICED in — the tailored résumé sections must survive.
    from resume_tailor import apply_data
    rec, out_dir = offline_cover
    run_mod.generate_cover_letter(_JOB, out_dir, tone="professional")
    md = (out_dir / "apply.md").read_text(encoding="utf-8")
    assert "## Cover letter" in md
    assert "TXT:cover body:BigCo" in md
    assert apply_data.parse_resume_bullets(md) == [
        "Built the ingestion pipeline fast.",
        "Cut cloud spend 40%.",
        "Shipped CoolApp end to end.",
    ]


def test_regenerate_migrates_a_legacy_txt_into_apply_md(offline_cover):
    # A folder tailored before the switch still has the old .txt; regenerating
    # replaces it with the apply.md section and drops the stale file.
    rec, out_dir = offline_cover
    legacy = out_dir / "Cand_Cover_Letter.txt"
    legacy.write_text("STALE LETTER", encoding="utf-8")
    run_mod.generate_cover_letter(_JOB, out_dir, tone="professional")
    md = (out_dir / "apply.md").read_text(encoding="utf-8")
    assert "TXT:cover body:BigCo" in md and "STALE LETTER" not in md
    assert not legacy.exists()


def test_bullets_shape_matches_tailors_dict_of_texts(offline_cover):
    # generate_body reads bullets.values() (tailor hands it the gkey->text dict);
    # the parsed flat list is adapted to the same dict-of-texts shape, in order.
    rec, out_dir = offline_cover
    run_mod.generate_cover_letter(_JOB, out_dir, tone="professional")
    assert isinstance(rec["bullets"], dict)
    assert list(rec["bullets"].values()) == [
        "Built the ingestion pipeline fast.",
        "Cut cloud spend 40%.",
        "Shipped CoolApp end to end.",
    ]


def test_tone_and_research_are_passed_through(offline_cover):
    rec, out_dir = offline_cover
    run_mod.generate_cover_letter(_JOB, out_dir, tone="enthusiastic")
    assert rec["tone"] == "enthusiastic"
    assert rec["research_text"] == "a company blurb"


def test_research_failure_is_nonfatal(offline_cover, monkeypatch):
    rec, out_dir = offline_cover

    def boom(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(run_mod.research, "company_blurb", boom)
    got = run_mod.generate_cover_letter(_JOB, out_dir, tone="professional")
    assert got.exists()
    assert rec["research_text"] == ""


def test_missing_apply_md_says_retailor(offline_cover):
    _rec, out_dir = offline_cover
    (out_dir / "apply.md").unlink()
    with pytest.raises(RuntimeError, match="[Rr]e-tailor"):
        run_mod.generate_cover_letter(_JOB, out_dir, tone="professional")


def test_placeholder_apply_md_says_retailor(offline_cover):
    _rec, out_dir = offline_cover
    (out_dir / "apply.md").write_text(_PLACEHOLDER_MD, encoding="utf-8")
    with pytest.raises(RuntimeError, match="[Rr]e-tailor"):
        run_mod.generate_cover_letter(_JOB, out_dir, tone="professional")


def test_short_jd_raises_friendly_error(offline_cover):
    _rec, out_dir = offline_cover
    job = dict(_JOB, job_description="too short")
    with pytest.raises(RuntimeError, match="too short"):
        run_mod.generate_cover_letter(job, out_dir, tone="professional")


def test_missing_pdflatex_raises(offline_cover, monkeypatch):
    _rec, out_dir = offline_cover
    monkeypatch.setattr(run_mod, "pdflatex_available", lambda: False)
    with pytest.raises(RuntimeError, match="pdflatex"):
        run_mod.generate_cover_letter(_JOB, out_dir, tone="professional")


def test_compile_failure_raises(offline_cover):
    rec, out_dir = offline_cover
    rec["cl_result"].ok = False
    rec["cl_result"].pdf_path = None
    rec["cl_result"].error = "boom"
    with pytest.raises(RuntimeError, match="compile"):
        run_mod.generate_cover_letter(_JOB, out_dir, tone="professional")
