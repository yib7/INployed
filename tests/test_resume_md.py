"""Tests for local/resume_md.py — the YAML→resume.md generator (Cycle 6 SP6).

The build must NEVER make a real Gemini call (paid), so every test injects a
fake `llm_call`. We verify: the fake receives the chosen model + a prompt that
carries the YAML and the never-invent rule; output is de-fenced; and the writer
backs up an existing resume.md before overwriting.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import resume_md  # noqa: E402


def test_generate_passes_model_and_yaml_to_injected_call():
    seen = {}

    def fake(system, user, model):
        seen.update(system=system, user=user, model=model)
        return "# Jane Doe\n\n## Summary\nGreat."

    out = resume_md.generate_resume_md("basics:\n  name: Jane Doe\n", "gemini-3.5-flash",
                                       llm_call=fake)
    assert seen["model"] == "gemini-3.5-flash"
    assert "Jane Doe" in seen["user"]                 # the YAML is in the prompt
    assert "NEVER invent" in seen["system"]           # the faithfulness rule
    assert out.startswith("# Jane Doe")


def test_generate_strips_code_fences():
    out = resume_md.generate_resume_md(
        "x: 1", "m", llm_call=lambda s, u, m: "```markdown\n# Title\n```")
    assert out == "# Title\n"                          # fence removed, trailing newline


def test_generate_rejects_empty_output():
    with pytest.raises(ValueError):
        resume_md.generate_resume_md("x: 1", "m", llm_call=lambda s, u, m: "   ")


def test_write_backs_up_then_overwrites(tmp_path):
    p = tmp_path / "resume.md"
    p.write_text("OLD CONTENT\n", encoding="utf-8")
    resume_md.write_resume_md("NEW CONTENT\n", path=p)
    assert p.read_text(encoding="utf-8") == "NEW CONTENT\n"
    assert (tmp_path / "resume.md.bak").read_text(encoding="utf-8") == "OLD CONTENT\n"


def test_write_creates_when_absent(tmp_path):
    p = tmp_path / "resume.md"
    resume_md.write_resume_md("FIRST\n", path=p)
    assert p.read_text(encoding="utf-8") == "FIRST\n"
    assert not (tmp_path / "resume.md.bak").exists()   # nothing to back up


def test_resume_md_path_is_repo_root():
    # The scorer reads resume.md from the repo root (score_jobs.OUTPUT_DIR).
    assert resume_md.RESUME_MD_PATH == REPO / "resume.md"


def _age(path, mtime):
    import os
    os.utime(path, (mtime, mtime))


def test_resume_md_stale_when_md_older_than_master(tmp_path):
    master = tmp_path / "master_experience.yaml"
    md = tmp_path / "resume.md"
    md.write_text("old\n", encoding="utf-8")
    master.write_text("basics: {}\n", encoding="utf-8")
    _age(md, 1000)
    _age(master, 2000)   # master edited after resume.md -> drift
    assert resume_md.resume_md_stale(master_path=master, resume_md_path=md) is True


def test_resume_md_fresh_when_md_newer(tmp_path):
    master = tmp_path / "master_experience.yaml"
    md = tmp_path / "resume.md"
    master.write_text("basics: {}\n", encoding="utf-8")
    md.write_text("new\n", encoding="utf-8")
    _age(master, 1000)
    _age(md, 2000)       # regenerated after the last edit -> in sync
    assert resume_md.resume_md_stale(master_path=master, resume_md_path=md) is False


def test_resume_md_stale_when_md_missing(tmp_path):
    master = tmp_path / "master_experience.yaml"
    master.write_text("basics: {}\n", encoding="utf-8")
    md = tmp_path / "resume.md"   # never generated
    assert resume_md.resume_md_stale(master_path=master, resume_md_path=md) is True


def test_resume_md_not_stale_when_master_absent(tmp_path):
    master = tmp_path / "master_experience.yaml"  # nothing to compare against
    md = tmp_path / "resume.md"
    md.write_text("x\n", encoding="utf-8")
    assert resume_md.resume_md_stale(master_path=master, resume_md_path=md) is False


_YAML_WITH_CONCEPTS = """\
basics:
  name: Jane Doe
