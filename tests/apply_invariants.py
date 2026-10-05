"""The flow harness's checks: `Sends` counts every send of one run,
`Recorder` records every action the run makes through Playwright, and
`invariant_breaks` / `assert_invariants` hold the run's safety rules
against them; `policy_park` names the parks the user's policy allows.

Part of the auto-apply flow harness; `apply_harness` re-exports it.
"""
from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# apply_pages first: it puts `local/` on the path the modules below import from
from apply_pages import FlowServer, _host, on_linkedin, Patches, unconfirmed_values, _value_text
import apply_run
import apply_limits
import apply_send_words
from apply_flows import Flow


# --- sends --------------------------------------------------------------------------------

BINDING = "__applyHarnessSend"
# once per document (a tab's window outlives its first blank document, so the
# mark is the document's own)
_SEND_JS = """(() => {
  if (document.__applyHarnessSendSeen) { return; }
  document.__applyHarnessSendSeen = true;
  const report = () => { try { window.%s(); } catch (e) {} };
  const seen = new MutationObserver((records) => {
    for (const r of records) {
      if (r.type === 'attributes' && r.attributeName === 'data-submitted'
          && r.target === document.body) { report(); }
    }
  });
  seen.observe(document, {attributes: true, attributeFilter: ['data-submitted'], subtree: true});
})();""" % BINDING


@dataclass
class Send:
    kind: str           # "dom" | "request" | "post"
    detail: str
    in_gate: bool
    accepted: bool = True       # the site took it (a refused post is still a send)


def _watch_document(page) -> None:
    try:
        page.evaluate(_SEND_JS)
    except Exception:       # noqa: BLE001  (a tab closed or moving on: its next page is watched)
        pass


# a request that failed before any connection was made never reached the site;
# a reset or a bare failure may have after it left
_NO_CONNECTION = re.compile(r"ERR_(?:CONNECTION_REFUSED|ADDRESS_UNREACHABLE|NAME_NOT_RESOLVED|"
                            r"NAME_RESOLUTION_FAILED|BLOCKED_BY_CLIENT)\b")


class Sends:
    """Every send of one run (see the module docstring). A send request
    that fails before any connection (`_NO_CONNECTION`) stays counted as an
    attempt and is marked not accepted."""

    def __init__(self, recorder: Recorder | None = None):
        self.recorder = recorder
        self.events: list[Send] = []
        self._requests: list[tuple[Any, Send]] = []

    def _in_gate(self) -> bool:
        return bool(self.recorder and self.recorder.gate_depth > 0)

    @property
    def count(self) -> int:
        return len(self.events)

    def install(self, context, flow: Flow, server: FlowServer | None = None) -> None:
        context.expose_binding(BINDING, self._dom)
        context.add_init_script(_SEND_JS)
        # a tab `window.open` made keeps its first blank window for the page
        # it loads, and the init script does not run again for that page: a
        # submit in such a tab went unseen (popup_step, found by the final
        # review's SUBMITTED-WITHOUT-SEND, C-M3). Each tab's loaded page gets
        # the watch too; the document's mark keeps it to one
        context.on("page", self._watch_tab)
        for glob in flow.send_urls:
            context.route(glob, self._request)
        if flow.send_urls:
            context.on("requestfailed", self._failed)
        if server is not None:
            server.on_post = lambda name: self.events.append(
                Send("post", f"/submit/{name}", self._in_gate(), name not in server.rejects))

    def uninstall(self, server: FlowServer | None = None) -> None:
        if server is not None:
            server.on_post = None

    def _watch_tab(self, page) -> None:
        page.on("domcontentloaded", _watch_document)

    def _dom(self, source, *args) -> None:
        try:
            url = str(source["frame"].url)
        except Exception:       # noqa: BLE001  (a frame gone by the time the call lands)
            url = ""
        self.events.append(Send("dom", url, self._in_gate()))

    def _request(self, route, request) -> None:
        send = Send("request", f"{request.method} {request.url}", self._in_gate())
        self.events.append(send)
        self._requests.append((request, send))
        route.fallback()

    def _failed(self, request) -> None:
        """A send request the network dropped: not accepted when no
        connection was made (a later route's abort)."""
        try:
            failure = str(request.failure or "")
        except Exception:       # noqa: BLE001  (a request gone with its page)
            return
        if not _NO_CONNECTION.search(failure):
            return
        for seen, send in self._requests:
            if seen is request:
                send.accepted = False


# --- the action recorder --------------------------------------------------------------------

