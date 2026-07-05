"""Tests for the batch auto-apply queue store + agent CLI (SP2).

local/apply_queue.py is the deterministic backend the dashboard (SP3) and the
apply agent (SP4) share: an atomic JSON queue beside seen.db with a sidecar
byte-0 file lock for cross-process mutations. Everything here is hermetic —
every call passes an explicit tmp_path queue (or sets APPLY_QUEUE_PATH), so the
real %LOCALAPPDATA%\\linkedin_watcher\\apply_queue.json is never touched.
"""
import io
import json
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_queue  # noqa: E402


def _q(tmp_path):
    return tmp_path / "apply_queue.json"


def _entry(jid="j1", **kw):
    kw.setdefault("company", "Acme")
    kw.setdefault("title", "Engineer")
    kw.setdefault("apply_url", "https://boards.greenhouse.io/acme/jobs/1")
    return apply_queue.new_entry(jid, **kw)


# --- path resolution ----------------------------------------------------------

def test_queue_path_env_override_read_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLY_QUEUE_PATH", str(tmp_path / "override.json"))
    assert apply_queue.queue_path() == tmp_path / "override.json"
    monkeypatch.delenv("APPLY_QUEUE_PATH")
    assert apply_queue.queue_path().name == "apply_queue.json"


def test_queue_path_explicit_beats_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLY_QUEUE_PATH", str(tmp_path / "env.json"))
    assert apply_queue.queue_path(tmp_path / "mine.json") == tmp_path / "mine.json"


# --- load: missing / corrupt --------------------------------------------------

def test_load_missing_file_returns_fresh_queue(tmp_path):
    data = apply_queue.load(_q(tmp_path))
    assert data == {"version": 1, "jobs": []}


def test_load_corrupt_lock_free_returns_fresh_without_rename(tmp_path):
    # A lock-free reader must NEVER rename: it could race a lock-holding writer
    # that already quarantined and rewrote a healthy queue (TOCTOU) and rename
    # the VALID file into .corrupt-*, silently emptying the active queue.
    q = _q(tmp_path)
    q.write_text("{not json!!", encoding="utf-8")
    with pytest.warns(RuntimeWarning):
        data = apply_queue.load(q)
    assert data == {"version": 1, "jobs": []}
    assert q.exists()
    assert q.read_text(encoding="utf-8") == "{not json!!"
    assert not list(tmp_path.glob("apply_queue.json.corrupt-*"))


def test_load_corrupt_quarantines_when_asked(tmp_path):
    # quarantine=True is the under-locked() flavor: rename aside + start fresh.
    q = _q(tmp_path)
    q.write_text("{not json!!", encoding="utf-8")
    with pytest.warns(RuntimeWarning):
        data = apply_queue.load(q, quarantine=True)
    assert data == {"version": 1, "jobs": []}
    sidecars = list(tmp_path.glob("apply_queue.json.corrupt-*"))
    assert len(sidecars) == 1
    assert sidecars[0].read_text(encoding="utf-8") == "{not json!!"
    assert not q.exists()  # renamed away, next write starts fresh


def test_locked_mutation_quarantines_corrupt_file(tmp_path):
    q = _q(tmp_path)
    q.write_text("{not json!!", encoding="utf-8")
    with pytest.warns(RuntimeWarning):
        apply_queue.enqueue(_entry("1"), path=q)   # mutation holds the lock
    sidecars = list(tmp_path.glob("apply_queue.json.corrupt-*"))
    assert len(sidecars) == 1
    assert sidecars[0].read_text(encoding="utf-8") == "{not json!!"
    data = apply_queue.load(q)                     # rewritten fresh + the entry
    assert [e["job_posting_id"] for e in data["jobs"]] == ["1"]


def test_load_wrong_shape_is_treated_as_corrupt(tmp_path):
    q = _q(tmp_path)
    q.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    with pytest.warns(RuntimeWarning):
        data = apply_queue.load(q)
    assert data["jobs"] == []
    assert q.exists()                              # lock-free: left in place
    with pytest.warns(RuntimeWarning):
        data = apply_queue.load(q, quarantine=True)
    assert data["jobs"] == []
    assert list(tmp_path.glob("apply_queue.json.corrupt-*"))