skills:
  languages: [Python, SQL]
  concepts_and_methodologies: ["A/B Testing", "ETL", "Feature Engineering"]
"""


def test_structure_prompt_names_concepts_and_methodologies():
    # the generator must explicitly ask for a Concepts & Methodologies line so a faithful
    # regen never silently drops the pool the scorer screens against.
    seen = {}

    def fake(system, user, model):
        seen["user"] = user
        return "# Jane Doe\n"

    resume_md.generate_resume_md(_YAML_WITH_CONCEPTS, "m", llm_call=fake)
    assert "Concepts & Methodologies" in seen["user"]


def test_generate_appends_concepts_when_model_omits_them():
    # the model returns a resume that never mentions the concepts pool -> the deterministic
    # guarantee folds every item back in so the scorer can still match those terms.
    md = resume_md.generate_resume_md(
        _YAML_WITH_CONCEPTS, "m",
        llm_call=lambda s, u, m: "# Jane Doe\n\n## Technical Skills\n**Languages:** Python, SQL\n")
    for item in ("A/B Testing", "ETL", "Feature Engineering"):
        assert item in md


def test_generate_does_not_duplicate_present_concepts():
    body = ("# Jane Doe\n\n## Technical Skills\n**Languages:** Python, SQL\n"
            "**Concepts & Methodologies:** A/B Testing, ETL, Feature Engineering\n")
    md = resume_md.generate_resume_md(_YAML_WITH_CONCEPTS, "m", llm_call=lambda s, u, m: body)
    for item in ("A/B Testing", "ETL", "Feature Engineering"):
        assert md.count(item) == 1                  # already on the page -> not re-appended


def test_generate_no_concepts_pool_is_noop():
    # a YAML lacking the pool must not crash and must not append a concepts line
    md = resume_md.generate_resume_md(
        "basics:\n  name: Jane Doe\n", "m",
        llm_call=lambda s, u, m: "# Jane Doe\n\n## Summary\nGreat.\n")
    assert "Concepts & Methodologies" not in md


def test_default_llm_call_routes_to_claude_when_provider_is_claude(monkeypatch):
    # provider=claude -> _default_llm_call must reach llm._call_claude with the
    # flash-tier Claude model, NOT the Gemini model id passed in `model`.
    from resume_tailor import config, llm

    monkeypatch.delenv("RESUME_TAILOR_PROVIDER", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {"tailor_provider": "claude"})
    captured = {}

    def fake_call_claude(system, user, model, **kwargs):
        captured.update(system=system, user=user, model=model, kwargs=kwargs)
        return "claude output"

    monkeypatch.setattr(llm, "_call_claude", fake_call_claude)
    out = resume_md._default_llm_call("sys", "usr", "gemini-3.5-flash")
    assert out == "claude output"
    assert captured["model"] == config.claude_model_for(config.TIER_FLASH)
    assert captured["system"] == "sys"
    assert captured["user"] == "usr"


def test_default_llm_call_routes_to_gemini_by_default(monkeypatch):
    from resume_tailor import config, llm

    monkeypatch.delenv("RESUME_TAILOR_PROVIDER", raising=False)
    monkeypatch.setattr(config, "_config_json", lambda: {})
    captured = {}

    def fake_call_gemini(system, user, model, **kwargs):
        captured.update(system=system, user=user, model=model, kwargs=kwargs)
        return "gemini output"

    monkeypatch.setattr(llm, "_call_gemini", fake_call_gemini)
    out = resume_md._default_llm_call("sys", "usr", "gemini-3.5-flash")
    assert out == "gemini output"
    assert captured["model"] == "gemini-3.5-flash"       # the passed model id, unchanged
    assert captured["system"] == "sys"
    assert captured["user"] == "usr"


def test_push_argv_targets_resume_md_on_vm():
    import vm_sync
    t = vm_sync.VMTarget(instance="vm", zone="z", user="u", remote_dir="~")
    argv = t.build_scp_cmd(str(resume_md.RESUME_MD_PATH), "resume.md")
    # remote_dir "~" sends a bare relative dest (pscp can't open a literal "~/" path).
    assert any(a.endswith(":resume.md") for a in argv)
    assert not any(a.endswith(":~/resume.md") for a in argv)
