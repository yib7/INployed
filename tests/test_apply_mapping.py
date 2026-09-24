"""SP5: what the mapping request carries and how the planner reads a pick.

- READ-10: every button in the mapping request carries its DOM flags
  (`in_form`, `disabled`, `primary`); `FakeJev` never reads a boolean (an
  object's boolean entry goes with its key), so the flags leave its roles as
  they were.

No browser, no network: hand-built digests, the harness's synthetic catalog,
`FakeJev`."""
import copy
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_facts  # noqa: E402
import apply_harness as h  # noqa: E402
import apply_judge  # noqa: E402
import jev  # noqa: E402
from apply_form import Button, Field, FormDigest  # noqa: E402


@pytest.fixture
def catalog(tmp_path):
    folder = h.write_job_folder(tmp_path / "job")
    return apply_facts.build(folder, answers=h.bank())


def _f(n, label, type_="text", required=False, options=(), section=""):
    return Field(n=n, locator=(0, f"#f{n}"), label=label, type=type_, required=required,
                 options=list(options))


def _strip_flags(state):
    out = copy.deepcopy(state)
    for b in out.get("buttons", []):
        for flag in ("in_form", "disabled", "primary"):
            b.pop(flag, None)
    return out


# --- READ-10: the buttons' DOM flags ---------------------------------------------------------

def test_the_mapping_sends_each_buttons_form_disabled_and_primary_flags(catalog):
    digest = FormDigest(url_host="x", title="Apply", text="", fields=[_f(0, "Email", "email")],
                        buttons=[Button(n=0, locator=(0, "#s"), text="Submit application",
                                        in_form=True, disabled=True, primary=True),
                                 Button(n=1, locator=(0, "#h"), text="Help")])
    state, _ = apply_judge.page_questions(digest, catalog, {})
    assert state["buttons"] == [
        {"n": 0, "text": "Submit application", "in_form": True, "disabled": True,
         "primary": True},
        {"n": 1, "text": "Help", "in_form": False, "disabled": False, "primary": False}]
    # the flags round-trip through the digest's JSON
    again = FormDigest.from_dict(digest.to_dict())
    assert (again.buttons[0].disabled, again.buttons[0].primary) == (True, True)


def test_the_fake_judge_reads_no_boolean_so_the_flags_leave_its_roles_as_they_were(catalog):
    texts = ["Submit application", "Apply now", "Continue", "Back", "Next", "Sign in",
             "Create account", "Cancel", "Upload resume", "Save and continue"]
    for flags in ((True, True, True), (True, False, False), (False, True, True)):
        digest = FormDigest(url_host="x", title="Apply", text="Apply for the role",
                            fields=[_f(0, "Email", "email", required=True)],
                            buttons=[Button(n=i, locator=(0, f"#b{i}"), text=t,
                                            in_form=flags[0], disabled=flags[1],
                                            primary=flags[2])
                                     for i, t in enumerate(texts)])
        state, q = apply_judge.page_questions(digest, catalog, {})
        with_flags = jev.FakeJev().judge(state, q)
        without = jev.FakeJev().judge(_strip_flags(state), q)
        assert {k: a.choice for k, a in with_flags.items()} == \
            {k: a.choice for k, a in without.items()}, flags
    # the rule itself: a boolean entry is never text, nor its key
    assert jev._as_text({"text": "Next", "in_form": True, "items": [True, "Back"]}) == \
        '{"text": "Next", "items": ["Back"]}'
    words = jev._flatten({"a": True, "b": "word", "c": [False, "more"]}).split()
    assert words == ["word", "more"]
