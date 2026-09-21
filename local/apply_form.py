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

The Playwright extractor (`extract(page) -> FormDigest`) joins this module in
SP3; it imports Playwright lazily inside the function, so this module stays
importable without it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

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


@dataclass
class Button:
    """One clickable control. `kind_hint` comes from the DOM (`submit`,
    `button`, `link`, ...) and is only a hint; the judge decides the role."""
    n: int
    locator: tuple[int, str]
    text: str
    kind_hint: str = ""


@dataclass
class FormDigest:
    url_host: str
    title: str
    text: str
    fields: list[Field] = field(default_factory=list)
    buttons: list[Button] = field(default_factory=list)

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
                        id_or_name=str(f.get("id_or_name", "") or ""))
                  for f in (raw.get("fields") or [])]
        buttons = [Button(n=int(b["n"]), locator=_locator(b.get("locator")),
                          text=str(b.get("text", "")),
                          kind_hint=str(b.get("kind_hint", "") or ""))
                   for b in (raw.get("buttons") or [])]
        return cls(url_host=str(raw.get("url_host", "")), title=str(raw.get("title", "")),
                   text=str(raw.get("text", "")), fields=fields, buttons=buttons)


def _locator(raw: Any) -> tuple[int, str]:
    if not raw:
        return (0, "")
    frame, css = raw
    return (int(frame), str(css))