_LIVE_JS = r"""el => {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  let text = '';
  if (tag === 'input') {
    if (['submit', 'button', 'reset', 'image'].includes(type)) text = el.value || '';
  } else if (tag !== 'textarea' && tag !== 'select') {
    text = el.innerText || el.textContent || '';
  }
  // an icon button's words are its accessible name (a submit's menu arrow
  // whose only text is white space around an svg)
  const shown = (text || '').replace(/\s+/g, ' ').trim();
  text = (shown || el.getAttribute('aria-label') || el.getAttribute('title') || '')
    .replace(/\s+/g, ' ').trim().slice(0, 160);
  // a box no person fills, by the page's own truth, never the extractor's
  // word lists: read-only (a combobox opens on a click), a box
  // the fixture declares one (`data-harness-junk`), off the page, two pixels
  // or less, or under aria-hidden; a text box or a textarea only
  let junk = el.getAttribute('data-harness-junk') || '';
  const typing = tag === 'textarea' || (tag === 'input'
    && !['checkbox', 'radio', 'file', 'submit', 'button', 'reset', 'image', 'hidden'].includes(type));
  if (typing && !junk) {
    const r = el.getBoundingClientRect();
    const x = window.scrollX || 0, y = window.scrollY || 0;
    if (el.readOnly && el.getAttribute('role') !== 'combobox') junk = 'read-only';
    else if (r.right + x < 0 || r.bottom + y < 0 || r.left + x < -500 || r.top + y < -500) junk = 'off the page';
    else if (r.width <= 2 && r.height <= 2) junk = 'two pixels or less';
    else if (el.closest('[aria-hidden=true]')) junk = 'aria-hidden';
  }
  // a tick or a toggle: it never sends
  const role = el.getAttribute('role') || '';
  const toggle = ['checkbox', 'switch', 'radio', 'option', 'menuitemcheckbox',
                  'menuitemradio'].includes(role) || ['checkbox', 'radio'].includes(type)
    || el.hasAttribute('aria-pressed');
  return {text: text, role: role, tag: tag, type: type, toggle: toggle,
          aria: (el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 160),
          form: !!(el.form || (el.closest && el.closest('form'))),
          url: String(el.ownerDocument.location.href), junk: junk,
          name: el.getAttribute('name') || el.id || ''};
}"""
# The focused element of a frame's document, for a key press or typed text
# without a target; `frame: true` when the focus sits in a child frame (the
# element is then read in that frame).
_FOCUS_JS = "() => { const el = document.activeElement; " \
            "if (!el || el === document.body) return {url: String(location.href)}; " \
            "if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') " \
            "return {url: String(location.href), frame: true}; " \
            "return (" + _LIVE_JS + ")(el); }"
LIVE_TIMEOUT_MS = 1_000
# The loop's own send vocabulary (`apply_send_words.SUBMIT_WORDS`, `FINAL_WORDS`).
SUBMIT_WORDS = apply_send_words.SUBMIT_WORDS
FINAL_WORDS = apply_send_words.FINAL_WORDS
_ENTER_KEYS = ("Enter", "NumpadEnter")
_TARGET_ACTIONS = {"click": "click", "dblclick": "click", "tap": "click", "fill": "fill",
                   "type": "fill", "press_sequentially": "fill", "press": "press",
                   "check": "tick", "uncheck": "tick", "set_checked": "tick",
                   "select_option": "pick", "set_input_files": "upload",
                   "dispatch_event": "event"}
_KEYBOARD_ACTIONS = {"press": "press", "down": "press", "type": "fill", "insert_text": "fill"}
_ON_LINKEDIN_FORBIDDEN = ("fill", "tick", "pick", "upload", "gate")
_CAPTCHA_TOUCH = ("click", "fill", "tick", "pick", "press", "event")   # never in a bot check


def submit_worded(text: str, *, park_mode: bool, account_step: bool = False,
                  toggle: bool = False) -> bool:
    """Does a control's live text read as sending the application, in the
    loop's words? A send word other than "apply" (a bare "Apply" is the
    posting's entry), unless the click is the account step's and the text
    names a sign-in or a code or link sent for one ("Sign in to apply", "Send
    code", "Send me a link": the account step's own exemption,
    `apply_send_words._sends_application`; everywhere else the loop routes those
    words to the submit gate); or in park mode a last-step word on anything
    but an account step (`apply_send_words._final_shaped`). A tick or a toggle
    (`toggle`: a checkbox, switch, radio or option, or an aria-pressed
    button) is left out of the last-step words only ("I confirm the
    information above is complete" is an answer the loop ticks); a toggle whose own name sends ("Submit application") still
    reads as sending."""
    words = {w.lower() for w in SUBMIT_WORDS.findall(text or "")}
    if words - {"apply"} and not (account_step and apply_send_words.SIGN_IN_WORDS.search(text or "")):
        return True
    return (park_mode and not toggle and bool(FINAL_WORDS.search(text or ""))
            and not apply_send_words._ACCOUNT_STEP_WORDS.search(text or ""))


