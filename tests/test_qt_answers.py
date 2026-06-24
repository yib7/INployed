"""The Qt Apply Answers editor — load/collect round-trip, validate, revert.

Cycle 13 retired the needs-review status: the editor has no status dropdown and no
needs-review filter; every row is saved active. A legacy needs-review entry still
loads and round-trips to active.
"""
from qt.answers_tab import AnswersEditor
from resume_tailor import apply_answers


def _seed(path, entries):
    apply_answers.save(entries, path)


def _editor(qtbot, path):
    ed = AnswersEditor(store_path=path)
    qtbot.addWidget(ed)
    return ed


def test_load_and_collect_round_trip(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed(store, [{"id": "race", "question": "Race?", "answer": "Decline",
                   "kind": "fixed", "status": "active"}])
    ed = _editor(qtbot, store)
    assert len(ed.rows) == 1
    ed.rows[0]["answer"].setText("Prefer not to say")
    out = ed.collect()
    assert out[0]["answer"] == "Prefer not to say"


def test_save_persists(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    _seed(store, [{"id": "q1", "question": "Work auth?", "answer": "Yes",
                   "kind": "fixed", "status": "active"}])
    ed = _editor(qtbot, store)
    ed.rows[0]["answer"].setText("Authorized")
    assert ed.save() is True
    reloaded = apply_answers.load(store)
    assert any(e["answer"] == "Authorized" for e in reloaded)


def test_editor_has_no_needs_review_controls(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed(store, [{"id": "a", "question": "Q", "answer": "x",
                   "kind": "fixed", "status": "active"}])
    ed = _editor(qtbot, store)
    assert not hasattr(ed, "filter_check")       # no needs-review filter
    assert "status" not in ed.rows[0]            # no per-row status dropdown
    assert all(e["status"] == "active" for e in ed.collect())


def test_collect_migrates_legacy_needs_review_to_active(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed(store, [{"id": "b", "question": "Q review", "answer": "y",
                   "kind": "open-ended", "status": "needs-review"}])
    ed = _editor(qtbot, store)
    assert len(ed.rows) == 1                      # the legacy row still loads
    out = ed.collect()
    assert out[0]["status"] == "active"          # ...and is migrated to active


def test_add_row_and_validate(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed(store, [])
    ed = _editor(qtbot, store)
    row = ed.add_row()
    row["question"].setText("New question")
    row["answer"].setText("New answer")
    assert ed.validate() == []                  # a complete row is valid


def test_default_store_editor_shows_full_standard_set(qtbot, tmp_path, monkeypatch):
    # The live tab (no explicit store_path) merges in any missing seeded defaults
    # (e.g. the new address_* fields) so the user can fill them.
    store = tmp_path / "apply_answers.json"
    _seed(store, [{"id": "how_did_you_hear", "question": "How?", "answer": "LinkedIn",
                   "kind": "open-ended", "status": "active"}])
    monkeypatch.setattr(apply_answers, "STORE_PATH", store)
    ed = AnswersEditor()  # no store_path -> live default store, merges defaults
    qtbot.addWidget(ed)
    ids = {r["id"] for r in ed.rows}
    assert "address_street" in ids and "address_country" in ids


def test_explicit_store_editor_is_exact(qtbot, tmp_path):
    # An explicit store_path (tests/tools) is loaded verbatim — no default merge.
    store = tmp_path / "apply_answers.json"
    _seed(store, [{"id": "only", "question": "Q", "answer": "a",
                   "kind": "fixed", "status": "active"}])
    ed = AnswersEditor(store_path=store)
    qtbot.addWidget(ed)
    assert len(ed.rows) == 1


def test_revert_restores_snapshot(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed(store, [{"id": "q1", "question": "Q", "answer": "orig",
                   "kind": "fixed", "status": "active"}])
    ed = _editor(qtbot, store)
    ed.rows[0]["answer"].setText("changed")
    ed.save()
    ed.revert()
    assert ed.rows[0]["answer"].text() == "orig"
