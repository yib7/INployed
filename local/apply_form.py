"""The form digest: what one page of an application looks like to the loop.

`FormDigest` is the extractor's output and the judge's input. It carries the
page's host and title, its visible text (capped by the extractor), every visible
form control as a `Field` and every clickable control as a `Button`. The judge
(`apply_judge`) turns a digest into Jev questions; the filler (`apply_fill`)
acts on the plan by the `locator` each field and button carries.

A `locator` is `(frame_index, css)`: the index of the frame the control lives
in (0 is the main frame) and a CSS selector that is stable for the page's
lifetime. `to_dict()` / `from_dict()` round-trip through JSON so tests can build
digests without a browser and the record writer can store one.

`extract(page) -> FormDigest` is the Playwright extractor: one JS pass per
frame (`apply_form_js._EXTRACT_JS`) returns plain dicts and Python builds the
dataclasses. Every page script this module runs lives in `apply_form_js`.
`resolve(page, locator)` turns a stored locator back into a Playwright
`Locator`. Both take a live `Page`; nothing here imports Playwright, so the
module stays importable without it.
"""
from __future__ import annotations

import logging
import re
import weakref
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from apply_form_js import (_CAPTCHA_WIDGETS_JS, _CONSENT_CONTROL_JS, _EXTRACT_JS, _FIND_BY_TEXT_JS,
                           _FORM_INDEX_JS, _SAME_SCOPE_JS, _SCAN_JS, _TEXT_JS, _VALIDITY_JS,
                           _VALUES_JS, LIVE_TEXT_JS)
# The page scripts other modules read through apply_form.
from apply_form_js import (CONSENT_ROOTS_JS, IDENT_FN_JS, LOCATOR_FN_JS,  # noqa: F401
                           PLACEHOLDER_TEXT_JS, RADIO_OPTION_LABEL_JS, SAME_IDENT_JS)

log = logging.getLogger(__name__)

FIELD_TYPES = ("text", "email", "tel", "url", "number", "textarea", "select",
               "radio", "checkbox", "file", "date", "listbox", "other")


@dataclass
class Field:
    """One visible form control. `options` is filled for select, radio,
    checkbox and listbox controls; `id_or_name` is the DOM id, else name."""
    n: int
    locator: tuple[int, str]
    label: str
    type: str
    required: bool
    placeholder: str = ""
    help: str = ""
    options: list[str] = field(default_factory=list)
    id_or_name: str = ""
    autocomplete: str = ""      # the control's autocomplete token, when it has one
    # How the control works when it is no plain native box: "choice" (a
    # custom radio group or Yes / No buttons, clicked through
    # `option_locators`), "checkbox_group" (one question's boxes, ticked
    # through `option_locators`), "popup" (a dropdown drawn as a button),
    # "typeahead" (a text box that offers matches as it is typed in),
    # "hidden_select" (a hidden <select> behind a styled trigger),
    # "aria_check" (a custom tick box), "editable" (a rich-text box),
    # "date:MDY" (date parts in that order, `option_locators`); "" else.
    widget: str = ""
    # the visible thing to click for a hidden native box (its label
    # or proxy), in the control's frame; None when the control takes the act
    click_locator: tuple[int, str] | None = None
    option_locators: list[str] = field(default_factory=list)   # per option, in its frame
    section: str = ""           # the heading the control sits under
    # the label was read in part: cut at its cap, or a button's or a
    # dropdown's words inside it left out; a consent so read is never routine
    label_partial: bool = False
    # set by the run when the control's own words send and its open was
    # refused (`apply_send_words.PopupRefused`): the plan leaves it
    # unanswered, and a required one parks on its question
    refused: str = ""
    ident: str = ""             # who the control is (tag|type|id|name|aria|...): read again
                                # before every act
    secret: bool = False        # an `<input type=password>`: its value is masked


PASSWORD_WORDS = ("pass", "pwd", "secret")
PASSWORD_AUTOCOMPLETE = ("current-password", "new-password")