_EASY_APPLY_WORDS = re.compile(r"easy\s*apply", re.I)


@dataclass
class Action:
    kind: str           # click | fill | press | tick | pick | upload | event | gate
    url: str
    text: str = ""
    role: str = ""
    tag: str = ""
    type: str = ""
    in_gate: bool = False
    how: str = ""       # the Playwright call
    key: str = ""       # a key press's key
    form: bool = False  # the element (or the focused one) sits in a form
    aria: str = ""      # the element's aria-label
    in_account: bool = False    # made by the account step (`_Accounts._fill`)
    junk: str = ""      # a box no person fills: read-only, or a honeypot
    toggle: bool = False    # a tick or a toggle (a checkbox, switch, radio or option role,
                            # aria-pressed): it never sends
    secret: bool = False    # the value typed is the master password (never the value)
    unconfirmed: bool = False   # the value filled or picked holds an answer the user has
                                # not confirmed (never the value)
    account: str = ""       # the account step's kind ("login" | "signup") and call, "login#2"
    name: str = ""          # the element's name, else its id
    user: str = ""          # the person's answer to a pause the value holds, else ""

    @property
    def host(self) -> str:
        return _host(self.url)


class Recorder:
    """Every Playwright action of one run with its live element (see the
    module docstring). `recording()` installs the patches and removes them."""

    def __init__(self, flow: Flow | None = None, *, park_mode: bool = False,
                 password: str = "", unconfirmed: tuple[str, ...] | None = None):
        self.flow = flow
        self.park_mode = park_mode
        self.password = password
        # the bank's answers the user has not confirmed (`unconfirmed_values`)
        self.unconfirmed = unconfirmed_values() if unconfirmed is None else tuple(unconfirmed)
        self.actions: list[Action] = []
        self.gate_depth = 0
        self.account_depth = 0
        self.final: dict[str, Any] = {}
        self.logs: list[str] = []
        self.files: list[Path] = []       # scanned for the password after the run
        self.judge_requests: list[str] = []   # every request the judge got, as JSON
        self.app_hosts: set[str] = set()  # where the master password may be typed
        self.ledger: Path | None = None   # the run's account ledger (no password key)
        self._keyboards: dict[int, Any] = {}
        self._account_calls = 0
        self._account_kind: list[str] = []    # the running account step's "login#n" / "signup#n"
        # each answer the person gave a pause -> (the field it
        # answers, the page the run paused on); and the values given for a
        # sensitive field, which code must never type
        self.user_values: dict[str, tuple[str, str]] = {}
        self.sensitive_values: set[str] = set()

    def _live(self, target) -> dict:
        try:
            if hasattr(target, "page") and not hasattr(target, "as_element"):
                return dict(target.evaluate(_LIVE_JS, timeout=LIVE_TIMEOUT_MS))
            return dict(target.evaluate(_LIVE_JS))
        except Exception:       # noqa: BLE001  (a detached or ambiguous element)
            for owner in ("page", "owner_frame"):
                try:
                    found = getattr(target, owner)
                    return {"url": str((found() if callable(found) else found).url)}
                except Exception:   # noqa: BLE001
                    continue
            return {}

    def _add(self, kind: str, how: str, info: dict, key: str = "", value: Any = None) -> None:
        # whether the value typed is the master password: a boolean, never
        # the value (the recorder keeps no typed value at all)
        secret = bool(self.password) and isinstance(value, str) and value == self.password
        # whether it holds an answer the user has not confirmed: the value
        # filled or picked, or the text of the option or box ticked
        shown = _value_text(value)
        if kind in ("click", "tick") and info.get("toggle"):
            shown = f"{shown} {info.get('text', '')}"
        unconfirmed = any(u in shown for u in self.unconfirmed)
        # the person's answer to a pause the value is, word for word
        user = next((u for u in list(self.user_values) + sorted(self.sensitive_values)
                     if u and u == shown.strip()), "") if kind in ("fill", "pick") else ""
        self.actions.append(Action(kind=kind, how=how, url=str(info.get("url", "")),
                                   text=str(info.get("text", "")), role=str(info.get("role", "")),
                                   tag=str(info.get("tag", "")), type=str(info.get("type", "")),
                                   in_gate=self.gate_depth > 0, key=key,
                                   form=bool(info.get("form", False)),
                                   aria=str(info.get("aria", "")),
                                   in_account=self.account_depth > 0,
                                   junk=str(info.get("junk", "")),
                                   toggle=bool(info.get("toggle", False)), secret=secret,
                                   unconfirmed=unconfirmed,
                                   account=self._account_kind[-1] if self._account_kind else "",
                                   name=str(info.get("name", "")), user=user))

    @staticmethod
    def focused(page) -> dict:
        """The focused element's live info, read in the frame that holds the
        focus: a key pressed while an input inside an iframe has the focus
        goes to that input and its form (N4), so the main document's
        `activeElement` (the `<iframe>`) is followed down, frame by frame."""
        try:
            info = dict(page.evaluate(_FOCUS_JS))
        except Exception:   # noqa: BLE001
            return {}
        frame = page.main_frame
        for _ in range(8):
            if not info.get("frame"):
                return info
            found = None
            for child in frame.child_frames:
                try:
                    if child.frame_element().evaluate("el => el === document.activeElement"):
                        found = child
                        break
                except Exception:   # noqa: BLE001  (a detached frame)
                    continue
            if found is None:
                return info
            frame = found
            try:
                info = dict(frame.evaluate(_FOCUS_JS))
            except Exception:   # noqa: BLE001
                return info
        return info

    @staticmethod
    def _kind(kind: str, name: str, args: tuple, kw: dict) -> str:
        if kind == "event":
            event = args[0] if args else kw.get("type", "")
            return "click" if str(event).lower() == "click" else "event"
        return kind

    @contextmanager
    def recording(self):
        from playwright.sync_api import ElementHandle, Frame, Keyboard, Locator, Page

        p = Patches()
        rec = self
        try:
            for cls, label in ((Locator, "Locator"), (ElementHandle, "ElementHandle")):
                for name, kind in _TARGET_ACTIONS.items():
                    orig = getattr(cls, name, None)
                    if orig is None:
                        continue

                    def _on(target, *a, _orig=orig, _kind=kind, _name=name, _label=label, **kw):
                        key = str(a[0] if a else kw.get("key", "")) if _kind == "press" else ""
                        value = (a[0] if a else kw.get("value", kw.get("text", kw.get("label")))) \
                            if _kind in ("fill", "pick") else None
                        rec._add(rec._kind(_kind, _name, a, kw), f"{_label}.{_name}",
                                 rec._live(target), key, value)
                        return _orig(target, *a, **kw)
                    p.setattr(cls, name, _on)
            for cls, label in ((Frame, "Frame"), (Page, "Page")):
                for name, kind in _TARGET_ACTIONS.items():
                    orig = getattr(cls, name, None)
                    if orig is None or name == "press_sequentially":
                        continue

                    def _sel(owner, selector, *a, _orig=orig, _kind=kind, _name=name,
                             _label=label, **kw):
                        try:
                            info = rec._live(owner.locator(selector).first)
                        except Exception:       # noqa: BLE001
                            info = {}
                        info.setdefault("url", str(getattr(owner, "url", "")))
                        key = str(a[0] if a else kw.get("key", "")) if _kind == "press" else ""
                        value = (a[0] if a else kw.get("value", kw.get("text", kw.get("label")))) \
                            if _kind in ("fill", "pick") else None
                        rec._add(rec._kind(_kind, _name, a, kw), f"{_label}.{_name}", info, key,
                                 value)
                        return _orig(owner, selector, *a, **kw)
                    p.setattr(cls, name, _sel)

            keyboard_prop = Page.keyboard

            def _keyboard(page):
                kb = keyboard_prop.fget(page)
                rec._keyboards[id(kb)] = page
                return kb
            p.setattr(Page, "keyboard", property(_keyboard))
            for name, kind in _KEYBOARD_ACTIONS.items():
                orig = getattr(Keyboard, name, None)
                if orig is None:
                    continue

                def _key(kb, *a, _orig=orig, _kind=kind, _name=name, **kw):
                    page = rec._keyboards.get(id(kb))
                    info = rec.focused(page) if page is not None else {}
                    key = str(a[0] if a else kw.get("key", "")) if _kind == "press" else ""
                    value = (a[0] if a else kw.get("text")) if _kind == "fill" else None
                    rec._add(_kind, f"Keyboard.{_name}", info, key, value)
                    return _orig(kb, *a, **kw)
                p.setattr(Keyboard, name, _key)

            gate = apply_run._JobRun._submit_gate

            def _gate(job, *a, **kw):
                url = ""
                try:
                    url = str(job.page.url)
                except Exception:   # noqa: BLE001
                    pass
                rec.actions.append(Action(kind="gate", url=url, in_gate=True,
                                          how="_JobRun._submit_gate"))
                rec.gate_depth += 1
                try:
                    return gate(job, *a, **kw)
                finally:
                    rec.gate_depth -= 1
            p.setattr(apply_run._JobRun, "_submit_gate", _gate)

            account_fill = apply_run._Accounts._fill

            def _account(accounts, *a, **kw):
                # `_fill(page, digest, host, email, signup)`: its kind and call
                signup = a[4] if len(a) > 4 else kw.get("signup", False)
                rec._account_calls += 1
                rec._account_kind.append(f"{'signup' if signup else 'login'}#{rec._account_calls}")
                rec.account_depth += 1
                try:
                    return account_fill(accounts, *a, **kw)
                finally:
                    rec.account_depth -= 1
                    rec._account_kind.pop()
            p.setattr(apply_run._Accounts, "_fill", _account)

            finish = apply_run._JobRun._finish

            def _finish(job, *a, **kw):
                rec.final = rec._final_state(job.page)
                return finish(job, *a, **kw)
            p.setattr(apply_run._JobRun, "_finish", _finish)

            handler = _ListHandler(self.logs)
            loggers = [logging.getLogger(n) for n in (
                "apply_run", "apply_fill", "apply_form", "apply_judge", "apply_inbox",
                "ats_accounts", "jev", "apply_trace")]
            levels = [lg.level for lg in loggers]
            for lg in loggers:
                lg.addHandler(handler)
                lg.setLevel(logging.INFO)       # the level a real run logs at
            try:
                yield self
            finally:
                for lg, level in zip(loggers, levels):
                    lg.removeHandler(handler)
                    lg.setLevel(level)
        finally:
            p.undo()

    def watch(self, judge: Any) -> Any:
        """`judge`, with every request it gets kept (for the password check)."""
        return _WatchedJudge(judge, self)

    def _final_state(self, page) -> dict[str, Any]:
        out: dict[str, Any] = {"url": "", "confirmed": False, "at_gate": False}
        if page is None:
            return out
        try:
            out["url"] = str(page.url)
            frames = list(page.frames)
        except Exception:       # noqa: BLE001  (the page is gone)
            return out

        def shown(selector: str) -> bool:
            for frame in frames:
                try:
                    if frame.locator(selector).count() > 0:
                        return True
                except Exception:   # noqa: BLE001
                    continue
            return False
        if self.flow is not None:
            out["confirmed"] = bool(self.flow.confirm) and shown(self.flow.confirm)
            out["at_gate"] = bool(self.flow.gate) and shown(self.flow.gate)
        return out


