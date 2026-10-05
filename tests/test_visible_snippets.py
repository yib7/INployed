"""The page scripts' visibility test has one text per form (`apply_form_js`).

Every script that defines `visible` takes it from `VISIBLE_FN_JS` (any box)
or `VISIBLE_AREA_FN_JS` (width and height), so the two rules cannot drift
apart copy by copy, and no splice token is left in a script a page runs.
"""
import apply_fill
import apply_form_js
import apply_linkedin

ANY = [apply_form_js._EXTRACT_JS, apply_form_js._VALIDITY_JS, apply_form_js._SCAN_JS,
       apply_fill._SNAPSHOT_JS, apply_fill._READY_JS, apply_fill._TYPEAHEAD_OPTIONS_JS]
AREA = [apply_form_js._CONSENT_CONTROL_JS, apply_linkedin._READ_JS, apply_linkedin._CONTINUE_JS]


def test_the_two_rules_differ_only_in_the_box_test():
    box_any, box_area = "r.width > 0 || r.height > 0", "r.width > 0 && r.height > 0"
    assert box_any in apply_form_js.VISIBLE_FN_JS
    assert apply_form_js.VISIBLE_FN_JS.replace(box_any, box_area) == apply_form_js.VISIBLE_AREA_FN_JS


def test_every_copy_is_the_snippet_and_no_token_is_left():
    for js in ANY:
        assert f"const visible = {apply_form_js.VISIBLE_FN_JS};" in js
    for js in AREA:
        assert f"const visible = {apply_form_js.VISIBLE_AREA_FN_JS};" in js
    for js in ANY + AREA:
        assert "__VISIBLE" not in js
