import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import keypool  # noqa: E402


def test_key_fingerprint_is_stable_8char_and_not_raw():
    fp = keypool.key_fingerprint("AQ.supersecretvalue")
    assert len(fp) == 8
    assert "secret" not in fp
    assert fp == keypool.key_fingerprint("AQ.supersecretvalue")
    assert fp != keypool.key_fingerprint("AQ.different")


def test_pacific_today_is_iso_date():
    d = keypool.pacific_today()
    assert len(d) == 10 and d[4] == "-" and d[7] == "-"


def test_usage_state_incr_and_persist(tmp_path):
    p = tmp_path / "score_state.json"
    st = keypool.UsageState(p)
    st.load()
    st.incr("fp1", "gemini-3.5-flash", 3)
    st.set_exhausted("fp2", "gemini-3.1-flash-lite", 500)
    st.save()

    st2 = keypool.UsageState(p)
    st2.load()
    assert st2.get("fp1", "gemini-3.5-flash") == 3
    assert st2.get("fp2", "gemini-3.1-flash-lite") == 500
    # fingerprints only -- no raw key material on disk
    assert "supersecret" not in p.read_text(encoding="utf-8")


def test_usage_state_resets_on_date_rollover(tmp_path):
    p = tmp_path / "score_state.json"
    p.write_text(json.dumps({"date": "2000-01-01", "usage": {"fp1:m": 9}}), encoding="utf-8")
    st = keypool.UsageState(p)
    st.load()
    assert st.get("fp1", "m") == 0
