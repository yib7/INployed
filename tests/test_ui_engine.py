import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))
import jobsdata  # noqa: E402
import ui  # noqa: E402


def test_engine_labels_are_gemini_auth_modes():
    assert set(jobsdata._ENGINE_LABELS) == {"vertex", "api_key"}


def test_label_to_auth_is_inverse():
    assert jobsdata._LABEL_TO_AUTH[jobsdata._ENGINE_LABELS["vertex"]] == "vertex"
    assert jobsdata._LABEL_TO_AUTH[jobsdata._ENGINE_LABELS["api_key"]] == "api_key"


def test_enable_dpi_awareness_is_safe():
    # Best-effort, idempotent, and never fatal (swallows wrong-OS / already-set /
    # missing-API errors) so it can run before Tk() on any platform.
    assert ui._enable_dpi_awareness() is None
    assert ui._enable_dpi_awareness() is None  # second call must not raise either


def test_engine_credential_warnings_flags_missing_api_key():
    assert jobsdata._engine_credential_warnings("api_key", project="", has_api_key=False)
    assert jobsdata._engine_credential_warnings("api_key", project="proj", has_api_key=True) == []


def test_engine_credential_warnings_flags_missing_vertex_project():
    assert jobsdata._engine_credential_warnings("vertex", project="", has_api_key=False)
    assert jobsdata._engine_credential_warnings("vertex", project="  ", has_api_key=False)  # blank
    assert jobsdata._engine_credential_warnings("vertex", project="my-proj", has_api_key=False) == []
