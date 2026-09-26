"""SP8a: the auto-apply run's resilience (the offline half of SP8).

- Chrome's own error page (`chrome-error://chromewebdata/`, a load the
  network dropped) reads as a failed load: one retry of a GET on the allowed
  sites, never "left the allowed sites: chromewebdata". After the submit
  click a GET up to the click's first navigation is never loaded again, and
  a later one only when the answer of a send to the application's sites
  that came back led to it (its HTTP redirect, or an address with no
  query): a GET that may have carried the send and any POST never are (at
  most one send per job).

Headless Chromium through the module-scoped test browser, the flow harness
(`apply_harness.run_flow`) and the fake judge; no network but the local
server and routed hosts."""
import dataclasses
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "local"))

import apply_harness as h  # noqa: E402
import jev  # noqa: E402

pytest_plugins = ["conftest_browser"]


# --- Chrome's error page ------------------------------------------------------------------

def _drop_first(error: str = "connectionrefused", *, body: str = ""):
    """A route handler that drops the first load of its address on the
    network (`error`, as a refused connection would) and lets every later one
    through, or answers it with `body`; `.seen` lists the methods it saw."""
    seen: list[str] = []

    def _handle(route, request) -> None:
        seen.append(str(request.method))
        if len(seen) == 1:
            route.abort(error)
        elif body:
            route.fulfill(body=body, content_type="text/html")
        else:
            route.fallback()
    _handle.seen = seen
    return _handle


def _trace_text(result) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in Path(result.trace).glob("*") if p.suffix in (".json", ".log"))


def _run(f, browser, server, tmp_path):
    return h.run_flow(f, jev.FakeJev(), "fake", browser=browser, server=server,
                      workdir=tmp_path)


def test_a_destination_the_network_drops_once_is_loaded_again_and_the_run_goes_on(
        _browser, flow_server, tmp_path):
    # the matrix flake (linkedin_gts_other): LinkedIn's Apply led to the
    # company's form, whose load the network dropped
    base = h.flow("linkedin_gts_other")
    drop = _drop_first()
    f = dataclasses.replace(base, routes=lambda b: {**base.routes(b),
                                                    f"{b}/forms/lever_single.html": drop})
    r = _run(f, _browser, flow_server, tmp_path)
    assert "chromewebdata" not in r.reason, r
    assert r.ok, (r.status, r.reason)
    assert r.breaks == []
    assert drop.seen == ["GET", "GET"]
    assert "error_page_retry" in _trace_text(r)


def test_a_redirect_hop_the_network_drops_once_is_loaded_again_and_its_destination_admitted(
        _browser, flow_server, tmp_path):
    # LinkedIn's /safety/go/ hop itself dropped: its retry redirects on to the
    # company's form, which is then admitted as the application's site
    base = h.flow("linkedin_gts_other")
    hop = "https://www.linkedin.com/safety/go/**"
    drop = _drop_first(body=base.routes(flow_server.base)[hop])
    f = dataclasses.replace(base, routes=lambda b: {**base.routes(b), hop: drop})
    r = _run(f, _browser, flow_server, tmp_path)
    assert "chromewebdata" not in r.reason, r
    assert r.ok, (r.status, r.reason)
    assert r.breaks == []
    assert drop.seen == ["GET", "GET"]


def test_a_load_the_network_drops_twice_parks_as_an_error_page_inside_the_policy(
        _browser, flow_server, tmp_path):
    base = h.flow("linkedin_gts_other")
    seen: list[str] = []

    def _always(route, request) -> None:
        seen.append(str(request.method))
        route.abort("connectionreset")
    f = dataclasses.replace(base, routes=lambda b: {**base.routes(b),
                                                    f"{b}/forms/lever_single.html": _always})
    r = _run(f, _browser, flow_server, tmp_path)
    assert r.status == "needs_human", r
    assert r.reason.startswith("error or dead page: GET "), r.reason
    assert "again after one retry" in r.reason
    assert "chromewebdata" not in r.reason
    assert h.policy_park(r.status, r.reason) is True
    assert seen == ["GET", "GET"]
    assert r.breaks == []


def test_after_the_send_the_get_that_carried_it_is_never_loaded_again(
        _browser, flow_server, tmp_path):
    # the submit_code noisy-14 end: the submit's own GET (the harness counts
    # it as the send) was dropped after it left; loading it again would send
    # twice
    base = h.flow("submit_code")
    drop = _drop_first("connectionreset")
    f = dataclasses.replace(base, routes=lambda b: {"**/forms/code_gate.html": drop})
    r = _run(f, _browser, flow_server, tmp_path)
    assert r.status == "submitted", r
    assert r.reason.startswith("submitted (unconfirmed): error or dead page: GET "), r.reason
    assert "carried the send" in r.reason
    assert "chromewebdata" not in r.reason
    assert drop.seen == ["GET"]
    assert r.sends == 1
    assert r.breaks == []


_POST_SITE = "https://careers.fabrikam.example"
_POST_FORM = """<!doctype html><html><head><title>Apply for Analytics Engineer</title></head>
<body><h1>Apply for Analytics Engineer</h1><p>Fabrikam, Remote</p>
<form method="post" action="/post/submit">
<label>First name * <input name="first_name" required></label>
<label>Last name * <input name="last_name" required></label>
<label>Email * <input type="email" name="email" required></label>
<button type="submit" id="btn-submit">Submit application</button>
</form></body></html>"""


# the POST's answer sends the tab on to the thanks page (a route's own 303
# would skip the routes, so the page's script does it)
_SENT = ("<!doctype html><html><head><title>Sending</title></head><body><p>Sending</p>"
         f"<script>location.replace('{_POST_SITE}/post/thanks')</script></body></html>")


def _answer_the_post(route) -> None:
    route.fulfill(body=_SENT, content_type="text/html")


def _post_flow(submit, thanks) -> h.Flow:
    return h.Flow("post_redirect", f"{_POST_SITE}/post/apply", True, "submitted",
                  r"^confirmation page", confirm="body[data-confirmed]",
                  send_urls=("**/post/submit",),
                  routes=lambda b: {f"{_POST_SITE}/post/apply": _POST_FORM,
                                    f"{_POST_SITE}/post/submit": submit,
                                    f"{_POST_SITE}/post/thanks": thanks})


def test_after_the_send_the_page_it_led_to_is_loaded_again_and_reads_as_the_confirmation(
        _browser, flow_server, tmp_path):
    # the POST went through; the GET its answer led to was dropped
    thanks = _drop_first(body=h.CONFIRMATION_HTML)
    posts: list[str] = []

    def _submit(route, request) -> None:
        posts.append(str(request.method))
        _answer_the_post(route)
    r = _run(_post_flow(_submit, thanks), _browser, flow_server, tmp_path)
    assert r.status == "submitted", r
    assert r.ok, (r.status, r.reason)
    assert posts == ["POST"]
    assert thanks.seen == ["GET", "GET"]
    assert r.sends == 1
    assert r.breaks == []


def test_after_the_send_a_post_the_network_dropped_is_never_sent_again(
        _browser, flow_server, tmp_path):
    posts: list[str] = []

    def _submit(route, request) -> None:
        posts.append(str(request.method))
        route.abort("connectionreset")
    r = _run(_post_flow(_submit, h.CONFIRMATION_HTML), _browser, flow_server, tmp_path)
    assert r.status == "submitted", r
    assert r.reason.startswith("submitted (unconfirmed): error or dead page: POST "), r.reason
    assert "never sent again" in r.reason
    assert posts == ["POST"]
    assert r.sends == 1
    assert r.breaks == []


@pytest.mark.parametrize("error, failure", [("connectionrefused", "ERR_CONNECTION_REFUSED"),
                                            ("namenotresolved", "ERR_NAME_NOT_RESOLVED")])
def test_a_send_that_never_made_its_connection_parks_as_nothing_sent(
        _browser, flow_server, tmp_path, error, failure):
    # SP8a review M7: a refused connection or a name that did not resolve
    # means nothing left the machine: the job is never "submitted
    # (unconfirmed)", and the POST is still never sent again
    posts: list[str] = []

    def _submit(route, request) -> None:
        posts.append(str(request.method))
        route.abort(error)
    r = _run(_post_flow(_submit, h.CONFIRMATION_HTML), _browser, flow_server, tmp_path)
    assert r.status == "needs_human", r
    assert r.reason.startswith("error or dead page: POST "), r.reason
    assert failure in r.reason
    assert "nothing was sent" in r.reason
    assert h.policy_park(r.status, r.reason) is True
    assert posts == ["POST"]
    assert r.sends == 1         # the harness counts the attempt at its route
    assert r.breaks == []


def test_a_get_send_that_never_made_its_connection_parks_as_nothing_sent_and_is_not_loaded_again(
        _browser, flow_server, tmp_path):
    base = h.flow("submit_code")
    drop = _drop_first("connectionrefused")
    f = dataclasses.replace(base, routes=lambda b: {"**/forms/code_gate.html": drop})
    r = _run(f, _browser, flow_server, tmp_path)
    assert r.status == "needs_human", r
    assert r.reason.startswith("error or dead page: GET "), r.reason
    assert "nothing was sent" in r.reason
    assert h.policy_park(r.status, r.reason) is True
    assert drop.seen == ["GET"]
    assert r.breaks == []


# A GET form whose button saves a draft (a POST) before it sends: the send is
# the second request the click caused
_GET_FORM = """<!doctype html><html><head><title>Apply for Analytics Engineer</title></head>
<body><h1>Apply for Analytics Engineer</h1><p>Fabrikam, Remote</p>
<form id="f" method="get" action="/get/send">
<label>First name * <input name="first_name" required></label>
<label>Last name * <input name="last_name" required></label>
<label>Email * <input type="email" name="email" required></label>
<button type="button" id="btn-submit">Submit application</button>
</form><script>document.getElementById('btn-submit').addEventListener('click', function () {
  fetch('/get/autosave', {method: 'POST', body: 'draft'}).then(function () {
    document.getElementById('f').submit(); }); });</script></body></html>"""


def test_after_the_send_the_get_that_carried_it_is_never_loaded_again_when_a_post_went_first(
        _browser, flow_server, tmp_path):
    # SP8a review M6: every request the submit click caused counts as the
    # send, the draft's POST before it too
    send = _drop_first("connectionreset", body=h.CONFIRMATION_HTML)
    saves: list[str] = []
    f = h.Flow("get_after_autosave", f"{_POST_SITE}/get/apply", True, "submitted",
               r"^submitted \(unconfirmed\): ", send_urls=(f"{_POST_SITE}/get/send**",),
               routes=lambda b: {f"{_POST_SITE}/get/apply": _GET_FORM,
                                 f"{_POST_SITE}/get/autosave": _sink(saves, "ok"),
                                 f"{_POST_SITE}/get/send**": send})
    r = _run(f, _browser, flow_server, tmp_path)
    assert send.seen == ["GET"], (send.seen, r.status, r.reason)
    assert saves == ["POST"]
    assert r.status == "submitted", r
    assert r.reason.startswith("submitted (unconfirmed): error or dead page: GET "), r.reason
    assert "carried the send" in r.reason
    assert r.sends == 1 and r.breaks == []


