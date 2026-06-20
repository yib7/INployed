"""Common application-form answers a browser form-filler can use confidently.

These are the boilerplate questions every Greenhouse/Lever/Workday application
asks (work authorization, sponsorship, EEO self-identification, "how did you
hear about us"). The defaults reflect the candidate's reality: a US citizen /
green-card holder who never needs visa sponsorship. They are personal, so the
backing file (repo-root apply_config.json) is git-ignored; absent, the defaults
below apply unchanged.

apply_data.write() embeds load_apply_config() as the "standard_answers" block in
each apply_data.json. The form-filler still leaves the final submit to the human.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

# settings.py / config.py treat the repo root as two levels up from this package.
PKG_DIR = Path(__file__).resolve().parent          # local/resume_tailor
REPO_ROOT = PKG_DIR.parent.parent                  # scrape_data
APPLY_CONFIG = REPO_ROOT / "apply_config.json"

# The candidate is a US citizen / green-card holder — never filter or answer as
# if sponsorship were needed (see MEMORY: work_authorization).
DEFAULTS: Dict[str, Any] = {
    "work_authorized": True,
    "requires_sponsorship": False,
    "years_experience": "0",
    "willing_to_relocate": True,
    "authorization_statement":
        "Authorized to work in the United States; no visa sponsorship required.",
    "gender": "Decline to self-identify",
    "race_ethnicity": "Decline to self-identify",
    "veteran_status": "Decline to self-identify",
    "disability_status": "Decline to self-identify",
    "how_did_you_hear": "LinkedIn",
}


def load_apply_config() -> Dict[str, Any]:
    """Return DEFAULTS merged with repo-root apply_config.json ({} when absent
    or unreadable). Override keys win; unspecified keys keep their default."""
    merged: Dict[str, Any] = dict(DEFAULTS)
    try:
        raw = json.loads(Path(APPLY_CONFIG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return merged
    if isinstance(raw, dict):
        merged.update({k: v for k, v in raw.items() if k in DEFAULTS})
    return merged
