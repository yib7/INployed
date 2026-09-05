# -*- coding: utf-8 -*-
"""The curated style exemplar (cycle 10, SP5).

``compose.rephrase`` shows the model a sample of the user's own writing labelled "STYLE
EXEMPLAR (match this voice, length and density; NEVER copy its facts)". The source used
to be one thing: text extracted from ``resume_sample.pdf``, sliced ``[:1200]``. Measured,
that slice spent its first 472 characters on name / contact / education, delivered 3
complete bullets out of 14 plus a fourth cut mid-word, glued the next section's heading
onto several bullets, and demonstrated a participial impact tail that the very same
prompt's ``BANNED_PHRASING`` forbids. The package had already made this call once for a
different consumer: ``assets._FALLBACK_VERBS`` records that the raw PDF dump was dropped
for the verb palette as "jumbled multi-column OCR -- weak signal AND expensive".

So ``assets.example_text()`` is now a three-arm resolver -- curated file, else PDF
extract, else "" -- and ``compose`` bounds it on a LINE boundary instead of mid-word.

Two rules this file holds that production's own gate deliberately does not:

* **The participial impact tail:** ``compose._STYLE_BANS`` matches a CLOSED verb list
  (``enabling|ensuring|allowing|driving|resulting in|...``) because a false positive
  there buys a repair call that can damage a correct bullet. Cycle 11 widened that
  pattern's REACH so the listed verb no longer has to sit directly after the comma,
  which is what made it miss "..., minimizing manual review bottlenecks and enabling
  product expansion", the exact tail the old exemplar taught. The verb LIST is still
  closed, so "..., reducing runtime from 4h to 20m" is still clean in production.
  Curation is not production: here the SHAPE is what must not be shown, so the check
  below stays broader -- any participle after a comma, listed or not.
* **Decorative marketing frames:** ``BANNED_PHRASING`` quotes 'end-to-end', 'one place'
  and 'all-in-one' verbatim, but they are absent from ``_STYLE_BANS`` for the same
  false-positive reason (a real bullet may mean end-to-end literally). An exemplar has
  no reason to take that risk, so it is checked here.

Hermetic: no test depends on the user's real ``style_exemplar.txt`` or ``resume_sample.pdf``
existing -- a fresh clone has neither. The one test that reads the real file skips when it
is absent. ``assets.example_text`` is ``lru_cache``d, so an autouse fixture clears it
around every test here (the pattern ``test_active_verbs`` uses for ``active_verbs``):
without it the first test's tmp_path answer would be served to its neighbours, and to the
rest of the session.
"""
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import assets, compose, config, selection  # noqa: E402

# Any participle directly after a comma -- see the module docstring for why this is
# broader than compose._STYLE_BANS and why it lives here rather than there.
PARTICIPIAL_TAIL = re.compile(r",\s+\w+ing\b", re.I)
DECORATIVE_FRAME = re.compile(r"\bend-to-end\b|\ball-in-one\b|\bone place\b", re.I)


@pytest.fixture(autouse=True)
def _clear_example_text_cache():
    assets.example_text.cache_clear()
    yield
    assets.example_text.cache_clear()


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    """Point both exemplar sources into tmp_path, neither existing yet.

    Every arm test creates only the files its arm needs, so no test can pass because
    the developer happens to have a real resume_sample.pdf on disk."""
    txt = tmp_path / "style_exemplar.txt"
    pdf = tmp_path / "resume_sample.pdf"
    monkeypatch.setattr(config, "STYLE_EXEMPLAR_TXT", txt)
    monkeypatch.setattr(config, "EXAMPLE_PDF", pdf)
    return txt, pdf


def _pdf_says(monkeypatch, text: str) -> None:
    """Stub the pypdf seam. The PDF arm's job is 'reach _pdf_text with EXAMPLE_PDF';
    building a real PDF here would test pypdf, not the resolver."""
    monkeypatch.setattr(assets, "_pdf_text", lambda p: text if Path(p).exists() else "")


# ── the three resolver arms ──────────────────────────────────────────────────
def test_curated_file_wins_over_the_pdf(paths, monkeypatch):
    txt, pdf = paths
    txt.write_text("Built a thing that did a measurable amount of good.\n", encoding="utf-8")
    pdf.write_bytes(b"%PDF-fake")
    _pdf_says(monkeypatch, "PDF EXTRACT, the old source")
    assert assets.example_text() == "Built a thing that did a measurable amount of good."