def is_password_field(type_: str, id_or_name: str = "", label: str = "",
                      autocomplete: str = "", *, secret: bool | None = None) -> bool:
    """A password box. For a control the extractor read (`secret` given:
    `Field.secret`, `password_box`), only a real `<input type=password>`:
    sites put `autocomplete="new-password"` on an address or a location box
    to stop the browser's autofill, and that box takes its fact; a masked box
    labelled "PIN" is one whatever its words. For a
    recorded row, which keeps no DOM type (`secret` None): an `other` control
    whose id, name or label carries `pass`, `pwd` or `secret`, or any control
    whose autocomplete token is `current-password` / `new-password`; the
    record hides such a row's value, and hiding more is never a leak.

    One definition for the planner (which never puts a fact in such a field),
    the accounts hook (the only writer) and the record (which hides it), so the
    three cannot drift apart. `Passport number` is a text control and stays an
    ordinary field."""
    if secret is not None:
        return bool(secret)
    if str(autocomplete or "").lower() in PASSWORD_AUTOCOMPLETE:
        return True
    if str(type_ or "") != "other":
        return False
    blob = f"{id_or_name or ''} {label or ''}".lower()
    return any(w in blob for w in PASSWORD_WORDS)


def typed_password(ident: str) -> bool:
    """Does the control's `ident` (`IDENT_FN_JS`: tag|type|...) name an
    `<input type=password>`?"""
    return str(ident or "").split("|")[:2] == ["input", "password"]


def password_box(f: Any) -> bool:
    """Is the extracted control `f` a password box (`is_password_field` on
    its `secret`)? A control built without `secret` (an older capture, a
    hand-built `Field`) is one when its ident names an `<input
    type=password>`."""
    secret = bool(getattr(f, "secret", False)) or typed_password(getattr(f, "ident", ""))
    return is_password_field(f.type, f.id_or_name, f.label, f.autocomplete, secret=secret)


@dataclass
class Button:
    """One clickable control. `kind_hint` comes from the DOM (`submit`,
    `button`, `link`, ...) and is only a hint; the judge decides the role.
    `in_form`: the button's form holds a control a person fills (an input
    other than hidden or a button, a select, a textarea, an editable box, a
    custom control): an Apply there is the form's own button, never a
    posting's entry. `chrome`: it sits in the site's header, nav or
    search landmark, a Workday header, or a bar fixed to the top of the page
    (a header's "Sign In" is no sign-in page)."""
    n: int
    locator: tuple[int, str]
    text: str
    kind_hint: str = ""
    in_form: bool = False
    chrome: bool = False        # in the site's header, nav or top bar: kept for
                                # the mapping, left out of the page read
    disabled: bool = False      # disabled or aria-disabled now (a Submit that
                                # waits for the form to validate is kept, flagged)
    primary: bool = False       # styled as the page's main action (a primary or CTA class,
                                # or its form's one submit control)


@dataclass
class FormDigest:
    url_host: str
    title: str
    text: str
    fields: list[Field] = field(default_factory=list)
    buttons: list[Button] = field(default_factory=list)
    dialog: str = ""            # an open modal's title: its controls are the page's

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> FormDigest:
        fields = [Field(n=int(f["n"]), locator=_locator(f.get("locator")),
                        label=str(f.get("label", "")), type=str(f.get("type", "other")),
                        required=bool(f.get("required", False)),
                        placeholder=str(f.get("placeholder", "") or ""),
                        help=str(f.get("help", "") or ""),
                        options=[str(o) for o in (f.get("options") or [])],
                        id_or_name=str(f.get("id_or_name", "") or ""),
                        autocomplete=str(f.get("autocomplete", "") or ""),
                        widget=str(f.get("widget", "") or ""),
                        click_locator=_locator(f["click_locator"]) if f.get("click_locator")
                        else None,
                        option_locators=[str(o) for o in (f.get("option_locators") or [])],
                        section=str(f.get("section", "") or ""),
                        ident=str(f.get("ident", "") or ""),
                        label_partial=bool(f.get("label_partial", False)),
                        secret=bool(f["secret"]) if "secret" in f
                        else typed_password(str(f.get("ident", "") or "")))
                  for f in (raw.get("fields") or [])]
        buttons = [Button(n=int(b["n"]), locator=_locator(b.get("locator")),
                          text=str(b.get("text", "")),
                          kind_hint=str(b.get("kind_hint", "") or ""),
                          in_form=bool(b.get("in_form", False)),
                          chrome=bool(b.get("chrome", False)),
                          disabled=bool(b.get("disabled", False)),
                          primary=bool(b.get("primary", False)))
                   for b in (raw.get("buttons") or [])]
        return cls(url_host=str(raw.get("url_host", "")), title=str(raw.get("title", "")),
                   text=str(raw.get("text", "")), fields=fields, buttons=buttons,
                   dialog=str(raw.get("dialog", "") or ""))


