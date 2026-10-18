"""What a fresh clone installs and seeds: README Steps 2 and 3.

requirements.txt is the lock file, so every package the pins pull in must be
pinned too, or a clean install quietly takes whatever was newest that day.
And the `.env` that setup.ps1 copies from `.env.example` must leave each value
the user has to supply blank: a placeholder there reads as a saved key in the
Settings tab, goes out to Bright Data as a token, and passes for a Google Cloud
project in the tailor's "is a project set" check.
"""
import sys
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import envfile  # noqa: E402
import settings  # noqa: E402


def _pins() -> dict[str, Requirement]:
    pins = {}
    for raw in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        req = Requirement(line)
        pins[canonicalize_name(req.name)] = req
    return pins


def test_every_requirement_is_an_exact_pin():
    loose = [str(r) for r in _pins().values()
             if len(r.specifier) != 1 or next(iter(r.specifier)).operator != "=="]
    assert not loose, loose


def test_every_package_the_pins_pull_in_is_pinned_too():
    """Walks each pinned package's own requirements, as installed here, with
    their markers evaluated for this platform."""
    pins = _pins()
    missing = {}
    for name, req in pins.items():
        if req.marker is not None and not req.marker.evaluate():
            continue
        try:
            requires = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            pytest.skip(f"{name} is not installed; run pip install -r requirements.txt")
        for raw in requires:
            sub = Requirement(raw)
            if sub.marker is not None and not sub.marker.evaluate({"extra": ""}):
                continue
            if canonicalize_name(sub.name) not in pins:
                missing.setdefault(canonicalize_name(sub.name), []).append(name)
    assert not missing, f"needed but not pinned in requirements.txt: {missing}"


def test_a_fresh_env_leaves_every_credential_and_id_blank():
    active = envfile.read(REPO / ".env.example")
    defaults = {f.key: f.default for f in settings.SETTINGS_SCHEMA if f.target == "env"}
    for key, value in active.items():
        if value:
            assert key in defaults and str(defaults[key]) == value, (
                f".env.example sets {key}={value!r}, which is not the code default; "
                "leave it blank so a fresh install reads as not set")
    for key in ("BRIGHT_DATA_API_TOKEN", "BRIGHT_DATA_DATASET_ID", "GOOGLE_CLOUD_PROJECT"):
        assert active.get(key) == "", key
