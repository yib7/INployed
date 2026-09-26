"""The Qt Apply Answers editor: load/collect round-trip, validate, revert.

Cycle 13 retired the needs-review status: the editor has no status dropdown and no
needs-review filter; every row is saved active. A legacy needs-review entry still
loads and round-trips to active.

Cycle 18 (SP1) moved the store to version 2 (typed, confirmed answers). Until SP4
rebuilds this tab, `collect()` carries each loaded entry's other fields through, a
new row is text, and a changed answer is confirmed; a damaged store shows its
error and is never saved over. `_seed` writes a version 1 file, which the store
migrates in memory on load.
"""
import json

from PySide6 import QtWidgets

from qt.answers_tab import AnswersEditor
from resume_tailor import apply_answers


def _seed(path, entries):
    """A version 1 store on disk (no "version" key); load migrates it."""
    path.write_text(json.dumps({"answers": entries}), encoding="utf-8")


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


def test_collect_keeps_kind_only_when_the_loaded_entry_had_one(qtbot, tmp_path):
    # SP1 fix round 1, item 8: a v1-migrated row still carries its legacy
    # "kind", but a brand-new row (and a v2 entry, which never had one) does not.
    store = tmp_path / "apply_answers.json"
    _seed(store, [{"id": "q1", "question": "Legacy Q", "answer": "a",
                   "kind": "fixed", "status": "active"}])
    ed = _editor(qtbot, store)
    row = ed.add_row()
    row["question"].setText("New question")
    row["answer"].setText("New answer")
    legacy, new = ed.collect()
    assert legacy["kind"] == "fixed"
    assert "kind" not in new


def test_revert_does_nothing_on_a_damaged_store_and_keeps_the_bak(qtbot, tmp_path):
    # SP1 fix round 1, item 1: reverting a damaged store must never touch the
    # one good backup sitting next to it.
    store = tmp_path / "apply_answers.json"
    bak = store.with_name(store.name + ".bak")
    bak.write_text("the good copy", encoding="utf-8")
    store.write_text("not json{", encoding="utf-8")
    ed = _editor(qtbot, store)
    assert ed.load_error
    ed.revert()
    assert "damaged" in ed.status.text()
    assert bak.read_text(encoding="utf-8") == "the good copy"
    assert store.read_text(encoding="utf-8") == "not json{"


# --- cycle 18 (SP1): the version 2 bridge ---------------------------------------------

def _v2_store(path):
    entries = [{"id": "work_authorized",
                "question": apply_answers.BUILTINS["work_authorized"].question,
                "type": "yes_no", "answer": "Yes", "note": "I am a US citizen",
                "confirmed": False, "status": "active", "extra_key": [1, 2]},
               {"id": "github", "question": "What is your GitHub?", "type": "text",
                "answer": "https://github.com/x", "note": "", "confirmed": True,
                "status": "active"}]
    apply_answers.save(entries, path)
    return entries


def test_collect_carries_the_typed_fields_through(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _v2_store(store)
    ed = _editor(qtbot, store)
    auth, github = ed.collect()
    assert auth["type"] == "yes_no" and auth["note"] == "I am a US citizen"
    assert auth["extra_key"] == [1, 2]                  # an unknown key survives
    assert auth["confirmed"] is False                   # unchanged answers keep their flag
    assert github["confirmed"] is True and github["type"] == "text"


def test_a_changed_answer_is_confirmed(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _v2_store(store)
    ed = _editor(qtbot, store)
    ed.rows[0]["answer"].setText("No")
    assert ed.save() is True
    saved = apply_answers.load(store)[0]
    assert (saved["answer"], saved["confirmed"], saved["type"]) == ("No", True, "yes_no")
    assert saved["note"] == "I am a US citizen"


def test_a_new_row_is_a_confirmed_text_answer(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed(store, [])
    ed = _editor(qtbot, store)
    row = ed.add_row()
    row["question"].setText("What is your GitHub?")
    row["answer"].setText("https://github.com/x")
    (entry,) = ed.collect()
    assert entry["type"] == "text" and entry["confirmed"] is True
    assert ed.save() is True


def test_saving_a_migrated_store_keeps_its_review_list(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed(store, [{"id": "work_authorized", "question": "Work auth?",
                   "answer": "Yes, I am a US citizen", "kind": "fixed", "status": "active"}])
    ed = _editor(qtbot, store)
    assert ed.save() is True
    data = json.loads(store.read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert [r["before"] for r in data["review"]] == ["Yes, I am a US citizen"]


def test_a_damaged_store_shows_its_error_and_is_never_saved_over(qtbot, tmp_path, monkeypatch):
    store = tmp_path / "apply_answers.json"
    store.write_text("not json{", encoding="utf-8")
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                        lambda *a, **k: shown.append(a[2]))
    ed = _editor(qtbot, store)                          # opens without raising
    assert ed.rows == []
    assert "damaged" in ed.status.text() and str(store) in ed.status.text()
    assert ed.save() is False
    assert shown and "damaged" in shown[0]
    assert store.read_text(encoding="utf-8") == "not json{"


def test_validate_button_reports_the_load_error_on_a_damaged_store(qtbot, tmp_path, monkeypatch):
    # SP1 fix round 1, item 7: an empty row list validates clean, so a damaged
    # store used to show "Looks good" and hide the real problem.
    store = tmp_path / "apply_answers.json"
    store.write_text("not json{", encoding="utf-8")
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                        lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: shown.append(a[2]))
    ed = _editor(qtbot, store)
    ed._validate_clicked()
    assert shown and "damaged" in shown[0]
    assert not any("Looks good" in s for s in shown)
    assert "damaged" in ed.status.text()