def _locator(raw: Any) -> tuple[int, str]:
    if not raw:
        return (0, "")
    frame, css = raw
    return (int(frame), str(css))


# --- the extractor ---------------------------------------------------------------


def same_ident(a: str, b: str) -> bool:
    """`SAME_IDENT_JS` in Python: every attribute of the two idents agrees and
    one label's words start the other's."""
    pa, pb = str(a).split("|"), str(b).split("|")
    la, lb = pa.pop(), pb.pop()
    return pa == pb and (la == lb or (bool(la) and bool(lb)
                                      and (la.startswith(lb) or lb.startswith(la))))


# The frame URLs of the last `extract` per page: a locator's frame
# index can shift when an ad or tracker frame detaches between the read and
# the act; `resolve` finds the frame by the URL it had at the read first.
_FRAME_URLS: "weakref.WeakKeyDictionary[Any, list[str]]" = weakref.WeakKeyDictionary()
CONTENT_FRAME_MIN = (600, 300)    # px: a child frame this big is the content
CONTENT_FRAME_ANY = (300, 150)    # px: or this big with fields or an Apply-worded control
_APPLY_WORD = re.compile(r"\bapply\b", re.I)


def frames(page) -> list:
    """The page's frames with the main frame first: index 0 is `page.main_frame`,
    1.. are the child frames in `page.frames` order. The digest's locators and
    `resolve` share this numbering."""
    main = page.main_frame
    return [main] + [f for f in page.frames if f is not main]


def extract(page, *, content_site: Callable[[str], bool] | None = None) -> FormDigest:
    """Read one page of an application into a `FormDigest`: every frame in one
    JS pass each, fields and buttons numbered across frames in document order,
    the visible text of every frame joined (a content frame's first,
    `_content_frame`) and capped at `apply_judge.PAGE_TEXT_CAP`. A frame whose
    evaluate fails (detached, cross-origin) is skipped and keeps its index.

    `content_site(frame_url)`: may that child frame be read first (an
    embedded video or an ad stays in place)? The runner passes the page's
    site and the ATS platforms (`apply_sites.content_frame_site`); the default
    takes only a frame of the page's own host, or a blank or srcdoc one.
    At most `apply_judge.FIELDS_MAX` + 1 fields are kept, so a caller can
    tell a page holds more than it reads."""
    from apply_judge import FIELDS_MAX, PAGE_TEXT_CAP   # lazy: apply_judge imports this module

    fields: list[Field] = []
    buttons: list[Button] = []
    texts: list[tuple[int, str]] = []     # (order, text): a content frame's first
    dialog = ""
    all_frames = frames(page)
    if content_site is None:
        page_host = (urlparse(str(page.url)).hostname or "").lower()

        def content_site(url: str) -> bool:
            host = (urlparse(str(url or "")).hostname or "").lower()
            return not host or host == page_host
    try:
        _FRAME_URLS[page] = [str(getattr(f, "url", "") or "") for f in all_frames]
    except TypeError:           # a page double that takes no weak reference
        pass
    for idx, frame in enumerate(all_frames):
        try:
            raw = frame.evaluate(_EXTRACT_JS, PAGE_TEXT_CAP)
        except Exception as e:      # noqa: BLE001  (a detached or cross-origin frame)
            # the main frame unread leaves the digest empty: said louder
            (log.warning if idx == 0 else log.info)(
                "apply_form: frame %d skipped: %s", idx, type(e).__name__)
            continue
        for f in raw.get("fields") or []:
            if len(fields) > FIELDS_MAX:
                break
            fields.append(Field(
                n=len(fields), locator=(idx, str(f["css"])), label=str(f["label"]),
                type=str(f["type"]), required=bool(f["required"]),
                placeholder=str(f.get("placeholder") or ""), help=str(f.get("help") or ""),
                options=[str(o) for o in (f.get("options") or [])],
                id_or_name=str(f.get("id_or_name") or ""),
                autocomplete=str(f.get("autocomplete") or ""),
                widget=str(f.get("widget") or ""),
                click_locator=(idx, str(f["click"])) if f.get("click") else None,
                option_locators=[str(o) for o in (f.get("option_css") or [])],
                section=str(f.get("section") or ""), ident=str(f.get("ident") or ""),
                label_partial=bool(f.get("label_partial")), secret=bool(f.get("secret"))))
        for b in raw.get("buttons") or []:
            buttons.append(Button(n=len(buttons), locator=(idx, str(b["css"])),
                                  text=str(b["text"]), kind_hint=str(b.get("kind_hint") or ""),
                                  in_form=bool(b.get("in_form")), chrome=bool(b.get("chrome")),
                                  disabled=bool(b.get("disabled")),
                                  primary=bool(b.get("primary"))))
        if raw.get("dialog") and not dialog:
            dialog = str(raw["dialog"])
        if raw.get("text"):
            own = bool(raw.get("fields")) or any(_APPLY_WORD.search(str(b.get("text") or ""))
                                                 for b in raw.get("buttons") or [])
            first = (idx > 0 and content_site(str(getattr(frame, "url", "") or ""))
                     and _content_frame(frame, CONTENT_FRAME_ANY if own else CONTENT_FRAME_MIN))
            texts.append((0 if first else 1, str(raw["text"])))
    text = "\n".join(t for _, t in sorted(texts, key=lambda row: row[0]))[:PAGE_TEXT_CAP]
    try:
        title = str(page.title() or "")
    except Exception as e:      # noqa: BLE001  (a page closed or navigating)
        log.info("apply_form: page title unread: %s", type(e).__name__)
        title = ""
    return FormDigest(url_host=urlparse(page.url).hostname or "", title=title,
                      text=text, fields=fields, buttons=buttons, dialog=dialog)


