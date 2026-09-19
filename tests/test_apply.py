"""Apply-button resolution (local/resume_tailor/apply.py).

The dashboard Apply button resolves a job's tailored folder. Folders tailored
before apply.md existed have a resume PDF but no apply.md; the button must still
find them (resolve by company+title) and backfill apply.md so build_apply_context
succeeds — the "nothing in the folder when there should be" bug.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import apply as apply_mod  # noqa: E402
from resume_tailor import apply_answers, apply_config, assets, config, output  # noqa: E402

_MASTER = {"basics": {"name": "Test User", "email": "t@e.com",
                      "phone": "1", "location": "Remote"}, "education": []}


def _tailored_folder_without_sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(assets, "load_master", lambda: _MASTER)
    monkeypatch.setenv("RESUME_TAILOR_CANDIDATE", "Test_User")
    # hermetic: don't read the developer's real answer store / apply config
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "apply_config.json")
    folder = tmp_path / "AcmeCo" / "Data Scientist"
    folder.mkdir(parents=True)
    (folder / output.resume_filename()).write_text("%PDF-1.4 fake", encoding="utf-8")
    return folder


def test_resolve_backfills_apply_md_when_pdf_present_but_sheet_missing(tmp_path, monkeypatch):
    folder = _tailored_folder_without_sheet(tmp_path, monkeypatch)
    assert not (folder / "apply.md").exists()  # precondition: the bug state

    resolved = apply_mod.resolve_generated_dir(
        job_id="999", company="AcmeCo", title="Data Scientist",
        job={"job_posting_id": "999", "company_name": "AcmeCo",
             "job_title": "Data Scientist", "url": "https://x/999"})

    assert resolved == folder
    assert (folder / "apply.md").exists()  # backfilled
    ctx = apply_mod.build_apply_context(resolved)
    assert ctx["job"]["job_posting_id"] == "999"
    assert ctx["apply_url"] == "https://x/999"
    assert ctx["resume_pdf"].endswith(output.resume_filename())


def test_resolve_does_not_nest_a_dated_subfolder_when_backfilling(tmp_path, monkeypatch):
    # output.resolve_dir nests a dated subfolder when a resume exists; backfill
    # must target the EXISTING folder, not a new empty dated one.
    folder = _tailored_folder_without_sheet(tmp_path, monkeypatch)
    resolved = apply_mod.resolve_generated_dir(
        company="AcmeCo", title="Data Scientist",
        job={"job_posting_id": "1", "company_name": "AcmeCo", "job_title": "Data Scientist"})
    assert resolved == folder  # not folder/<today>


def test_build_apply_context_returns_linkedin_and_github_links(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(assets, "load_master", lambda: {
        "basics": {"name": "Test User", "email": "t@e.com", "phone": "1",
                   "location": "Remote", "linkedin": "linkedin.com/in/testuser",
                   "github": "github.com/testuser"},
        "education": [],
    })
    monkeypatch.setenv("RESUME_TAILOR_CANDIDATE", "Test_User")
    monkeypatch.setattr(apply_answers, "STORE_PATH", tmp_path / "apply_answers.json")
    monkeypatch.setattr(apply_config, "APPLY_CONFIG", tmp_path / "apply_config.json")
    folder = tmp_path / "AcmeCo" / "Data Scientist"
    folder.mkdir(parents=True)
    (folder / output.resume_filename()).write_text("%PDF-1.4 fake", encoding="utf-8")

    resolved = apply_mod.resolve_generated_dir(
        job_id="42", company="AcmeCo", title="Data Scientist",
        job={"job_posting_id": "42", "company_name": "AcmeCo",
             "job_title": "Data Scientist", "url": "https://x/42"})
    ctx = apply_mod.build_apply_context(resolved)

    assert ctx["links"] == {
        "LinkedIn": "https://linkedin.com/in/testuser",
        "GitHub": "https://github.com/testuser",
    }


def test_build_apply_context_blank_links_come_back_as_empty_strings(tmp_path, monkeypatch):
    # _tailored_folder_without_sheet's synthetic master has no linkedin/github keys.
    _tailored_folder_without_sheet(tmp_path, monkeypatch)
    resolved = apply_mod.resolve_generated_dir(
        job_id="7", company="AcmeCo", title="Data Scientist",
        job={"job_posting_id": "7", "company_name": "AcmeCo", "job_title": "Data Scientist"})
    ctx = apply_mod.build_apply_context(resolved)
    assert ctx["links"] == {"LinkedIn": "", "GitHub": ""}