# A single-page app's form: the button sends by fetch, then the page's script
# sends the tab to its thank-you page
_SPA_FORM = """<!doctype html><html><head><title>Apply for Analytics Engineer</title></head>
<body><h1>Apply for Analytics Engineer</h1><p>Fabrikam, Remote</p>
<form id="f" onsubmit="return false">
<label>First name * <input name="first_name" required></label>
<label>Last name * <input name="last_name" required></label>
<label>Email * <input type="email" name="email" required></label>
<button type="button" id="btn-submit">Submit application</button>
</form><script>document.getElementById('btn-submit').addEventListener('click', function () {
  fetch('/spa/submit', {method: 'POST', body: 'answers'}).then(function () {
    location.href = '/spa/thanks'; }); });</script></body></html>"""


def test_after_a_fetch_send_the_thank_you_page_the_script_led_to_is_never_loaded_again(
        _browser, flow_server, tmp_path):
    # SP8a review R3-I1: every GET up to the click's first navigation may be
    # the send, so the thank-you page a script loads after the fetch is never
    # loaded again (the network cannot tell it from a script GET send after
    # a draft's save): the job ends "submitted (unconfirmed)", sent once
    posts: list[str] = []
    thanks = _drop_first("connectionreset", body=h.CONFIRMATION_HTML)
    f = h.Flow("spa_fetch", f"{_POST_SITE}/spa/apply", True, "submitted",
               r"^submitted \(unconfirmed\): ", send_urls=(f"{_POST_SITE}/spa/submit",),
               routes=lambda b: {f"{_POST_SITE}/spa/apply": _SPA_FORM,
                                 f"{_POST_SITE}/spa/submit": _sink(posts, '{"ok": true}'),
                                 f"{_POST_SITE}/spa/thanks": thanks})
    r = _run(f, _browser, flow_server, tmp_path)
    assert posts == ["POST"]
    assert thanks.seen == ["GET"], (thanks.seen, r.status, r.reason)
    assert r.status == "submitted", r
    assert r.reason.startswith("submitted (unconfirmed): error or dead page: GET "), r.reason
    assert "carried the send" in r.reason
    assert r.sends == 1 and r.breaks == []


# A form whose button saves a draft on the application's own host, then sends
# the answers by a script GET to an address other than the form's action
_DRAFT_FORM = """<!doctype html><html><head><title>Apply for Analytics Engineer</title></head>
<body><h1>Apply for Analytics Engineer</h1><p>Fabrikam, Remote</p>
<form id="f" onsubmit="return false">
<label>First name * <input name="first_name" required></label>
<label>Last name * <input name="last_name" required></label>
<label>Email * <input type="email" name="email" required></label>
<button type="button" id="btn-submit">Submit application</button>
</form><script>document.getElementById('btn-submit').addEventListener('click', function () {
  fetch('/draft/save', {method: 'POST', body: 'draft'}).then(function () {
    location.href = '/draft/send?' + new URLSearchParams(new FormData(document.getElementById('f')));
  }); });</script></body></html>"""


def test_a_script_get_send_after_a_draft_save_that_came_back_is_never_loaded_again(
        _browser, flow_server, tmp_path):
    # SP8a review R3-I1: the draft's POST to the application's host came back
    # before the GET, which is still the click's own first navigation
    saves: list[str] = []
    send = _drop_first("connectionreset", body=h.CONFIRMATION_HTML)
    f = h.Flow("draft_then_get", f"{_POST_SITE}/draft/apply", True, "submitted",
               r"^submitted \(unconfirmed\): ", send_urls=(f"{_POST_SITE}/draft/send**",),
               routes=lambda b: {f"{_POST_SITE}/draft/apply": _DRAFT_FORM,
                                 f"{_POST_SITE}/draft/save": _sink(saves, '{"ok": true}'),
                                 f"{_POST_SITE}/draft/send**": send})
    r = _run(f, _browser, flow_server, tmp_path)
    assert saves == ["POST"]
    assert send.seen == ["GET"], (send.seen, r.status, r.reason, r.sends, r.breaks)
    assert r.status == "submitted", r
    assert r.reason.startswith("submitted (unconfirmed): error or dead page: GET "), r.reason
    assert "carried the send" in r.reason
    assert r.sends == 1 and r.breaks == []


# A POST form whose answer is an interstitial page: its script sends the
# answers again by a GET form on the same host
_INTER_FORM = _POST_FORM.replace('action="/post/submit"', 'action="/inter/submit"')
_INTERSTITIAL = """<!doctype html><html><head><title>One moment</title></head><body>
<p>One moment</p><form id="g" method="get" action="/inter/send">
<input type="hidden" name="first_name" value="Jane"><input type="hidden" name="last_name" value="Doe">
</form><script>document.getElementById('g').submit()</script></body></html>"""


def test_a_script_get_send_from_the_page_a_post_led_to_is_never_loaded_again(
        _browser, flow_server, tmp_path):
    # SP8a review R4-M1: after the click's first navigation a GET is loaded
    # again only when an HTTP redirect from the send led to it, or when its
    # address has no query; a GET send carries the answers in its query
    posts: list[str] = []
    send = _drop_first("connectionreset", body=h.CONFIRMATION_HTML)
    f = h.Flow("interstitial_get", f"{_POST_SITE}/inter/apply", True, "submitted",
               r"^submitted \(unconfirmed\): ", send_urls=(f"{_POST_SITE}/inter/send**",),
               routes=lambda b: {f"{_POST_SITE}/inter/apply": _INTER_FORM,
                                 f"{_POST_SITE}/inter/submit": _sink(posts, _INTERSTITIAL),
                                 f"{_POST_SITE}/inter/send**": send})
    r = _run(f, _browser, flow_server, tmp_path)
    assert posts == ["POST"]
    assert send.seen == ["GET"], (send.seen, r.status, r.reason, r.sends, r.breaks)
    assert r.status == "submitted", r
    assert r.reason.startswith("submitted (unconfirmed): error or dead page: GET "), r.reason
    assert "carried the send" in r.reason
    assert r.sends == 1 and r.breaks == []


