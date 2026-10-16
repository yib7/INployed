"""The upstream avoid-ai-writing detector, run through Node as an optional extra check.

``aiwriting`` carries a hand-picked Python extract of the avoid-ai-writing skill
(MIT, Conor Bronsdon). This module runs the skill's own deterministic engine,
``detector/patterns.js`` from v3.38.0, vendored unmodified under
``vendor/avoid_ai_writing/`` with its CLI, LICENSE and NOTICE. The engine is
upstream's corpus-measured regex set (54 issue types), so the letter gate gets the
sentence shapes a short Python ban list cannot carry: negative parallelism,
performed insight, lingering-attention claims, the dramatized crowd contrast,
launch-copy introductions, and the rest of ``ALLOWED_TYPES`` below.

**Optional, and never load-bearing.** Node is not a dependency of this package.
When ``node`` is not on PATH, or the run fails, times out or prints something
that is not the expected JSON, ``violations()`` returns ``[]`` and the letter gate
carries on with its own checks. The Settings toggle (config.aiwriting_detector_enabled,
default OFF) decides whether the check runs at all.

**Why an allowlist.** The same rule as ``aiwriting.EXTRA_BANS``: a finding buys a
repair call that can damage correct text, so only phrase-level types that are
always a tell in a cover letter are kept. Left out, each for a reason:

  * the vocabulary tiers (``tier1``, ``tier1-clarity``, ``tier2``, ``tier3`` and
    the tier-3 phrase types): ``aiwriting._TIER1_RE`` and ``compose._STYLE_BANS``
    own vocabulary, with the technical carve-outs (realm, paradigm, beacon,
    genuine) the engine's lists do not make;
  * ``em-dash``: ``compose`` bans and strips every em dash already;
  * the stylometric types (``uniformity``, ``cross-para-burstiness``,
    ``punct-distribution``, ``fnword-trigram-entropy``, ``low-ttr``): one letter
    is too short a sample, and ``aiwriting.uniform_rhythm`` measures rhythm;
  * ``hollow-intensifier``, ``real-actual-inflation``, ``transition``,
    ``filler``, ``confidence-calibration``, ``emotional-flatline``,
    ``parenthetical-hedge``, ``dev-blog-boilerplate``: context-dependent in a
    letter (the prompts ask for "genuine but MEASURED interest", a candidate may
    describe a real-time system or a zero-config tool), so they stay in
    ``aiwriting.RULES_PROMPT`` where the model can read the context;
  * the formatting and markup types (``formatting``, ``title-case-header``,
    ``hashtag-stuff``, ``bullet-np-list``, ``unnecessary-hyphenation``,
    ``smart-punct-signature``, ``normalization-flag``, ``ai-utm-source``): a
    letter body is plain paragraphs, and the template owns its typography.

Findings come back under the same name-list contract as ``aiwriting.violations``,
one name per type, so ``coverletter._body_violations`` can concatenate the lists
and keep its single repair call and its strict-improvement rule.
"""
from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

VENDOR_DIR = Path(__file__).resolve().parent / "vendor" / "avoid_ai_writing"
CLI = VENDOR_DIR / "bin" / "avoid-ai-writing.js"
UPSTREAM_VERSION = "3.38.0"

# The engine's context. Only "technical" changes what flags, by muting eight
# vocabulary words this module drops anyway, so the plain default is used.
CONTEXT = "general"
TIMEOUT_S = 20.0

# detector issue type -> the finding name the letter gate reports. Names reuse
# aiwriting's spelling where the two overlap ("chatbot artifact", "let's opener",
# "generic conclusion"), so _body_violations can drop a duplicate by name.
ALLOWED_TYPES: Dict[str, str] = {
    "cutoff-disclaimer": "cutoff disclaimer",
    "chatbot": "chatbot artifact",
    "sycophantic": "sycophantic tone",
    "reasoning-artifact": "reasoning artifact",
    "ai-placeholder": "unfilled placeholder",
    "ai-citation-markup": "chatbot citation markup",
    "lets-construction": "let's opener",
    "vague-attribution": "vague attribution",
    "significance-inflation": "significance inflation",
    "novelty-inflation": "novelty inflation",
    "template-phrase": "template phrase",
    "formulaic-opener": "formulaic opening",
    "speculative-opener": "speculative opener",
    "rhetorical-question": "rhetorical question",
    "false-concession": "false concession",
    "hedge-stack": "hedge-stacked prediction",
    "generic-conclusion": "generic conclusion",
    "future-narrative": "generic future narrative",
    "social-cta-closer": "engagement-bait closer",
    "negative-parallelism": "negative parallelism",
    "negation-chain": "negation chain",
    "performed-insight": "performed insight",
    "lingering-attention": "lingering-attention claim",
    "crowd-contrast": "crowd contrast",
    "launch-intro": "launch-copy introduction",
    "fake-casual-prop": "fake-casual prop",
}

# No console window flashes up behind the dashboard on Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_node() -> Optional[str]:
    """The node executable, or None. RESUME_TAILOR_NODE names one explicitly
    (a portable install that is not on PATH); otherwise PATH decides."""
    explicit = (os.getenv("RESUME_TAILOR_NODE") or "").strip()
    if explicit:
        return explicit if Path(explicit).is_file() else None
    return shutil.which("node")


def available() -> bool:
    """Whether the check can run here: node found and the vendored CLI present."""
    return find_node() is not None and CLI.is_file()


@functools.lru_cache(maxsize=64)
def _analyze(text: str) -> Optional[tuple]:
    """The engine's issue types for `text`, in report order, or None when the
    run is unavailable or fails. Cached because the gate scores a body, then
    its repair, and a retry can score the same text again."""
    node = find_node()
    if node is None or not CLI.is_file():
        return None
    try:
        proc = subprocess.run(
            [node, str(CLI), "--context", CONTEXT],
            input=text, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT_S, cwd=tempfile.gettempdir(),
            creationflags=_NO_WINDOW,
        )
        if proc.returncode != 0:
            return None
        issues = json.loads(proc.stdout).get("issues") or []
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None
    return tuple(str(i.get("type", "")) for i in issues if isinstance(i, dict))


def violations(text: str) -> List[str]:
    """Names of the allowlisted detector findings in `text`, one per type, in the
    engine's order (empty = clean, or the check could not run)."""
    if not (text or "").strip():
        return []
    types = _analyze(text)
    if not types:
        return []
    names: List[str] = []
    for kind in types:
        name = ALLOWED_TYPES.get(kind)
        if name and name not in names:
            names.append(name)
    return names
