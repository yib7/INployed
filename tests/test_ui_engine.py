import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))
import ui  # noqa: E402


def test_engine_labels_are_gemini_auth_modes():
    assert set(ui._ENGINE_LABELS) == {"vertex", "api_key"}


def test_label_to_auth_is_inverse():
    assert ui._LABEL_TO_AUTH[ui._ENGINE_LABELS["vertex"]] == "vertex"
    assert ui._LABEL_TO_AUTH[ui._ENGINE_LABELS["api_key"]] == "api_key"
