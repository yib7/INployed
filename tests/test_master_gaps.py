"""Tests for the smarter master_experience JD-gap feature (PLAN stage 5).

Deterministic parts (gap detection, comment-preserving insertion, diff, fallback
placement) are tested directly; the flash-lite screen/placement calls are tested
with a stubbed llm.call so no network/credentials are needed.
"""
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import master_edit, master_gaps  # noqa: E402
from resume_tailor.llm import LLMError  # noqa: E402

_MASTER = {
    "skills": {
        "languages": ["Python", "SQL"],
        "developer_tools": ["Git", "Docker"],
        "concepts_and_methodologies": ["OLS Regression"],
    }
}

_MASTER_TEXT = textwrap.dedent("""\
    basics:
      name: Jane Doe

    # SKILLS taxonomy
    skills:
      # languages line
      languages: [Python, SQL]
      developer_tools: [Git, Docker]
      concepts_and_methodologies: [
        "OLS Regression",
        "K-Fold Cross-Validation"
      ]
""")


def test_candidate_skill_terms_lowercased():
    terms = master_gaps.candidate_skill_terms(_MASTER)
    assert "python" in terms and "git" in terms and "ols regression" in terms


def test_find_gap_keywords_excludes_known():
    jd = "We need Python, Kubernetes, Airflow, Snowflake and SQL. Kubernetes daily."
    gaps = master_gaps.find_gap_keywords(jd, _MASTER)
    low = [g.lower() for g in gaps]
    assert "python" not in low and "sql" not in low      # already owned
    assert any("kubernetes" == g for g in low)           # genuinely missing
    assert any("airflow" == g for g in low)


def test_screen_candidates_filters_to_offered(monkeypatch):
    # Model tries to keep a real skill + sneak in an item that wasn't offered.
    monkeypatch.setattr(master_gaps, "call",
                        lambda *a, **k: {"keep": ["Kubernetes", "TotallyNew"]})
    kept = master_gaps.screen_candidates(["Kubernetes", "Acme Corp"])
    assert kept == ["Kubernetes"]  # offered-only; "TotallyNew" rejected


def test_screen_candidates_conservative_on_error(monkeypatch):
    # A genuine LLM-boundary failure (what the real call() raises) is swallowed
    # to [] — suggest nothing rather than junk.
    def boom(*a, **k):
        raise LLMError("no creds")
    monkeypatch.setattr(master_gaps, "call", boom)
    assert master_gaps.screen_candidates(["Kubernetes"]) == []


def test_screen_candidates_lets_programming_errors_surface(monkeypatch):
    # P2-10: a non-LLM error (a bug in our own code) must NOT be swallowed as [].
    def boom(*a, **k):
        raise KeyError("programming bug")
    monkeypatch.setattr(master_gaps, "call", boom)
    with pytest.raises(KeyError):
        master_gaps.screen_candidates(["Kubernetes"])


def test_place_skills_lets_programming_errors_surface(monkeypatch):
    # P2-10: place_skills must likewise let non-LLM errors surface.
    def boom(*a, **k):
        raise KeyError("programming bug")
    monkeypatch.setattr(master_gaps, "call", boom)
    with pytest.raises(KeyError):
        master_gaps.place_skills(["Kubernetes"], ["developer_tools"])


def test_place_skills_fallback_to_last_bucket(monkeypatch):
    monkeypatch.setattr(master_gaps, "call",
                        lambda *a, **k: {"assignments": {"Kubernetes": "developer_tools",
                                                          "Airflow": "not_a_bucket"}})
    buckets = ["languages", "developer_tools", "concepts_and_methodologies"]
    placements = master_gaps.place_skills(["Kubernetes", "Airflow"], buckets)
    assert placements["developer_tools"] == ["Kubernetes"]
    # invalid bucket -> falls back to the last (catch-all) bucket
    assert placements["concepts_and_methodologies"] == ["Airflow"]


def test_preview_preserves_comments_and_inserts_single_line():
    placements = {"developer_tools": ["Kubernetes", "Airflow"]}
    new_text, diff = master_gaps.preview_additions(placements, _MASTER_TEXT)
    assert "# SKILLS taxonomy" in new_text       # comments preserved
    assert "# languages line" in new_text
    assert '"Kubernetes"' in new_text and '"Airflow"' in new_text
    # inserted into the right bucket, before its closing bracket
    dev_line = [ln for ln in new_text.splitlines() if ln.strip().startswith("developer_tools:")][0]
    assert dev_line.rstrip().endswith("]")
    assert "Kubernetes" in dev_line and "Docker" in dev_line
    assert "+" in diff and "Kubernetes" in diff