def _closed_port() -> int:
    """A local port nothing listens on: a load of it is refused at once."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_after_a_post_send_the_page_its_http_redirect_led_to_is_loaded_again(
        _browser, flow_server, tmp_path):
    # SP8a review R4-M1: the POST's answer is a 303 to a thank-you address
    # with a query. The routes never see a redirect their own answer made, so
    # the redirected load goes to a closed local port and fails; the retry
    # is a new load, which the routes answer
    site = f"http://127.0.0.1:{_closed_port()}"
    posts: list[str] = []
    thanks: list[str] = []

    def _submit(route, request) -> None:
        posts.append(str(request.method))
        route.fulfill(status=303, headers={"location": f"{site}/redir/thanks?ref=apply"}, body="")

    def _thanks(route, request) -> None:
        thanks.append(str(request.method))
        route.fulfill(body=h.CONFIRMATION_HTML, content_type="text/html")
    f = h.Flow("post_http_redirect", f"{site}/redir/apply", True, "submitted",
               r"^confirmation page", confirm="body[data-confirmed]",
               send_urls=(f"{site}/redir/submit",),
               routes=lambda b: {
                   f"{site}/redir/apply": _POST_FORM.replace('action="/post/submit"',
                                                             'action="/redir/submit"'),
                   f"{site}/redir/submit": _submit, f"{site}/redir/thanks**": _thanks})
    r = _run(f, _browser, flow_server, tmp_path)
    assert posts == ["POST"]
    assert thanks == ["GET"], (thanks, r.status, r.reason)
    assert r.status == "submitted" and r.ok, (r.status, r.reason)
    assert r.sends == 1 and r.breaks == []
    assert "error_page_retry" in _trace_text(r)


# A form whose button posts an analytics beacon to a host `_tracking` does not
# know, then sends the answers by a script GET to an address other than the
# form's action
_BEACON = "https://collect.statfox.example/b"
_BEACON_FORM = """<!doctype html><html><head><title>Apply for Analytics Engineer</title></head>
<body><h1>Apply for Analytics Engineer</h1><p>Fabrikam, Remote</p>
<form id="f" onsubmit="return false">
<label>First name * <input name="first_name" required></label>
<label>Last name * <input name="last_name" required></label>
<label>Email * <input type="email" name="email" required></label>
<button type="button" id="btn-submit">Submit application</button>
</form><script>document.getElementById('btn-submit').addEventListener('click', function () {
  var go = function () {
    location.href = '/beacon/send?' + new URLSearchParams(new FormData(document.getElementById('f')));
  };
  fetch('%s', {method: 'POST', body: 'e=submit', mode: 'no-cors', keepalive: true}).then(go, go);
});</script></body></html>""" % _BEACON


def test_a_script_get_send_after_a_beacon_is_never_loaded_again(
        _browser, flow_server, tmp_path):
    # SP8a review R2-M4 addition and R3-I1: the GET is the click's own first
    # navigation, so it may be the send, a beacon that came back before it or not
    beacons: list[str] = []
    send = _drop_first("connectionreset", body=h.CONFIRMATION_HTML)
    f = h.Flow("beacon_then_get", f"{_POST_SITE}/beacon/apply", True, "submitted",
               r"^submitted \(unconfirmed\): ", send_urls=(f"{_POST_SITE}/beacon/send**",),
               routes=lambda b: {f"{_POST_SITE}/beacon/apply": _BEACON_FORM,
                                 _BEACON: _sink(beacons, ""),
                                 f"{_POST_SITE}/beacon/send**": send})
    r = _run(f, _browser, flow_server, tmp_path)
    assert beacons == ["POST"]
    assert send.seen == ["GET"], (send.seen, r.status, r.reason)
    assert r.status == "submitted", r
    assert r.reason.startswith("submitted (unconfirmed): error or dead page: GET "), r.reason
    assert "carried the send" in r.reason
    assert r.sends == 1 and r.breaks == []


def test_a_get_after_the_clicks_navigation_is_loaded_again_only_when_the_sends_answer_led_to_it():
    # SP8a review R2-M4 addition, R3-I1 and R4-M1: `led_on` decides only for
    # a GET after the click's first navigation (`caused` holds a POST form's
    # own navigation here): an HTTP redirect from a send that came back, or
    # an address with no query after one came back; the click's own rows
    # are always carried
    from unittest.mock import Mock

    import apply_run
    site = "https://careers.fabrikam.example"
    beacon, post, late = (Mock(redirected_from=None) for _ in range(3))
    get = Mock(url=f"{site}/thanks", redirected_from=None)
    watch = apply_run.SendWatch(Mock(), Mock())
    watch._order = [(beacon, "possible", f"POST {_BEACON}"), (post, "sent", f"POST {site}/submit"),
                    (get, "sent", f"GET {site}/thanks"), (late, "sent", f"POST {site}/late")]
    watch.caused = [f"POST {_BEACON}", f"POST {site}/submit"]
    thanks = f"GET {site}/thanks"
    assert watch.carried_get(thanks) is True            # nothing came back yet
    watch.answered |= {id(beacon), id(late)}            # another host's, and one after the GET
    assert watch.sent_left(thanks) is False
    assert watch.carried_get(thanks) is True
    get.redirected_from = beacon                        # a redirect from another host's
    assert watch.carried_get(thanks, url=f"{site}/thanks?ref=a") is True
    watch.answered.add(id(post))
    assert watch.sent_left(thanks) is True
    get.redirected_from = None
    assert watch.carried_get(thanks) is False           # no query: the site's own page
    assert watch.carried_get(thanks, url=f"{site}/thanks?first_name=Jane") is True
    get.redirected_from = post                          # the answered POST's own redirect
    assert watch.carried_get(thanks, url=f"{site}/thanks?first_name=Jane") is False
    assert watch.carried_get(thanks, f"{site}/thanks?x=1") is True     # the form's action
    assert watch.carried_get(f"GET {site}/never-seen") is True
    watch.caused.append(thanks)             # the click's first navigation after the POSTs
    assert watch.sent_left(thanks) is True
    assert watch.carried_get(thanks) is True


def test_an_answer_counts_only_for_a_request_the_watch_holds():
    # SP8a review R3-M2: the context reports every tab's answers; the id of a
    # request the watch does not hold may be reused by a later one
    from unittest.mock import Mock

    import apply_run
    held, stranger = Mock(), Mock()
    watch = apply_run.SendWatch(Mock(), Mock())
    watch._order = [(held, "sent", "POST https://careers.fabrikam.example/submit")]
    watch._finished(stranger)
    watch._answered(Mock(request=stranger))
    assert watch.answered == set()
    watch._answered(Mock(request=held))
    assert watch.answered == {id(held)}
    watch.answered.clear()
    watch._finished(held)
    assert watch.answered == {id(held)}


# --- a malformed queue entry (RES-09)-------------------------------------------------------

def _queue(tmp_path, *entries):
    """A queue file holding `entries` as written, hand-edited ones included."""
    import json

    import apply_queue
    path = tmp_path / "queue.json"
    path.write_text(json.dumps({"version": 1, "jobs": list(entries)}), encoding="utf-8")
    return path, apply_queue


def _plain(jid: str, folder: Path, **extra) -> dict:
    import apply_queue
    e = apply_queue.new_entry(jid, company="Fabrikam", title="Analytics Engineer",
                              apply_url="https://careers.fabrikam.example/apply")
    e["artifacts"]["folder"] = str(folder)
    e["artifacts"]["apply_md"] = str(folder / "apply.md")
    e.update(extra)
    return e


def _runner_for(queue, ctx, tmp_path):
    import apply_run
    return apply_run.Runner(jev=jev.FakeJev(), queue_path=queue, profile_dir=tmp_path / "p",
                            settings={"auto_apply_headless": True, "auto_apply_jev_mode": "fake"},
                            context=ctx, run_context={"inbox_url": ""}, sleep=lambda s: None)


def test_a_malformed_entry_ends_that_job_failed_and_the_drain_goes_on(_browser, tmp_path,
                                                                        monkeypatch):
    import apply_run
    monkeypatch.chdir(tmp_path)
    bad = _plain("bad", tmp_path / "bad")
    bad["artifacts"]["apply_md"] = {"path": "x"}         # hand-edited into a mapping
    good = _plain("good", tmp_path / "good")               # no apply.md: fails fast
    queue, apply_queue = _queue(tmp_path, bad, good)
    ctx = _browser.new_context()
    try:
        outcomes = _runner_for(queue, ctx, tmp_path).drain(cap=5)
    finally:
        ctx.close()
    assert [o.job_id for o in outcomes] == ["bad", "good"]
    first = outcomes[0]
    assert first.status == "failed"
    assert first.reason == (f"{apply_run.MALFORMED_REASON}: artifacts.apply_md must be text "
                            f"(got dict)")
    assert outcomes[1].reason == "no apply.md"
    jobs = {e["job_posting_id"]: e for e in apply_queue.load(queue)["jobs"]}
    assert jobs["bad"]["status"] == "failed" and jobs["good"]["status"] == "failed"
    # nothing written where the malformed path would point (the working dir)
    assert not (tmp_path / "apply_trace").exists()
    assert not list(tmp_path.glob("application_record*"))


def test_a_malformed_attempts_count_is_claimed_as_a_first_attempt(tmp_path):
    bad = _plain("bad", tmp_path / "bad", attempts="twice")
    queue, apply_queue = _queue(tmp_path, bad)
    got = apply_queue.claim("apply_run", path=queue)
    assert got["job_posting_id"] == "bad"
    assert got["status"] == "in_progress" and got["attempts"] == 1


def test_an_entry_the_run_cannot_set_up_ends_failed_and_the_drain_goes_on(_browser, tmp_path,
                                                                          monkeypatch):
    import apply_run
    queue, apply_queue = _queue(tmp_path, _plain("a", tmp_path / "a"),
                                _plain("b", tmp_path / "b"))
    real = apply_run._JobRun.__init__

    def _init(self, runner, ctx, entry):
        if entry["job_posting_id"] == "a":
            raise RuntimeError("the set-up broke at /secret/path")
        real(self, runner, ctx, entry)
    monkeypatch.setattr(apply_run._JobRun, "__init__", _init)
    ctx = _browser.new_context()
    try:
        outcomes = _runner_for(queue, ctx, tmp_path).drain(cap=5)
    finally:
        ctx.close()
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "failed"), ("b", "failed")]
    assert outcomes[0].reason == "RuntimeError while the job was set up"
    jobs = {e["job_posting_id"]: e for e in apply_queue.load(queue)["jobs"]}
    assert jobs["a"]["status"] == "failed"
    assert "secret" not in jobs["a"]["notes"]


_SET_UP_ERROR = "the set-up broke at /private/queue/path"


def test_a_job_that_cannot_be_set_up_logs_its_frames_and_retries_its_queue_write(
        _browser, tmp_path, monkeypatch, caplog):
    # SP8a review M10, M11: the frames and never the message, and the one
    # retrying queue write
    import logging

    import apply_run
    queue, apply_queue = _queue(tmp_path, _plain("a", tmp_path / "a"))

    def _init(self, runner, ctx, entry):
        raise RuntimeError(_SET_UP_ERROR)
    monkeypatch.setattr(apply_run._JobRun, "__init__", _init)
    real, tries = apply_queue.finish, []

    def _finish(job_id, *a, **kw):
        tries.append(job_id)
        if len(tries) == 1:
            raise OSError("the queue is locked")
        return real(job_id, *a, **kw)
    monkeypatch.setattr(apply_queue, "finish", _finish)
    ctx = _browser.new_context()
    sleeps: list[float] = []
    try:
        runner = _runner_for(queue, ctx, tmp_path)
        runner.sleep = sleeps.append
        with caplog.at_level(logging.INFO, logger="apply_run"):
            outcomes = runner.drain(cap=5)
    finally:
        ctx.close()
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "failed")]
    assert tries == ["a", "a"] and sleeps == [apply_run.FINISH_RETRY_S]
    assert apply_queue.load(queue)["jobs"][0]["status"] == "failed"
    assert "private" not in caplog.text and "Traceback" not in caplog.text
    assert "RuntimeError while the job was set up" in caplog.text
    assert "test_apply_resilience.py:" in caplog.text     # the frames


def test_a_malformed_entry_is_a_dead_end_inside_the_policy():
    import apply_run
    assert h.policy_park("failed", f"{apply_run.MALFORMED_REASON}: ats must be a mapping "
                                   f"(got list)") is True


@pytest.mark.parametrize("entry, problem", [
    ([1, 2], "the entry must be a mapping (got list)"),
    ({"artifacts": ["a"]}, "artifacts must be a mapping (got list)"),
    ({"ats": {"domain": 3}}, "ats.domain must be text (got int)"),
    ({"apply_url": 5}, "apply_url must be text (got int)"),
    ({"attempts": "twice"}, "attempts must be a number"),
])
def test_a_malformed_entry_names_what_each_value_must_be(entry, problem):
    # SP8a review M14: what the value must be, and its type, with no
    # contrast framing
    import apply_run
    assert apply_run.entry_problem(entry) == problem


@pytest.mark.parametrize("after", ["submit click", "code step"])
def test_a_judge_down_after_a_possible_send_is_a_dead_end_inside_the_policy(after):
    """RES-02's one park: the judge stays down once something may have been
    sent, so the job is never re-queued (at most one send per job). Any
    other stop after the submit click stays outside the policy, and the
    re-queue before a send is no park at all."""
    import apply_run
    down = f"{apply_run.JUDGE_DOWN_REASON}: _Busy 529 at fill_and_verify"
    reason = f"{apply_run.CHECK_SENT_REASON}: the run stopped after the {after} ({down})"
    assert h.policy_park("needs_human", reason) is True
    assert h.policy_park("needs_human", f"{apply_run.CHECK_SENT_REASON}: the run stopped "
                                        f"after the {after} (RuntimeError at read)") is False
    assert h.policy_park("queued", f"{down}; {apply_run.REQUEUED_NOTE}") is False


# --- an unexpected error's reason (RES-05) -----------------------------------------------------

_CALL_LOG = ("Locator.fill: Timeout 5000ms exceeded.\nCall log:\n  - waiting for "
             "locator(\"#first_name\")\n  - fill(\"Jane Doe\") on <input id=first_name>")


def test_an_unexpected_error_names_its_type_and_step_and_never_its_message(
        _browser, flow_server, tmp_path, monkeypatch):
    import apply_fill

    def _broken(*a, **kw):
        raise RuntimeError(_CALL_LOG)
    monkeypatch.setattr(apply_fill, "apply", _broken)
    r = _run(h.flow("lever_single_park"), _browser, flow_server, tmp_path)
    assert r.status == "failed", r
    assert r.reason == "RuntimeError at fill_and_verify (page 1)", r.reason
    run = (Path(r.trace) / "run.json").read_text(encoding="utf-8")
    pages = "".join(p.read_text(encoding="utf-8") for p in Path(r.trace).glob("page-*.json"))
    for text in (run, pages, r.reason):
        assert "Jane Doe" not in text and "Call log" not in text
    # the step and the code's frames in the trace and the job's log, the message nowhere
    assert '"step": "fill_and_verify"' in pages + run
    assert "apply_run.py:" in pages + run
    log = (Path(r.trace) / "job.log").read_text(encoding="utf-8")
    assert "RuntimeError at fill_and_verify" in log and "apply_run.py:" in log
    assert "Jane Doe" not in log and "Call log" not in log


# --- the judge request's size (RES-03) ----------------------------------------------------------

_COUNTRIES = [f"Country number {i} of the long list" for i in range(40)]


def _long_form(n_fields: int = 80, n_buttons: int = 30):
    """A long application: `n_fields` fields with help text, a third of them
    lists of 40 options, and `n_buttons` buttons, a third in the site's header."""
    import apply_form
    fields = []
    for n in range(n_fields):
        options = list(_COUNTRIES) if n % 3 == 0 else []
        fields.append(apply_form.Field(
            n, (0, f"#q{n}"), f"Question number {n} about your background and experience",
            "select" if options else "text", n % 2 == 0,
            help=("Tell us in your own words what you did, where, for how long and with whom; "
                  "a few lines are enough. ") * 2,
            options=options, id_or_name=f"question_{n}_answer", section=f"Part {n // 10}"))
    buttons = [apply_form.Button(n_fields + i, (0, f"#b{i}"), f"Header link {i}", chrome=i % 3 == 0)
               for i in range(n_buttons)]
    return apply_form.FormDigest("careers.fabrikam.example", "Apply for Analytics Engineer",
                                 "Apply for Analytics Engineer. " * 200, fields=fields,
                                 buttons=buttons)