class _WatchedJudge:
    def __init__(self, inner: Any, recorder: Recorder):
        self.inner = inner
        self.recorder = recorder

    def judge(self, state: Any, questions: dict) -> dict:
        try:
            self.recorder.judge_requests.append(
                json.dumps({"state": state, "questions": questions}, default=str))
        except Exception:       # noqa: BLE001
            pass
        return self.inner.judge(state, questions)


class _ListHandler(logging.Handler):
    def __init__(self, sink: list[str]):
        super().__init__(logging.DEBUG)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.append(record.getMessage())
        except Exception:       # noqa: BLE001
            pass


# --- the invariants -------------------------------------------------------------------------

_TEXT_SUFFIXES = (".md", ".json", ".jsonl", ".log", ".txt", ".html", ".csv", ".yaml", ".yml")


def _text_files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        path = Path(path)
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and child.suffix.lower() in _TEXT_SUFFIXES:
                    yield child
        elif path.is_file():
            yield path


_NOTHING_SENT = "nothing was sent"
# the runner's check-whether end: its reason's prefix, or the same words as
# a clause of their own after "; " (the parks `_send_evidence` gives the
# check-sent note: an error banner or page, a code not accepted); a page's words quoted in a reason are never a clause
_CHECK_SENT_CLAUSE = re.compile(r"(?:^|; )" + re.escape(apply_run.CHECK_SENT_REASON)
                                + r"(?=$|[:;])")