def test_preview_inserts_into_multiline_bucket():
    placements = {"concepts_and_methodologies": ["A/B Testing"]}
    new_text, _diff = master_gaps.preview_additions(placements, _MASTER_TEXT)
    assert '"A/B Testing"' in new_text
    # languages/developer_tools untouched
    assert new_text.count("Python") == 1


def test_preview_skips_unknown_bucket():
    placements = {"nonexistent_bucket": ["Foo"]}
    new_text, diff = master_gaps.preview_additions(placements, _MASTER_TEXT)
    assert new_text == _MASTER_TEXT  # nothing inserted
    assert "skipped" in diff


def test_preview_skips_yaml_breaking_items():
    # Defense-in-depth: an item with a quote/bracket must not corrupt the file.
    placements = {"developer_tools": ["Kubernetes", 'Ev"il', "Bad]name", "back\\slash"]}
    new_text, _diff = master_gaps.preview_additions(placements, _MASTER_TEXT)
    assert '"Kubernetes"' in new_text
    assert 'Ev"il' not in new_text and "Bad]name" not in new_text and "back\\slash" not in new_text
    dev_line = [ln for ln in new_text.splitlines() if ln.strip().startswith("developer_tools:")][0]
    assert dev_line.rstrip().endswith("]")  # list still well-formed


def test_apply_to_file_backs_up_and_writes(tmp_path):
    p = tmp_path / "master_experience.yaml"
    p.write_text(_MASTER_TEXT, encoding="utf-8")
    diff = master_gaps.apply_to_file({"developer_tools": ["Kubernetes"]}, p)
    assert (tmp_path / "master_experience.yaml.bak").read_text(encoding="utf-8") == _MASTER_TEXT
    assert '"Kubernetes"' in p.read_text(encoding="utf-8")
    assert "Kubernetes" in diff


# ── P1-1: apply_to_file must be atomic (old-or-new, never truncated) ───────────
def test_apply_to_file_atomic_on_write_failure(tmp_path, monkeypatch):
    p = tmp_path / "master_experience.yaml"
    p.write_text(_MASTER_TEXT, encoding="utf-8")

    def boom(*a, **k):
        raise RuntimeError("simulated crash mid-write")

    # The replace is the last step of the atomic write; if it (or the write)
    # fails, the on-disk master must still hold the ORIGINAL, never a truncation.
    monkeypatch.setattr(master_edit.os, "replace", boom)
    with pytest.raises(RuntimeError):
        master_gaps.apply_to_file({"developer_tools": ["Kubernetes"]}, p)
    assert p.read_text(encoding="utf-8") == _MASTER_TEXT   # intact, not truncated


# ── P2-30: a second save keeps a timestamped backup ring, not one clobbered .bak
def test_apply_to_file_keeps_timestamped_backup_ring(tmp_path):
    p = tmp_path / "master_experience.yaml"
    p.write_text(_MASTER_TEXT, encoding="utf-8")
    master_gaps.apply_to_file({"developer_tools": ["Kubernetes"]}, p)
    master_gaps.apply_to_file({"languages": ["Rust"]}, p)
    ring = sorted(tmp_path.glob("master_experience.yaml.bak.*"))
    assert len(ring) >= 2   # each save left its own timestamped backup


# ── P2-15: _bucket_span must not leak an insertion into a following section ────
_MASTER_TRAILING = textwrap.dedent("""\
    skills:
      languages: [Python]
    skill_aliases:
      frameworks: [React]
""")


def test_bucket_span_bounded_to_skills_section():
    # 'frameworks' is NOT a skills bucket here, but appears as a flow-list key in
    # a later top-level section. An insertion for it must be skipped, never leak
    # into the trailing skill_aliases block.
    placements = {"frameworks": ["FastAPI"]}
    new_text, diff = master_gaps.preview_additions(placements, _MASTER_TRAILING)
    assert new_text == _MASTER_TRAILING     # nothing inserted into skill_aliases
    assert "FastAPI" not in new_text
    assert "skipped" in diff


# ── P2-18: CLI --apply requires per-item confirmation (or --yes) ──────────────
def test_confirm_placements_drops_declined_items():
    placements = {"developer_tools": ["Kubernetes", "Airflow"]}
    # Declining every item yields an empty confirmed set (nothing folded in).
    assert master_gaps._confirm_placements(
        placements, assume_yes=False, prompt=lambda *_: "n") == {}
    # Accept only the first item.
    answers = iter(["y", "n"])
    got = master_gaps._confirm_placements(
        placements, assume_yes=False, prompt=lambda *_: next(answers))
    assert got == {"developer_tools": ["Kubernetes"]}


def test_confirm_placements_yes_flag_keeps_all():
    placements = {"developer_tools": ["Kubernetes", "Airflow"]}
    assert master_gaps._confirm_placements(placements, assume_yes=True) == placements
