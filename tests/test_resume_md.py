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


def test_push_argv_targets_resume_md_on_vm():
    import vm_sync
    t = vm_sync.VMTarget(instance="vm", zone="z", user="u", remote_dir="~")
    argv = t.build_scp_cmd(str(resume_md.RESUME_MD_PATH), "resume.md")
    assert any(a.endswith(":~/resume.md") for a in argv)   # remote dest is ~/resume.md
