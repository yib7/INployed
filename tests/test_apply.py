"""Apply-button resolution (local/resume_tailor/apply.py).

The dashboard Apply button resolves a job's tailored folder. Folders tailored
before apply_data.json existed have a resume PDF but no apply_data.json; the
button must still find them (resolve by company+title) and backfill the json so
build_apply_context succeeds — the "nothing in the folder when there should be"
bug.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import apply as apply_mod  # noqa: E402
from resume_tailor import assets, config, output  # noqa: E402

_MASTER = {"basics": {"name": "Test User", "email": "t@e.com",
                      "phone": "1", "location": "Remote"}, "education": []}


def _tailored_folder_without_json(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(assets, "load_master", lambda: _MASTER)
    monkeypatch.setenv("RESUME_TAILOR_CANDIDATE", "Test_User")
    folder = tmp_path / "AcmeCo" / "Data Scientist"
    folder.mkdir(parents=True)
    (folder / output.resume_filename()).write_text("%PDF-1.4 fake", encoding="utf-8")
    return folder


def test_resolve_backfills_apply_data_when_pdf_present_but_json_missing(tmp_path, monkeypatch):
    folder = _tailored_folder_without_json(tmp_path, monkeypatch)
    assert not (folder / "apply_data.json").exists()  # precondition: the bug state

    resolved = apply_mod.resolve_generated_dir(
        job_id="999", company="AcmeCo", title="Data Scientist",
        job={"job_posting_id": "999", "company_name": "AcmeCo",
             "job_title": "Data Scientist", "url": "https://x/999"})

    assert resolved == folder
    assert (folder / "apply_data.json").exists()  # backfilled
    ctx = apply_mod.build_apply_context(resolved)
    assert ctx["job"]["job_posting_id"] == "999"
    assert ctx["apply_url"] == "https://x/999"
    assert ctx["resume_pdf"].endswith(output.resume_filename())


def test_resolve_does_not_nest_a_dated_subfolder_when_backfilling(tmp_path, monkeypatch):
    # output.resolve_dir nests a dated subfolder when a resume exists; backfill
    # must target the EXISTING folder, not a new empty dated one.
    folder = _tailored_folder_without_json(tmp_path, monkeypatch)
    resolved = apply_mod.resolve_generated_dir(
        company="AcmeCo", title="Data Scientist",
        job={"job_posting_id": "1", "company_name": "AcmeCo", "job_title": "Data Scientist"})
    assert resolved == folder  # not folder/<today>