def _catalog(tmp_path):
    import apply_facts
    return apply_facts.build(h.write_job_folder(tmp_path / "job"), answers=h.bank())


def test_an_80_field_form_is_asked_in_requests_that_fit_the_judges_limits(tmp_path):
    import apply_judge
    digest, catalog = _long_form(), _catalog(tmp_path)
    whole = apply_judge.page_questions(digest, catalog, {})
    assert not jev.request_fits(*whole)     # asked as one, it would not fit
    requests = apply_judge.page_requests(digest, catalog, {})
    for state, questions in requests:
        longest, total = jev.request_size(state, questions)
        assert longest <= jev.STATE_TOKENS_MAX * jev.SIZE_MARGIN, (longest, total)
        assert total <= jev.REQUEST_TOKENS_MAX * jev.SIZE_MARGIN, (longest, total)
    asked = [q for _, qs in requests for q in qs]
    # every field asked once, in the part whose `fields` holds it
    assert sorted(int(q.split("_")[1]) for q in asked if q.endswith("_source")) == list(range(80))
    for state, questions in requests:
        held = {f["n"] for f in state["fields"]}
        assert {int(q.split("_")[1]) for q in questions if q.endswith("_source")} == held
    # the page's own buttons asked once; the header's left out
    roles = sorted(int(q.split("_")[1]) for q in asked if q.endswith("_role"))
    assert roles == [b.n for b in digest.buttons if not b.chrome]


def test_a_form_that_fits_is_asked_as_it_was(tmp_path):
    import apply_judge
    digest, catalog = _long_form(12, 6), _catalog(tmp_path)
    assert apply_judge.page_requests(digest, catalog, {}) == [
        apply_judge.page_questions(digest, catalog, {})]


def test_the_parts_answers_read_as_one_and_a_yes_to_the_prohibited_question_holds():
    import apply_judge
    no = jev.Answer(kind="noul", noul=0.1)
    yes = jev.Answer(kind="noul", noul=0.8)
    pick = jev.Answer(kind="choice", choice="email", probabilities={"email": 0.9},
                      confidence=0.9)
    got = apply_judge.merge_answers([{"asks_for_prohibited": no, "field_1_source": pick},
                                     {"asks_for_prohibited": yes},
                                     {"asks_for_prohibited": no}])
    assert got == {"asks_for_prohibited": yes, "field_1_source": pick}


def test_the_run_maps_a_long_form_in_parts_and_plans_every_field(tmp_path):
    from unittest.mock import Mock

    import apply_run

    sizes: list[tuple[int, int]] = []

    class Sized(jev.FakeJev):
        def judge(self, state, questions):
            sizes.append(jev.request_size(state, questions))
            return super().judge(state, questions)
    runner = apply_run.Runner(jev=Sized(), context=Mock(), run_context={}, sleep=lambda s: None)
    run = apply_run._JobRun(runner, Mock(), {"job_posting_id": "s",
                                             "apply_url": "https://x.example/1"})
    run.catalog = _catalog(tmp_path)
    run.page = Mock(url="https://careers.fabrikam.example/apply")
    answers = run._map(_long_form(), {}, "application_form", discover=False, own_page=False)
    assert len(sizes) > 1
    for longest, total in sizes:
        assert longest <= jev.STATE_TOKENS_MAX * jev.SIZE_MARGIN
        assert total <= jev.REQUEST_TOKENS_MAX * jev.SIZE_MARGIN
    assert all(f"field_{n}_source" in answers for n in range(80))
    assert "asks_for_prohibited" in answers


def test_the_module_docstring_names_the_wall_clock_by_its_constant():
    # RES-08: the docstring once said eight minutes while the constant said 15
    import re

    import apply_run
    doc = apply_run.__doc__ or ""
    assert "JOB_WALL_CLOCK_S" in doc
    assert not re.search(r"\b(?:eight|8)[- ]minute", doc, re.I)
    assert apply_run.JOB_WALL_CLOCK_S == 15 * 60


# --- the judge's outage (RES-02) ---------------------------------------------------------------

class _Busy(Exception):
    """A service error the way the TypeSafe SDK raises one: `status`, and
    `retry_after_ms` on a 429; its message quotes the request."""

    def __init__(self, status: int, retry_after_ms: float | None = None):
        super().__init__(f"{status}: the request for Jane Doe was not answered")
        self.status = status
        self.retry_after_ms = retry_after_ms


class _Flaky:
    """Raises `errors` in turn, then answers as `inner` (the fake judge)."""

    def __init__(self, errors, inner=None):
        self.errors = list(errors)
        self.inner = inner if inner is not None else jev.FakeJev()
        self.calls = 0

    def judge(self, state, questions):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.inner.judge(state, questions)


class _Answers:
    def judge(self, state, questions):
        return {q: jev.Answer(kind="noul", noul=0.9) for q in questions}


def test_a_busy_judge_is_tried_again_after_its_retry_after_and_answers():
    sleeps: list[float] = []
    inner = _Flaky([_Busy(429, retry_after_ms=20_000), _Busy(503), ConnectionError("reset")],
                   inner=_Answers())
    guarded = jev.Guarded(inner, sleep=sleeps.append)
    got = guarded.judge({"page": "x"}, {"q": {"type": "noul", "instructions": "x"}})
    assert got["q"].noul == 0.9
    # the service's 20 s over the first 5 s delay, then the run's own 15 s and 40 s
    assert sleeps == [20.0, 15.0, 40.0] and inner.calls == 4
    assert guarded.down == ""


def test_a_judge_that_stays_down_opens_the_breaker_and_later_requests_fail_at_once():
    sleeps: list[float] = []
    inner = _Flaky([_Busy(529)] * 9)
    guarded = jev.Guarded(inner, sleep=sleeps.append)
    with pytest.raises(jev.JudgeOutage) as got:
        guarded.judge({}, {"q": {"type": "noul", "instructions": "x"}})
    assert got.value.kind == "_Busy 529" and "Jane" not in str(got.value)
    assert sleeps == list(jev.RETRY_DELAYS_S) and inner.calls == 4
    assert guarded.down == "_Busy 529"
    with pytest.raises(jev.JudgeOutage):
        guarded.judge({}, {"q": {"type": "noul", "instructions": "x"}})
    assert inner.calls == 4 and len(sleeps) == 3      # no request while it is open


def test_a_refused_key_or_a_long_retry_after_opens_the_breaker_without_a_wait():
    # C review M5: a retired or renamed model (404, 410) is refused like a key
    for error in (_Busy(401), _Busy(403), _Busy(404), _Busy(410),
                  _Busy(429, retry_after_ms=600_000)):
        sleeps: list[float] = []
        guarded = jev.Guarded(_Flaky([error]), sleep=sleeps.append)
        with pytest.raises(jev.JudgeOutage):
            guarded.judge({}, {})
        assert sleeps == [] and guarded.down == f"_Busy {error.status}"
        assert guarded.refused is (error.status != 429), error.status


def test_a_request_the_service_rejects_passes_through_without_a_retry():
    sleeps: list[float] = []
    guarded = jev.Guarded(_Flaky([_Busy(400), ValueError("a bug")]), sleep=sleeps.append)
    for kind in (_Busy, ValueError):
        with pytest.raises(kind):
            guarded.judge({}, {})
    assert sleeps == [] and guarded.down == ""


def test_the_guard_counts_the_requests_the_judge_answered():
    # SP8a review R2-I1: an outage counts toward a job's cap only after the
    # judge answered in the drain, so the guard keeps the count
    guarded = jev.Guarded(_Flaky([_Busy(503)], inner=_Answers()), sleep=lambda s: None)
    assert guarded.answers == 0
    guarded.judge({}, {"q": {"type": "noul", "instructions": "x"}})
    guarded.judge({}, {"q": {"type": "noul", "instructions": "x"}})
    assert guarded.answers == 2
    down = jev.Guarded(_Flaky([_Busy(529)] * 9), sleep=lambda s: None)
    with pytest.raises(jev.JudgeOutage):
        down.judge({}, {})
    assert down.answers == 0


@pytest.mark.parametrize("errors, fault", [
    ([_Busy(500)] * 4, True), ([_Busy(502)] * 4, True), ([_Busy(408)] * 4, True),
    ([TimeoutError()] * 4, True),
    ([ConnectionError("reset")] * 4, False), ([ConnectionResetError()] * 4, False),
    ([_Busy(529)] * 4, False), ([_Busy(503)] * 4, False), ([_Busy(429)] * 4, False),
    ([_Busy(409)] * 4, False), ([_Busy(500, retry_after_ms=600_000)], False),
    ([_Busy(529), _Busy(500), _Busy(500), _Busy(500)], False), ([_Busy(401)], False)])
def test_only_an_error_a_request_can_cause_is_the_requests_fault(errors, fault):
    # SP8a review R3-M1 and R4-M2: a 5xx other than 503 and 529 or a timeout
    # on every try; never a busy or overloaded service, a dropped connection
    # (most often the network's), a long Retry-After or a refused key
    guarded = jev.Guarded(_Flaky(errors), sleep=lambda s: None)
    assert guarded.request_fault is False
    with pytest.raises(jev.JudgeOutage):
        guarded.judge({}, {})
    assert guarded.request_fault is fault


def test_a_new_drain_starts_the_answer_count_over(tmp_path):
    import apply_run
    guarded = jev.Guarded(_Answers(), sleep=lambda s: None)
    guarded.judge({}, {"q": {"type": "noul", "instructions": "x"}})
    runner = apply_run.Runner(jev=guarded, queue_path=tmp_path / "queue.json",
                              context=object(), run_context={}, drain_report=False)
    guarded.down, guarded.refused, guarded.request_fault = "_Busy 500", False, True
    assert runner.drain(cap=1) == []
    assert (guarded.answers, guarded.down, guarded.request_fault) == (0, "", False)


def test_a_runner_holds_its_judge_behind_the_guard_whoever_sets_it():
    import apply_run
    judge = jev.FakeJev()
    runner = apply_run.Runner(jev=judge, context=object(), run_context={})
    assert isinstance(runner.jev, jev.Guarded) and runner.jev.inner is judge
    other = _Flaky([])
    runner.jev = other
    assert runner.jev.inner is other and runner.jev.calls == 0     # its attributes read through


def test_a_judge_busy_for_a_while_lets_the_run_go_on(_browser, flow_server, tmp_path):
    flaky = _Flaky([_Busy(529), _Busy(429, retry_after_ms=1_000)])
    r = h.run_flow(h.flow("lever_single_park"), flaky, "fake", browser=_browser,
                   server=flow_server, workdir=tmp_path)
    assert r.ok and r.breaks == [], (r.status, r.reason)
    assert flaky.calls > 2


