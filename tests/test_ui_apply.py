"""The dashboard 'Apply' worker (SP4 T4.3) — pure logic, no Tk widgets.

_apply_worker runs on a background thread: it resolves the tailored folder,
builds the apply context, opens the posting URL, then marshals a result back via
root.after. It must NEVER submit and must degrade gracefully (messagebox) when
the job hasn't been tailored. We bind the unbound method to a SimpleNamespace
fake that records root.after callbacks instead of building real widgets.
"""
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import ui  # noqa: E402
from resume_tailor import apply as apply_mod  # noqa: E402


def _fake_app():
    """A stand-in App that runs root.after callbacks inline and records them."""
    calls: list = []
    fake = types.SimpleNamespace()
    fake._applying = True
    fake._set_status = lambda msg: calls.append(("status", msg))
    fake._log_error = lambda ctx, exc: calls.append(("log", ctx, str(exc)))
    fake.root = types.SimpleNamespace(after=lambda _ms, fn: fn())
    # Real bound finishers so we exercise their logic too.
    fake._finish_apply_ok = ui.App._finish_apply_ok.__get__(fake)
    fake._finish_apply_error = ui.App._finish_apply_error.__get__(fake)
    fake._calls = calls
    return fake


def _ctx(url="http://job/1", pdf="/abs/Cand_Resume.pdf"):
    return {
        "job": {"company": "Acme", "title": "Engineer", "url": url},
        "apply_url": url,
        "resume_pdf": pdf,
        "cover_letter_pdf": "",
        "generated_dir": "/abs/folder",
    }


def test_apply_worker_opens_url_and_surfaces_context(monkeypatch):
    fake = _fake_app()
    opened: list = []
    shown: list = []
    monkeypatch.setattr(apply_mod, "resolve_generated_dir",
                        lambda job_id, job=None: Path("/abs/folder"))
    monkeypatch.setattr(apply_mod, "build_apply_context", lambda folder: _ctx())
    monkeypatch.setattr(ui, "open_in_chrome", lambda url: opened.append(url))
    monkeypatch.setattr(ui.messagebox, "showinfo", lambda *a, **k: shown.append((a, k)))
    # clipboard ops on the bare namespace would AttributeError -> handled below
    fake.root.clipboard_clear = lambda: None
    fake.root.clipboard_append = lambda v: shown.append(("clip", v))

    ui.App._apply_worker(fake, "1")

    assert opened == ["http://job/1"], "the posting URL must be opened for review"
    assert fake._applying is False
    # context surfaced via a messagebox mentioning review-before-submit
    assert shown, "expected a messagebox with the apply context"
    body = shown[-1][0][1]  # showinfo(title, body, ...)
    assert "Submission is left to you" in body
    assert "/abs/Cand_Resume.pdf" in body


def test_apply_worker_missing_folder_tells_user_to_tailor(monkeypatch):
    fake = _fake_app()
    shown: list = []

    def _raise(job_id, job=None):
        raise FileNotFoundError("No tailored résumé found. Tailor this job first.")

    monkeypatch.setattr(apply_mod, "resolve_generated_dir", _raise)
    monkeypatch.setattr(ui.messagebox, "showinfo", lambda *a, **k: shown.append(a))

    ui.App._apply_worker(fake, "nope")

    assert fake._applying is False
    assert shown, "expected a guidance messagebox"
    title, body = shown[-1][0], shown[-1][1]
    assert "Tailor" in title
    assert "Tailor" in body