def test_load_transient_oserror_retries_then_succeeds(tmp_path, monkeypatch):
    q = _q(tmp_path)
    apply_queue.enqueue(_entry("1"), path=q)
    real = Path.read_text
    calls = {"n": 0}

    def flaky(self, *a, **kw):
        if self == q:
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError("AV scan holding the file")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", flaky)
    monkeypatch.setattr(apply_queue.time, "sleep", lambda s: None)
    data = apply_queue.load(q)
    assert [e["job_posting_id"] for e in data["jobs"]] == ["1"]
    assert calls["n"] == 3


def test_load_persistent_oserror_never_quarantines(tmp_path, monkeypatch):
    # A transient read failure (AV scan, sharing violation) is NOT corruption:
    # even the locked flavor must not rename a healthy queue over an OSError.
    q = _q(tmp_path)
    apply_queue.enqueue(_entry("1"), path=q)
    before = q.read_text(encoding="utf-8")

    def always(self, *a, **kw):
        raise PermissionError("sharing violation")

    monkeypatch.setattr(Path, "read_text", always)
    monkeypatch.setattr(apply_queue.time, "sleep", lambda s: None)
    with pytest.warns(RuntimeWarning):
        data = apply_queue.load(q, quarantine=True)
    assert data == {"version": 1, "jobs": []}
    monkeypatch.undo()
    assert q.read_text(encoding="utf-8") == before          # untouched
    assert not list(tmp_path.glob("apply_queue.json.corrupt-*"))


# --- new_entry: every field always present -------------------------------------

def test_new_entry_carries_all_fields(tmp_path):
    e = _entry("77", is_easy_apply=True, batch_id="b1")
    for key in ("job_posting_id", "company", "title", "apply_url", "is_easy_apply",
                "batch_id", "status", "attempts", "claimed_by", "notes", "tab_note",
                "missing_answers", "artifacts", "ats",
                "queued_at", "started_at", "finished_at", "updated_at"):
        assert key in e, key
    assert e["status"] == "queued"
    assert e["attempts"] == 0
    assert e["missing_answers"] == []
    for k in ("folder", "resume_pdf", "cover_letter_pdf", "cover_letter_txt",
              "apply_md", "application_record"):
        assert k in e["artifacts"], k
    for k in ("domain", "system", "account_status"):
        assert k in e["ats"], k
    assert e["queued_at"] and e["updated_at"]
    assert e["started_at"] == "" and e["finished_at"] == ""


def test_new_entry_infers_ats_from_url():
    cases = {
        "https://www.linkedin.com/jobs/view/1": "linkedin",
        "https://acme.wd5.myworkdayjobs.com/x": "workday",
        "https://boards.greenhouse.io/acme/jobs/1": "greenhouse",
        "https://jobs.lever.co/acme/1": "lever",
        "https://careers-acme.icims.com/jobs/1": "icims",
        "https://apply.example.com/1": "other",
    }
    for url, system in cases.items():
        e = apply_queue.new_entry("x", apply_url=url)
        assert e["ats"]["system"] == system, url
        assert e["ats"]["domain"] == url.split("/")[2].lower()


def test_new_entry_tailoring_has_no_queued_at():
    e = apply_queue.new_entry("x", status="tailoring")
    assert e["status"] == "tailoring"
    assert e["queued_at"] == ""


def test_new_entry_rejects_unknown_status():
    with pytest.raises(ValueError):
        apply_queue.new_entry("x", status="bogus")


# --- enqueue: upsert by job_posting_id -----------------------------------------

def test_enqueue_appends_and_persists(tmp_path):
    q = _q(tmp_path)
    apply_queue.enqueue(_entry("1"), path=q)
    apply_queue.enqueue(_entry("2"), path=q)
    data = apply_queue.load(q)
    assert [j["job_posting_id"] for j in data["jobs"]] == ["1", "2"]


def test_enqueue_nonterminal_dup_returned_unchanged(tmp_path):
    q = _q(tmp_path)
    first = apply_queue.enqueue(_entry("1", batch_id="old"), path=q)
    again = apply_queue.enqueue(_entry("1", batch_id="new"), path=q)
    assert again["batch_id"] == "old"          # existing entry wins
    assert again["queued_at"] == first["queued_at"]
    assert len(apply_queue.load(q)["jobs"]) == 1