def _two_jobs(judges, browser, server, tmp_path, sleeps, ids=("a", "b")):
    """Drains of two queued jobs (or `ids`) on the fixture form, one drain
    under each of `judges` in turn: each drain's outcomes, the queue's
    entries by id after the last, and the tabs left open. Each job's
    company is "Fabrikam", its id upper-cased after it ("Fabrikam B")."""
    import apply_queue
    import apply_run
    queue = tmp_path / "queue.json"
    url = f"{server.base}/forms/lever_single.html"
    with h.hermetic(tmp_path), h.fast_timing():
        for jid in ids:
            folder = h.write_job_folder(tmp_path / jid)
            apply_queue.enqueue(apply_queue.new_entry(jid, company=f"Fabrikam {jid.upper()}",
                                                      title="Analytics Engineer", apply_url=url),
                                path=queue)
            apply_queue.set_artifacts(jid, {"folder": str(folder),
                                            "apply_md": str(folder / "apply.md"),
                                            "resume_pdf": str(folder / "Jane_Doe_Resume.pdf")},
                                      path=queue)
        ctx = browser.new_context()
        h.offline(ctx)
        runs = []
        try:
            runner = apply_run.Runner(
                jev=judges[0], queue_path=queue, profile_dir=tmp_path / "p",
                settings={"auto_apply_headless": True, "auto_apply_jev_mode": "fake",
                          "auto_apply_submit": False},
                context=ctx, run_context={"inbox_url": ""}, sleep=sleeps.append,
                drain_report=False)
            for judge in judges:
                runner.jev = judge
                runs.append(runner.drain(cap=5))
            left = [p for p in ctx.pages if not p.is_closed()]
        finally:
            ctx.close()
    return runs, {e["job_posting_id"]: e for e in apply_queue.load(queue)["jobs"]}, left


def test_a_judge_that_stays_down_hands_the_job_back_and_stops_the_drain(
        _browser, flow_server, tmp_path):
    import json

    import apply_run
    sleeps: list[float] = []
    (outcomes,), jobs, left = _two_jobs([_Flaky([_Busy(529)] * 20)], _browser, flow_server,
                                        tmp_path, sleeps)
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "queued")], outcomes
    reason = outcomes[0].reason
    assert reason.startswith(f"{apply_run.JUDGE_DOWN_REASON}: _Busy 529 at "), reason
    assert reason.endswith(apply_run.REQUEUED_NOTE) and "Jane" not in reason
    assert outcomes[0].judge_down
    assert sleeps == list(jev.RETRY_DELAYS_S)
    # the job is back in the queue as if never claimed, the next one untouched
    a, b = jobs["a"], jobs["b"]
    assert (a["status"], a["attempts"], a["claimed_by"], a["started_at"]) == ("queued", 0, "", "")
    assert a["notes"] == reason
    # the judge answered nothing in the drain: a global outage, no job's
    # doing, counts toward no cap, and the job goes behind the next (R2-I1)
    assert a.get("outages", 0) == 0
    assert (b["status"], b["attempts"]) == ("queued", 0)
    assert a["queued_at"] >= b["queued_at"]
    # no park: no tab left open, no record; the trace ends with the reason
    assert left == []
    assert not list((tmp_path / "a").glob("application_record*"))
    run = json.loads((tmp_path / "a" / "apply_trace" / "attempt-1" / "run.json")
                     .read_text(encoding="utf-8"))
    assert (run["status"], run["reason"]) == ("queued", reason)
    assert "re-queued 1" in apply_run.summary_line(outcomes)


@pytest.mark.parametrize("status", [401, 404])
def test_a_refused_key_hands_the_job_back_with_its_attempt_counted(
        _browser, flow_server, tmp_path, status):
    # SP8a review M1: only an error the service may get over gives the attempt
    # back; a refused key is no outage of this job's, so none is counted.
    # C review M5: a retired model (404) stops the drain the same way, so it
    # never fails every job in the batch
    import apply_run
    sleeps: list[float] = []
    (outcomes,), jobs, _ = _two_jobs([_Flaky([_Busy(status)] * 20)], _browser, flow_server,
                                     tmp_path, sleeps)
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "queued")], outcomes
    assert outcomes[0].reason.endswith(apply_run.KEY_REFUSED_NOTE), outcomes[0].reason
    assert outcomes[0].judge_down and sleeps == []
    a, b = jobs["a"], jobs["b"]
    assert (a["status"], a["attempts"], a.get("outages", 0)) == ("queued", 1, 0)
    assert (b["status"], b["attempts"]) == ("queued", 0)


class _DownFor:
    """The fake judge, except that a request about a job of `companies`
    fails with `status` (a 500 by default, a failure that job's own request
    causes, SP8a review R3-M1), or with what `error` makes."""

    def __init__(self, companies, status: int = 500, error=None):
        self.companies = set(companies)
        self.status = status
        self.error = error
        self.inner = jev.FakeJev()

    def judge(self, state, questions):
        if str((state.get("job") or {}).get("company") or "") in self.companies:
            raise self.error() if self.error is not None else _Busy(self.status)
        return self.inner.judge(state, questions)


def test_a_judge_down_for_every_job_parks_none_and_the_queue_turns(
        _browser, flow_server, tmp_path):
    # SP8a review R2-I1: a long outage for everyone is no job's doing: drain
    # after drain each job goes back with nothing counted, behind the next
    sleeps: list[float] = []
    runs, jobs, _ = _two_jobs([_Flaky([_Busy(529)] * 20) for _ in range(4)], _browser,
                              flow_server, tmp_path, sleeps)
    assert [[(o.job_id, o.status) for o in run] for run in runs] == [
        [("a", "queued")], [("b", "queued")], [("a", "queued")], [("b", "queued")]], runs
    assert all(o.judge_down for run in runs for o in run)
    for e in jobs.values():
        assert (e["status"], e["attempts"], e.get("outages", 0)) == ("queued", 0, 0), e


def test_a_job_whose_own_request_downs_the_judge_twice_parks_and_the_queue_moves_on(
        _browser, flow_server, tmp_path):
    # SP8a review M1 and R2-I1: the judge answers the other jobs and fails on
    # this one's request in two drains: it parks at the cap; without the cap
    # it would stop every drain once it reached the queue's head
    import apply_run
    sleeps: list[float] = []
    judge = _DownFor({"Fabrikam B"})
    runs, jobs, _ = _two_jobs([judge, judge, judge], _browser, flow_server, tmp_path, sleeps,
                              ids=("a", "b", "c"))
    first, second, third = runs
    assert [(o.job_id, o.status) for o in first] == [("a", "ready_to_submit"),
                                                     ("b", "queued")], first
    assert [(o.job_id, o.status) for o in second] == [("c", "ready_to_submit"),
                                                      ("b", "needs_human")], second
    reason = second[1].reason
    assert reason.startswith(f"{apply_run.JUDGE_DOWN_REASON}: _Busy 500 at "), reason
    assert reason.endswith(apply_run.OUTAGES_PARKED), reason
    assert h.policy_park("needs_human", reason) is True
    assert second[1].judge_down                  # the drain stopped all the same
    assert third == []
    assert (jobs["b"]["status"], jobs["b"]["attempts"], jobs["b"]["outages"]) == (
        "needs_human", 1, 1)


@pytest.mark.parametrize("error", [lambda: _Busy(529), lambda: _Busy(503), lambda: _Busy(429),
                                   lambda: ConnectionError("reset")],
                         ids=["529", "503", "429", "dropped"])
def test_a_busy_service_or_a_dropped_connection_under_the_same_job_twice_never_parks_it(
        _browser, flow_server, tmp_path, error):
    # SP8a review R3-M1 and R4-M2: a busy or overloaded service, or a
    # network that drops the connection, is no request's doing, even when the
    # judge answered the other jobs: the job goes back each time with no
    # outage counted
    sleeps: list[float] = []
    judge = _DownFor({"Fabrikam B"}, error=error)
    runs, jobs, _ = _two_jobs([judge, judge, judge], _browser, flow_server, tmp_path, sleeps,
                              ids=("a", "b", "c"))
    assert [[(o.job_id, o.status) for o in run] for run in runs] == [
        [("a", "ready_to_submit"), ("b", "queued")],
        [("c", "ready_to_submit"), ("b", "queued")],
        [("b", "queued")]], runs
    assert all(run[-1].judge_down for run in runs)
    b = jobs["b"]
    assert (b["status"], b["attempts"], b.get("outages", 0)) == ("queued", 0, 0), b


def test_the_harness_accepts_a_cap_park_only_for_a_requests_error_after_an_answer():
    # SP8a review R2-I1 and R3-M1: the cap's park is a dead end only after
    # the judge answered in the drain, and only for an error a request can
    # cause; a park in a global outage or a busy service is outside the policy
    import apply_run

    def cap(kind: str, after: str = " after 3 answers in this drain") -> bool:
        return h.policy_park("needs_human", f"{apply_run.JUDGE_DOWN_REASON}: {kind} at fill"
                                            f"{after}; {apply_run.OUTAGES_PARKED}")
    assert cap("_Busy 500", "") is False
    assert cap("_Busy 500", " after 0 answers in this drain") is False
    assert cap("_Busy 500") is True
    assert cap("_Busy 502", " after 1 answer in this drain") is True
    assert cap("_Busy 408") is True and cap("TimeoutError") is True
    for status in (529, 503, 429, 409, 425):
        assert cap(f"_Busy {status}") is False, status
    # R4-M2: a dropped connection is most often the network's
    for kind in ("ConnectionError", "ConnectionResetError", "BrokenPipeError"):
        assert cap(kind) is False, kind


def test_a_request_the_judge_rejects_ends_that_job_and_the_drain_goes_on(
        _browser, flow_server, tmp_path):
    # SP8a review M1: a 400 (or a request too large for the service) is the
    # request's own fault: that job fails with the status named, the next runs
    sleeps: list[float] = []
    (outcomes,), jobs, _ = _two_jobs([_Flaky([_Busy(400)])], _browser, flow_server,
                                     tmp_path, sleeps)
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "failed"),
                                                        ("b", "ready_to_submit")], outcomes
    assert outcomes[0].reason.startswith("_Busy 400 at "), outcomes[0].reason
    assert "Jane" not in outcomes[0].reason
    assert not outcomes[0].judge_down and sleeps == []
    assert (jobs["a"]["status"], jobs["a"]["attempts"]) == ("failed", 1)


def test_an_outage_at_the_grounding_gate_reaches_the_run():
    import types

    import apply_answergen

    class Down:
        def judge(self, state, questions):
            raise jev.JudgeOutage("_Busy 529")
    field = types.SimpleNamespace(label="Why this role?", help="", placeholder="")
    catalog = types.SimpleNamespace(sheet_excerpt=lambda: "Jane built ingestion pipelines.")
    with pytest.raises(jev.JudgeOutage):
        apply_answergen.attempt(field, catalog, Down(), budget=1,
                                llm_call=lambda *a, **k: "I built ingestion pipelines.")


