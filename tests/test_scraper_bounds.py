"""Bounding controls for scraper.py — cap keywords and per-input limit so a
verification run can't fire thousands of billed Bright Data collections."""
import scraper


def test_max_keywords_caps_inputs():
    inputs = scraper.build_inputs([], max_keywords=2)
    keywords = {i["keyword"] for i in inputs}
    assert len(keywords) == 2
    # one input per (kept keyword x remote type)
    assert len(inputs) == 2 * len(scraper.REMOTE_TYPES)


def test_no_cap_uses_all_keywords():
    inputs = scraper.build_inputs([])
    assert len(inputs) == len(scraper.KEYWORDS) * len(scraper.REMOTE_TYPES)


def test_max_keywords_larger_than_list_is_safe():
    inputs = scraper.build_inputs([], max_keywords=10_000)
    assert len(inputs) == len(scraper.KEYWORDS) * len(scraper.REMOTE_TYPES)


def test_exclude_ids_threaded_into_each_input():
    inputs = scraper.build_inputs(["123", "456"], max_keywords=1)
    assert all(i["jobs_to_not_include"] == ["123", "456"] for i in inputs)
