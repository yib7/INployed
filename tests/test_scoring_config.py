"""Tests for score_jobs.py's externalized scoring config (scoring_config.json).

Precedence is env > config-file > built-in default. The VM runs score_jobs.py
standalone with NO json config, so the loader MUST yield today's constants. A
local user can drop a scoring_config.json next to score_jobs.py to retune the
models/concurrency/thresholds; an env var still wins over the file (the VM's
run_scraper.sh exports already override via os.environ today).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import score_jobs  # noqa: E402


# The UTF-8 byte order mark, written as ints so this source stays pure ASCII.
_BOM = bytes([0xEF, 0xBB, 0xBF])


def _write_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "scoring_config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _clear_env(monkeypatch):
    for k in (
        "SCORE_STAGE1_MODEL", "SCORE_STAGE2_MODEL",
        "SCORE_STAGE1_CONCURRENCY", "SCORE_STAGE2_CONCURRENCY",
        "SCORE_STAGE2_THRESHOLD", "SCORE_MAX_PER_RUN", "SCORE_RESCORE_CAP",
        "SCORE_MIN_FILTER_YEARS", "SCORE_DROP_EASY_APPLY",
    ):
        monkeypatch.delenv(k, raising=False)


def test_absent_file_uses_builtin_defaults(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    cfg = score_jobs.load_scoring_config()
    assert cfg["stage1_model"] == "gemini-3.1-flash-lite"
    assert cfg["stage2_model"] == "gemini-3.5-flash"
    assert cfg["stage1_concurrency"] == 6
    assert cfg["stage2_concurrency"] == 4
    assert cfg["stage2_threshold"] == 4
    assert cfg["max_scored_per_run"] == 800
    assert cfg["rescore_cap"] == 200
    assert cfg["min_filter_years"] == 1
    # the module constants the existing tests rely on keep their defaults
    assert score_jobs.MIN_FILTER_YEARS == 1
    assert score_jobs.STAGE2_THRESHOLD == 4


def test_config_file_overrides_threshold_and_years(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    _write_config(tmp_path, {"stage2_threshold": 3, "min_filter_years": 2,
                             "max_scored_per_run": 50})
    cfg = score_jobs.load_scoring_config()
    assert cfg["stage2_threshold"] == 3
    assert cfg["min_filter_years"] == 2
    assert cfg["max_scored_per_run"] == 50
    # untouched keys still fall back to defaults
    assert cfg["rescore_cap"] == 200
    assert cfg["stage1_model"] == "gemini-3.1-flash-lite"


def test_env_overrides_config_file(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    _write_config(tmp_path, {"stage2_threshold": 3, "min_filter_years": 2})
    monkeypatch.setenv("SCORE_STAGE2_THRESHOLD", "5")
    monkeypatch.setenv("SCORE_MIN_FILTER_YEARS", "0")
    cfg = score_jobs.load_scoring_config()
    assert cfg["stage2_threshold"] == 5   # env beats the file's 3
    assert cfg["min_filter_years"] == 0   # env beats the file's 2


# --- drop_easy_apply: a "bool"-kind key, coerced from JSON bools AND env strings.


def test_drop_easy_apply_default_false_when_absent(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    cfg = score_jobs.load_scoring_config()
    assert cfg["drop_easy_apply"] is False


def test_drop_easy_apply_config_file_true(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    _write_config(tmp_path, {"drop_easy_apply": True})  # JSON bool from the file
    cfg = score_jobs.load_scoring_config()
    assert cfg["drop_easy_apply"] is True


def test_drop_easy_apply_env_beats_file_false(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    _write_config(tmp_path, {"drop_easy_apply": False})
    monkeypatch.setenv("SCORE_DROP_EASY_APPLY", "1")  # env string beats the file
    cfg = score_jobs.load_scoring_config()
    assert cfg["drop_easy_apply"] is True


def test_drop_easy_apply_env_zero_is_false(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("SCORE_DROP_EASY_APPLY", "0")  # "0" must coerce to False
    cfg = score_jobs.load_scoring_config()
    assert cfg["drop_easy_apply"] is False


def test_a_bom_does_not_discard_the_whole_config(monkeypatch, tmp_path):
    """Notepad and PowerShell 5.1's Set-Content -Encoding UTF8 both write a BOM.

    json.loads rejects a leading BOM, so reading this file as plain utf-8 threw
    the WHOLE config away and fell back to built-ins with one line in
    scraper.log. local/jsonutil.read_json_dict has read the same file utf-8-sig
    since the setup script was caught doing exactly this, so the two halves used
    to disagree about one file: the dashboard honoured it, the VM ignored it.
    """
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    (tmp_path / "scoring_config.json").write_text(
        json.dumps({"stage2_threshold": 7, "min_filter_years": 3}), encoding="utf-8-sig")
    raw = (tmp_path / "scoring_config.json").read_bytes()
    assert raw.startswith(_BOM)            # the premise
    cfg = score_jobs.load_scoring_config()
    assert cfg["stage2_threshold"] == 7
    assert cfg["min_filter_years"] == 3


def test_a_plain_utf8_config_still_reads_identically(monkeypatch, tmp_path):
    """utf-8-sig must be a superset, not a swap: no BOM, same result."""
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    _write_config(tmp_path, {"stage2_threshold": 7, "min_filter_years": 3})
    assert not (tmp_path / "scoring_config.json").read_bytes().startswith(_BOM)
    cfg = score_jobs.load_scoring_config()
    assert cfg["stage2_threshold"] == 7 and cfg["min_filter_years"] == 3


def test_a_negative_spend_cap_falls_back_instead_of_disabling_the_guard(
        monkeypatch, tmp_path):
    """`SCORE_MAX_PER_RUN=-1` must not read as "no cap".

    Both spend guards are spent as a pandas row slice, and pandas reads a negative
    count as "all but N": `df.head(-1)` returns every row but the last. So the old
    loader let `-1` -- the obvious way to write "unlimited" -- take the
    `len(to_score) > MAX_SCORED_PER_RUN` branch, print "capping at -1 of N jobs",
    and then score N-1 of them. A guard that announces itself and lifts itself.
    """
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("SCORE_MAX_PER_RUN", "-1")
    monkeypatch.setenv("SCORE_RESCORE_CAP", "-500")
    cfg = score_jobs.load_scoring_config()
    assert cfg["max_scored_per_run"] == 800     # the built-in default, not -1
    assert cfg["rescore_cap"] == 200


def test_the_negative_cap_in_the_json_file_is_caught_too(monkeypatch, tmp_path):
    """scoring_config.json is hand-edited and scp'd to the VM; same rule there."""
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    _write_config(tmp_path, {"max_scored_per_run": -1, "rescore_cap": -1})
    cfg = score_jobs.load_scoring_config()
    assert cfg["max_scored_per_run"] == 800
    assert cfg["rescore_cap"] == 200


