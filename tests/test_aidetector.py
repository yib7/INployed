"""The upstream avoid-ai-writing engine, run through Node (resume_tailor.aidetector).

Two halves. The first stubs the subprocess, so it pins the contract on any machine:
the allowlist mapping, one name per type, and that every way the run can fail
(no Node, a crash, a timeout, output that is not JSON) reads as "no findings" so
the letter gate carries on with its own checks. The second runs the vendored
engine for real and is skipped where Node is not installed; it is what notices an
upstream rename when the vendored files are updated.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from resume_tailor import aidetector  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_cache():
    aidetector._analyze.cache_clear()
    yield
    aidetector._analyze.cache_clear()


def _fake_run(issues=None, returncode=0, stdout=None, raises=None):
    def run(argv, **kwargs):
        if raises is not None:
            raise raises
        out = stdout if stdout is not None else json.dumps({"issues": issues or []})
        return subprocess.CompletedProcess(argv, returncode, stdout=out, stderr="")
    return run


@pytest.fixture
def fake_node(monkeypatch):
    monkeypatch.setattr(aidetector, "find_node", lambda: "node")

    def _install(**kw):
        monkeypatch.setattr(aidetector.subprocess, "run", _fake_run(**kw))
    return _install


# ── the vendored files ────────────────────────────────────────────────────────
def test_the_vendored_engine_cli_and_licence_are_present():
    for rel in ("detector/patterns.js", "bin/avoid-ai-writing.js", "LICENSE",
                "NOTICE.md", "README.md"):
        assert (aidetector.VENDOR_DIR / rel).is_file(), rel
    licence = (aidetector.VENDOR_DIR / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in licence and "Conor Bronsdon" in licence


def test_the_vendor_readme_names_the_version_the_module_claims():
    readme = (aidetector.VENDOR_DIR / "README.md").read_text(encoding="utf-8")
    assert aidetector.UPSTREAM_VERSION in readme


# ── the contract, with the subprocess stubbed ─────────────────────────────────
def test_allowlisted_types_map_to_names_once_each_in_engine_order(fake_node):
    fake_node(issues=[
        {"type": "tier1", "text": "delve"},                       # not allowlisted
        {"type": "negative-parallelism", "text": "isn't just"},
        {"type": "performed-insight", "text": "the real story was"},
        {"type": "negative-parallelism", "text": "is not only"},  # same type again
        {"type": "uniformity"},                                   # not allowlisted
    ])
    assert aidetector.violations("Some letter text.") == [
        "negative parallelism", "performed insight"]


def test_types_that_map_to_one_name_count_once(fake_node, monkeypatch):
    monkeypatch.setitem(aidetector.ALLOWED_TYPES, "x-alias", "chatbot artifact")
    fake_node(issues=[{"type": "chatbot"}, {"type": "x-alias"}])
    assert aidetector.violations("Some letter text.") == ["chatbot artifact"]


def test_blank_text_never_starts_node(monkeypatch):
    monkeypatch.setattr(aidetector, "find_node", lambda: pytest.fail("node looked up"))
    assert aidetector.violations("   \n ") == []


def test_no_node_means_no_findings(monkeypatch):
    monkeypatch.setattr(aidetector, "find_node", lambda: None)
    assert aidetector.available() is False
    assert aidetector.violations("It isn't just a tool, it's a foundation.") == []


@pytest.mark.parametrize("kw", [
    {"returncode": 2, "stdout": ""},
    {"stdout": "not json"},
    {"stdout": "[]"},                       # JSON, but not the result object
    {"raises": subprocess.TimeoutExpired("node", 20)},
    {"raises": FileNotFoundError("node")},
])
def test_a_failed_run_reads_as_no_findings(fake_node, kw):
    fake_node(**kw)
    assert aidetector.violations("Some letter text.") == []


def test_a_repeat_of_the_same_text_runs_node_once(monkeypatch):
    monkeypatch.setattr(aidetector, "find_node", lambda: "node")
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs["input"])
        return subprocess.CompletedProcess(argv, 0, stdout='{"issues": []}', stderr="")

    monkeypatch.setattr(aidetector.subprocess, "run", run)
    aidetector.violations("Same text.")
    aidetector.violations("Same text.")
    assert calls == ["Same text."]


def test_the_text_goes_on_stdin_with_the_pinned_context(monkeypatch):
    monkeypatch.setattr(aidetector, "find_node", lambda: "node")
    seen = {}

    def run(argv, **kwargs):
        seen["argv"], seen["input"] = argv, kwargs["input"]
        return subprocess.CompletedProcess(argv, 0, stdout='{"issues": []}', stderr="")

    monkeypatch.setattr(aidetector.subprocess, "run", run)
    aidetector.violations("A letter.")
    assert seen["argv"] == ["node", str(aidetector.CLI), "--context", aidetector.CONTEXT]
    assert seen["input"] == "A letter."


def test_resume_tailor_node_names_an_explicit_executable(monkeypatch, tmp_path):
    exe = tmp_path / "node.exe"
    exe.write_text("")
    monkeypatch.setenv("RESUME_TAILOR_NODE", str(exe))
    assert aidetector.find_node() == str(exe)
    monkeypatch.setenv("RESUME_TAILOR_NODE", str(tmp_path / "missing.exe"))
    assert aidetector.find_node() is None


# ── the real engine (skipped without Node) ────────────────────────────────────
needs_node = pytest.mark.skipif(not aidetector.available(),
                                reason="node is not installed")


@needs_node
def test_every_allowlisted_type_is_one_the_engine_reports():
    """An upstream rename would silently empty an allowlist entry; this is the
    check that fails instead when the vendored files are updated."""
    proc = subprocess.run(
        [aidetector.find_node(), "-e",
         "process.stdout.write(JSON.stringify(Object.keys("
         "require(process.argv[1]).TYPE_LABELS)))",
         str(aidetector.VENDOR_DIR / "detector" / "patterns.js")],
        capture_output=True, text=True, timeout=30)
    engine_types = set(json.loads(proc.stdout))
    assert set(aidetector.ALLOWED_TYPES) <= engine_types


@needs_node
def test_the_engine_flags_the_shapes_the_python_arm_cannot_carry():
    body = ("Meet Atlas, your new favorite scheduler. It isn't just a scheduler, "
            "it's a foundation for trust.\n\n"
            "The benchmark turned out to be the least interesting part. The real "
            "story was the migration.")
    names = aidetector.violations(body)
    assert {"launch-copy introduction", "negative parallelism",
            "performed insight"} <= set(names)


@needs_node
@pytest.mark.parametrize("body", [
    # Plain, grounded letter prose, including the technical words aiwriting
    # keeps out of its regex arm and an ordinary single correction.
    "I configured the Keycloak realm and rotated its signing keys every quarter.\n\n"
    "The first version was slow. I profiled it, moved the joins into the "
    "database, and cut the nightly run from four hours to forty minutes.",
    "Your team's work on the BLE beacon firmware is the reason I applied. I have "
    "shipped firmware for 400 devices, and the hard part was the update path, "
    "not the radio.\n\nI would like to bring that experience to Acme.",
])
def test_the_engine_leaves_plain_letter_prose_alone(body):
    assert aidetector.violations(body) == []