def invariant_breaks(outcome: Any, recorder: Recorder, sends: Sends, *,
                     tab_note: str = "") -> list[str]:
    """Every invariant the run broke, one line each; [] when it held them all.
    `tab_note`: the finished queue entry's note, when the caller read it."""
    breaks: list[str] = []
    status = str(getattr(outcome, "status", ""))
    reason = str(getattr(outcome, "reason", ""))
    # a send the site accepted: a post it refused, or a
    # request that never made its connection, sent nothing
    accepted = any(s.accepted for s in sends.events)
    if sends.count > 1:
        breaks.append(f"SEND-COUNT: {sends.count} sends, at most one is allowed")
    if recorder.park_mode and sends.count:
        breaks.append(f"PARK-SENT: park mode sent {sends.count} application(s)")
    for s in sends.events:
        if not s.in_gate:
            breaks.append(f"SEND-OUTSIDE-GATE: a {s.kind} send ({s.detail}) while the "
                          "submit gate was not running")
    # the runner's own prefix, never the word anywhere in
    # a reason that may quote the page
    if status == "submitted" and not recorder.final.get("confirmed"):
        if not (reason.startswith("submitted (unconfirmed)") and accepted):
            breaks.append(f"FALSE-SUBMITTED: submitted without the confirmation marker "
                          f"({reason})")
    if status == "submitted" and not accepted:
        breaks.append(f"SUBMITTED-WITHOUT-SEND: submitted ({reason[:80]!r}) and no send the "
                      "site accepted was seen: the job would never be tried again")
    # after a send the site accepted, a park the person
    # may re-queue sends a second application; only a check-whether park
    # (which is never re-queued as is) may follow one: its reason says so,
    # or its note is the check-sent note
    if accepted and status in ("needs_human", "failed") \
            and not _CHECK_SENT_CLAUSE.search(reason) and tab_note != apply_run.CHECK_SENT_NOTE:
        breaks.append(f"REQUEUABLE-AFTER-SEND: {status} ({reason[:80]!r}) after a send the site "
                      "accepted: a re-queue would send it twice")
    if status == "ready_to_submit" and sends.count:
        breaks.append(f"READY-AFTER-SEND: ready_to_submit ({reason}) after {sends.count} "
                      "send(s): a review would send it again")
    # the run says nothing went: the submit that did not go through, a send
    # that never made its connection, a post the navigation guard stopped
    nothing_sent = reason.startswith(apply_run.NOT_SENT_REASON) or _NOTHING_SENT in reason
    if nothing_sent and any(s.accepted for s in sends.events):
        breaks.append(f"NOT-SENT-AFTER-SEND: {reason[:80]!r} after a send the site accepted: "
                      "a retry would send it twice")
    for a in recorder.actions:
        if a.kind == "click" and not a.in_gate \
                and submit_worded(a.text, park_mode=recorder.park_mode,
                                  account_step=a.in_account, toggle=a.toggle):
            breaks.append(f"CLICK-OUTSIDE-GATE: clicked {a.text!r} ({a.role or a.tag}) on "
                          f"{a.host} outside the submit gate")
        enter = a.kind == "press" and a.key in _ENTER_KEYS
        if enter and not a.in_gate and (a.form or submit_worded(a.text,
                                                                 park_mode=recorder.park_mode)):
            breaks.append(f"ENTER-OUTSIDE-GATE: Enter on {a.tag or 'the page'} "
                          f"({'in a form' if a.form else repr(a.text)}) on {a.host} outside "
                          "the submit gate")
        if on_linkedin(a.url) and (a.kind in _ON_LINKEDIN_FORBIDDEN or enter):
            breaks.append(f"LINKEDIN-{'PRESS' if enter else a.kind.upper()}: a "
                          f"{a.kind} on {a.host}")
        if a.kind == "click" and _EASY_APPLY_WORDS.search(f"{a.text} {a.aria}"):
            breaks.append(f"EASY-APPLY-CLICK: clicked {a.text or a.aria!r} on {a.host}")
        if a.kind in _CAPTCHA_TOUCH and apply_run._is_captcha_url(a.url):
            breaks.append(f"CAPTCHA-TOUCH: a {a.kind} inside the bot check on {a.host}")
        if a.kind == "fill" and a.type == "password" and recorder.app_hosts \
                and a.host not in recorder.app_hosts:
            breaks.append(f"PASSWORD-OFF-SITE: a password box filled on {a.host}")
        if a.kind in ("fill", "tick", "pick", "upload") and a.junk:
            breaks.append(f"JUNK-FILL: a {a.kind} into a box no person fills ({a.junk}) on "
                          f"{a.host}")
        if a.kind == "click" and _OTHER_SITE_SIGN_IN.search(f"{a.text} {a.aria}"):
            breaks.append(f"OTHER-SITE-CLICK: clicked {a.text or a.aria!r} on {a.host}: the run "
                          "never signs in or applies with another site's account")
        if a.secret and recorder.app_hosts and a.host not in recorder.app_hosts:
            breaks.append(f"PASSWORD-OFF-SITE: the master password typed on {a.host}")
        if a.secret and (a.type != "password" or a.how.startswith("Keyboard.")):
            breaks.append(f"PASSWORD-NOT-A-PASSWORD-BOX: the master password typed into a "
                          f"{a.tag or 'focused'} {a.type or 'element'} on {a.host}")
        if a.unconfirmed:
            breaks.append(f"UNCONFIRMED-ANSWER: a {a.kind} of an answer the user has not "
                          f"confirmed, into a {a.tag or 'focused'} {a.type or 'element'} on "
                          f"{a.host}")
        breaks += _pause_breaks(a, recorder)
    breaks += _password_tries(recorder.actions)
    if recorder.password:
        for path in _text_files(recorder.files):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if recorder.password in text:
                breaks.append(f"PASSWORD-LEAK: the master password is in {path.name}")
        if any(recorder.password in line for line in recorder.logs):
            breaks.append("PASSWORD-LEAK: the master password is in the log")
        if any(recorder.password in blob for blob in recorder.judge_requests):
            breaks.append("PASSWORD-TO-JUDGE: the master password reached the judge")
    if recorder.ledger is not None and recorder.ledger.is_file():
        try:
            ledger = json.loads(recorder.ledger.read_text(encoding="utf-8"))
        except ValueError:
            ledger = {}
        keys = [str(k) for rec in (ledger.values() if isinstance(ledger, dict) else [])
                if isinstance(rec, dict) for k in rec]
        bad = sorted({k for k in keys if _PASSWORD_KEY.search(k)})
        if bad:
            breaks.append(f"LEDGER-PASSWORD-KEY: the account ledger holds {bad[0]!r}")
    return breaks