def test_an_outage_while_the_inbox_is_read_reaches_the_run(browser_page, fixtures_server):
    import apply_inbox

    class Down:
        def judge(self, state, questions):
            raise jev.JudgeOutage("_Busy 529")
    with pytest.raises(jev.JudgeOutage):
        apply_inbox.fetch_code(browser_page, "127.0.0.1",
                               fixtures_server + "/inbox/outlook_list.html", jev=Down(), polls=2,
                               sleep=lambda s: None)
    assert len(browser_page.context.pages) == 1     # its tab closed all the same


def _account_run(**kw):
    """A stand-in `_JobRun` for `_Accounts`: no account in the ledger, a
    clock inside the budget, and whatever `kw` adds (`_map`)."""
    import types
    run = types.SimpleNamespace(
        r=types.SimpleNamespace(clock=lambda: 0.0), deadline=1e9, pages=[], catalog=None,
        errors=[], _account_for=lambda host: None, _company=lambda: "",
        _decide=lambda *a, **k: None)
    run._trace = lambda kind, **k: run.errors.append((kind, k.get("error")))
    for name, value in kw.items():
        setattr(run, name, value)
    return run


def _down(*a, **k):
    raise jev.JudgeOutage("_Busy 529")


@pytest.mark.parametrize("step", ["login", "fill"])
def test_an_outage_inside_the_account_step_reaches_the_run(step, monkeypatch):
    """The account step's catch-all (ACC-10) lets a judge outage through to
    the run's breaker (RES-02); it is never noted as the step's own error."""
    import apply_run
    from apply_form import Field, FormDigest
    monkeypatch.setattr(apply_run.ats_accounts, "has_password", lambda: True)
    monkeypatch.setattr(apply_run, "account_forms", lambda page, digest: [])
    monkeypatch.setattr(apply_run.apply_form, "frames", lambda page: [])
    run = _account_run(_map=_down)
    accounts = apply_run._Accounts(run)
    digest = FormDigest(url_host="jobs.example.com", title="Sign in", text="Sign in", fields=[
        Field(n=0, locator=(0, "#email"), label="Email", type="email", required=True),
        Field(n=1, locator=(0, "#password"), label="Password", type="other", required=True,
              autocomplete="current-password", secret=True)])
    if step == "login":     # the sign-up link's judge request, the judge down
        monkeypatch.setattr(apply_run._Accounts, "_signup_link", _down)
        blank = FormDigest(url_host="jobs.example.com", title="Sign in", text="Sign in")
        call = lambda: accounts.login(object(), blank, "jobs.example.com")  # noqa: E731
    else:                   # the account form's mapping request
        call = lambda: accounts._fill(object(), digest, "jobs.example.com",  # noqa: E731
                                      "jane@example.com", False)
    with pytest.raises(jev.JudgeOutage):
        call()
    assert run.errors == [] and accounts.last_error == ""


# --- a tab the site closes (RES-06), one tab per job (RES-07) ---------------------------------

_CAREERS = "https://careers.fabrikam.example"
_FORMS = REPO / "tests" / "fixtures" / "forms"
_LINKEDIN_JOB = "https://www.linkedin.com/jobs/view/4438751519/"


def _posting(to: str) -> str:
    """The fixture posting, its Apply opening `to` in a new tab."""
    return (_FORMS / "job_posting.html").read_text(encoding="utf-8").replace(
        'href="ashby_steps.html"', f'href="{to}"')


# A popup's first step that hands the flow back to the tab that opened it
# and closes itself (a `window.close()` after a step, as after an OAuth)
_HANDBACK_STEP = f"""<!doctype html><html><head><title>Apply</title></head><body>
<h1>Apply for Analytics Engineer</h1>
<label>First name * <input name="first" required></label>
<button type="button" onclick="window.opener.location.href = '{_CAREERS}/apply/next';
  window.close()">Continue</button></body></html>"""
_THANKS = """<!doctype html><html><head><title>Application received</title></head><body>
<h1>Thank you for applying</h1><p>Your application has been received.</p></body></html>"""


def _handback_form(posts: list) -> str:
    """The Lever fixture's form, whose Submit posts, sends the tab that
    opened it to /apply/next and closes itself; `posts` counts its sends."""
    form = (_FORMS / "lever_single.html").read_text(encoding="utf-8")
    script = form[form.index("<script>"):form.index("</script>") + len("</script>")]
    return form.replace(script, (
        "<script>document.getElementById('btn-submit').addEventListener('click', function () {"
        " fetch('/apply/post', {method: 'POST', body: 'a'}).then(function () {"
        f" window.opener.location.href = '{_CAREERS}/apply/next'; window.close(); }});"
        " });</script>"))


def _counter(posts: list):
    def _handle(route) -> None:
        posts.append(route.request.method)
        route.fulfill(body="ok", content_type="text/plain")
    return _handle


def _drain_jobs(browser, tmp_path, jobs, routes, *, submit=False, judge=None, inbox=None):
    """A drain of `jobs` ((id, url) pairs) under `judge` (the fake one by
    default) on an offline context with `routes` (a glob -> HTML or a
    handler, the later winning), `inbox` standing in for the mailbox when
    given: the outcomes, and the URLs of the tabs left open. The queue is
    `tmp_path / "queue.json"` (`_entries`)."""
    import apply_queue
    import apply_run
    queue = tmp_path / "queue.json"
    with h.hermetic(tmp_path), h.fast_timing():
        for jid, url in jobs:
            folder = h.write_job_folder(tmp_path / jid)
            apply_queue.enqueue(apply_queue.new_entry(jid, company="Fabrikam",
                                                      title="Analytics Engineer", apply_url=url),
                                path=queue)
            apply_queue.set_artifacts(jid, {"folder": str(folder),
                                            "apply_md": str(folder / "apply.md"),
                                            "resume_pdf": str(folder / "Jane_Doe_Resume.pdf")},
                                      path=queue)
        ctx = browser.new_context()
        h.offline(ctx)
        for glob, body in routes.items():
            ctx.route(glob, body if callable(body) else h._fulfiller(body))
        try:
            runner = apply_run.Runner(
                jev=judge if judge is not None else jev.FakeJev(), queue_path=queue,
                profile_dir=tmp_path / "p",
                settings={"auto_apply_headless": True, "auto_apply_jev_mode": "fake",
                          "auto_apply_submit": submit, "auto_apply_generate": True},
                context=ctx, run_context={"signup_email": h.SIGNUP_EMAIL, "inbox_url": ""},
                inbox=inbox, sleep=lambda s: None)
            outcomes = runner.drain(cap=5)
            left = [str(p.url) for p in ctx.pages if not p.is_closed()]
        finally:
            ctx.close()
    return outcomes, left


def _entries(tmp_path) -> dict[str, dict]:
    """The queue `_drain_jobs` worked, its entries by id."""
    import apply_queue
    return {e["job_posting_id"]: e for e in apply_queue.load(tmp_path / "queue.json")["jobs"]}


def _trace_of(tmp_path, jid: str) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in (tmp_path / jid / "apply_trace").rglob("*.json"))


def test_a_popup_that_hands_the_flow_back_and_closes_goes_on_in_the_tab_that_opened_it(
        _browser, tmp_path):
    outcomes, left = _drain_jobs(_browser, tmp_path, [("a", f"{_CAREERS}/jobs/a")], {
        f"{_CAREERS}/jobs/a": _posting(f"{_CAREERS}/apply/start"),
        f"{_CAREERS}/apply/start": _HANDBACK_STEP,
        f"{_CAREERS}/apply/next": (_FORMS / "lever_single.html").read_text(encoding="utf-8")})
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "ready_to_submit")], outcomes
    assert left == [f"{_CAREERS}/apply/next"]         # the job's one tab, at the gate
    assert "tab_taken_over" in _trace_of(tmp_path, "a")


def test_a_popup_that_sends_then_hands_back_is_confirmed_by_the_tab_that_opened_it(
        _browser, tmp_path):
    posts: list = []
    outcomes, left = _drain_jobs(_browser, tmp_path, [("a", f"{_CAREERS}/jobs/a")], {
        f"{_CAREERS}/jobs/a": _posting(f"{_CAREERS}/apply/start"),
        f"{_CAREERS}/apply/start": _handback_form(posts),
        f"{_CAREERS}/apply/post": _counter(posts),
        f"{_CAREERS}/apply/next": _THANKS}, submit=True)
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "submitted")], outcomes
    assert outcomes[0].reason.startswith("confirmation page (in the tab that opened"), outcomes
    assert "received" in outcomes[0].reason
    assert posts == ["POST"] and left == []           # one send; a confirmation closes its tab


def test_after_a_send_the_tab_handed_back_to_is_only_read_never_filled_or_sent_again(
        _browser, tmp_path):
    # the tab that opened the closed one shows a form again: the run never
    # takes it over after the submit click (at most one send per job)
    posts: list = []
    again = (_FORMS / "lever_single.html").read_text(encoding="utf-8").replace(
        "document.body.setAttribute('data-submitted', '1');",
        "fetch('/apply/post', {method: 'POST', body: 'b'});")
    outcomes, left = _drain_jobs(_browser, tmp_path, [("a", f"{_CAREERS}/jobs/a")], {
        f"{_CAREERS}/jobs/a": _posting(f"{_CAREERS}/apply/start"),
        f"{_CAREERS}/apply/start": _handback_form(posts),
        f"{_CAREERS}/apply/post": _counter(posts),
        f"{_CAREERS}/apply/next": again}, submit=True)
    assert posts == ["POST"], posts
    assert outcomes[0].status == "submitted", outcomes
    assert outcomes[0].reason.startswith("submitted (unconfirmed): the job's tab was closed"), \
        outcomes
    assert "tab_taken_over" not in _trace_of(tmp_path, "a")
    assert left == [f"{_CAREERS}/apply/next"]         # the job's last open tab stays


def test_the_linkedin_tab_closes_once_the_company_tab_is_adopted_and_each_job_keeps_one_tab(
        _browser, flow_server, tmp_path):
    routes = h.linkedin_job_routes("linkedin_posting_button.html",
                                   "lever_single.html")(flow_server.base)
    outcomes, left = _drain_jobs(_browser, tmp_path, [
        ("a", _LINKEDIN_JOB), ("b", f"{flow_server.base}/forms/job_posting.html")], routes)
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "ready_to_submit"),
                                                        ("b", "ready_to_submit")], outcomes
    # one tab per job, each at its form: no LinkedIn tab, no posting behind a popup
    assert sorted(left) == sorted([f"{flow_server.base}/forms/lever_single.html",
                                   f"{flow_server.base}/forms/ashby_steps.html"]), left
    assert "source_tab_closed" in _trace_of(tmp_path, "a")


def _bare_run(tmp_path):
    """A `_JobRun` with no browser, the company's site allowed."""
    import apply_run
    runner = apply_run.Runner(jev=jev.FakeJev(), profile_dir=tmp_path / "p", settings={},
                              context=object(), run_context={"inbox_url": ""})
    run = apply_run._JobRun(runner, None, {"job_posting_id": "42"})
    run.allowed.add("careers.fabrikam.example")
    return run


