"""Tests for the email-verification / security-code gate handling (apply_verify).

Pure/fast: exercises code extraction from email bodies and the file handshake
between the Playwright driver (request_code / await_code) and the orchestrator
(write_code). No browser, no network — the Playwright bits (detect_code_gate,
fill_code) are thin locator wrappers exercised in live runs, not here.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_verify  # noqa: E402

# Verbatim body of the real Greenhouse "Security code" email (Gotion, 2026-07-06).
_GREENHOUSE_BODY = (
    "Hi Jane,\r\n\r\nCopy and paste this code into the security code field "
    "on your application:\r\n\r\nK2A5IMA0\r\n\r\nAfter you enter the code, "
    "resubmit your application.\r\n\r\n© 2026 Greenhouse\r\n\r\n"
    "18 West 18th Street, 11th Floor, New York, NY 10011, USA"
)


def test_extract_greenhouse_security_code():
    assert apply_verify.extract_code(_GREENHOUSE_BODY, length=8) == "K2A5IMA0"


def test_extract_greenhouse_code_without_length_hint():
    # The code sits alone on its own line, so it wins even without a length hint
    # (footer tokens like "2026" appear inline, not alone on a line).
    assert apply_verify.extract_code(_GREENHOUSE_BODY) == "K2A5IMA0"


def test_extract_mixed_case_code():
    # Live run surfaced this: Greenhouse codes can be mixed-case ("tffCw7Xp").
    body = (
        "Hi Jane,\r\n\r\nCopy and paste this code into the security code field "
        "on your application:\r\n\r\ntffCw7Xp\r\n\r\nAfter you enter the code, "
        "resubmit your application.\r\n\r\n© 2026 Greenhouse"
    )
    assert apply_verify.extract_code(body, length=8) == "tffCw7Xp"
    assert apply_verify.extract_code(body) == "tffCw7Xp"


def test_extract_numeric_otp():
    assert apply_verify.extract_code("Your verification code is 483920.", length=6) == "483920"


def test_extract_ignores_common_allcaps_words():
    assert apply_verify.extract_code("Please enter the code below:\n\nHELLO", length=5) is None


def test_extract_empty_returns_none():
    assert apply_verify.extract_code("") is None
    assert apply_verify.extract_code(None) is None


def test_handshake_request_then_await(tmp_path):
    apply_verify.request_code(tmp_path, {"company": "Gotion", "email": "jane.doe@example.com"})
    assert (tmp_path / apply_verify.REQUEST_FILE).exists()
    apply_verify.write_code(tmp_path, "K2A5IMA0")
    assert apply_verify.await_code(tmp_path, timeout=2, poll=0.05) == "K2A5IMA0"


def test_request_code_clears_stale_response(tmp_path):
    apply_verify.write_code(tmp_path, "OLDCODE1")
    apply_verify.request_code(tmp_path, {})
    assert not (tmp_path / apply_verify.RESPONSE_FILE).exists()


def test_await_times_out_when_no_code(tmp_path):
    assert apply_verify.await_code(tmp_path, timeout=0.3, poll=0.05) is None