def test_enqueue_terminal_dup_is_reset_to_fresh_queued(tmp_path):
    q = _q(tmp_path)
    apply_queue.enqueue(_entry("1"), path=q)
    got = apply_queue.claim(claimed_by="agent", path=q)
    assert got["job_posting_id"] == "1"
    apply_queue.finish("1", "failed", path=q)
    fresh = apply_queue.enqueue(_entry("1", batch_id="retry"), path=q)
    assert fresh["status"] == "queued"
    assert fresh["attempts"] == 0
    assert fresh["batch_id"] == "retry"
    assert fresh["finished_at"] == ""
    assert len(apply_queue.load(q)["jobs"]) == 1


# --- claim: FIFO by queued_at, skips tailoring/terminal -------------------------

def _force_queued_at(q, stamps):
    data = apply_queue.load(q)
    for e in data["jobs"]:
        if e["job_posting_id"] in stamps:
            e["queued_at"] = stamps[e["job_posting_id"]]
    apply_queue.atomic_write_json(q, data)


def test_claim_fifo_oldest_first(tmp_path):
    q = _q(tmp_path)
    for jid in ("a", "b", "c"):
        apply_queue.enqueue(_entry(jid), path=q)
    _force_queued_at(q, {"a": "2026-07-04T10:00:02", "b": "2026-07-04T10:00:00",
                         "c": "2026-07-04T10:00:01"})
    assert apply_queue.claim(claimed_by="w", path=q)["job_posting_id"] == "b"
    assert apply_queue.claim(claimed_by="w", path=q)["job_posting_id"] == "c"
    assert apply_queue.claim(claimed_by="w", path=q)["job_posting_id"] == "a"


def test_claim_sets_progress_fields(tmp_path):
    q = _q(tmp_path)
    apply_queue.enqueue(_entry("1"), path=q)
    got = apply_queue.claim(claimed_by="subagent-3", path=q)
    assert got["status"] == "in_progress"
    assert got["attempts"] == 1
    assert got["claimed_by"] == "subagent-3"
    assert got["started_at"]
    # persisted, not just returned
    stored = apply_queue.load(q)["jobs"][0]
    assert stored["status"] == "in_progress" and stored["attempts"] == 1


def test_claim_skips_tailoring_and_terminal(tmp_path):
    q = _q(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry("t", status="tailoring"), path=q)
    apply_queue.enqueue(_entry("done"), path=q)
    apply_queue.claim(path=q)
    apply_queue.finish("done", "ready_to_submit", path=q)
    assert apply_queue.claim(path=q) is None   # nothing claimable left


def test_claim_empty_queue_returns_none(tmp_path):
    assert apply_queue.claim(path=_q(tmp_path)) is None


# --- set_artifacts: fills paths, flips tailoring -> queued ----------------------

def test_set_artifacts_fills_and_flips_tailoring_to_queued(tmp_path):
    q = _q(tmp_path)
    apply_queue.enqueue(apply_queue.new_entry("1", status="tailoring"), path=q)
    got = apply_queue.set_artifacts(
        "1", {"folder": "C:/x", "resume_pdf": "C:/x/r.pdf", "apply_md": "C:/x/apply.md"},
        path=q)
    assert got["status"] == "queued"
    assert got["queued_at"]
    assert got["artifacts"]["folder"] == "C:/x"
    assert got["artifacts"]["resume_pdf"] == "C:/x/r.pdf"
    assert got["artifacts"]["cover_letter_pdf"] == ""      # untouched keys stay
    # now claimable
    assert apply_queue.claim(path=q)["job_posting_id"] == "1"


def test_set_artifacts_ignores_unknown_keys_and_keeps_status(tmp_path):
    q = _q(tmp_path)
    apply_queue.enqueue(_entry("1"), path=q)
    got = apply_queue.set_artifacts("1", {"folder": "C:/y", "evil": "x"}, path=q)
    assert got["status"] == "queued"           # already queued: no flip needed
    assert "evil" not in got["artifacts"]


# --- hand-edited entries: mutations tolerate missing sub-keys ---------------------