def test_falls_back_to_the_pdf_when_the_curated_file_is_absent(paths, monkeypatch):
    txt, pdf = paths
    pdf.write_bytes(b"%PDF-fake")
    _pdf_says(monkeypatch, "PDF EXTRACT, the old source")
    assert not txt.exists(), "this arm is only meaningful with the .txt missing"
    assert assets.example_text() == "PDF EXTRACT, the old source"


def test_returns_empty_when_neither_source_exists(paths):
    """A fresh clone has neither file. Note this arm runs the REAL _pdf_text against a
    missing path, so it also pins that a pypdf failure is swallowed rather than raised."""
    txt, pdf = paths
    assert not txt.exists() and not pdf.exists()
    assert assets.example_text() == ""


# ── the parser ───────────────────────────────────────────────────────────────
def test_parser_drops_comments_and_blanks_and_strips_each_line(paths):
    txt, _ = paths
    txt.write_text(
        "# a header comment\n"
        "\n"
        "   Analyzed three surveys and reported the result.   \n"
        "\t\n"
        "# an interior comment\n"
        "Processed 100+ transactions per shift.\n"
        "   \n",
        encoding="utf-8")
    assert assets.example_text() == (
        "Analyzed three surveys and reported the result.\n"
        "Processed 100+ transactions per shift.")


def test_a_comments_only_file_falls_through_to_the_pdf(paths, monkeypatch):
    """An exemplar of nothing is worse than the old one: the prompt would carry an empty
    STYLE EXEMPLAR block. A file the user has only commented out means 'not configured'."""
    txt, pdf = paths
    txt.write_text("# nothing but comments\n#\n\n   \n", encoding="utf-8")
    pdf.write_bytes(b"%PDF-fake")
    _pdf_says(monkeypatch, "PDF EXTRACT, the old source")
    assert assets.example_text() == "PDF EXTRACT, the old source"


def test_an_empty_file_falls_through_to_the_pdf(paths, monkeypatch):
    txt, pdf = paths
    txt.write_text("", encoding="utf-8")
    pdf.write_bytes(b"%PDF-fake")
    _pdf_says(monkeypatch, "PDF EXTRACT, the old source")
    assert assets.example_text() == "PDF EXTRACT, the old source"


def test_a_non_utf8_file_never_breaks_a_tailoring_run(paths, monkeypatch):
    """read_text raises UnicodeDecodeError (a ValueError, not an OSError) on binary
    content. The exemplar is a nice-to-have; a malformed one must degrade, never raise."""
    txt, pdf = paths
    txt.write_bytes(b"\xff\xfe\x00Built a thing\x00\xff")
    pdf.write_bytes(b"%PDF-fake")
    _pdf_says(monkeypatch, "PDF EXTRACT, the old source")
    assert assets.example_text() == "PDF EXTRACT, the old source"


def test_a_directory_at_the_exemplar_path_never_breaks_a_tailoring_run(paths):
    txt, _ = paths
    txt.mkdir()
    assert assets.example_text() == ""


def test_example_text_stays_cached(paths):
    """main_window._tailor_work pre-warms this before fanning out across threads, so the
    lru_cache is required, not incidental: N concurrent tailors must not each re-read."""
    txt, _ = paths
    txt.write_text("First answer.\n", encoding="utf-8")
    assert assets.example_text() == "First answer."
    txt.write_text("Second answer.\n", encoding="utf-8")
    assert assets.example_text() == "First answer."
    assets.example_text.cache_clear()
    assert assets.example_text() == "Second answer."


# ── the prompt's character cap ───────────────────────────────────────────────
def test_an_exemplar_under_the_cap_is_passed_through_whole():
    text = "Built a thing.\nShipped another thing.\nMeasured both."
    assert compose._exemplar_for_prompt(text) == text


def test_the_cap_cuts_on_a_line_boundary_never_mid_word():
    """The defect this replaced: `[:1200]` over the PDF extract ended the exemplar at
    "Proc", so the prompt that calls a bullet ending mid-clause "a failure" showed one."""
    lines = [f"Line {i} " + "word " * 10 for i in range(20)]
    text = "\n".join(lines)
    out = compose._exemplar_for_prompt(text, cap=200)
    assert len(out) <= 200
    assert out, "the cap must not empty a multi-line exemplar"
    assert all(ln in lines for ln in out.splitlines()), "a line was cut in half"
    assert text.startswith(out), "lines must be kept in order from the top"


