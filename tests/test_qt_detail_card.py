"""Cycle 40 Phase 3b/3c: jobsdata.job_detail_fields + the JobDetailCard."""
import pandas as pd
from PySide6 import QtCore, QtWidgets

import jobsdata
from qt import theme
from qt.detail_card import JobDetailCard


def _row(**over):
    base = {
        "job_posting_id": "1", "job_title": "AI Engineer",
        "company_name": "Riverstone", "job_location": "Boston, MA",
        "url": "https://x/1", "score": "5", "deep_score": "8.5",
        "recommendation": "apply", "applicants": "4",
        "job_base_pay_range": "$105k–$135k", "job_posted_date": "2026-07-11T00:00",
        "reason": "great fit", "strengths": "LLM pipeline|Python depth",
        "gaps": "No fintech", "job_summary": "A summary long enough to clear the "
        "40-character bar, so it is the card's JD when no description survives.",
    }
    base.update(over)
    return pd.Series(base)


# ---- job_detail_fields ------------------------------------------------------------

def test_job_detail_fields_structured_output():
    f = jobsdata.job_detail_fields(_row())
    assert f["title"] == "AI Engineer" and f["company"] == "Riverstone"
    assert f["score"] == "5" and f["deep_score"] == "8.5"
    assert f["strengths"] == ["LLM pipeline", "Python depth"]
    assert f["gaps"] == ["No fintech"]
    assert f["posted"] == "2026-07-11"           # date-only, like the segments
    assert f["salary"] == "$105k–$135k"
    assert f["jd"].startswith("A summary")
    assert f["snapshot_only"] is False


def test_job_detail_fields_snapshot_only_and_empty():
    assert jobsdata.job_detail_fields(None) == {}
    f = jobsdata.job_detail_fields(None, {"job_title": "Old Role",
                                          "company": "GoneCo", "url": "https://x"})
    assert f["snapshot_only"] is True
    assert f["title"] == "Old Role" and f["company"] == "GoneCo"
    assert "tracker snapshot" in f["note"]


def test_job_detail_fields_segments_stay_untouched():
    # The card's dict feed rides ALONGSIDE job_detail_segments (test-coupled).
    segs = jobsdata.job_detail_segments(_row())
    assert any("Riverstone" in t for t, _s in segs)


# ---- the JD field: precedence, no truncation --------------------------------------

def test_job_detail_fields_prefers_description_over_a_long_summary():
    # LinkedIn's job_summary is often a truncated stub even when it is long
    # enough to clear the 40-char bar, so the real posting wins.
    f = jobsdata.job_detail_fields(_row(
        job_description_formatted="<p>The real posting, with every requirement "
                                  "spelled out at length.</p>"))
    assert f["jd"].startswith("The real posting")
    assert "A summary" not in f["jd"]


def test_job_detail_fields_prefers_formatted_over_plain_description():
    f = jobsdata.job_detail_fields(_row(
        job_description_formatted="<p>Formatted wins the precedence race here.</p>",
        job_description="Plain description that is also long enough to qualify."))
    assert f["jd"].startswith("Formatted wins")


def test_job_detail_fields_uses_plain_description_when_formatted_is_missing():
    f = jobsdata.job_detail_fields(_row(
        job_description="Plain description that is also long enough to qualify."))
    assert f["jd"].startswith("Plain description")


def test_job_detail_fields_stub_description_falls_through_to_summary():
    f = jobsdata.job_detail_fields(_row(job_description_formatted="<p>See site.</p>"))
    assert f["jd"].startswith("A summary")


def test_job_detail_fields_keeps_the_whole_description():
    body = "\n".join(f"<p>Requirement number {i} spelled out in full.</p>"
                     for i in range(200))
    f = jobsdata.job_detail_fields(_row(job_description_formatted=body))
    assert len(f["jd"]) > 5000
    assert "…" not in f["jd"]
    assert "Requirement number 0 spelled out in full." in f["jd"]
    assert "Requirement number 199 spelled out in full." in f["jd"]


def test_job_detail_fields_blank_row_yields_empty_jd():
    f = jobsdata.job_detail_fields(pd.Series({"job_title": "", "job_summary": "",
                                              "job_description_formatted": ""}))
    assert f["jd"] == ""