# a sign-in or an apply with another site's account (`apply_judge.THIRD_PARTY`)
_OTHER_SITE_SIGN_IN = apply_run.apply_judge.THIRD_PARTY
_PASSWORD_KEY = re.compile(r"pass|pwd|secret|token|credential", re.I)
# The master password goes into one sign-in per site (a second is a
# rejected password: typing it again moves toward a lockout), a sign-up's
# boxes twice at most (the one re-type of a form the site emptied),
# and no more than this many boxes on one site in all
PASSWORD_TYPINGS_MAX = 6


def _pause_breaks(a: Action, recorder: Recorder) -> list[str]:
    """A person's answer to a pause goes only into its own
    field, on the page the run paused on; code never types into a sensitive
    field, and a value the answer gave for one is never typed anywhere."""
    out = []
    if a.user in recorder.sensitive_values:
        out.append(f"SENSITIVE-ANSWER-TYPED: the value given for a sensitive field went into "
                   f"a {a.tag or 'focused'} {a.type or 'element'} on {a.host}")
    elif a.user:
        field_id, page_url = recorder.user_values[a.user]
        if a.name != field_id or a.url != page_url:
            out.append(f"USER-ANSWER-ELSEWHERE: an answer for {field_id!r} went into "
                       f"{a.name or a.tag or 'an element'!r} on {a.url}; it belongs on "
                       f"{page_url}")
    if a.kind == "fill" and a.name and apply_run.apply_judge.is_sensitive_field(
            re.sub(r"[_\-\[\]]+", " ", a.name)):
        out.append(f"SENSITIVE-TYPED: code typed into the sensitive field {a.name!r} on "
                   f"{a.host}")
    return out


