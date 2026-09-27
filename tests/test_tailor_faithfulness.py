"""TL-4 as the run meets it: the faithfulness check after the rephrase and after
every later rewrite.

The deterministic grounding gate (`verify.enforce_grounded`) traces only
distinctive tokens, so a claim written in lowercase common words passes it: "Led
the team" over an atom that says the candidate helped carries nothing it can
check (`verify.py`'s docstring states the gap). With Jev on, TL-4 asks the judge
about every bullet against the atoms it was written from. A flagged bullet gets
one reground call with its finding named; still flagged, it reverts to its last
passing version, or is dropped exactly as the grounding gate drops one. TL-4 may
only reject, revert or drop a bullet; the reground call writes any new text, and
the grounding gate still runs as before.

Nothing here calls a model or Jev: `compose.call` is replaced in every test, and
every judge is a fake.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

from resume_tailor import assets, compose, config, jev_assist, measure  # noqa: E402

PROJECT = "Harbor"

_ATOMS = {
    "h1": {"id": "h1", "_block": PROJECT,
           "what": "Helped the team move the billing service to a queue-based design"},
    "h2": {"id": "h2", "_block": PROJECT,
           "what": "Wrote 40 integration tests for the billing service"},
}
_INFLATION = jev_assist.FINDINGS["inflates"]


def _sel():
    return {"experience": [], "leadership": [],
            "projects": [{"name": PROJECT, "groups": [["h1"], ["h2"]]}]}


class Recorder:
    """A `compose.call` replacement that keeps every prompt and answers `answer`."""

    def __init__(self, answer):
        self.answer = answer
        self.systems, self.users = [], []

    def __call__(self, system, user, tier, **kw):
        self.systems.append(system)
        self.users.append(user)
        return self.answer


@pytest.fixture()
def engine(monkeypatch):
    """Every input reground and the gate read, pinned. No user data, no config.json."""
    monkeypatch.delenv("RESUME_TAILOR_REGROUND", raising=False)
    monkeypatch.setattr(assets, "atoms_by_id", lambda: {k: dict(v) for k, v in _ATOMS.items()})
    monkeypatch.setattr(config, "_config_json", lambda: {})
    monkeypatch.setattr(config, "DEFAULT_LINE_TARGETS", [2, 2])
    monkeypatch.setattr(config, "PROJECT_BULLET_LINES", 2)
    monkeypatch.setattr(config, "verbatim_blocks", lambda: {})
    monkeypatch.setattr(measure, "BODY_LINE_CAPACITY", 53464)
    jev_assist.reset_usage()


def _sent(user):
    """The payload out of a reground user message, parsed."""
    body = user.split("REJECTED BULLETS", 1)[1].split("Return ONLY JSON", 1)[0]
    return json.loads(body[body.index("["):body.rindex("]") + 1])


def _reground(monkeypatch, dropped, **kw):
    rec = Recorder({"bullets": []})
    monkeypatch.setattr(compose, "call", rec)
    compose.reground("Platform role.", "Engineer", _sel(), dropped, **kw)
    return rec


# ── reground names the finding ───────────────────────────────────────────────
def test_a_finding_rides_with_its_bullet_and_is_explained_once(engine, monkeypatch):
    rec = _reground(monkeypatch, {"h1": [], "h2": ["Kafka"]}, findings={"h1": _INFLATION})
    sent = {b["gkey"]: b for b in _sent(rec.users[0])}
    assert sent["h1"]["finding"] == _INFLATION
    assert sent["h1"]["banned_tokens"] == []
    assert "finding" not in sent["h2"]
    (system,) = rec.systems
    assert system.count(compose.REGROUND_FINDING_RULE) == 1


@pytest.mark.parametrize("findings", [None, {}, {"h1": ""}], ids=["none", "empty", "passing"])
def test_without_a_finding_the_prompt_is_todays(engine, monkeypatch, findings):
    """No finding, no change: the Jev-off prompt stays word for word what it was,
    which `test_tailor_jev.py`'s recording pins against the engine before TL-4."""
    today = _reground(monkeypatch, {"h1": ["Kafka"]})
    got = _reground(monkeypatch, {"h1": ["Kafka"]}, findings=findings)
    assert (got.systems, got.users) == (today.systems, today.users)
    assert compose.REGROUND_FINDING_RULE not in got.systems[0]
    assert "finding" not in _sent(got.users[0])[0]


def test_the_finding_rule_is_free_of_the_banned_phrasing():
    rule = compose.REGROUND_FINDING_RULE
    assert compose.style_violations(rule) == []
    assert chr(0x2014) not in rule