def _write_minimal_entry(q, status="queued"):
    q.write_text(json.dumps({"version": 1, "jobs": [
        {"job_posting_id": "h1", "status": status}]}), encoding="utf-8")


def test_mutations_tolerate_minimal_hand_edited_entry(tmp_path):
    q = _q(tmp_path)
    _write_minimal_entry(q)
    got = apply_queue.set_artifacts("h1", {"folder": "C:/x"}, path=q)   # no KeyError
    assert got["artifacts"]["folder"] == "C:/x"
    got = apply_queue.add_missing("h1", "Salary?", path=q)
    assert got["missing_answers"] == [
        {"question": "Salary?", "context": "", "suggestion": ""}]
    got = apply_queue.update("h1", ats={"account_status": "existing"}, path=q)
    assert got["ats"]["account_status"] == "existing"
    got = apply_queue.finish("h1", "ready_to_submit", record="C:/x/rec.md", path=q)
    assert got["artifacts"]["application_record"] == "C:/x/rec.md"
    # the persisted entry now carries the full schema
    stored = apply_queue.load(q)["jobs"][0]
    for key in ("company", "title", "apply_url", "is_easy_apply", "batch_id",
                "attempts", "claimed_by", "notes", "tab_note", "missing_answers",
                "artifacts", "ats", "queued_at", "started_at", "finished_at",
                "updated_at"):
        assert key in stored, key


def test_claim_tolerates_minimal_hand_edited_entry(tmp_path):
    q = _q(tmp_path)
    _write_minimal_entry(q)
    got = apply_queue.claim(claimed_by="w", path=q)
    assert got["job_posting_id"] == "h1"
    assert got["status"] == "in_progress"
    assert got["attempts"] == 1
    assert got["artifacts"] == {k: "" for k in apply_queue.ARTIFACT_KEYS}


# --- update / add_missing / finish ----------------------------------------------

def test_update_sets_only_given_fields(tmp_path):
    q = _q(tmp_path)
    apply_queue.enqueue(_entry("1"), path=q)
    got = apply_queue.update("1", notes="hello", tab_note="url | title",
                             ats={"account_status": "existing"}, path=q)
    assert got["notes"] == "hello"
    assert got["tab_note"] == "url | title"
    assert got["ats"]["account_status"] == "existing"
    assert got["ats"]["system"] == "greenhouse"    # merge, not replace
    assert got["company"] == "Acme"                # untouched


def test_update_unknown_job_raises(tmp_path):
    with pytest.raises(apply_queue.UnknownJobError):
        apply_queue.update("nope", notes="x", path=_q(tmp_path))


def test_add_missing_appends_items(tmp_path):
    q = _q(tmp_path)
    apply_queue.enqueue(_entry("1"), path=q)
    apply_queue.add_missing("1", "Desired salary?", context="page 2",
                            suggestion="leave blank", path=q)
    apply_queue.add_missing("1", "Clearance?", path=q)
    got = apply_queue.load(q)["jobs"][0]
    assert got["missing_answers"] == [
        {"question": "Desired salary?", "context": "page 2", "suggestion": "leave blank"},
        {"question": "Clearance?", "context": "", "suggestion": ""},
    ]


def test_finish_sets_terminal_fields(tmp_path):
    q = _q(tmp_path)
    apply_queue.enqueue(_entry("1"), path=q)
    apply_queue.claim(path=q)
    got = apply_queue.finish("1", "ready_to_submit",
                             tab_note="https://x/review | Review your application",
                             record="C:/x/application_record.md", path=q)
    assert got["status"] == "ready_to_submit"
    assert got["finished_at"]
    assert got["tab_note"].startswith("https://x/review")
    assert got["artifacts"]["application_record"] == "C:/x/application_record.md"


def test_finish_rejects_non_terminal_status(tmp_path):
    q = _q(tmp_path)
    apply_queue.enqueue(_entry("1"), path=q)
    for bad in ("queued", "in_progress", "tailoring", "nonsense"):
        with pytest.raises(ValueError):
            apply_queue.finish("1", bad, path=q)


# --- requeue ---------------------------------------------------------------------

def _finished_entry(q):
    apply_queue.enqueue(_entry("1"), path=q)
    apply_queue.claim(claimed_by="w", path=q)
    apply_queue.add_missing("1", "Salary?", path=q)
    apply_queue.finish("1", "needs_human", tab_note="left at page 3", path=q)