def test_the_cap_keeps_as_many_whole_lines_as_fit():
    text = "aaaa\nbbbb\ncccc\ndddd"           # 4 chars each, 19 total with newlines
    assert compose._exemplar_for_prompt(text, cap=9) == "aaaa\nbbbb"
    assert compose._exemplar_for_prompt(text, cap=13) == "aaaa\nbbbb"   # 14 needed for 3
    assert compose._exemplar_for_prompt(text, cap=14) == "aaaa\nbbbb\ncccc"


def test_a_single_over_long_line_is_hard_cut_rather_than_dropped():
    """One line longer than the whole cap is not a bullet list; something has to give,
    and an empty STYLE EXEMPLAR block is worse than a truncated one."""
    out = compose._exemplar_for_prompt("x" * 5000 + "\ntail", cap=100)
    assert out == "x" * 100


def test_the_cap_matches_the_slice_it_replaced():
    assert compose.EXEMPLAR_CHAR_CAP == 1200


# ── the prompt seam ──────────────────────────────────────────────────────────
def _capture_rephrase_prompt(monkeypatch, exemplar: str) -> str:
    monkeypatch.setattr(compose, "_atom_payload", lambda a: {"what": f"did {a}"})
    monkeypatch.setattr(selection, "_block_of", lambda a: "Globex")
    monkeypatch.setattr(compose.assets, "example_text", lambda: exemplar)
    monkeypatch.setattr(compose.assets, "active_verbs",
                        lambda: {"Technical Skills": ["Built", "Engineered"]})
    captured = {}

    def fake_call(system, user, tier, **kw):
        captured["user"] = user
        return {"bullets": [{"gkey": "a1", "text": "Built A."}]}

    monkeypatch.setattr(compose, "call", fake_call)
    sel = {"experience": [{"name": "Globex", "groups": [["a1"]]}],
           "projects": [], "leadership": []}
    compose.rephrase("jd", "Eng", sel)
    return captured["user"]


def test_rephrase_injects_the_curated_exemplar_verbatim(monkeypatch):
    exemplar = "Processed 100+ transactions per shift.\nAnalyzed three national surveys."
    user = _capture_rephrase_prompt(monkeypatch, exemplar)
    assert "STYLE EXEMPLAR" in user
    assert exemplar in user


def test_rephrase_bounds_an_oversized_exemplar_without_cutting_a_word(monkeypatch):
    bullets = [f"Bullet {i} " + "supporting detail " * 8 for i in range(40)]
    user = _capture_rephrase_prompt(monkeypatch, "\n".join(bullets))
    body = user.split("STYLE EXEMPLAR (match this voice, length and density; "
                      "NEVER copy its facts):\n", 1)[1].split("\n\nBLOCKS", 1)[0]
    assert len(body) <= compose.EXEMPLAR_CHAR_CAP
    assert all(ln in bullets for ln in body.splitlines())


# ── the user's own generated file ────────────────────────────────────────────
def _real_exemplar_lines() -> list:
    """The real file's bullets, read WITHOUT going through the cached example_text() so
    this test cannot poison the session cache with the developer's own content."""
    return [ln for ln in assets._exemplar_lines(config.STYLE_EXEMPLAR_TXT).splitlines() if ln]


@pytest.mark.skipif(not config.STYLE_EXEMPLAR_TXT.exists(),
                    reason="style_exemplar.txt is personal, git-ignored and optional; "
                           "CI and a fresh clone do not have one")
def test_the_configured_exemplar_never_demonstrates_what_the_prompt_bans():
    """The point of the whole phase. Whatever the user puts in this file rides into every
    rephrase prompt as the thing to imitate, so a banned construction there is worse than
    one in a bullet: it is taught."""
    lines = _real_exemplar_lines()
    assert lines, "a present-but-empty exemplar would send the model an empty block"
    for line in lines:
        assert not compose.style_violations(line), (
            f"style_violations: {compose.style_violations(line)} in {line!r}")
        assert not PARTICIPIAL_TAIL.search(line), f"participial impact tail in {line!r}"
        assert not DECORATIVE_FRAME.search(line), f"decorative frame in {line!r}"


@pytest.mark.skipif(not config.STYLE_EXEMPLAR_TXT.exists(),
                    reason="style_exemplar.txt is personal, git-ignored and optional")
def test_the_configured_exemplar_fits_the_prompt_without_being_cut():
    """A curated exemplar should never reach the cap: if it does, the file has grown into
    the thing the cap exists to bound and the last bullets are silently not being shown."""
    text = assets._exemplar_lines(config.STYLE_EXEMPLAR_TXT)
    assert compose._exemplar_for_prompt(text) == text.strip()
