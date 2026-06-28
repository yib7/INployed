import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))
import jobsdata  # noqa: E402


def test_engine_labels_are_gemini_auth_modes():
    assert set(jobsdata._ENGINE_LABELS) == {"vertex", "api_key"}


def test_label_to_auth_is_inverse():
    assert jobsdata._LABEL_TO_AUTH[jobsdata._ENGINE_LABELS["vertex"]] == "vertex"
    assert jobsdata._LABEL_TO_AUTH[jobsdata._ENGINE_LABELS["api_key"]] == "api_key"


def test_engine_credential_warnings_flags_missing_api_key():
    assert jobsdata._engine_credential_warnings("api_key", project="", has_api_key=False)
    assert jobsdata._engine_credential_warnings("api_key", project="proj", has_api_key=True) == []


def test_engine_credential_warnings_flags_missing_vertex_project():
    assert jobsdata._engine_credential_warnings("vertex", project="", has_api_key=False)
    assert jobsdata._engine_credential_warnings("vertex", project="  ", has_api_key=False)  # blank
    assert jobsdata._engine_credential_warnings("vertex", project="my-proj", has_api_key=False) == []


def test_projects_count_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)  # empty config.json dir
    assert jobsdata.load_projects_count() == (3, "max")


def test_projects_count_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    jobsdata.save_projects_count(5, "exact")
    assert jobsdata.load_projects_count() == (5, "exact")


def test_projects_count_clamps_and_normalizes(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    jobsdata.save_projects_count(99, "weird")          # over the cap + bad mode
    assert jobsdata.load_projects_count() == (6, "max")
    jobsdata.save_projects_count(0, "Exact")           # under the floor + mixed case
    assert jobsdata.load_projects_count() == (1, "exact")


def test_projects_count_does_not_clobber_layout_maps(tmp_path, monkeypatch):
    # save_projects_count merges into config.json — it must not wipe resume_layout.
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    jobsdata.save_resume_layout({"Globex": {"line_targets": [2, 1]}})
    jobsdata.save_projects_count(4, "exact")
    assert jobsdata.load_resume_layout() == {"Globex": {"line_targets": [2, 1]}}
    assert jobsdata.load_projects_count() == (4, "exact")


def test_verbatim_blocks_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(jobsdata, "HERE", tmp_path)
    assert jobsdata.load_verbatim_blocks() == {}
    jobsdata.save_verbatim_blocks({"Globex": ["A", "B"]})
    assert jobsdata.load_verbatim_blocks() == {"Globex": ["A", "B"]}