# ---- html_to_text -----------------------------------------------------------------

def test_html_to_text_breaks_lines_on_block_tags():
    # A block that both closes and opens gets a blank line between it and the
    # next one — that is the paragraph spacing, not an accident.
    out = jobsdata.html_to_text("one<br>two<p>three</p><div>four</div><h2>five</h2>")
    assert out == "one\ntwo\nthree\n\nfour\n\nfive"


def test_html_to_text_keeps_a_lead_in_off_the_first_bullet():
    # "Responsibilities:<ul><li>" has no closing tag between the two, so the
    # opening <ul> has to break the line as well.
    out = jobsdata.html_to_text("Responsibilities:<ul><li>ship it</li></ul>")
    assert out == "Responsibilities:\n• ship it"


def test_html_to_text_marks_list_items_with_bullets():
    out = jobsdata.html_to_text("<ul><li>alpha</li><li>beta</li></ul>")
    assert out.splitlines() == ["• alpha", "• beta"]


def test_html_to_text_unescapes_entities_after_stripping_tags():
    assert jobsdata.html_to_text("R&amp;D &gt; sales") == "R&D > sales"
    # an escaped tag in the source must stay inert text, not become live markup
    assert jobsdata.html_to_text("literal &lt;b&gt;bold&lt;/b&gt;") == "literal <b>bold</b>"


def test_html_to_text_collapses_blank_runs_and_trims():
    out = jobsdata.html_to_text("<p>a   </p><br><br><br><br><p>b</p>")
    assert out == "a\n\nb"


def test_html_to_text_passes_plain_text_through():
    plain = "No markup here.\n\nJust two paragraphs of ordinary text."
    assert jobsdata.html_to_text(plain) == plain
    assert jobsdata.html_to_text("") == ""


# ---- html_to_text: non-content elements, list tightness, indentation ---------------

def test_html_to_text_drops_non_content_elements_with_their_text():
    # Stripping the tags alone leaves the chrome's own words in the posting.
    for tag in ("script", "style", "button", "icon", "svg", "nav",
                "header", "footer", "noscript", "form", "select"):
        out = jobsdata.html_to_text(f"<p>keep me</p><{tag}>drop me</{tag}>")
        assert out == "keep me", f"<{tag}> survived: {out!r}"


def test_html_to_text_drops_an_element_whose_opening_tag_spans_lines():
    # LinkedIn wraps class lists across newlines inside the opening tag.
    raw = ('<p>keep me</p>\n'
           '<button class="show-more-less-html__button show-more-less-button\n'
           '        show-more-less-html__button--less\n'
           '        ml-0.5" data-tracking-control-name="public_jobs_show-less-html-btn"\n'
           ' aria-label="Show less" aria-expanded="true">\n'
           '            Show less\n'
           '          <icon class="show-more-less-button-icon"></icon>\n'
           '    </button>')
    assert jobsdata.html_to_text(raw) == "keep me"


def test_html_to_text_leaves_an_unclosed_opener_to_the_tag_strip():
    # A bare <icon/> with no closer must not swallow the rest of the document.
    out = jobsdata.html_to_text("<p>before</p><icon/><p>after</p>")
    assert out == "before\n\nafter"
    out = jobsdata.html_to_text("<p>before</p><button aria-label='x'><p>after</p>")
    assert "before" in out and "after" in out


def test_html_to_text_never_deletes_prose_between_mismatched_openers():
    # The teeth of the rule above: a self-closing opener must not pair with a
    # LATER element's closer, and an unclosed <button> must not reach the next
    # button's `</button>`. Leaking a word of chrome beats deleting the prose
    # in between, which the reader cannot even notice.
    out = jobsdata.html_to_text(
        "<p>before</p><icon/><p>KEEP</p><icon>x</icon><p>after</p>")
    assert "KEEP" in out and "x" not in out.split("KEEP")[1]
    out = jobsdata.html_to_text(
        "<p>A</p><button>Show more<p>MIDDLE</p><button>Show less</button><p>END</p>")
    assert "MIDDLE" in out and "END" in out