def _content_frame(frame, size: tuple[int, int] = CONTENT_FRAME_MIN) -> bool:
    """A child frame big enough to be the page's content: at least
    `CONTENT_FRAME_MIN` on the page by its size alone (an iCIMS posting's
    frame holds no field), or `CONTENT_FRAME_ANY` when it holds fields or an
    Apply-worded control (a Greenhouse embed)."""
    try:
        box = frame.frame_element().bounding_box()
    except Exception:       # noqa: BLE001  (a detached frame)
        return False
    return bool(box) and box["width"] >= size[0] and box["height"] >= size[1]


def consent_control(page, allow=None) -> tuple[int, dict] | None:
    """(frame index, {css, text, kind, banner}) of the control the loop may
    click to dismiss a visible cookie or consent banner: its reject,
    decline or necessary-only control, else its close (`_CONSENT_CONTROL_JS`);
    None when no banner shows or it offers neither. `allow(index, frame)`
    says which frames may be looked in (the runner's: the page's own frames
    on the allowed sites, never a bot check's)."""
    for idx, frame in enumerate(frames(page)):
        if allow is not None and not allow(idx, frame):
            continue
        try:
            found = frame.evaluate(_CONSENT_CONTROL_JS)
        except Exception:       # noqa: BLE001  (a detached or cross-origin frame)
            continue
        if found:
            return idx, dict(found)
    return None


def page_texts(page) -> list[str]:
    """The `innerText` of every frame, main frame first, uncapped. A frame that
    cannot be read is skipped."""
    out: list[str] = []
    for frame in frames(page):
        try:
            t = frame.evaluate(_TEXT_JS)
        except Exception:       # noqa: BLE001
            continue
        if t:
            out.append(str(t))
    return out


def resolve(page, locator: tuple[int, str]):
    """A digest locator `(frame_index, css)` as a Playwright `Locator` on that
    frame. The frame is found by the URL it had at the last `extract` first
    (a frame that detached since shifts the indexes after it),
    then by its index. Raises `IndexError` when the frame no longer
    exists.

    The URLs are the page's last `extract`'s (`_FRAME_URLS`), not carried on
    the locator: that holds while no other extract runs between a read and
    its act, as in the loop today. Two frames at one URL (two `about:blank`
    frames, two copies of one widget) cannot be told apart this way; the
    first frame at the URL is taken, so for them the index is the better
    guide, and it is used whenever the frame at the index still has the
    URL."""
    return resolve_frame(page, locator).locator(str(locator[1]))


