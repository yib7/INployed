# -*- coding: utf-8 -*-
"""`scripts/atom_audit.py slop` -- the atom layer's avoid-ai-writing arm.

Its own file rather than more of tests/test_atom_audit.py, because the two
audits disagree about what a fixture is. The census and the gate need atoms that
REPEAT each other or carry figures, and their module docstring is written about
that; the slop arm needs atoms that carry prose, and most of what it has to
prove is the opposite of a finding -- eight technical words that must stay
silent, a noun that must not be read as a verb, ten comparison phrases that are
facts and not contrast framing. Those pins and the reasons behind them belong
next to each other.

Hermetic: every fixture is a one-atom master written into `tmp_path`. The user's
real `resume_tailor_files/master_experience.yaml` is gitignored personal data
and is never read here (see tests/test_hermetic_repo_data.py).

Nothing imports `local.resume_tailor`: `config.py` calls `load_dotenv()` at
import scope. The audit reaches the vendored ban list by executing two files by
path instead, and `test_the_audit_loads_two_files_and_never_the_package` holds
that line from a clean subprocess, where no other test can have imported the
real package first.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO / "scripts" / "atom_audit.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("atom_audit_slop", MODULE_PATH)
    assert spec and spec.loader, f"missing {MODULE_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


atom_audit = _load_module()

CLEAN_WHAT = "Built a viewer for an offline archive of exported messages"
CLEAN_HOW = "Indexed every message into a trie at import time"


def _master(**fields) -> dict:
    """A one-atom master whose fields default to text with nothing to report."""
    atom = {"id": "core", "what": CLEAN_WHAT, "how": CLEAN_HOW}
    atom.update(fields)
    return {"projects": [{"name": "Widget", "achievements": [atom]}]}


def _slop(tmp_path: Path, master: dict, *flags: str):
    path = tmp_path / "master.yaml"
    path.write_text(yaml.safe_dump(master, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    return atom_audit.main(["slop", "--file", str(path), *flags])


def _report(tmp_path: Path, capsys, text: str, *flags: str):
    code = _slop(tmp_path, _master(what=text), *flags)
    return capsys.readouterr().out, code


# ── A clean atom is silent ───────────────────────────────────────────────────

def test_a_clean_atom_reports_nothing(tmp_path, capsys):
    master = _master(scope="9,120 conversations",
                     impact=["opening any thread takes under a second"])
    code = _slop(tmp_path, master)
    out = capsys.readouterr().out
    assert code == 0
    assert "TOTAL FINDINGS: 0" in out
    for tier in ("P0", "P1", "P2"):
        assert f"{tier}  " in out
    # ASCII only, so a cp1252 console never breaks the report
    assert out.isascii()


# ── Every enforced category fires ────────────────────────────────────────────
# One seeded example per rule, keyed by the name the report prints.
# `test_every_rule_has_a_seeded_example` fails if a rule is added without one.
SEEDS = {
    # P0, the vendored arm
    "chatbot artifact": ["Certainly, here is the rewritten bullet."],
    "vague attribution": ["Studies show the index halved lookup time."],
    "significance inflation": ["Drove a paradigm shift in how the team ships."],
    # P0, the atom arm
    "cutoff disclaimer": ["As of my last update the exporter handled 40 formats."],
    "chat citation leak": ["Shipped the exporter contentReference[oaicite:0]{index=0}"],
    "ai-tool url parameter": ["Documented at https://example.com/d?utm_source=chatgpt.com"],
    "unfilled placeholder": ["Built the exporter with [Your Name] on 2026-XX-XX"],
    "vague third-party validation": ["Independent testing confirms the parser is faster."],
    # P1, the vendored arm
    "tier-1 vocabulary": ["Delved into the crash logs and found the leak."],
    # Lower case and mid-field on purpose: the vendored `_HEDGE_RE` carries no
    # `re.I`, because "May" is a month and "Launched in May 2024" is a fact.
    "hedge": ["Cut the nightly build time, potentially by half."],
    "promotional language": ["Built an industry-leading crawler for the archive."],
    "formulaic opening": ["Responsible for the nightly ingestion job."],
    # P1, the atom arm
    "em or en dash": ["Cut the run time — the cache did it.",
                      "Cut the run time – the cache did it."],
    "contrast framing": [
        # joined
        "The win is not the speed, it's the cache.",
        # not X but Y
        "The fix was not a rewrite but a one-line cache key change.",
        # split-sentence: two innocent-looking declaratives
        "The headline isn't the speed. The real story is the cache.",
        # multi-negation countdown
        "It's not the price. It's not the size. It's the trust.",
        # tailing negation
        "The options come from the selected item, no guessing.",
    ],
    "harness as a verb": ["Harnessed the power of the GPU cluster."],
    "tier-1 vocabulary (atom arm)": ["Embarked on a rewrite of the parser."],
    "hollow intensifier": ["Truly cut the nightly build time in half."],
    "template phrase": ["Played a key role in the storage migration."],
    "adjective stacking": [
        "Shipped a high-quality, well-architected, future-proof viewer.",
        "Shipped a reusable, portable, maintainable archive format.",
    ],
    # The second one is not covered by the vendored chatbot rule, whose
    # `let's\s+\w)\b` branch can only match a one-letter verb. See the comment
    # on the rule.
    "engagement hook": ["Cut the run time. The catch? It only runs on weekends.",
                        "Let's walk through the exporter."],
    "novelty inflation": ["Coined the term context poisoning for the failure."],
    "emotional flatline": ["What surprised me most was the cache hit rate."],
    "speculative opener": ["Imagine a world where every deploy is instant."],
    "aphorism formula": ["Latency is the currency of trust in the viewer."],
    "future-narrative closer": ["The pipeline may become one of the most important tools."],
    "meaning-telling": ["The rewrite represents a broader shift in how the team works."],
    "tier-2 cluster": ["Fostered a nuanced review process for every change."],
    # P2, the atom arm
    "copula avoidance": ["The exporter serves as the archive index.",
                         "The dashboard features a saved-search sidebar."],
    "transition phrase": ["Cut the run time. Moreover, the cache warmed faster."],
    "filler phrase": ["In terms of throughput, the parser doubled."],
    "generic conclusion": ["One thing is certain about the storage migration."],
}


def test_every_rule_has_a_seeded_example():
    """A rule added without a firing example is a rule nobody has run."""
    names = {rule.name for rule in atom_audit.SLOP_RULES} | set(atom_audit.VENDORED_TIERS)
    assert names == set(SEEDS)


@pytest.mark.parametrize("name, text", [(name, text)
                                        for name, texts in sorted(SEEDS.items())
                                        for text in texts])
def test_each_enforced_category_fires(tmp_path, capsys, name, text):
    out, code = _report(tmp_path, capsys, text)
    assert code == 0                      # no --strict, so a finding is not a failure
    assert name in out, out
    assert "TOTAL FINDINGS: 0" not in out


def test_a_finding_carries_the_atom_id_the_field_and_the_span(tmp_path, capsys):
    master = _master(impact=["clean", "Delved into the crash logs"])
    _slop(tmp_path, master)
    out = capsys.readouterr().out
    assert "projects/Widget :: core / impact[1]" in out
    assert "tier-1 vocabulary: Delved" in out


def test_findings_are_grouped_under_the_skill_s_severity_tiers(tmp_path, capsys):
    master = _master(what="Studies show the index halved lookup time.",
                     how="Delved into the crash logs and found the leak.",
                     scope="In terms of throughput, the parser doubled.")
    _slop(tmp_path, master)
    out = capsys.readouterr().out
    assert "P0  CREDIBILITY KILLERS: 1" in out
    assert "P1  OBVIOUS AI SMELL: 1" in out
    assert "P2  STYLISTIC POLISH: 1" in out
    assert "TOTAL FINDINGS: 3" in out


# ── The technical-blog word-table exception ──────────────────────────────────
# The skill's own carve-out, applied verbatim: these eight have real technical
# meanings and an atom is the register that uses them. Each sentence below must
# report NOTHING -- the bullet layer bans every one of them through
# `compose._STYLE_BANS`, and that is the difference between the two layers.
TECHNICAL_EXCEPTIONS = {
    "robust": "Rewrote the parser to be robust against truncated archives.",
    "comprehensive": "Wrote a comprehensive test suite for the exporter.",
    "seamless": "Made the handoff between the CLI and the web app seamless.",
    "ecosystem": "Packaged the tool for the Python ecosystem.",
    "leverage": "Rewrote the loader to leverage the platform cache API.",
    "facilitate": "Added a queue to facilitate retries between workers.",
    "underpin": "The trie underpins every lookup in the viewer.",
    "streamline": "Streamlined the import path down to a single pass.",
}


@pytest.mark.parametrize("word", sorted(TECHNICAL_EXCEPTIONS))
def test_the_technical_blog_exception_words_never_fire(tmp_path, capsys, word):
    out, code = _report(tmp_path, capsys, TECHNICAL_EXCEPTIONS[word], "--strict")
    assert code == 0, out
    assert "TOTAL FINDINGS: 0" in out, out


@pytest.mark.parametrize("word", ["delve", "tapestry", "beacon", "embark",
                                  "testament to", "game-changer"])
def test_the_exception_s_own_carve_outs_still_fire(tmp_path, capsys, word):
    """The same table keeps these flaggable in technical writing."""
    out, code = _report(tmp_path, capsys, f"Wrote a {word} into the exporter.", "--strict")
    assert code == 1, out


# ── harness: the noun is a test rig, the verb is the tier-2 word ─────────────
# A long sentence of the shape an atom takes. A word-list match calls this slop;
# it is a reproducible test rig a candidate built and wrote down.
CORPUS_HARNESS_NOUN = (
    "Shipped a Flask dashboard that streams build logs line-by-line over "
    "WebSockets (with a CLI sharing the same core), and a reproducible "
    "evaluation harness that drove the parser and ranking fixes.")


@pytest.mark.parametrize("text", [
    CORPUS_HARNESS_NOUN,
    "a reproducible evaluation harness",
    "The evaluation harness runs scored sample batches through the pipeline.",
    "Built a test harness for the parser.",
    "Wrote a harness the reviewers run unattended.",
])
def test_the_harness_noun_is_not_the_harness_verb(tmp_path, capsys, text):
    out, code = _report(tmp_path, capsys, text, "--strict")
    assert code == 0, out
    assert "harness as a verb" not in out, out


@pytest.mark.parametrize("text", [
    "Harnessed the power of the GPU cluster.",
    "Rewrote the loader to harness the full cache.",
    "The scheduler harnesses their idle capacity.",
])
def test_the_harness_verb_does_fire(tmp_path, capsys, text):
    out, code = _report(tmp_path, capsys, text, "--strict")
    assert code == 1, out
    assert "harness as a verb" in out


# ── Contrast framing is the reveal, not every comparison ─────────────────────
# In an atom, "rather than" / "instead of" usually records an implementation
# choice. The bullet layer bans both phrases outright; at the atom layer that ban
# would report those facts as slop.
@pytest.mark.parametrize("text", [
    "The two exports merge on record id rather than name.",
    "A missing API key prints one line instead of a traceback.",
    "Search results sort by edit date rather than by relevance.",
    "Fixed all 37 warnings rather than lowering the lint bar.",
    "No rows were dropped, no counts changed, and no index was rebuilt.",
])
def test_an_ordinary_comparison_is_not_contrast_framing(tmp_path, capsys, text):
    out, code = _report(tmp_path, capsys, text, "--strict")
    assert code == 0, out


# ── Adjective stacking is a pile-up, not a pair ──────────────────────────────
# Every string below was reported by a two-modifier threshold when this rule was
# first written, and every one of them is correct technical English: a pair of
# precise compound modifiers, or two coordinated compound NOUNS.
@pytest.mark.parametrize("text", [
    "Built a single-page, offline viewer for the logs.",
    "Added size-limit and bad-input rejections to the uploader.",
    "Blocked the cross-origin and open-redirect route into the API.",
    "Set a per-host, rate-limit budget for the fetcher.",
    "A restarted job re-queued, de-duplicates results on resume.",
    "Wrote the security-clearance and advanced-degree filters.",
])
def test_two_compound_modifiers_are_not_a_stack(tmp_path, capsys, text):
    out, code = _report(tmp_path, capsys, text, "--strict")
    assert code == 0, out


# ── Exit codes ───────────────────────────────────────────────────────────────

def test_strict_exits_one_on_findings_and_zero_on_a_clean_atom(tmp_path, capsys):
    dirty = _master(what="Delved into the crash logs and found the leak.")
    loose = _slop(tmp_path, dirty)
    capsys.readouterr()
    strict = _slop(tmp_path, dirty, "--strict")
    capsys.readouterr()
    clean = _slop(tmp_path, _master(), "--strict")
    capsys.readouterr()
    assert (loose, strict, clean) == (0, 1, 0)


def test_slop_reports_a_missing_file_instead_of_crashing(tmp_path, capsys):
    code = atom_audit.main(["slop", "--file", str(tmp_path / "nope.yaml")])
    err = capsys.readouterr().err
    assert code == 2
    assert "nope.yaml" in err


def test_slop_cli_runs_as_a_subprocess(tmp_path):
    path = tmp_path / "master.yaml"
    path.write_text(yaml.safe_dump(_master(what="Delved into the crash logs."),
                                   sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    proc = subprocess.run([sys.executable, str(MODULE_PATH), "slop",
                           "--file", str(path), "--strict"],
                          capture_output=True, text=True, encoding="utf-8",
                          cwd=str(REPO), timeout=60)
    assert proc.returncode == 1, proc.stderr
    assert "tier-1 vocabulary" in proc.stdout


# ── The vendored arm is the bullet layer's own list, not a copy ──────────────

def test_the_vendored_arm_agrees_with_resume_violations():
    """Reuse, verified: the names this audit reports for the vendored patterns
    are exactly what `aiwriting.resume_violations()` returns for the same text."""
    aiwriting = atom_audit.load_aiwriting()
    text = ("Responsible for delving into studies show the industry-leading "
            "paradigm shift.")
    reported = {name for _, name, _ in
                atom_audit.slop_findings(text, atom_audit.vendored_rules())}
    vendored = {name for name in reported if name in atom_audit.VENDORED_TIERS}
    assert vendored == set(aiwriting.resume_violations(text))
    assert len(vendored) >= 4, vendored


def test_every_vendored_ban_is_tiered():
    """A ban added at the bullet layer must be tiered here or the audit fails
    loudly, rather than silently dropping it."""
    bans = {name for name, _ in atom_audit.load_aiwriting().RESUME_EXTRA_BANS}
    assert bans == set(atom_audit.VENDORED_TIERS)


def test_the_audit_loads_two_files_and_never_the_package(tmp_path):
    """`local/resume_tailor/config.py` calls `load_dotenv()` at import scope, so
    a full `import resume_tailor.aiwriting` would pull live credentials into a
    read-only audit. Run from a clean interpreter, the audit loads exactly two
    modules and neither is the real package."""
    path = tmp_path / "master.yaml"
    path.write_text(yaml.safe_dump(_master(), sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('aa', {str(MODULE_PATH)!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        f"mod.main(['slop', '--file', {str(path)!r}])\n"
        "print('MODULES', sorted(n for n in sys.modules "
        "if 'resume_tailor' in n or n.startswith('_atom_audit')))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, encoding="utf-8", cwd=str(REPO), timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert ("MODULES ['_atom_audit_aiwriting', '_atom_audit_aiwriting.aiwriting', "
            "'_atom_audit_aiwriting.common']") in proc.stdout, proc.stdout