class _Tab:
    """A tab double: its address, open unless `closed`, and its main frame."""

    def __init__(self, url: str, closed: bool = False):
        self.url = url
        self.closed = closed
        self.main_frame = _Frame(self)

    def is_closed(self) -> bool:
        return self.closed

    def wait_for_timeout(self, ms) -> None:
        pass


class _Frame:
    def __init__(self, page):
        self.page = page
        self.parent_frame = None


class _Held:
    """A held first load's request double: its tab's frame once the tab is
    known (`frame`), else it raises as Playwright's does before then."""

    def __init__(self, frame=None):
        self._frame = frame

    @property
    def frame(self):
        if self._frame is None:
            raise RuntimeError("Frame for this navigation request is not available")
        return self._frame


def test_a_left_tab_whose_clock_or_relative_time_ticks_has_not_moved_on(_browser):
    # SP8a review M3: the print leaves out what changes while a page stands
    # still (`_VOLATILE_TEXT`), and keeps a step marker and the words
    import apply_run
    page = _browser.new_page()
    try:
        def _print(body: str) -> str:
            page.set_content(f"<h1>Apply for Analytics Engineer</h1>{body}")
            return apply_run._page_print(page)[0]
        before = _print("<p>Posted 3 minutes ago</p><p>Your session ends at 12:04</p>"
                        "<p>Step 1 of 3</p>")
        assert _print("<p>Posted 4 minutes ago</p><p>Your session ends at 12:05</p>"
                      "<p>Step 1 of 3</p>") == before
        assert _print("<p>Posted 4 minutes ago</p><p>Your session ends at 12:05</p>"
                      "<p>Step 2 of 3</p>") != before
        assert _print("<p>Thank you for applying</p>") != before
    finally:
        page.close()


def test_a_linkedin_tab_is_never_taken_over_as_the_flow(tmp_path, monkeypatch):
    # SP8a review M4: LinkedIn is an allowed site, and never the company's flow
    import apply_run
    monkeypatch.setattr(apply_run, "TAKEOVER_WAIT_S", 0)
    monkeypatch.setattr(apply_run, "_page_print", lambda page: ("moved", "text now"))
    run = _bare_run(tmp_path)
    linkedin = _Tab("https://www.linkedin.com/jobs/view/4000000001/")
    company = _Tab(f"{_CAREERS}/apply/step-2")
    run._left_pages = [(linkedin, "then", "text then")]
    assert run._moved_on() is None
    run._left_pages = [(company, "then", "text then"), (linkedin, "then", "text then")]
    assert run._moved_on() == (company, "text then", "text now")


_RESET = "net::ERR_CONNECTION_RESET"


def test_a_new_tabs_failed_first_load_is_taken_by_its_own_tab_whatever_else_is_held(tmp_path):
    # SP8a review M5 and R2-M3: a new tab's first load names no tab when it
    # fails; by the error page its tab is known, so the load is tied to it,
    # a closed tab's is dropped, another tab's goes to that tab, and a load
    # still tied to no tab stays held and is never taken
    import apply_run
    run = _bare_run(tmp_path)
    tab = _Tab("chrome-error://chromewebdata/")
    closed = _Tab("chrome-error://chromewebdata/", closed=True)
    other = _Tab("chrome-error://chromewebdata/")
    unknown = _Held()
    run._unplaced_loads = [
        (_Held(closed.main_frame), ("https://ads.example.net/x", "GET", _RESET)),
        (unknown, ("https://ads.example.net/y", "GET", _RESET)),
        (_Held(tab.main_frame), (f"{_CAREERS}/apply", "POST", _RESET)),
        (_Held(other.main_frame), (f"{_CAREERS}/other", "POST", _RESET))]
    with pytest.raises(apply_run._Parked) as parked:
        run._recover_error_page(tab)
    # this tab's own load (a POST, so never sent again: the reason names it)
    assert parked.value.reason == (f"{apply_run.ERROR_PAGE_REASON}: POST {_CAREERS}/apply "
                                   f"failed on the network ({_RESET}); a POST is never sent "
                                   f"again"), parked.value.reason
    assert [request for request, _ in run._unplaced_loads] == [unknown]
    with pytest.raises(apply_run._Parked) as parked:
        run._recover_error_page(other)
    assert parked.value.reason.startswith(f"{apply_run.ERROR_PAGE_REASON}: POST "
                                          f"{_CAREERS}/other failed"), parked.value.reason


def test_a_held_first_load_tied_to_no_tab_is_never_taken(tmp_path):
    # SP8a review R2-M3: once the error tab is known a load still tied to no
    # tab is another tab's (one that closed before it was reported): taking
    # it would load a stale address in this tab
    import apply_run
    run = _bare_run(tmp_path)
    run._unplaced_loads = [(_Held(), ("https://ads.example.net/x", "GET", _RESET))]
    with pytest.raises(apply_run._Parked) as parked:
        run._recover_error_page(_Tab("chrome-error://chromewebdata/"))
    assert parked.value.reason == (f"{apply_run.ERROR_PAGE_REASON}: the tab shows Chrome's "
                                   f"error page and the address that failed is not known")


def test_a_popup_that_failed_and_closed_earlier_never_hides_the_next_tabs_load(
        _browser, flow_server, tmp_path, monkeypatch):
    # SP8a review R2-M3: a tracker's window whose first load failed and that
    # closed at once is held beside the Apply tab's own dropped first load;
    # the Apply tab's load is still the one loaded again
    import apply_run
    real = apply_run._JobRun._listen_loads

    def _listen_then_a_tracker_fails(self) -> None:
        real(self)
        opener = self.ctx.new_page()
        with opener.expect_popup() as info:
            opener.evaluate("window.open('https://ads.example.net/px')")
        info.value.close()
        opener.close()
    monkeypatch.setattr(apply_run._JobRun, "_listen_loads", _listen_then_a_tracker_fails)
    base = h.flow("linkedin_gts_other")
    hop = "https://www.linkedin.com/safety/go/**"
    drop = _drop_first(body=base.routes(flow_server.base)[hop])
    f = dataclasses.replace(base, routes=lambda b: {**base.routes(b), hop: drop})
    r = _run(f, _browser, flow_server, tmp_path)
    assert "chromewebdata" not in r.reason and "not known" not in r.reason, r
    assert r.ok, (r.status, r.reason)
    assert r.breaks == []
    assert drop.seen == ["GET", "GET"]


# --- the judge down once something may have been sent (RES-02, SP8a review I1) ---------------

class _GoesDown(jev.FakeJev):
    """The fake judge until `off` is set, then a service that stays down (a
    529 on every try): `jev.Guarded`'s retries run out and its breaker opens."""

    off = False

    def judge(self, state, questions):
        if self.off:
            raise _Busy(529)
        return super().judge(state, questions)


def _down_from(monkeypatch, judge: _GoesDown, step: str, *, after: bool = False) -> None:
    """The judge goes down as the run enters `step` (a `_JobRun` method), or
    once that step returns (`after`)."""
    import apply_run
    real = getattr(apply_run._JobRun, step)

    def _step(self, *a, **kw):
        judge.off = judge.off or not after
        result = real(self, *a, **kw)
        judge.off = True
        return result
    monkeypatch.setattr(apply_run._JobRun, step, _step)


_WAIT = """<!doctype html><html><head><title>Next</title></head><body>
<p>Please wait while we process your details.</p></body></html>"""


def _sink(posts: list, body: str = _WAIT):
    """A route handler that counts each request it answers (`posts`) and
    answers with `body`."""
    def _handle(route, request) -> None:
        posts.append(str(request.method))
        route.fulfill(body=body, content_type="text/html")
    return _handle


# The form's own script sends the answers to a form service on another host
# and shows the wait: no request to the application's sites
_FORM_SERVICE = "https://forms.backend.example/submit"
_OFF_SITE_FORM = _POST_FORM.replace('<form method="post" action="/post/submit">', "<form>").replace(
    'type="submit"', 'type="button"').replace("</form>", (
        "</form><p id='wait' hidden>Please wait while we process your details.</p>"
        "<script>document.getElementById('btn-submit').addEventListener('click', function () {"
        f" fetch('{_FORM_SERVICE}', {{method: 'POST', body: 'a'}}).catch(function () {{}});"
        " document.querySelector('form').hidden = true;"
        " document.getElementById('wait').hidden = false; });</script>"))
# The application's first step, then a code step whose Verify sends
_STEP_ONE = """<!doctype html><html><head><title>Apply for Analytics Engineer</title></head>
<body><h1>Apply for Analytics Engineer</h1><p>Fabrikam, Remote</p>
<label>First name * <input name="first_name" required></label>
<label>Last name * <input name="last_name" required></label>
<label>Email * <input type="email" name="email" required></label>
<button type="button" onclick="location.href='/apply/verify'">Continue</button>
</body></html>"""
_CODE_STEP = (_FORMS / "code_gate.html").read_text(encoding="utf-8").replace(
    "window.location.href = 'confirmation.html';",
    "fetch('/post/verify', {method: 'POST', body: 'c'}).then(function () {"
    " location.href = '/post/next'; });")


class _Mailbox:
    """The inbox's code, with no mailbox page read."""

    def fetch_code(self, page, site, inbox_url):
        return "MKPZ3QRA"


def _one_send_and_the_drain_stopped(outcomes, jobs, posts) -> None:
    """Something may have been sent: the job ended, never back in the queue
    with its attempt given back; exactly one send reached the site; the drain
    stopped with the next job still queued."""
    assert [o.job_id for o in outcomes] == ["a"], outcomes
    assert outcomes[0].judge_down
    assert posts == ["POST"], posts
    a, b = jobs["a"], jobs["b"]
    assert a["status"] == outcomes[0].status != "queued", a
    assert a["attempts"] == 1
    assert (b["status"], b["attempts"]) == ("queued", 0)


def test_a_judge_down_after_the_submit_sent_ends_the_job_submitted_unconfirmed(
        _browser, tmp_path, monkeypatch):
    import apply_run
    posts: list[str] = []
    judge = _GoesDown()
    _down_from(monkeypatch, judge, "_after_submit")
    outcomes, _ = _drain_jobs(_browser, tmp_path, [("a", f"{_CAREERS}/apply"),
                                                   ("b", f"{_CAREERS}/apply")], {
        f"{_CAREERS}/apply": _POST_FORM, f"{_CAREERS}/post/submit": _sink(posts)},
        submit=True, judge=judge)
    _one_send_and_the_drain_stopped(outcomes, _entries(tmp_path), posts)
    reason = outcomes[0].reason
    assert outcomes[0].status == "submitted", outcomes
    assert reason.startswith(f"submitted (unconfirmed): {apply_run.JUDGE_DOWN_REASON}: "
                             f"_Busy 529 at "), reason
    assert reason.endswith(f"(after POST {_CAREERS}/post/submit)"), reason


