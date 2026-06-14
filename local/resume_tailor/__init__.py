"""Gemini-native resume tailor: turns a scraped LinkedIn job into a one-page,
fact-grounded PDF resume. Triggered from the dashboard or run standalone.

Core principle: SELECT and RE-PHRASE atoms from master_experience.yaml — never
generate new claims. Every bullet traces to exactly one fact-atom.
"""

__all__ = ["tailor"]


def __getattr__(name):
    # Lazy so importing submodules doesn't pull the whole pipeline (and its
    # heavy deps) at import time.
    if name == "tailor":
        from .run import tailor
        return tailor
    raise AttributeError(name)
