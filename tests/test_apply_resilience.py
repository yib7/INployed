"""SP8a: the auto-apply run's resilience (the offline half of SP8).

- Chrome's own error page (`chrome-error://chromewebdata/`, a load the
  network dropped) reads as a failed load: one retry of a GET on the allowed
  sites, never "left the allowed sites: chromewebdata". After the submit
  click only the page the send led to is loaded again: the GET that carried
  the send and any POST never are (at most one send per job).

Headless Chromium through the module-scoped test browser, the flow harness
(`apply_harness.run_flow`) and the fake judge; no network but the local
server and routed hosts."""
import dataclasses
import sys
from pathlib import Path

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
    # it as the send) was dropped; loading it again would send twice
    base = h.flow("submit_code")
    drop = _drop_first()
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