def test_html_to_text_drop_list_is_tag_exact_and_case_insensitive():
    # `<selection>` is not `<select>`, and postings arrive in any casing.
    assert jobsdata.html_to_text("<selection>keep me</selection>") == "keep me"
    assert jobsdata.html_to_text("<p>keep</p><BUTTON>drop</BUTTON>") == "keep"
    # a `>` inside a quoted attribute value is still inside the opening tag
    assert jobsdata.html_to_text('<p>keep</p><button title="a>b">drop</button>') == "keep"


def test_html_to_text_tightens_bullets_split_across_sibling_lists():
    # 278 master rows put every bullet in its OWN <ul>, so the `</ul><ul>` pair
    # — not a `</li><li>` one — is what separated them with a blank line.
    out = jobsdata.html_to_text("<ul><li>a</li></ul><ul><li>b</li></ul>")
    assert out.splitlines() == ["• a", "• b"]


def test_html_to_text_tightens_a_long_run_of_one_bullet_lists():
    raw = "".join(f"<ul><li>perk {i}</li></ul>" for i in range(4))
    out = jobsdata.html_to_text(raw)
    assert out.splitlines() == [f"• perk {i}" for i in range(4)]


def test_html_to_text_keeps_the_blank_line_between_a_bullet_and_a_paragraph():
    # Only a blank line BETWEEN TWO BULLETS closes up — the gap that sets a list
    # off from the prose around it is what makes the list readable.
    out = jobsdata.html_to_text("<ul><li>a</li></ul><p>After the list</p>")
    assert out.splitlines() == ["• a", "", "After the list"]


def test_html_to_text_keeps_the_blank_line_between_a_paragraph_and_a_bullet():
    out = jobsdata.html_to_text("<p>Before the list</p><ul><li>a</li></ul>")
    assert out.splitlines() == ["Before the list", "", "• a"]


def test_html_to_text_flattens_a_nested_list_without_blank_lines():
    out = jobsdata.html_to_text("<ul><li>a<ul><li>b</li><li>c</li></ul></li></ul>")
    assert out.splitlines() == ["• a", "• b", "• c"]


def test_html_to_text_keeps_bullets_in_one_list_adjacent():
    # `</li>\n<li>` used to yield a blank line between every bullet.
    out = jobsdata.html_to_text("<ul><li>a</li>\n<li>b</li>\n<li>c</li></ul>")
    assert out.splitlines() == ["• a", "• b", "• c"]


def test_html_to_text_separates_a_list_from_the_paragraph_around_it():
    out = jobsdata.html_to_text(
        "<p>Lead in</p><ul><li>a</li><li>b</li></ul><p>After</p>")
    assert out.splitlines() == ["Lead in", "", "• a", "• b", "", "After"]


def test_html_to_text_normalizes_source_indentation():
    raw = "<div>\n            indented body line\n</div><p>\n    another\n</p>"
    assert jobsdata.html_to_text(raw) == "indented body line\n\nanother"


def test_html_to_text_strips_the_linkedin_show_more_wrapper():
    # The real wrapper shape: 2,675 of 2,678 master rows ended in this chrome.
    raw = ('<section class="show-more-less-html" data-max-lines="5">\n'
           '        <div class="show-more-less-html__markup\n'
           '            relative overflow-hidden">\n'
           '          The posting body, which must survive.\n'
           '          <ul><li>alpha</li>\n<li>beta</li></ul>\n'
           '        </div>\n'
           '    <button class="show-more-less-html__button" '
           'data-tracking-control-name="public_jobs_show-more-html-btn" '
           'aria-label="Show more" aria-expanded="false">\n'
           '<!---->\n        \n            Show more\n          \n\n'
           '          <icon class="show-more-less-button-icon" '
           'data-delayed-url="https://static.licdn.com/x"></icon>\n'
           '    </button>\n  \n\n'
           '    <button class="show-more-less-html__button--less" '
           'aria-label="Show less" aria-expanded="true">\n'
           '<!---->\n            Show less\n          \n'
           '          <icon class="show-more-less-button-icon"></icon>\n'
           '    </button>\n  \n<!---->    </section>')
    out = jobsdata.html_to_text(raw)
    assert "Show more" not in out and "Show less" not in out
    assert "The posting body, which must survive." in out
    assert out.splitlines()[-2:] == ["• alpha", "• beta"]