def _password_tries(actions: list[Action]) -> list[str]:
    """The password invariants over the typings: one sign-in per site, two
    sign-ups per site, `PASSWORD_TYPINGS_MAX` boxes per site."""
    out: list[str] = []
    calls: dict[tuple[str, str], set[str]] = {}
    boxes: dict[str, int] = {}
    for a in actions:
        if not a.secret:
            continue
        boxes[a.host] = boxes.get(a.host, 0) + 1
        if a.account:
            kind = a.account.split("#", 1)[0]
            calls.setdefault((a.host, kind), set()).add(a.account)
    for (host, kind), seen in sorted(calls.items()):
        allowed = 1 if kind == "login" else 2
        if len(seen) > allowed:
            out.append(f"PASSWORD-RETRY: the master password went into {len(seen)} {kind} "
                       f"screens on {host} (at most {allowed})")
    for host, n in sorted(boxes.items()):
        if n > PASSWORD_TYPINGS_MAX:
            out.append(f"PASSWORD-RETRY: the master password was typed {n} times on {host} (at "
                       f"most {PASSWORD_TYPINGS_MAX})")
    return out


def assert_invariants(outcome: Any, recorder: Recorder, sends: Sends, *,
                      tab_note: str = "") -> None:
    breaks = invariant_breaks(outcome, recorder, sends, tab_note=tab_note)
    assert not breaks, "invariant breaks:\n" + "\n".join(breaks)