def test_requeue_clears_the_right_fields_keeps_attempts(tmp_path):
    q = _q(tmp_path)
    _finished_entry(q)
    got = apply_queue.requeue("1", path=q)
    assert got["status"] == "queued"
    assert got["missing_answers"] == []
    assert got["finished_at"] == ""
    assert got["tab_note"] == ""
    assert got["claimed_by"] == ""
    assert got["attempts"] == 1        # kept
    assert got["queued_at"]            # re-entered the FIFO


def test_requeue_refresh_hook_called_with_folder(tmp_path, monkeypatch):
    q = _q(tmp_path)
    _finished_entry(q)
    apply_queue.set_artifacts("1", {"folder": str(tmp_path / "gen")}, path=q)
    calls = []
    from resume_tailor import apply_data
    monkeypatch.setattr(apply_data, "refresh_standard_answers",
                        lambda folder: calls.append(Path(folder)) or None)
    apply_queue.requeue("1", refresh_answers=True, path=q)
    assert calls == [tmp_path / "gen"]


def test_requeue_refresh_hook_failure_is_tolerated(tmp_path, monkeypatch):
    q = _q(tmp_path)
    _finished_entry(q)
    apply_queue.set_artifacts("1", {"folder": str(tmp_path / "gen")}, path=q)

    def boom(folder):
        raise RuntimeError("store on fire")

    from resume_tailor import apply_data
    monkeypatch.setattr(apply_data, "refresh_standard_answers", boom)
    got = apply_queue.requeue("1", refresh_answers=True, path=q)  # must not raise
    assert got["status"] == "queued"


# --- remove / clear_finished / stats ----------------------------------------------

def test_remove_deletes_and_unknown_raises(tmp_path):
    q = _q(tmp_path)
    apply_queue.enqueue(_entry("1"), path=q)
    apply_queue.remove("1", path=q)
    assert apply_queue.load(q)["jobs"] == []
    with pytest.raises(apply_queue.UnknownJobError):
        apply_queue.remove("1", path=q)


def test_clear_finished_removes_only_terminal(tmp_path):
    q = _q(tmp_path)
    for jid in ("a", "b", "c"):
        apply_queue.enqueue(_entry(jid), path=q)
    apply_queue.claim(path=q)
    apply_queue.finish("a", "failed", path=q)
    removed = apply_queue.clear_finished(path=q)
    assert removed == 1
    assert [j["job_posting_id"] for j in apply_queue.load(q)["jobs"]] == ["b", "c"]


def test_stats_counts_per_status(tmp_path):
    q = _q(tmp_path)
    for jid in ("a", "b", "c"):
        apply_queue.enqueue(_entry(jid), path=q)
    apply_queue.claim(path=q)
    apply_queue.finish("a", "failed", path=q)
    s = apply_queue.stats(path=q)
    assert s["queued"] == 2
    assert s["failed"] == 1
    assert s["in_progress"] == 0
    assert s["total"] == 3


# --- lock contention ----------------------------------------------------------------

def _hold_lock(q, held, release):
    with apply_queue.locked(q):
        held.set()
        release.wait(timeout=10)


def test_two_thread_contention_raises_queue_lock_timeout(tmp_path):
    q = _q(tmp_path)
    held, release = threading.Event(), threading.Event()
    t = threading.Thread(target=_hold_lock, args=(q, held, release), daemon=True)
    t.start()
    assert held.wait(timeout=5)
    start = time.monotonic()
    try:
        with pytest.raises(apply_queue.QueueLockTimeout):
            with apply_queue.locked(q, timeout=0.3):
                pass
        assert time.monotonic() - start < 5   # honored the short timeout, not the 5s default
    finally:
        release.set()
        t.join(timeout=5)
    # lock is free again afterwards
    with apply_queue.locked(q, timeout=1.0):
        pass