def test_zero_is_left_alone_because_it_already_fails_closed(monkeypatch, tmp_path):
    """`head(0)`/`tail(0)` are empty, so 0 means what it says: score nothing.

    Collapsing 0 to the default would turn "spend nothing this run" into "spend up
    to 800", which is the wrong direction for a guard to round.
    """
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("SCORE_MAX_PER_RUN", "0")
    monkeypatch.setenv("SCORE_RESCORE_CAP", "0")
    cfg = score_jobs.load_scoring_config()
    assert cfg["max_scored_per_run"] == 0
    assert cfg["rescore_cap"] == 0


def test_a_positive_cap_is_untouched(monkeypatch, tmp_path):
    """The clamp must not move a value anybody would actually set."""
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("SCORE_MAX_PER_RUN", "1500")
    cfg = score_jobs.load_scoring_config()
    assert cfg["max_scored_per_run"] == 1500


def test_the_other_int_settings_may_still_go_negative(monkeypatch, tmp_path):
    """The clamp is scoped to the two spend guards, not to every int.

    `min_filter_years` is a cutoff, not a row slice; -1 there is meaningless but
    harmless, and widening the clamp would be a behaviour change dressed as a fix.
    """
    _clear_env(monkeypatch)
    monkeypatch.setattr(score_jobs, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("SCORE_MIN_FILTER_YEARS", "-1")
    assert score_jobs.load_scoring_config()["min_filter_years"] == -1