# The parks the user's policy allows: the park-mode submit, a required
# question the data cannot answer, a sensitive question, a payment, a check
# nobody solved, a dead page, and the window or tab the user closed; on
# LinkedIn, an Easy Apply job (the user applies there, by the user's call), a
# job already applied to, a closed posting, and LinkedIn signed out (dead ends
# the run cannot pass).
_POLICY_PARKS = tuple(re.compile(p) for p in (
    r"^auto_apply_submit is off(; |$)", r"^required field without an answer",
    # an optional answer that failed its check and that the
    # run could not take out again (a radio group): the person removes it
    r"^a wrong answer could not be removed: ",
    r"^asks for .*which auto-apply never fills", r"^payment requested",
    # a bot check nobody solved: the loop's park and the run's own CAPTCHA
    # reasons, anchored (an unanchored word matched a reads list's
    # "captcha_or_bot_check 0.17" inside another park's evidence)
    r"^captcha or bot check on the page", r"^a CAPTCHA (?:challenge|check) ",
    # its one producer's shape (`_JobRun._click_advance`), anchored so a park
    # whose evidence quotes the sentence never reads as it
    r"^the \S+ button \(.*\) did nothing \(.*clicked twice\); a CAPTCHA checkbox",
    r"^error or dead page",
    "^" + re.escape(apply_run.CLOSED_REASON), "^" + re.escape(apply_run.TAB_CLOSED_REASON),
    "^" + re.escape(apply_run.EASY_APPLY_REASON) + "$",
    "^" + re.escape(apply_run.apply_linkedin.APPLIED_REASON),
    "^" + re.escape(apply_run.apply_linkedin.CLOSED_REASON),
    "^" + re.escape(apply_run.apply_linkedin.SIGNED_OUT_REASON),
    # the site's own dead ends: a job it says was applied to before, a
    # posting it says is closed
    "^" + re.escape(apply_run.ALREADY_APPLIED_REASON),
    "^" + re.escape(apply_run.CLOSED_POSTING_REASON),
    # a posting the run cannot apply to itself: a job board's with no link to
    # the company's site, an Apply that is an email address
    "^" + re.escape(apply_run.AGGREGATOR_REASON) + " on ",
    "^" + re.escape(apply_run.MAILTO_REASON) + " to ",
    # a real dead end: a way on still disabled once every
    # field is answered, and no field the form or the plan names as blank
    r"^the .{1,80} button stays disabled after the fill( \(|$)",
    # the account dead ends: a portal whose only way on is a sign-in with another
    # site's account, which the run never uses; a site whose
    # password rules the stored master password cannot meet
    "^" + re.escape(apply_run.SSO_REASON) + " ",
    "^" + re.escape(apply_run.PASSWORD_RULE_REASON) + " on ",
    # a queue entry the run cannot work, which the user fixes
    "^" + re.escape(apply_run.MALFORMED_REASON) + ": ",
    # a judge that stays down after the submit click or the code step: the
    # job is never re-queued once something may have been
    # sent, and the run cannot read on, a dead end the user checks. The
    # window or the tab the user closed there, and the link and the
    # final-worded steps, end the same way (`_stopped_after_send`); a pause's
    # wait that failed with the page still open ends as a close too
    # (`_pause_closed`)
    "^" + re.escape(apply_run.CHECK_SENT_REASON) + r": the run stopped after the "
    r"(?:submit click|code step|link step|final-worded step|pause) \((?:"
    + re.escape(apply_run.JUDGE_DOWN_REASON) + ": |" + re.escape(apply_run.CLOSED_REASON)
    + r"\)|" + re.escape(apply_run.TAB_CLOSED_REASON) + r"\)|"
    + re.escape(apply_run.PAUSE_UNANSWERED_REASON) + r"\))",
    # the page moved on while the run waited for the person,
    # who may have sent it in the browser; the user checks it
    "^" + re.escape(apply_run.CHECK_SENT_REASON) + r": the page moved on during the pause \(",
    # the judge down under the same job a second time, a
    # failure the job's own request may cause: parked so the queue moves on.
    # Only after the judge answered in the drain: a park while it
    # answered nothing (an outage for every job) is outside the policy. Only
    # for an error a request can cause: a 5xx other than 503 and
    # 529, a 408, or an error with no status (a timeout), never a dropped
    # connection, most often the network's
    "^" + re.escape(apply_run.JUDGE_DOWN_REASON) + r": (?!Connection|BrokenPipe)\S+"
    r"(?: (?:408|5(?!03|29)\d\d))? (?:at .+ )?after [1-9]\d* answers? in this drain; "
    + re.escape(apply_limits.OUTAGES_PARKED) + "$"))


def policy_park(status: str, reason: str) -> bool | None:
    """True for a park inside the user's policy, False for any other stop
    (a failure included), None for a submit."""
    if status == "submitted":
        return None
    return any(p.search(reason or "") for p in _POLICY_PARKS)