def test_mutation_under_held_lock_times_out(tmp_path, monkeypatch):
    q = _q(tmp_path)
    apply_queue.enqueue(_entry("1"), path=q)
    monkeypatch.setattr(apply_queue, "LOCK_TIMEOUT", 0.3)
    held, release = threading.Event(), threading.Event()
    t = threading.Thread(target=_hold_lock, args=(q, held, release), daemon=True)
    t.start()
    assert held.wait(timeout=5)
    try:
        with pytest.raises(apply_queue.QueueLockTimeout):
            apply_queue.update("1", notes="blocked", path=q)
    finally:
        release.set()
        t.join(timeout=5)


# --- build_context --------------------------------------------------------------------

def test_build_context_reads_master_email_and_config(tmp_path, monkeypatch):
    from resume_tailor import assets, config as rt_config
    monkeypatch.setattr(assets, "load_master",
                        lambda: {"basics": {"email": "cand@example.com"}})
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"auto_apply_inbox_url": "https://inbox.example",
                               "auto_apply_batch_cap": 4}), encoding="utf-8")
    monkeypatch.setattr(apply_queue, "CONFIG_JSON", cfg)
    monkeypatch.setattr(rt_config, "OUTPUT_ROOT", tmp_path / "Generated_Resumes")
    ctx = apply_queue.build_context(path=_q(tmp_path))
    assert ctx == {
        "signup_email": "cand@example.com",
        "inbox_url": "https://inbox.example",
        "batch_cap": 4,
        "output_root": str(tmp_path / "Generated_Resumes"),
        "queue_path": str(_q(tmp_path)),
    }


def test_build_context_defaults_when_config_keys_absent(tmp_path, monkeypatch):
    from resume_tailor import assets
    monkeypatch.setattr(assets, "load_master", lambda: {"basics": {}})
    monkeypatch.setattr(apply_queue, "CONFIG_JSON", tmp_path / "missing.json")
    ctx = apply_queue.build_context(path=_q(tmp_path))
    assert ctx["inbox_url"] == "https://mail.google.com"
    assert ctx["batch_cap"] == 10
    assert ctx["signup_email"] == ""


def test_build_context_tolerates_garbage_batch_cap(tmp_path, monkeypatch):
    from resume_tailor import assets
    monkeypatch.setattr(assets, "load_master", lambda: {"basics": {}})
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"auto_apply_batch_cap": "lots"}), encoding="utf-8")
    monkeypatch.setattr(apply_queue, "CONFIG_JSON", cfg)
    assert apply_queue.build_context(path=_q(tmp_path))["batch_cap"] == 10


# --- CLI ---------------------------------------------------------------------------------

def test_cli_enqueue_list_claim_finish_stats_roundtrip(tmp_path, capsys):
    q = str(_q(tmp_path))
    rc = apply_queue.main(["enqueue", "--queue", q, "--job-id", "9", "--company",
                           "Acme", "--title", "Engineer", "--url",
                           "https://jobs.lever.co/acme/9", "--batch-id", "b1"])
    assert rc == 0
    rc = apply_queue.main(["list", "--queue", q])
    assert rc == 0
    out = capsys.readouterr().out
    assert "9" in out and "queued" in out and "Acme" in out

    rc = apply_queue.main(["claim", "--queue", q, "--json", "--by", "agent-1"])
    assert rc == 0
    got = json.loads(capsys.readouterr().out)
    assert got["job_posting_id"] == "9"
    assert got["status"] == "in_progress"
    assert got["claimed_by"] == "agent-1"

    rc = apply_queue.main(["add-missing", "--queue", q, "9",
                           "--question", "Salary?", "--suggestion", "skip"])
    assert rc == 0
    rc = apply_queue.main(["finish", "--queue", q, "9", "--status",
                           "ready_to_submit", "--tab-note", "https://x | Review"])
    assert rc == 0
    rc = apply_queue.main(["stats", "--queue", q])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ready_to_submit" in out and "1" in out


def test_cli_update_requeue_remove(tmp_path, capsys):
    q = str(_q(tmp_path))
    apply_queue.main(["enqueue", "--queue", q, "--job-id", "9"])
    assert apply_queue.main(["update", "--queue", q, "9", "--notes", "n",
                             "--ats-account-status", "created"]) == 0
    stored = apply_queue.load(Path(q))["jobs"][0]
    assert stored["notes"] == "n" and stored["ats"]["account_status"] == "created"
    apply_queue.main(["claim", "--queue", q])
    apply_queue.main(["finish", "--queue", q, "9", "--status", "failed"])
    assert apply_queue.main(["requeue", "--queue", q, "9"]) == 0
    assert apply_queue.load(Path(q))["jobs"][0]["status"] == "queued"
    assert apply_queue.main(["remove", "--queue", q, "9"]) == 0
    assert apply_queue.load(Path(q))["jobs"] == []


