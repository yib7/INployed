"""`scripts/atom_audit.py` — the master-atom hygiene census and the no-new-facts gate.

Hermetic: every fixture is a small synthetic master written into `tmp_path`. The
user's real `resume_tailor_files/master_experience.yaml` is gitignored personal
data and is never read here (see tests/test_hermetic_repo_data.py for why a test
that reads it passes only on one machine).

The one import from `local/` is `resume_tailor.assets`, and only to pin the
census's field set against `atom_line()` — the flattening the tailor actually
sends. If those two ever disagree, the census measures a payload nobody ships.
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
    spec = importlib.util.spec_from_file_location("atom_audit", MODULE_PATH)
    assert spec and spec.loader, f"missing {MODULE_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


atom_audit = _load_module()


def _write(path: Path, master: dict) -> Path:
    path.write_text(yaml.safe_dump(master, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    return path


def _clean_master() -> dict:
    """Two atoms that share no 4-word run, with no figure at or above 10,000."""
    return {
        "projects": [
            {"name": "Widget",
             "stack": "Python 3.12, FastAPI",
             "achievements": [
                 {"id": "core",
                  "what": "built a viewer for an offline archive",
                  "how": "indexed every message into a trie at import time",
                  "scope": "9,120 conversations",
                  "impact": ["opening any thread takes under a second"]},
                 {"id": "tests",
                  "what": "wrote the regression suite",
                  "impact": ["three platforms run it on every push"]},
             ]},
        ],
    }


def _census(tmp_path: Path, master: dict, *flags: str):
    """Run the census over `master`; returns (the file written, the exit code)."""
    path = _write(tmp_path / "master.yaml", master)
    return path, atom_audit.main(["census", "--file", str(path), *flags])


# ── census: repeats ──────────────────────────────────────────────────────────

def test_census_reports_an_intra_atom_repeat(tmp_path, capsys):
    master = _clean_master()
    master["projects"][0]["achievements"][0].update(
        scope="stress tested against generated mailboxes of about 90K messages",
        impact=["stress tested against generated mailboxes of about 90K messages "
                "with no dropped frames"],
    )
    _, code = _census(tmp_path, master)
    out = capsys.readouterr().out
    assert code == 0
    assert "core" in out
    assert "scope" in out and "impact[0]" in out
    # the overlapping 4-word shingles collapse into the one maximal phrase
    assert "stress tested against generated mailboxes of about 90k messages" in out
    assert out.count("stress tested against generated") == 1


def test_census_reports_a_cross_atom_repeat(tmp_path, capsys):
    master = _clean_master()
    master["projects"][0]["achievements"][0]["how"] = (
        "the LaTeX tailoring engine renders the page")
    master["projects"][0]["achievements"][1]["what"] = (
        "built the LaTeX tailoring engine renders nothing else")
    _, code = _census(tmp_path, master)
    out = capsys.readouterr().out
    assert code == 0
    assert "Widget" in out
    assert "core" in out and "tests" in out
    assert "the latex tailoring engine renders" in out


def test_census_does_not_cross_entry_boundaries(tmp_path, capsys):
    master = _clean_master()
    twin = {"name": "Other", "achievements": [
        {"id": "other_core", "what": "built a viewer for an offline archive"}]}
    master["projects"].append(twin)
    _, code = _census(tmp_path, master)
    out = capsys.readouterr().out
    assert code == 0
    assert "other_core" not in out
    assert "cross-atom repeats: 0" in out.lower()


# ── census: figures ──────────────────────────────────────────────────────────

def test_census_reports_a_large_figure_in_an_atom(tmp_path, capsys):
    master = _clean_master()
    master["projects"][0]["achievements"][0]["scope"] = "512,384 rows indexed"
    _, code = _census(tmp_path, master)
    out = capsys.readouterr().out
    assert code == 0
    assert "512,384" in out and "core" in out and "scope" in out


def test_census_reports_a_large_figure_in_entry_prose(tmp_path, capsys):
    master = _clean_master()
    master["projects"][0]["origin"] = "the catalog alone has 52,170 entries"
    _, code = _census(tmp_path, master)
    out = capsys.readouterr().out
    assert "52,170" in out and "origin" in out


def test_census_flags_plus_and_tilde_figures_at_any_magnitude(tmp_path, capsys):
    master = _clean_master()
    master["projects"][0]["achievements"][0]["impact"] = [
        "over 200K+ emails archived", "~21k pages crawled", "400+ volunteers"]
    _, code = _census(tmp_path, master)
    out = capsys.readouterr().out
    assert "200K+" in out and "~21k" in out and "400+" in out


def test_census_does_not_read_a_plus_inside_a_name_as_a_floor(tmp_path, capsys):
    """A name like "BM25+rerank" is an algorithm, not "25 or more"."""
    master = _clean_master()
    master["projects"][0]["achievements"][0]["impact"] = [
        "a BM25+rerank pipeline ran twice", "C++ was not involved", "3 + 4 runs"]
    _, code = _census(tmp_path, master)
    out = capsys.readouterr().out
    assert "figures with + or ~: 0" in out.lower(), out


def test_census_ignores_years_versions_and_the_embedding_constant(tmp_path, capsys):
    master = _clean_master()
    master["projects"][0]["achievements"][0]["how"] = (
        "1024-dimensional embeddings, shipped in v1.4.0 during 2024 on Python 3.12")
    master["projects"][0]["ship_state"] = "6 tagged releases through v2.7.1 (2025-11-04)"
    _, code = _census(tmp_path, master)
    out = capsys.readouterr().out
    assert code == 0
    assert "figures" in out.lower()
    for excluded in ("1024", "1.4.0", "2024", "3.12", "2.7.1", "2025"):
        assert f"{excluded}\n" not in out, f"{excluded} was reported as a figure"
    assert "findings: 0" in out.lower()


# ── census: style + exit codes ───────────────────────────────────────────────

def test_census_counts_style_characters(tmp_path, capsys):
    master = _clean_master()
    master["projects"][0]["achievements"][0]["what"] = (
        "built a viewer — the archive’s own “browser”")
    _, code = _census(tmp_path, master)
    out = capsys.readouterr().out
    assert "U+2014" in out and "U+2019" in out and "U+201C" in out
    # ASCII only, so a cp1252 console never breaks the report
    assert out.isascii()


def test_a_clean_file_reports_zero_everywhere(tmp_path, capsys):
    _, code = _census(tmp_path, _clean_master())
    out = capsys.readouterr().out.lower()
    assert code == 0
    assert "findings: 0" in out
    for label in ("intra-atom repeats: 0", "cross-atom repeats: 0", "figures: 0"):
        assert label in out


def test_strict_exits_one_on_findings_and_zero_on_a_clean_file(tmp_path, capsys):
    dirty = _clean_master()
    dirty["projects"][0]["achievements"][0]["scope"] = "512,384 rows indexed"
    _, loose = _census(tmp_path, dirty)
    capsys.readouterr()
    _, strict = _census(tmp_path, dirty, "--strict")
    capsys.readouterr()
    _, clean = _census(tmp_path, _clean_master(), "--strict")
    capsys.readouterr()
    assert (loose, strict, clean) == (0, 1, 0)


def test_census_cli_runs_as_a_subprocess(tmp_path):
    dirty = _clean_master()
    dirty["projects"][0]["achievements"][0]["scope"] = "512,384 rows indexed"
    path = _write(tmp_path / "master.yaml", dirty)
    proc = subprocess.run([sys.executable, str(MODULE_PATH), "census",
                           "--file", str(path), "--strict"],
                          capture_output=True, text=True, encoding="utf-8",
                          cwd=str(REPO), timeout=60)
    assert proc.returncode == 1, proc.stderr
    assert "512,384" in proc.stdout


# ── the field set the census measures ────────────────────────────────────────

def test_atom_fields_mirror_the_tailor_payload():
    """The census must flatten exactly what `assets.atom_line()` sends."""
    sys.path.insert(0, str(REPO / "local"))
    from resume_tailor import assets

    atom = {"id": "x", "what": "did a thing", "how": "with a tool",
            "scope": "at some size", "impact": ["one", "", "two"],
            "angles": ["ignored"], "hardest_problem": "also ignored"}
    labels = [label for label, _ in atom_audit.atom_fields(atom)]
    # the label carries the impact entry's own index in the YAML list, empties
    # included, so a reported field is the one a human counts to in the file
    assert labels == ["what", "how", "scope", "impact[0]", "impact[2]"]
    assert "; ".join(t for _, t in atom_audit.atom_fields(atom)) == assets.atom_line(atom)


# ── gate ─────────────────────────────────────────────────────────────────────

def _gate(tmp_path: Path, old: dict, new: dict):
    old_p = _write(tmp_path / "old.yaml", old)
    new_p = _write(tmp_path / "new.yaml", new)
    return atom_audit.main(["gate", "--old", str(old_p), "--new", str(new_p)])


def test_gate_passes_identical_files(tmp_path, capsys):
    master = _clean_master()
    code = _gate(tmp_path, master, master)
    out = capsys.readouterr().out
    assert code == 0
    assert "no new facts" in out.lower()


def test_gate_fails_on_an_invented_number(tmp_path, capsys):
    new = _clean_master()
    new["projects"][0]["achievements"][0]["scope"] = "48,000 conversations"
    code = _gate(tmp_path, _clean_master(), new)
    out = capsys.readouterr().out
    assert code == 1
    assert "48,000" in out and "core" in out


def test_gate_fails_on_an_invented_proper_noun(tmp_path, capsys):
    new = _clean_master()
    new["projects"][0]["achievements"][0]["how"] = (
        "indexed every message into a trie on Kubernetes")
    code = _gate(tmp_path, _clean_master(), new)
    out = capsys.readouterr().out
    assert code == 1
    assert "Kubernetes" in out and "core" in out


def test_gate_passes_an_ordinary_paraphrase(tmp_path, capsys):
    old = _clean_master()
    old["projects"][0]["achievements"][0]["how"] = (
        "indexed every message into a trie at import time, in Python")
    new = _clean_master()
    new["projects"][0]["achievements"][0]["how"] = (
        "every message lands in a Python trie as the import runs")
    code = _gate(tmp_path, old, new)
    out = capsys.readouterr().out
    assert code == 0, out


def test_gate_ignores_an_inert_sibling_key_in_the_new_file(tmp_path, capsys):
    """Relocated detail leaves the tailor payload; it is not a new fact."""
    old = _clean_master()
    new = _clean_master()
    new["projects"][0]["achievements"][0]["hardest_problem"] = (
        "the Kubernetes rollout nobody asked for")
    code = _gate(tmp_path, old, new)
    assert code == 0, capsys.readouterr().out


@pytest.mark.parametrize("old_text, new_text", [
    ("512,384 rows indexed", "512K rows indexed"),
    ("147,203 records after merge", "147K records after merge"),
    ("58,442 images", "58K images"),
    ("70,000+ labels", "over 70K labels"),
    ("~21k pages crawled", "21K pages crawled"),
    ("200K+ emails archived", "over 200K emails archived"),
    ("400+ volunteers trained", "over 400 volunteers trained"),
    ("2,000+ page reports", "over 2,000 page reports"),
])
def test_gate_passes_the_declared_conversions(tmp_path, capsys, old_text, new_text):
    old = _clean_master()
    old["projects"][0]["achievements"][0]["scope"] = old_text
    new = _clean_master()
    new["projects"][0]["achievements"][0]["scope"] = new_text
    code = _gate(tmp_path, old, new)
    assert code == 0, capsys.readouterr().out


def test_gate_still_fails_a_figure_that_is_not_a_rounding(tmp_path, capsys):
    """`512,384 -> 500K` is not a rounding of anything in the old file."""
    old = _clean_master()
    old["projects"][0]["achievements"][0]["scope"] = "512,384 rows indexed"
    new = _clean_master()
    new["projects"][0]["achievements"][0]["scope"] = "500K rows indexed"
    code = _gate(tmp_path, old, new)
    out = capsys.readouterr().out
    assert code == 1
    assert "500" in out


def test_gate_reports_a_missing_file_instead_of_crashing(tmp_path, capsys):
    old_p = _write(tmp_path / "old.yaml", _clean_master())
    code = atom_audit.main(["gate", "--old", str(old_p),
                            "--new", str(tmp_path / "nope.yaml")])
    err = capsys.readouterr().err
    assert code == 2
    assert "nope.yaml" in err
