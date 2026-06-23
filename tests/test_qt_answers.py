"""SP8: the Qt Apply Answers editor — load/collect round-trip, filter, validate, revert."""
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


def test_needs_review_filter_hides_but_never_drops(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed(store, [
        {"id": "a", "question": "Q active", "answer": "x", "kind": "fixed", "status": "active"},
        {"id": "b", "question": "Q review", "answer": "y", "kind": "open-ended",
         "status": "needs-review"},
    ])
    ed = _editor(qtbot, store)
    ed.filter_check.setChecked(True)            # show needs-review only
    hidden = [r for r in ed.rows if r["frame"].isHidden()]
    assert len(hidden) == 1                      # the active row is hidden
    assert len(ed.collect()) == 2               # ...but filter never drops on collect


def test_add_row_and_validate(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed(store, [])
    ed = _editor(qtbot, store)
    row = ed.add_row()
    row["question"].setText("New question")
    row["answer"].setText("New answer")
    assert ed.validate() == []                  # a complete row is valid


def test_revert_restores_snapshot(qtbot, tmp_path):
    store = tmp_path / "apply_answers.json"
    _seed(store, [{"id": "q1", "question": "Q", "answer": "orig",
                   "kind": "fixed", "status": "active"}])
    ed = _editor(qtbot, store)
    ed.rows[0]["answer"].setText("changed")
    ed.save()
    ed.revert()
    assert ed.rows[0]["answer"].text() == "orig"