def test_cli_unknown_job_id_exits_2(tmp_path, capsys):
    q = str(_q(tmp_path))
    assert apply_queue.main(["finish", "--queue", q, "nope", "--status", "failed"]) == 2
    assert apply_queue.main(["update", "--queue", q, "nope", "--notes", "x"]) == 2
    assert apply_queue.main(["remove", "--queue", q, "nope"]) == 2


def test_cli_claim_empty_queue_exits_4(tmp_path, capsys):
    assert apply_queue.main(["claim", "--queue", str(_q(tmp_path))]) == 4


def test_cli_lock_timeout_exits_3(tmp_path, capsys, monkeypatch):
    q = _q(tmp_path)
    apply_queue.main(["enqueue", "--queue", str(q), "--job-id", "1"])
    monkeypatch.setattr(apply_queue, "LOCK_TIMEOUT", 0.3)
    held, release = threading.Event(), threading.Event()
    t = threading.Thread(target=_hold_lock, args=(q, held, release), daemon=True)
    t.start()
    assert held.wait(timeout=5)
    try:
        assert apply_queue.main(["update", "--queue", str(q), "1", "--notes", "x"]) == 3
    finally:
        release.set()
        t.join(timeout=5)


def test_cli_claim_json_survives_cp1252_pipe(tmp_path, monkeypatch):
    # On this machine sys.stdout.encoding is cp1252 when stdout is a pipe —
    # exactly how the SP4 agent invokes every verb. Before the reconfigure fix,
    # claim persisted the mutation then crashed with UnicodeEncodeError (exit 1,
    # outside the 0/2/3/4 contract) and the agent never saw the entry it owns.
    q = _q(tmp_path)
    title = "✅ Data Engineer → NYC"        # ✅ … → : not in cp1252
    apply_queue.enqueue(apply_queue.new_entry("9", company="Acme", title=title),
                        path=q)
    out_buf, err_buf = io.BytesIO(), io.BytesIO()
    monkeypatch.setattr(sys, "stdout",
                        io.TextIOWrapper(out_buf, encoding="cp1252"))
    monkeypatch.setattr(sys, "stderr",
                        io.TextIOWrapper(err_buf, encoding="cp1252"))
    rc = apply_queue.main(["claim", "--queue", str(q), "--json"])
    sys.stdout.flush()
    assert rc == 0
    got = json.loads(out_buf.getvalue().decode("utf-8"))
    assert got["title"] == title
    assert got["status"] == "in_progress"
    # `list` prints the same title, and no longer crashes either
    assert apply_queue.main(["list", "--queue", str(q)]) == 0
    sys.stdout.flush()
    assert out_buf.getvalue().decode("utf-8").count(title) == 2


def test_cli_unexpected_error_exits_1_one_line(tmp_path, monkeypatch, capsys):
    def boom(*a, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(apply_queue, "load", boom)
    assert apply_queue.main(["list", "--queue", str(_q(tmp_path))]) == 1
    err = capsys.readouterr().err
    assert "apply_queue: error: RuntimeError: disk on fire" in err
    assert "Traceback" not in err
    assert len(err.strip().splitlines()) == 1


def test_cli_context_json_has_five_keys_no_password(tmp_path, capsys, monkeypatch):
    from resume_tailor import assets
    monkeypatch.setattr(assets, "load_master",
                        lambda: {"basics": {"email": "cand@example.com"}})
    monkeypatch.setattr(apply_queue, "CONFIG_JSON", tmp_path / "missing.json")
    assert apply_queue.main(["context", "--queue", str(_q(tmp_path)), "--json"]) == 0
    ctx = json.loads(capsys.readouterr().out)
    assert set(ctx) == {"signup_email", "inbox_url", "batch_cap", "output_root",
                        "queue_path"}
    assert "password" not in json.dumps(ctx).lower()