def resolve_frame(page, locator: tuple[int, str]):
    """The frame `resolve` finds a digest locator in (by the URL it had at
    the last `extract` first, then by its index): a check of where a control
    sits looks at the frame the act will use. Raises `IndexError` when the
    frame no longer exists."""
    idx = int(locator[0])
    all_frames = frames(page)
    try:
        urls = _FRAME_URLS.get(page) or []
    except TypeError:           # a page double that takes no weak reference
        urls = []
    if 0 < idx < len(urls) and urls[idx]:
        want = urls[idx]
        here = str(getattr(all_frames[idx], "url", "") or "") if idx < len(all_frames) else ""
        if here != want:
            moved = [f for f in all_frames[1:] if str(getattr(f, "url", "") or "") == want]
            if moved:
                return moved[0]
    if not 0 <= idx < len(all_frames):
        raise IndexError(f"frame {idx} is gone (page has {len(all_frames)})")
    return all_frames[idx]


# --- live reads of the page (the submit gate and every click) ------------------------


def live_text(loc) -> dict[str, str]:
    """The live element's {text, aria, type, tag} (`LIVE_TEXT_JS`), read just
    before a click; {} when it cannot be read."""
    try:
        return dict(loc.first.evaluate(LIVE_TEXT_JS, timeout=2_000))
    except Exception:       # noqa: BLE001  (gone, detached, or a page double)
        return {}


def find_by_text(page, frame_index: int, text: str) -> list[str]:
    """The CSS of every visible control in frame `frame_index` whose text is
    `text` (the way a control is found again when its stored locator now
    names another control)."""
    try:
        return [str(c) for c in frames(page)[int(frame_index)].evaluate(
            _FIND_BY_TEXT_JS, " ".join(str(text or "").split()))]
    except Exception:       # noqa: BLE001  (a frame gone)
        return []


def same_scope(page, button_locator: tuple[int, str],
               field_locators: list[tuple[int, str]]) -> tuple[str, str]:
    """("same" | "apart" | "unclear", why): does the button sit with these
    fields (`_SAME_SCOPE_JS`)? Only fields in the button's frame count."""
    idx = int(button_locator[0])
    css = [str(loc[1]) for loc in field_locators if int(loc[0]) == idx]
    try:
        out = frames(page)[idx].evaluate(_SAME_SCOPE_JS, [str(button_locator[1]), css])
    except Exception as e:      # noqa: BLE001  (a frame gone)
        return "unclear", f"unreadable ({type(e).__name__})"
    return str(out.get("verdict") or "unclear"), str(out.get("why") or "")


def form_index(page, locators: list[tuple[int, str]]) -> list[tuple[int, int]]:
    """(frame, the index of its form among the frame's forms) for each
    locator: -1 outside any form, -2 when the control is gone (a
    sign-in and a sign-up side by side)."""
    out: list[tuple[int, int]] = [(int(loc[0]), -2) for loc in locators]
    by_frame: dict[int, list[int]] = {}
    for i, loc in enumerate(locators):
        by_frame.setdefault(int(loc[0]), []).append(i)
    all_frames = frames(page)
    for idx, rows in by_frame.items():
        if not 0 <= idx < len(all_frames):
            continue
        try:
            got = all_frames[idx].evaluate(_FORM_INDEX_JS, [str(locators[i][1]) for i in rows])
        except Exception:       # noqa: BLE001  (a frame gone)
            continue
        for i, v in zip(rows, got or []):
            out[i] = (idx, int(v))
    return out


def box_values(page, locators: list[tuple[int, str]]) -> list[str | None]:
    """What each box holds now (None for a file, password or hidden box, or
    one that is gone), in `locators` order: the run compares them before and
    after its submit click; they are never written to the trace or the
    record."""
    out: list[str | None] = [None] * len(locators)
    by_frame: dict[int, list[int]] = {}
    for i, loc in enumerate(locators):
        by_frame.setdefault(int(loc[0]), []).append(i)
    all_frames = frames(page)
    for idx, rows in by_frame.items():
        if not 0 <= idx < len(all_frames):
            continue
        try:
            got = all_frames[idx].evaluate(_VALUES_JS, [str(locators[i][1]) for i in rows])
        except Exception:       # noqa: BLE001  (a frame mid-navigation)
            continue
        for i, v in zip(rows, got or []):
            out[i] = v
    return out


