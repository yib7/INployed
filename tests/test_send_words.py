"""The send-word rule's one source (`apply_send_words`).

Every pattern and every page-script regex source built from the word lists
is pinned here to its text, so a change to a list shows up as one red line
per copy it reaches, and no copy can drift from the lists.
"""
import apply_fill
import apply_form
import apply_judge
import apply_send_words as sw


def test_the_python_patterns_are_built_from_the_word_lists():
    assert sw.SUBMIT_WORDS.pattern == r"\b(submit|apply|send|finish)\b"
    assert sw.FINAL_WORDS.pattern == r"\b(complete|confirm|finali[sz]e|done)\b"
    assert sw.SEND_WORDS.pattern == r"\b(submit|send|finish)\b"
    assert sw._POPUP_VERB.pattern == r"\b(submit|send|finish|complete|confirm|finali[sz]e|done)\b"
    assert sw._POPUP_LEAD.pattern == r"^(submit|send)$"
    assert sw._POPUP_SEND_OBJECT.pattern == (
        r"^(applications?|forms?|answers?|responses?|options?|request|submission|now|here"
        r"|everything|all|it|this|submit|send|finish|complete|confirm|finali[sz]e|done|apply)$")


def test_the_page_script_sources_are_built_from_the_word_lists():
    assert sw.js_union() == r"\\b(submit|apply|send|finish|complete|confirm|finali[sz]e|done)\\b"
    assert sw.js_submit() == r"/\b(submit|apply|send|finish)\b/i"
    assert sw.js_send_lead() == r"/^(submit|send)$/i"
    assert sw.js_send_verb() == r"/^(submit|send|finish|complete|confirm|finali[sz]e|done)$/i"
    assert sw.js_send_object() == (
        r"/^(applications?|forms?|answers?|responses?|options?|request|submission|now|here"
        r"|everything|all|it|this|submit|send|finish|complete|confirm|finali[sz]e|done|apply)$/i")


def test_every_page_script_holds_the_built_sources():
    js = apply_form._EXTRACT_JS
    assert f"const SEND_LEAD = {sw.js_send_lead()};" in js
    assert f"const SEND_VERB = {sw.js_send_verb()};" in js
    assert f"const SEND_OBJECT = {sw.js_send_object()};" in js
    assert f"submits || {sw.js_submit()}.test(text)" in js
    assert "__SEND_" not in js and "__SUBMIT_WORDS__" not in js
    assert apply_fill._SEND_JS == sw.js_union()


def test_the_old_homes_read_the_same_objects():
    assert apply_judge.SEND_WORDS is sw.SEND_WORDS
    assert apply_judge.SIGN_IN_WORDS is sw.SIGN_IN_WORDS
    assert apply_fill.PopupRefused is sw.PopupRefused
    assert apply_fill.popup_refusal is sw.popup_refusal
