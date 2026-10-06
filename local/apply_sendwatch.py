"""What a page sends: `_NavGuard` (while the credentials are on a page, it
may not navigate off the application's sites), `SendWatch` (the requests the
page and its tabs send after the submit click), `LateWatch`, and the words a
page shows once an application was received.

Split out of `apply_run`, which re-exports these names.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import apply_judge
import apply_linkedin
from apply_sites import _host, _insecure, _is_captcha_url, _tracking


# The words a page shows once an application was received
# (`apply_judge.CONFIRMATION_WORDS`): with the judge's read, the deterministic
# half of a confirmation after the submit click, and only when they were not
# on the page before it.
CONFIRMATION_WORDS = apply_judge.CONFIRMATION_WORDS
confirmation_words = apply_judge.confirmation_words


# Armed, the route turns Chromium's HTTP cache off (measured on a local server): the
# worst cases were +2.07 s for a 20 MB body posted from page memory and +0.9 s for a warm
# page of 150 subresources at 30 ms each; a 150-request page costs about +0.35 s, a 5 MB
# file box sent as FormData +2 ms. Only a page with the master password on it arms it.
class _NavGuard:
    """While the credentials are on the page, the page and the frames that
    hold them (`frames`, by id) may not navigate off the application's sites:
    a form that posts there is stopped and the host lands in `blocked`. Every
    other request goes through (a fetch, a popup, another frame), so the
    site's own bot check, sign-in API and scripts work as they would for a
    person; the password is only ever typed on the application's site
    (`_password_ok`), which is the protection that matters. `before` names
    the page as it was when the guard went on (a stopped navigation leaves
    the tab on a browser error page). A form post goes only where the
    password may be typed (`_password_ok`: never LinkedIn, the inbox or a
    job board); a GET wherever the run may go
    (`_allowed_site`), so a hop back to LinkedIn after the submit still
    loads. A navigation to an unencrypted address (`_insecure`) is stopped
    either way. `posts` keeps each stopped post's method and bare URL, as
    `SendWatch` writes its rows."""

    def __init__(self, run, page):
        self.run = run
        self.page = page
        self.frames: set[int] = set()   # the page's main frame joins at `start`
        self.blocked: list[str] = []
        self.posted = False             # a stopped navigation carried a form post
        self.posts: list[str] = []      # the stopped posts, "POST scheme://host/path"
        self.before = ""
        self._on = False

    def _route(self, route, request) -> None:
        target_host = _host(request.url)
        try:
            frame_id = id(request.frame)
        except Exception:       # noqa: BLE001  (a service-worker request has no frame)
            frame_id = None
        if target_host and request.is_navigation_request() and frame_id in self.frames:
            post = request.method.upper() == "POST"
            # an unencrypted address (`_insecure`) is stopped either way: a
            # GET form carries the password in its URL
            if _insecure(request.url) or not (self.run._password_ok(request.url) if post
                                              else self.run._allowed_site(target_host)):
                self.blocked.append(target_host)
                if post:
                    self.posted = True
                    self.posts.append(f"POST {SendWatch._bare(request.url)}")
                route.abort()
                return
        route.fallback()        # on to any other handler, then the network

    def post_host(self) -> str:
        """The host of the first stopped post ("" with none)."""
        return _host(self.posts[0].split(" ", 1)[1]) if self.posts else ""

    def start(self) -> None:
        if not self._on:
            self.frames.add(id(self.page.main_frame))
            try:
                self.before = f"{self.page.url} | {self.page.title()}"
            except Exception:       # noqa: BLE001  (a page mid-navigation has no title yet)
                self.before = str(self.page.url)
            self.page.route("**/*", self._route)
            self._on = True

    def stop(self) -> None:
        if self._on:
            self._on = False
            try:
                self.page.unroute("**/*", self._route)
            except Exception:       # noqa: BLE001  (the page is gone, and its routes with it)
                pass


ACTION_READ_MS = 2_000


class SendWatch:
    """What the page sends after the submit click, from the page,
    its frames and any tab it opens:

    - `sent`: a navigation of the main frame or of the submit's own frame,
      or a POST, PUT or PATCH, to the application's sites
      (`_JobRun._allowed_site`): the evidence for "submitted (unconfirmed)";
    - `possible`: a POST, PUT or PATCH to any other host (a form backend on
      another domain): it may have been the send, so the job never reads as
      unsent after it.

    Only the job's page and the tabs it opens after `start` count: an
    earlier job's parked tab, the inbox tab and a tab the person uses never
    do. A tab's first request comes before its page is known (Playwright
    gives no frame for it): it is held (`_unplaced`) and counted when the
    job's page reports that tab, at the same URL. A held POST, PUT or PATCH
    never matched (the tab's first answer redirected, or was an error page,
    so the tab reports another URL; or a service worker sent it) counts as a
    possible send (`unplaced_sends`, in `any` and `first`, and moved to
    `possible` when the watch stops): only a brand-new tab or a worker makes
    such a request, and it may have been the send. A bot-check provider, an
    analytics, ad, chat or consent host (`_tracking`), LinkedIn (its Insight
    Tag posts from company pages) and the inbox's host (unless the job's page
    is served from it) never count. Each row
    keeps the method and the URL without its query (a GET form puts the
    answers there); `pending` keeps the read waiting while one of the job's
    page is in flight. `caused` keeps the rows the click itself caused: each
    one up to the first navigation of the job's page, that navigation too; a
    navigation after it is the site's own (a POST's answer sending the tab
    on). `_order` keeps every such row of the job's page and its tabs in the
    order they left, and `answered` the requests whose answer came back, for
    `sent_left`. Which GET may have carried the send is `carried_get`'s."""

    _SEND_METHODS = ("POST", "PUT", "PATCH")

    def __init__(self, run, page, frame=None):
        self.run = run
        self.page = page
        self.frames: set[int] = set()
        for f in (getattr(page, "main_frame", None), frame):
            if f is not None:
                self.frames.add(id(f))
        self.sent: list[str] = []
        self.possible: list[str] = []
        self.caused: list[str] = []
        self._navigated = False            # the click's own navigation was seen
        self._order: list[tuple[Any, str, str]] = []   # (request, kind, row)
        # the ids of requests `_order` holds (so no other request has them)
        # whose answer came back, and of those still in flight
        self.answered: set[int] = set()
        self.pending: set[int] = set()
        self._targets: list = []
        self._before: set[int] = set()     # the context's tabs before `start`
        self._opened: set[int] = set()     # the tabs the job's page opened since
        self._unplaced: list[tuple[str, str, str]] = []    # (url, kind, row)
        self._inbox = ""
        self._on = False

    @staticmethod
    def _bare(url: str) -> str:
        parts = urlsplit(str(url or ""))
        return f"{parts.scheme}://{parts.netloc}{parts.path}"

    def _owner(self, request) -> str:
        """"job" (the job's page or a tab it opened), "other", or "unknown"
        (a new tab's first request, whose page Playwright cannot name yet)."""
        try:
            page = request.frame.page
        except Exception:       # noqa: BLE001  (no frame yet, or a service worker's request)
            return "unknown"
        if page is self.page or id(page) in self._opened:
            return "job"
        if page is None or id(page) in self._before:
            return "other"
        try:
            return "job" if page.opener() is self.page else "other"
        except Exception:       # noqa: BLE001
            return "other"

    def _navigation(self, request) -> bool:
        """A navigation of the job's page's main frame or of the submit's frame."""
        try:
            return bool(request.is_navigation_request()) and id(request.frame) in self.frames
        except Exception:       # noqa: BLE001
            return False

    def _kind(self, request, navigation: bool) -> str:
        url = str(request.url)
        host = _host(url)
        if not host or _is_captcha_url(url) or apply_linkedin.is_linkedin(host) or _tracking(url):
            return ""
        if self._inbox and host == self._inbox:
            return ""
        send = str(request.method).upper() in self._SEND_METHODS
        if self.run._allowed_site(host) and (navigation or send):
            return "sent"
        return "possible" if send else ""

    def _request(self, request) -> None:
        try:
            owner = self._owner(request)
            if owner == "other":
                return
            navigation = owner == "job" and self._navigation(request)
            kind = self._kind(request, navigation)
            row = f"{str(request.method).upper()} {self._bare(request.url)}"
            if owner == "job" and (kind or navigation):
                self._order.append((request, kind, row))
                if not self._navigated:
                    self.caused.append(row)
                    self._navigated = navigation
            if not kind:
                return
            if owner == "unknown":
                self._unplaced.append((self._bare(request.url), kind, row))
                return
            (self.sent if kind == "sent" else self.possible).append(row)
            self.pending.add(id(request))
        except Exception:       # noqa: BLE001  (a request that cannot be read counts as nothing)
            pass

    def carried_get(self, row: str, action: str = "", url: str = "") -> bool:
        """May the GET `row` ("GET bare-url"; `url` in full) have carried the
        send? Always when its URL without the query is the submit form's
        `action` (a GET form's send, after a draft's POST too), and when it is among the requests the click caused (`caused`,
        up to and with the click's first navigation) or is the first seen: a
        script can send the answers by a GET to any address after a draft's
        save came back, and the network cannot tell that from a thank-you
        page loaded after a fetch send. A GET after the click's
        first navigation carried it unless the answer of a send that came
        back led to it (`led_on`)."""
        if action and row == f"GET {self._bare(action)}":
            return True
        if row in self.caused or self.first() == row:
            return True
        return not self.led_on(row, url)

    def led_on(self, row: str, url: str = "") -> bool:
        """Did the answer of a send that came back lead to the GET `row`
        (`url` in full, else the address it was requested at)? When an HTTP
        redirect from that send led to it (`redirected_from`), or when such
        a send came back before it (`sent_left`) and its address has no
        query: a script GET send carries the answers in its query, as an
        interstitial page's GET form does, so a GET with one is never loaded
        again. A POST form's thank-you page (no query,
        or its HTTP redirect) is loaded again. The risk left: a page after
        the click's first navigation that sends by a GET with no query (the
        answers in a path token) after a POST to the application's sites
        came back."""
        at = self._last(row)
        if at < 0:
            return False
        request = self._order[at][0]
        if self._redirected_from_answered(request):
            return True
        full = str(url or getattr(request, "url", "") or "")
        return not urlsplit(full).query and self.sent_left(row)

    def sent_left(self, row: str) -> bool:
        """Is a send known to have left before the load `row`: a POST, PUT
        or PATCH to the application's sites, from the job's page or a tab it
        opened, whose answer came back? A send to
        another host (a beacon `_tracking` does not know may be one), a send
        whose answer never came, and anything when `row` was never seen are
        no such send."""
        at = self._last(row)
        return at >= 0 and any(self._answered_send(request, kind, r)
                               for request, kind, r in self._order[:at])

    def _last(self, row: str) -> int:
        """Where `row` last stands in `_order`, or -1."""
        for i in range(len(self._order) - 1, -1, -1):
            if self._order[i][2] == row:
                return i
        return -1

    def _answered_send(self, request, kind: str, row: str) -> bool:
        return (kind == "sent" and row.split(" ", 1)[0] in self._SEND_METHODS
                and id(request) in self.answered)

    _REDIRECT_HOPS = 20

    def _redirected_from_answered(self, request) -> bool:
        """Did an HTTP redirect chain from a send `_answered_send` holds lead
        to `request`?"""
        for _ in range(self._REDIRECT_HOPS):
            try:
                request = request.redirected_from
            except Exception:   # noqa: BLE001  (a request that cannot be read counts as nothing)
                return False
            if request is None:
                return False
            if any(held is request and self._answered_send(held, kind, r)
                   for held, kind, r in self._order):
                return True
        return False

    def _done(self, request) -> None:
        self.pending.discard(id(request))

    def _answer(self, request) -> None:
        """`request`'s answer came back: kept only for a request `_order`
        holds, whose id no other request can have while it is held (the
        context reports every tab's answers, and a freed request's id is
        reused)."""
        if any(held is request for held, _, _ in self._order):
            self.answered.add(id(request))

    def _finished(self, request) -> None:
        self.pending.discard(id(request))
        self._answer(request)

    def _answered(self, response) -> None:
        """A response's headers came back: its request reached the site,
        even when its body is cut off after."""
        try:
            self._answer(response.request)
        except Exception:       # noqa: BLE001  (a response that cannot be read counts as nothing)
            pass

    def _popup(self, popup) -> None:
        """A tab the job's page opened: its requests count, and a first
        request held for it (the same URL) is counted now."""
        self._opened.add(id(popup))
        try:
            url = self._bare(popup.url)
        except Exception:       # noqa: BLE001
            return
        keep = []
        for bare, kind, row in self._unplaced:
            if bare == url:
                (self.sent if kind == "sent" else self.possible).append(row)
            else:
                keep.append((bare, kind, row))
        self._unplaced = keep

    def start(self) -> None:
        """Listen on the page's context (a new tab's first request reaches
        only the context) and for the tabs the page opens."""
        if self._on:
            return
        self._on = True
        try:
            self._inbox = _host(str(self.run.r.run_context().get("inbox_url") or ""))
            if self._inbox and _host(str(self.page.url)) == self._inbox:
                # the application is served from the inbox's own host: only
                # the tab rule keeps the inbox out
                self._inbox = ""
        except Exception:       # noqa: BLE001  (a runner double)
            self._inbox = ""
        try:
            self._before = {id(p) for p in self.page.context.pages if p is not self.page}
        except Exception:       # noqa: BLE001  (a page double)
            self._before = set()
        context = getattr(self.page, "context", None) or self.page
        for target, events in ((context, (("request", self._request),
                                          ("response", self._answered),
                                          ("requestfinished", self._finished),
                                          ("requestfailed", self._done))),
                               (self.page, (("popup", self._popup),))):
            for event, fn in events:
                try:
                    target.on(event, fn)
                    self._targets.append((target, event, fn))
                except Exception:   # noqa: BLE001  (a page double)
                    pass

    def stop(self) -> None:
        if not self._on:
            return
        self._on = False
        for target, event, fn in self._targets:
            try:
                target.remove_listener(event, fn)
            except Exception:   # noqa: BLE001  (the page is gone)
                pass
        self._targets = []
        # a held send no tab claimed may have been the send
        self.possible += [r for r in self.unplaced_sends() if r not in self.possible]
        self._unplaced = []

    def unplaced_sends(self) -> list[str]:
        """The held POST, PUT or PATCH rows no tab of the job's page claimed."""
        return [row for _, _, row in self._unplaced
                if row.split(" ", 1)[0] in self._SEND_METHODS]

    def any(self) -> bool:
        """Something that may have been the send left."""
        return bool(self.sent or self.possible or self.unplaced_sends())

    def first(self) -> str:
        return (self.sent or self.possible or self.unplaced_sends() or [""])[0]

    def only(self, row: str) -> bool:
        """Is `row` the one request seen that may have been the send?"""
        rows = self.sent + self.possible + self.unplaced_sends()
        return rows == [row]


def new_confirmation(before: str, after: str) -> str:
    """A received phrase the page shows now and did not show before the
    submit click, or ""."""
    fresh = sorted(confirmation_words(after) - confirmation_words(before))
    return fresh[0] if fresh else ""


class LateWatch:
    """The popups an entry click opens after `click_entry` stopped waiting
    (a site that shows "Opening..." and opens the tab a second later): the
    listener stays on the page until `stop`."""

    def __init__(self, page, signal: str, source_url: str):
        self.page = page
        self.signal = signal
        self.source_url = source_url
        self.popups: list = []
        self._on = False

    def _add(self, popup) -> None:
        self.popups.append(popup)

    def start(self) -> None:
        try:
            self.page.on("popup", self._add)
            self._on = True
        except Exception:       # noqa: BLE001  (a page double)
            pass

    def stop(self) -> None:
        if self._on:
            self._on = False
            try:
                self.page.remove_listener("popup", self._add)
            except Exception:   # noqa: BLE001  (the page is gone)
                pass