def validity_report(page, button_locator: tuple[int, str] | None = None,
                    field_locators: list[tuple[int, str]] | None = None) -> dict[str, list]:
    """The controls that would not validate and the visible error texts
    (`_VALIDITY_JS`): with `button_locator`, that button's form in its frame,
    or for a button outside any form the forms of `field_locators` (the
    fields this page filled) and the controls outside a form beside them;
    without, every frame's controls outside a form and every frame's error
    texts. {invalid: [{label, message, reason, frame}], errors: [{text,
    field, frame}]}."""
    out: dict[str, list] = {"invalid": [], "errors": []}
    targets = [(int(button_locator[0]), str(button_locator[1]))] if button_locator \
        else [(i, "") for i in range(len(frames(page)))]
    # without a button, the filled fields' forms: a button no longer found
    # (a server's answer whose inserted summary shifts a path) is looked for
    # through them
    all_frames = frames(page)
    for idx, css in targets:
        if not 0 <= idx < len(all_frames):
            continue
        fcss = [str(loc[1]) for loc in field_locators or [] if int(loc[0]) == idx]
        try:
            got = all_frames[idx].evaluate(_VALIDITY_JS, {"bcss": css or None, "fcss": fcss})
        except Exception:       # noqa: BLE001  (a detached or cross-origin frame)
            continue
        for key in ("invalid", "errors"):
            out[key] += [{**row, "frame": idx} for row in got.get(key) or []]
    return out


def control_scan(page, frame_indexes: list[int] | None = None, *,
                 required_only: bool = False) -> list[dict[str, Any]]:
    """The controls the extractor leaves out (`_SCAN_JS`), in the given
    frames (every frame when None), each {label, kind, required, empty,
    shadow, frame}; `required_only` keeps the required empty ones (the
    filter runs before the 40-row cut)."""
    out: list[dict[str, Any]] = []
    for idx, frame in enumerate(frames(page)):
        if frame_indexes is not None and idx not in frame_indexes:
            continue
        try:
            got = frame.evaluate(_SCAN_JS, bool(required_only))
        except Exception:       # noqa: BLE001  (a detached or cross-origin frame)
            continue
        out += [{**row, "frame": idx} for row in got or []]
    return out


def captcha_widgets(page) -> list[dict[str, Any]]:
    """Every frame document's CAPTCHA widgets (`_CAPTCHA_WIDGETS_JS`): one
    row per document that has one, {widgets, tokens, frame}."""
    out: list[dict[str, Any]] = []
    try:
        all_frames = frames(page)
    except Exception:       # noqa: BLE001  (a page double)
        return out
    for idx, frame in enumerate(all_frames):
        evaluate = getattr(frame, "evaluate", None)
        if evaluate is None:
            continue
        try:
            got = evaluate(_CAPTCHA_WIDGETS_JS)
        except Exception:       # noqa: BLE001  (a detached or cross-origin frame)
            continue
        if isinstance(got, Mapping) and got.get("widgets"):
            out.append({**got, "frame": idx})
    return out


_CHECKBOX_SIZES = ("normal", "compact", "flexible")


def unsolved_checkbox(page) -> str:
    """A visible reCAPTCHA, hCaptcha or Turnstile checkbox (its frame's size
    `normal`, whatever its height; Turnstile's compact and
    flexible too) whose document holds an empty response token, or none:
    the provider's name, else "". The invisible badge (`size=invisible`)
    never counts."""
    for row in captcha_widgets(page):
        normal = [w for w in row.get("widgets") or [] if w.get("visible")
                  and (w.get("size") == "normal" or (w.get("provider") == "turnstile"
                                                     and w.get("size") in _CHECKBOX_SIZES))]
        if not normal:
            continue
        tokens = row.get("tokens") or []
        if not tokens or not all(tokens):
            return str(normal[0].get("provider") or "captcha")
    return ""
