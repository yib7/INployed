"""Tests for the apply launcher (SP4 T4.2).

resolve_generated_dir() locates the tailored-resume folder for a job (by
company+title, or by scanning ~/Downloads/Generated_Resumes/**/apply_data.json
for a matching job_posting_id, newest wins). build_apply_context() loads that
profile, asserts the résumé PDF exists, and returns absolute paths + the apply
URL. Neither ever submits anything.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import apply  # noqa: E402


def _make_folder(base: Path, company: str, title: str, job_id: str,
                 *, with_pdf: bool = True, sub: str = "", url: str = "http://x") -> Path:
    folder = base / company / title
    if sub:
        folder = folder / sub
    folder.mkdir(parents=True, exist_ok=True)
    pdf = folder / "Cand_Resume.pdf"
    if with_pdf:
        pdf.write_bytes(b"%PDF-1.4 fake")
    (folder / "apply_data.json").write_text(json.dumps({
        "candidate": {"full_name": "Cand"},
        "documents": {"resume_pdf": str(pdf), "cover_letter_pdf": ""},
        "job": {"job_posting_id": job_id, "company": company, "title": title, "url": url},
        "standard_answers": {"requires_sponsorship": False},
    }), encoding="utf-8")
    return folder


@pytest.fixture
def base(tmp_path, monkeypatch):
    root = tmp_path / "Generated_Resumes"
    root.mkdir()
    monkeypatch.setattr(apply, "BASE_DIR", root)
    return root


# --- resolve_generated_dir ---------------------------------------------------

def test_resolve_by_job_id_finds_the_right_folder(base):
    _make_folder(base, "Acme", "Engineer", "111")
    target = _make_folder(base, "Beta", "Analyst", "222")
    found = apply.resolve_generated_dir(job_id="222")
    assert found == target


def test_resolve_by_job_id_picks_most_recent_on_collision(base):
    import os
    import time
    old = _make_folder(base, "Acme", "Engineer", "999", sub="2026-01-01")
    new = _make_folder(base, "Acme", "Engineer", "999", sub="2026-02-02")
    # make `new`'s apply_data.json newer than `old`'s
    later = time.time()
    os.utime(old / "apply_data.json", (later - 100, later - 100))
    os.utime(new / "apply_data.json", (later, later))
    assert apply.resolve_generated_dir(job_id="999") == new


def test_resolve_missing_job_raises_filenotfound(base):
    _make_folder(base, "Acme", "Engineer", "111")
    with pytest.raises(FileNotFoundError) as exc:
        apply.resolve_generated_dir(job_id="nope")
    assert "tailor" in str(exc.value).lower()


def test_resolve_by_company_title_uses_output_resolve_dir(base, monkeypatch):
    target = _make_folder(base, "Acme", "Engineer", "111")
    monkeypatch.setattr(apply.output, "resolve_dir", lambda c, t: target)
    assert apply.resolve_generated_dir(company="Acme", title="Engineer") == target


# --- build_apply_context -----------------------------------------------------

def test_build_apply_context_returns_profile_and_abs_paths(base):
    folder = _make_folder(base, "Beta", "Analyst", "222", url="http://job/222")
    ctx = apply.build_apply_context(folder)
    assert ctx["job"]["url"] == "http://job/222"
    assert ctx["candidate"]["full_name"] == "Cand"
    assert Path(ctx["resume_pdf"]).is_absolute()
    assert Path(ctx["resume_pdf"]).exists()
    assert ctx["apply_url"] == "http://job/222"
    assert ctx["generated_dir"] == str(folder)


def test_build_apply_context_flags_missing_pdf(base):
    folder = _make_folder(base, "Beta", "Analyst", "333", with_pdf=False)
    with pytest.raises(FileNotFoundError) as exc:
        apply.build_apply_context(folder)
    assert "pdf" in str(exc.value).lower() or "resume" in str(exc.value).lower()


def test_build_apply_context_missing_json_raises(base):
    empty = base / "Empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        apply.build_apply_context(empty)
