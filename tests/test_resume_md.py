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


def test_push_argv_targets_resume_md_on_vm():
    import vm_sync
    t = vm_sync.VMTarget(instance="vm", zone="z", user="u", remote_dir="~")
    argv = t.build_scp_cmd(str(resume_md.RESUME_MD_PATH), "resume.md")
    assert any(a.endswith(":~/resume.md") for a in argv)   # remote dest is ~/resume.md