# ---- JobDetailCard ------------------------------------------------------------------

def test_card_renders_fields_to_plain_text(qtbot):
    card = JobDetailCard()
    qtbot.addWidget(card)
    card.set_fields(jobsdata.job_detail_fields(_row()), jid="1")
    text = card.toPlainText()
    for expected in ("AI Engineer", "Riverstone", "great fit",
                     "LLM pipeline", "No fintech", "https://x/1"):
        assert expected in text
    assert not card._empty.isVisible() or card._empty.isHidden()


def test_card_empty_state(qtbot):
    card = JobDetailCard()
    qtbot.addWidget(card)
    assert card.toPlainText() == ""
    assert card._content.isHidden()
    card.set_fields(jobsdata.job_detail_fields(_row()), jid="1")
    assert not card._content.isHidden()
    card.set_empty()
    assert card.toPlainText() == "" and card._content.isHidden()


def test_card_description_collapsed_behind_toggle(qtbot):
    # Locked user decision: the JD stays, collapsed by default.
    card = JobDetailCard()
    qtbot.addWidget(card)
    card.set_fields(jobsdata.job_detail_fields(_row()), jid="1")
    assert card.desc_view.isHidden()
    assert card.desc_toggle.text() == "Show description"
    card.desc_toggle.setChecked(True)
    assert not card.desc_view.isHidden()
    assert card.desc_toggle.text() == "Hide description"
    # a new selection re-collapses it
    card.set_fields(jobsdata.job_detail_fields(_row(job_posting_id="2")), jid="2")
    assert card.desc_view.isHidden()
    assert card.desc_toggle.text() == "Show description"


def test_card_description_toggle_hides_when_there_is_no_jd(qtbot):
    card = JobDetailCard()
    qtbot.addWidget(card)
    card.set_fields(jobsdata.job_detail_fields(_row()), jid="1")
    assert not card.desc_toggle.isHidden()
    card.set_fields({"title": "T", "company": "C", "jd": ""}, jid="2")
    assert card.desc_toggle.isHidden()
    # and the view cannot be forced open with nothing to show
    card.desc_toggle.setChecked(True)
    assert card.desc_view.isHidden()