def test_a_judge_down_at_the_first_look_after_the_submit_still_reads_the_confirmation(
        _browser, tmp_path, monkeypatch):
    # SP8a review M8: received words the page did not show before the click
    # need no judge
    posts: list[str] = []
    judge = _GoesDown()
    _down_from(monkeypatch, judge, "_after_submit")
    outcomes, _ = _drain_jobs(_browser, tmp_path, [("a", f"{_CAREERS}/apply"),
                                                   ("b", f"{_CAREERS}/apply")], {
        f"{_CAREERS}/apply": _POST_FORM,
        f"{_CAREERS}/post/submit": _sink(posts, h.CONFIRMATION_HTML)},
        submit=True, judge=judge)
    _one_send_and_the_drain_stopped(outcomes, _entries(tmp_path), posts)
    assert outcomes[0].status == "submitted", outcomes
    assert outcomes[0].reason.startswith("confirmation page"), outcomes[0].reason
    assert "unconfirmed" not in outcomes[0].reason


def test_a_judge_down_after_the_submit_with_no_send_seen_on_the_sites_asks_the_person(
        _browser, tmp_path, monkeypatch):
    import apply_run
    posts: list[str] = []
    judge = _GoesDown()
    _down_from(monkeypatch, judge, "_after_submit")
    outcomes, _ = _drain_jobs(_browser, tmp_path, [("a", f"{_CAREERS}/apply"),
                                                   ("b", f"{_CAREERS}/apply")], {
        f"{_CAREERS}/apply": _OFF_SITE_FORM, _FORM_SERVICE: _sink(posts, "ok")},
        submit=True, judge=judge)
    _one_send_and_the_drain_stopped(outcomes, _entries(tmp_path), posts)
    reason = outcomes[0].reason
    assert outcomes[0].status == "needs_human", outcomes
    assert reason.startswith(f"{apply_run.CHECK_SENT_REASON}: the run stopped after the submit "
                             f"click ({apply_run.JUDGE_DOWN_REASON}: _Busy 529 at "), reason
    assert reason.endswith(f"; a request left: POST {_FORM_SERVICE}"), reason
    assert h.policy_park(outcomes[0].status, reason) is True


def test_a_judge_down_after_the_code_step_of_a_filled_application_asks_the_person(
        _browser, tmp_path, monkeypatch):
    import apply_run
    posts: list[str] = []
    judge = _GoesDown()
    _down_from(monkeypatch, judge, "_code_gate", after=True)
    outcomes, _ = _drain_jobs(_browser, tmp_path, [("a", f"{_CAREERS}/apply"),
                                                   ("b", f"{_CAREERS}/apply")], {
        f"{_CAREERS}/apply": _STEP_ONE, f"{_CAREERS}/apply/verify": _CODE_STEP,
        f"{_CAREERS}/post/verify": _sink(posts, "ok"), f"{_CAREERS}/post/next": _WAIT},
        submit=True, judge=judge, inbox=_Mailbox())
    _one_send_and_the_drain_stopped(outcomes, _entries(tmp_path), posts)
    reason = outcomes[0].reason
    assert outcomes[0].status == "needs_human", outcomes
    assert reason.startswith(f"{apply_run.CHECK_SENT_REASON}: the run stopped after the code "
                             f"step ({apply_run.JUDGE_DOWN_REASON}: _Busy 529 at "), reason
    assert h.policy_park(outcomes[0].status, reason) is True


def test_a_code_step_click_whose_tab_closes_is_never_taken_over_or_handed_back(
        _browser, tmp_path, monkeypatch):
    # final review A-I1: the code step's click after the application's
    # answers may send what the site held for the code. The job's tab closes
    # inside the click, with the tab that opened it moved on to the form: the
    # person checks the job, which is never re-queued or taken over, and the
    # form is never sent again
    import apply_run
    from playwright.sync_api import Error as PlaywrightError
    posts: list[str] = []
    real = apply_run._JobRun._click

    def _click(self, digest, n, role, rec, **kw):
        result = real(self, digest, n, role, rec, **kw)
        if any(f.id_or_name == "code" for f in digest.fields):
            self.page.evaluate(f"window.opener && (window.opener.location.href = "
                               f"'{_CAREERS}/apply/again')")
            self.page.close()
            raise PlaywrightError("Target page, context or browser has been closed")
        return result
    monkeypatch.setattr(apply_run._JobRun, "_click", _click)
    outcomes, _ = _drain_jobs(_browser, tmp_path, [("a", f"{_CAREERS}/jobs/a")], {
        f"{_CAREERS}/jobs/a": _posting(f"{_CAREERS}/apply"), f"{_CAREERS}/apply": _STEP_ONE,
        f"{_CAREERS}/apply/again": _STEP_ONE, f"{_CAREERS}/apply/verify": _CODE_STEP,
        f"{_CAREERS}/post/verify": _sink(posts, "ok"), f"{_CAREERS}/post/next": _WAIT},
        submit=True, inbox=_Mailbox())
    assert posts == ["POST"], posts
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "needs_human")], outcomes
    reason = outcomes[0].reason
    assert reason.startswith(f"{apply_run.CHECK_SENT_REASON}: the run stopped after the code "
                             f"step ({apply_run.TAB_CLOSED_REASON}"), reason
    assert _entries(tmp_path)["a"]["status"] == "needs_human"
    assert "tab_taken_over" not in _trace_of(tmp_path, "a")


_ACCOUNT_CODE = (_FORMS / "verify_email_code.html").read_text(encoding="utf-8").replace(
    "window.location.href = 'email_verified.html';",
    "fetch('/account/verify', {method: 'POST', body: 'c'}).then(function () {"
    " location.href = '/apply/form'; });")


def test_a_judge_down_after_an_account_code_hands_the_job_back_to_the_queue(
        _browser, tmp_path, monkeypatch):
    # the account's own code, before any of the application's answers went
    # on a page, sends nothing of the application (SP8a review M9)
    import apply_run
    posts: list[str] = []
    judge = _GoesDown()
    _down_from(monkeypatch, judge, "_code_gate", after=True)
    outcomes, _ = _drain_jobs(_browser, tmp_path, [("a", f"{_CAREERS}/apply/verify"),
                                                   ("b", f"{_CAREERS}/apply/verify")], {
        f"{_CAREERS}/apply/verify": _ACCOUNT_CODE, f"{_CAREERS}/account/verify": _sink(posts, "ok"),
        f"{_CAREERS}/apply/form": _POST_FORM}, submit=True, judge=judge, inbox=_Mailbox())
    assert [(o.job_id, o.status) for o in outcomes] == [("a", "queued")], outcomes
    assert outcomes[0].reason.startswith(f"{apply_run.JUDGE_DOWN_REASON}: _Busy 529 at "), \
        outcomes[0].reason
    assert posts == ["POST"]            # the account's check, never the application
    jobs = _entries(tmp_path)
    assert (jobs["a"]["status"], jobs["a"]["attempts"]) == ("queued", 0)
    assert (jobs["b"]["status"], jobs["b"]["attempts"]) == ("queued", 0)


def test_a_park_a_step_reached_with_the_judge_down_after_the_submit_click_stands(
        _browser, tmp_path, monkeypatch):
    # a step that noted the outage and went on to a park of its own (the
    # run's `_Parked` branch): the park stands, the job is never re-queued
    import apply_run
    posts: list[str] = []
    judge = _GoesDown()

    def _swallowed(self, watch, before, account, handoff):
        judge.off = True
        try:
            self.r.jev.judge({"page": "after the submit"}, {"q": {"type": "noul",
                                                                  "instructions": "x"}})
        except jev.JudgeOutage:
            pass
        raise apply_run._Parked("needs_human", f"the site showed an error after the submit "
                                               f"click (Something went wrong); "
                                               f"{apply_run.CHECK_SENT_REASON}")
    monkeypatch.setattr(apply_run._JobRun, "_read_after_submit", _swallowed)
    outcomes, _ = _drain_jobs(_browser, tmp_path, [("a", f"{_CAREERS}/apply"),
                                                   ("b", f"{_CAREERS}/apply")], {
        f"{_CAREERS}/apply": _POST_FORM, f"{_CAREERS}/post/submit": _sink(posts)},
        submit=True, judge=judge)
    _one_send_and_the_drain_stopped(outcomes, _entries(tmp_path), posts)
    assert outcomes[0].status == "needs_human", outcomes
    assert outcomes[0].reason.startswith("the site showed an error after the submit click"), \
        outcomes[0].reason


# --- the drain's summary table ---------------------------------------------------------------

_GONE = """<!doctype html><html><head><title>Job closed</title></head><body>
<h1>This job is no longer available</h1><p>The position has been filled. Thank you for your
interest in Fabrikam; see our other openings.</p></body></html>"""


def test_a_drain_prints_one_table_over_its_jobs_and_writes_it_beside_the_job_folders(
        _browser, tmp_path, capsys):
    import re

    import apply_run
    outcomes, _ = _drain_jobs(_browser, tmp_path, [("a", f"{_CAREERS}/jobs/a"),
                                                   ("b", f"{_CAREERS}/jobs/b")], {
        f"{_CAREERS}/jobs/a": (_FORMS / "lever_single.html").read_text(encoding="utf-8"),
        f"{_CAREERS}/jobs/b": _GONE})
    assert [o.job_id for o in outcomes] == ["a", "b"], outcomes
    assert outcomes[0].status == "ready_to_submit", outcomes
    out = capsys.readouterr().out
    reports = list(tmp_path.glob(f"{apply_run.DRAIN_REPORT_PREFIX}*.md"))
    assert len(reports) == 1, list(tmp_path.iterdir())     # beside the job folders a/ and b/
    assert re.fullmatch(r"apply_drain-\d{8}-\d{6}\.md", reports[0].name), reports
    assert f"drain report: {reports[0]}" in out
    text = reports[0].read_text(encoding="utf-8")
    assert apply_run.summary_line(outcomes) in text
    for table, linked in ((out, False), (text, True)):
        rows = [ln for ln in table.splitlines() if re.match(r"\| \d+ \| ", ln)]
        assert len(rows) == 2, table
        for i, (o, row) in enumerate(zip(outcomes, rows), 1):
            cells = [c.strip() for c in row.strip("|").split(" | ")]
            assert cells[:4] == [str(i), o.job_id, o.status, str(o.pages)], row
            assert cells[4] == apply_run._cell(o.reason, apply_run.REASON_CELL_MAX), row
            assert o.trace_dir and Path(o.trace_dir).is_dir(), o
            if linked:          # relative to the report, and it resolves
                target = re.fullmatch(r"\[[^]]+\]\(<([^>]+)/>\)", cells[5]).group(1)
                assert not Path(target).is_absolute(), target
                assert (reports[0].parent / target).resolve() == Path(o.trace_dir).resolve()
            else:
                assert cells[5] == Path(o.trace_dir).as_posix(), row


def test_a_reason_with_a_bar_or_a_line_break_stays_in_its_cell(tmp_path):
    import apply_run
    o = apply_run.Outcome(job_id="x", status="needs_human", reason="a | b\nc", record_path="",
                          pages=0)
    row = apply_run.drain_table([o]).splitlines()[2]
    assert row == "| 1 | x | needs_human | 0 | a \\| b c | - |", row
    assert apply_run.drain_report_dir([o], tmp_path / "q" / "queue.json") == tmp_path / "q"
