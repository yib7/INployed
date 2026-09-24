"""Checks over the real-page captures, which stay on this machine
(`tests/fixtures/local_captures/`, git-excluded). Every test here skips when
the folder is absent (a fresh clone, CI).

- The consent pre-step's control (`apply_form.consent_control`) on every
  capture that shows a cookie banner: the reject or close control the
  captures' expectations file names (`_consent_expected.json`, beside the
  captures), never an accept.

Each capture's `page.html` is loaded offline (the browser tests' guard
aborts every request off the local machine), so nothing reaches the site
it came from."""
import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_form  # noqa: E402
import apply_run  # noqa: E402

pytest_plugins = ["conftest_browser"]

CAPTURES = REPO / "tests" / "fixtures" / "local_captures"
EXPECTED = CAPTURES / "_consent_expected.json"


def _expected() -> dict:
    if not EXPECTED.is_file():
        return {}
    return {k: v for k, v in json.loads(EXPECTED.read_text(encoding="utf-8")).items()
            if not k.startswith("_")}


@pytest.mark.skipif(not EXPECTED.is_file(), reason="no local captures on this machine")
@pytest.mark.parametrize("capture", sorted(_expected()))
def test_the_consent_control_on_each_capture_with_a_banner(_browser, capture):
    folder = CAPTURES / capture
    if not (folder / "page.html").is_file():
        pytest.skip(f"{capture} is not on this machine")
    want = _expected()[capture]
    ctx = _browser.new_context(viewport={"width": 1400, "height": 1000})
    try:
        page = ctx.new_page()
        page.set_content((folder / "page.html").read_text(encoding="utf-8", errors="replace"),
                         wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(300)
        found = apply_form.consent_control(page)
    finally:
        ctx.close()
    got = [found[1]["kind"], found[1]["text"]] if found else None
    assert got == want, (capture, found)
    if found:
        assert not re.search(r"accept|allow|agree", found[1]["text"], re.I)


# --- SP5: the fields a person sees on each capture -------------------------------------------------
#
# Each capture's MHTML snapshot (its page.html when it has none) is loaded
# offline, frames and all, with no script running and every request off the
# machine aborted; `apply_form.extract` reads it. Every capture: no field's
# label is a bare required marker or keeps one, and no field is a box no
# person fills. The captures' expectations file (`_fields_expected.json`,
# beside the captures, local only like them) names per capture the fields
# a person sees there (label, type, required), the questions a person sees
# marked required (`required`, by their first words) and those a person sees
# as optional (`optional`), their widgets, the heading a question sits under
# (`sections`: its first words to the heading) and the headings no field sits
# under (`no_sections`: a posting's or a resume parser's), the labels no
# field may carry (`no_labels` inside any label, `not_labels` as a whole
# label, `max_fields` for a page whose widgets are no application's), and the
# buttons that must show (with `disabled` where the page keeps one disabled).
# A bot check's frame is left out, as the run leaves it out.

FIELDS = CAPTURES / "_fields_expected.json"
_MARKER_ONLY = re.compile(r"^\s*([*✱＊]+|\(required\)|required\.?)\s*$", re.I)
_MARKER_LEFT = re.compile(r"(\s[*✱＊]|\(required\))\s*$|^\s*[*✱＊]", re.I)
_JUNK = re.compile(r"honey\s*-?pot|robots? only|leave (this )?(field )?blank", re.I)


def _captures() -> list[str]:
    return sorted(f"{p.parent.parent.name}/{p.parent.name}"
                  for p in CAPTURES.glob("*/*/meta.json")) if CAPTURES.is_dir() else []


def _fields_expected() -> dict:
    if not FIELDS.is_file():
        return {}
    return {k: v for k, v in json.loads(FIELDS.read_text(encoding="utf-8")).items()
            if not k.startswith("_")}


_CID = re.compile(rb"cid:[^\"')\s>]+")


def _snapshot_parts(path: Path) -> tuple[str, dict]:
    """(the page's URL, {url: (content type, body)}) of a Blink MHTML
    snapshot: every MIME part at its own URL, a `cid:` part at a made-up
    one with every reference to it rewritten (a frame's document, a
    stylesheet)."""
    import email
    from email import policy
    raw = path.read_bytes().replace(bytes((13, 13, 10)), bytes((13, 10)))  # a text-mode save
    rows = []
    for part in email.message_from_bytes(raw, policy=policy.compat32).walk():
        if part.is_multipart():
            continue
        rows.append((str(part.get("Content-Location", "") or "").strip(),
                     str(part.get("Content-ID", "") or "").strip().strip("<>"),
                     part.get_content_type(), part.get_payload(decode=True) or b""))
    main = next(loc for loc, _, ctype, _ in rows if ctype == "text/html" and loc.startswith("http"))
    urls = [loc if loc.startswith("http") else f"https://mhtml.invalid/part/{i}"
            for i, (loc, _, _, _) in enumerate(rows)]
    to_url = {}
    for url, (loc, cid, _, _) in zip(urls, rows):
        if cid:
            to_url[b"cid:" + cid.encode()] = url
        if loc.startswith("cid:"):
            to_url[loc.encode()] = url
    served: dict = {}
    for url, (_, _, ctype, body) in zip(urls, rows):
        if ctype.startswith("text/"):
            body = _CID.sub(lambda m: to_url[m.group(0)].encode() if m.group(0) in to_url
                            else m.group(0), body)
        served.setdefault(url, (ctype, body))
    return main, served


def _read_capture(browser, folder: Path):
    """The capture's digest, read offline with no script running: its MHTML
    snapshot replayed from its own parts (the page and its frames as
    captured; every other request aborted), else its page.html."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 1000}, java_script_enabled=False)
    try:
        mhtml = folder / "snapshot.mhtml"
        if mhtml.is_file() and mhtml.stat().st_size:
            main, served = _snapshot_parts(mhtml)

            def _serve(route):
                hit = served.get(route.request.url)
                if hit is None:
                    route.abort()
                else:
                    route.fulfill(status=200, content_type=hit[0], body=hit[1])
            ctx.route("**/*", _serve)
            page = ctx.new_page()
            page.goto(main, wait_until="load", timeout=30_000)
        else:
            page = ctx.new_page()
            page.set_content((folder / "page.html").read_text(encoding="utf-8",
                                                               errors="replace"),
                             wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(300)
        d = apply_form.extract(page)
        # the run never reads a bot check's frame (`_drop_foreign_controls`):
        # a reCAPTCHA anchor is no field of the application (review M12)
        urls = [str(f.url) for f in apply_form.frames(page)]
        bot = {i for i, u in enumerate(urls) if apply_run._is_captcha_url(u)}
        d.fields = [f for f in d.fields if int(f.locator[0]) not in bot]
        d.buttons = [b for b in d.buttons if int(b.locator[0]) not in bot]
        return d
    finally:
        ctx.close()


@pytest.mark.skipif(not CAPTURES.is_dir(), reason="no local captures on this machine")
@pytest.mark.parametrize("capture", _captures())
def test_each_capture_shows_the_fields_a_person_sees(_browser, capture):
    d = _read_capture(_browser, CAPTURES / capture)
    for f in d.fields:
        assert not _MARKER_ONLY.match(f.label or "") and not _MARKER_LEFT.search(f.label or ""), \
            (capture, f.label)
        assert not _JUNK.search(f"{f.label} {f.id_or_name}"), (capture, f.label)
    want = _fields_expected().get(capture)
    if not want:
        return
    got = {(f.label, f.type, f.required) for f in d.fields}
    labels = [f.label for f in d.fields]
    for label, type_, required in want.get("fields", []):
        assert (label, type_, required) in got, (capture, label, sorted(got))
    for label, widget in (want.get("widgets") or {}).items():
        # (a long question by its first words)
        assert any(f.label.startswith(label) and f.widget == widget for f in d.fields), \
            (capture, label, widget)
    for label in want.get("no_labels", []):
        assert not [x for x in labels if label.lower() in x.lower()], (capture, label, labels)
    for label in want.get("not_labels", []):
        assert label not in labels, (capture, label, labels)
    for start in want.get("required", []):
        # (a question by its first words)
        want_req = (capture, start, [(f.label[:40], f.required) for f in d.fields])
        assert any(f.label.startswith(start) and f.required for f in d.fields), want_req
    for start in want.get("optional", []):
        want_opt = (capture, start, [(f.label[:40], f.required) for f in d.fields])
        assert any(f.label.startswith(start) and not f.required for f in d.fields), want_opt
    for start, section in (want.get("sections") or {}).items():
        want_sec = (capture, start, section, [(f.label[:40], f.section) for f in d.fields])
        held = [f for f in d.fields if f.label.startswith(start) and f.section == section]
        assert held, want_sec
    for section in want.get("no_sections", []):
        assert not [f.label for f in d.fields if f.section == section], (capture, section)
    assert len(d.fields) >= want.get("min_fields", 0), (capture, labels)
    if "max_fields" in want:
        assert len(d.fields) <= want["max_fields"], (capture, labels)
    texts = {b.text: b for b in d.buttons}
    for text in want.get("buttons", []):
        assert text in texts, (capture, text, sorted(texts))
    for text in want.get("no_buttons", []):
        assert text not in texts, (capture, text)
    for text in want.get("disabled", []):
        assert text in texts and texts[text].disabled, (capture, text)