def test_card_description_view_is_a_read_only_plain_text_edit(qtbot):
    card = JobDetailCard()
    qtbot.addWidget(card)
    assert isinstance(card.desc_view, QtWidgets.QPlainTextEdit)
    assert card.desc_view.isReadOnly()
    # still selectable/copyable — read-only must not mean inert
    flags = card.desc_view.textInteractionFlags()
    assert flags & QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
    assert flags & QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard
    # wrapped, own vertical scrollbar, never a horizontal one
    assert card.desc_view.lineWrapMode() == QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth
    assert (card.desc_view.horizontalScrollBarPolicy()
            == QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    assert (card.desc_view.verticalScrollBarPolicy()
            == QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    # not dressed as an editable form field
    assert card.desc_view.frameShape() == QtWidgets.QFrame.Shape.NoFrame
    assert card.desc_view.cursorWidth() == 0


def test_card_description_holds_the_entire_jd_verbatim(qtbot):
    body = "\n".join(f"<p>Requirement number {i} spelled out in full.</p>"
                     for i in range(200))
    fields = jobsdata.job_detail_fields(_row(job_description_formatted=body))
    assert len(fields["jd"]) > 5000            # SP1 stopped truncating
    card = JobDetailCard()
    qtbot.addWidget(card)
    card.set_fields(fields, jid="1")
    shown = card.desc_view.toPlainText()
    assert shown == fields["jd"]
    assert "Requirement number 0 spelled out in full." in shown     # head
    assert shown.rstrip().endswith("Requirement number 199 spelled out in full.")
    assert "…" not in shown


def test_card_description_preserves_line_breaks_and_bullets(qtbot):
    jd = jobsdata.html_to_text(
        "<p>Responsibilities:</p><ul><li>ship it</li><li>own it</li></ul>")
    card = JobDetailCard()
    qtbot.addWidget(card)
    card.set_fields({"title": "T", "company": "C", "jd": jd}, jid="1")
    shown = card.desc_view.toPlainText()
    assert shown.splitlines() == ["Responsibilities:", "", "• ship it", "• own it"]
    assert "\n• " in shown


def test_card_collapsed_size_is_not_inflated_by_the_hidden_view(qtbot):
    # A 120px minimum on a HIDDEN widget still inflates a layout if it is set
    # carelessly (retainSizeWhenHidden, or a minimum parked on the card).
    # Measure the content layout, not card.sizeHint(): the card's own outer
    # layout caches heightForWidth and an unshown test window never flushes it.
    card = JobDetailCard()
    qtbot.addWidget(card)
    card.set_fields(jobsdata.job_detail_fields(_row()), jid="1")
    floor = round(120 * theme._current_scale)
    assert card.desc_view.minimumHeight() == floor
    lay = card._content_layout
    item = lay.itemAt(card._desc_at)

    def measure():
        lay.invalidate()
        return lay.sizeHint().height(), lay.minimumSize().height()

    collapsed = measure()
    assert item.isEmpty()                      # contributes nothing at all
    assert item.sizeHint().height() == 0 and item.minimumSize().height() == 0
    card.desc_toggle.setChecked(True)
    expanded = measure()
    assert not item.isEmpty()
    assert item.minimumSize().height() == floor
    assert expanded[0] >= collapsed[0] + floor and expanded[1] >= collapsed[1] + floor
    card.desc_toggle.setChecked(False)
    assert measure() == collapsed              # no hole left behind
    # and a new job leaves the card at its compact size too
    card.set_fields(jobsdata.job_detail_fields(_row(job_posting_id="2")), jid="2")
    assert measure() == collapsed


def test_card_description_takes_the_slack_only_while_expanded(qtbot):
    # The spacer above the toggle and the view trade stretch on toggle, so
    # expanding grows the description instead of squashing STRENGTHS/GAPS,
    # and collapsing does not leave a hole where the view was.
    card = JobDetailCard()
    qtbot.addWidget(card)
    card.set_fields(jobsdata.job_detail_fields(_row()), jid="1")
    lay, spacer_at, view_at = card._content_layout, card._spacer_at, card._desc_at
    assert (lay.stretch(spacer_at), lay.stretch(view_at)) == (1, 0)
    card.desc_toggle.setChecked(True)
    assert (lay.stretch(spacer_at), lay.stretch(view_at)) == (0, 1)
    card.desc_toggle.setChecked(False)
    assert (lay.stretch(spacer_at), lay.stretch(view_at)) == (1, 0)


def test_card_buttons_fire_callbacks(qtbot):
    fired = []
    card = JobDetailCard(on_open=lambda jid: fired.append(("open", jid)),
                         on_tailor=lambda: fired.append(("tailor",)),
                         on_apply=lambda: fired.append(("apply",)))
    qtbot.addWidget(card)
    card.set_fields(jobsdata.job_detail_fields(_row()), jid="1")
    card.open_btn.click()
    card.tailor_btn.click()
    card.apply_btn.click()
    assert fired == [("open", "1"), ("tailor",), ("apply",)]


def test_card_tracker_variant_swaps_actions_and_lede(qtbot):
    card = JobDetailCard()
    qtbot.addWidget(card)
    tracker = {"status": "applied", "applied_date": "2026-07-04", "days": "8",
               "follow_up": "DUE", "next_step": "No reply in 8 days — follow up."}
    card.set_fields(jobsdata.job_detail_fields(_row()), jid="1", tracker=tracker)
    assert card.tailor_btn.isHidden() and card.apply_btn.isHidden()
    assert not card.resume_btn.isHidden() and not card.followup_btn.isHidden()
    text = card.toPlainText()
    assert "NEXT STEP" in text and "follow up" in text.lower()
    assert "8 days since applying" in text
    # discovery mode restores the Tailor/Apply pair
    card.set_fields(jobsdata.job_detail_fields(_row()), jid="1")
    assert not card.tailor_btn.isHidden() and not card.apply_btn.isHidden()
    assert card.resume_btn.isHidden() and card.followup_btn.isHidden()
