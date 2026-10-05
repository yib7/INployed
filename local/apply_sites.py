"""Which site a URL belongs to: the ATS, front-end, tracker, aggregator,
CAPTCHA, identity and inbox tables, the registrable site and the ATS tenant
of a host, the tracking hosts a request after the submit click never counts
on, and the readers of a job board's or a verification email's links.

Split out of `apply_run`, which re-exports these names.
"""
from __future__ import annotations

import re
from html import unescape
from typing import Any, Mapping
from urllib.parse import urlsplit

import apply_click
import apply_form
import apply_judge
import apply_linkedin
import ats_accounts
import jev
from apply_outcome import (LINK_BOT_WORDS, LINK_CHALLENGE_STATUS, LINK_DOWN_WORDS,
                           LINK_FAILED_WORDS, LINK_VERIFIED_WORDS)


LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_HOSTS = ("linkedin.com", "www.linkedin.com")
# The sites (registrable domains) of the platforms most applications run on. A
# page or a frame on one of them is part of the application wherever the flow
# met it: a company page embeds Greenhouse, a careers site hands off to Workday,
# an iCIMS portal signs in on login.icims.com.
ATS_SITES = frozenset((
    "greenhouse.io", "lever.co", "ashbyhq.com", "icims.com", "myworkdayjobs.com",
    "myworkdaysite.com", "myworkday.com", "workday.com", "smartrecruiters.com",
    "jobvite.com", "workable.com", "bamboohr.com", "taleo.net", "oraclecloud.com",
    "successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu", "brassring.com",
    "adp.com", "ultipro.com", "ukg.com", "dayforcehcm.com", "applytojob.com",
    "jazzhr.com", "recruitee.com", "teamtailor.com", "breezy.hr", "pinpointhq.com",
    "rippling.com", "paylocity.com", "paycomonline.net", "avature.net", "eightfold.ai",
    "gem.com", "comeet.com", "personio.de", "personio.com", "csod.com",
    "clearcompany.com", "hrmdirect.com", "applicantpro.com", "isolvedhire.com",
    "phenompeople.com", "trinethire.com",
    # ALLOW-01 (the audit's list)
    "jobs2web.com", "selectminds.com", "saashr.com", "pageuppeople.com", "silkroad.com",
    "hirebridge.com", "zohorecruit.com", "bullhornstaffing.com", "jobdiva.com", "ceipal.com",
    "paycor.com", "recruitingbypaycor.com", "freshteam.com", "hireology.com", "careerplug.com",
    "catsone.com", "applicantstack.com", "trakstar.com", "careers-page.com", "jobscore.com",
    "peopleadmin.com", "governmentjobs.com", "jazz.co", "harri.com", "fountain.com",
    "gusto.com", "dover.com", "wellfound.com"))
# Career-site front ends on `ATS_SITES`: a company's job search and posting
# pages whose Apply hands the application to the company's own application
# platform (its Workday, SuccessFactors, Taleo or iCIMS). They stay allowed
# sites, and the job's account is pinned on the platform the hand-off reaches.
FRONT_END_SITES = frozenset(("phenompeople.com", "eightfold.ai", "avature.net",
                             "jobs2web.com", "selectminds.com"))
# Programmatic-ad trackers and link shorteners between a posting's Apply and
# the careers site: a hop to wait out, never the destination, never
# a place for the master password.
TRACKER_SITES = frozenset((
    "appcast.io", "joveo.com", "pandologic.com", "recruitics.com", "grnh.se", "lnkd.in",
    "bit.ly", "tinyurl.com", "ow.ly", "buff.ly", "rebrand.ly", "clickcast.cloud",
    "jobadx.com", "talentify.io", "cvtrack.com", "jobs2careers.com"))
# Job boards and aggregators: a posting there is followed once to
# the company's own site through its "Apply on company site" control, and
# never signed in on or filled.
AGGREGATOR_SITES = frozenset((
    "dice.com", "lensa.com", "jobright.ai", "talent.com", "ziprecruiter.com", "indeed.com",
    "jooble.org", "glassdoor.com", "builtin.com", "jobot.com", "welcometothejungle.com",
    "hiring.cafe", "simplyhired.com", "careerbuilder.com", "monster.com", "snagajob.com",
    "adzuna.com", "jobleads.com", "theladders.com"))
NAV_ATS_MAX = 20                   # ATS hosts a job's tabs were sent to, kept in order
TRACKER_HOPS_MAX = 3               # tracker hops waited out after one entry click
AGGREGATOR_BOARDS_MAX = 2          # job boards read for their company link in one job
# Bot-check providers. Their frames' controls are never filled or clicked: a
# challenge is the user's to solve in the visible window. DataDome
# (`captcha-delivery.com`) and PerimeterX serve full-page checks.
CAPTCHA_SITES = frozenset(("hcaptcha.com", "recaptcha.net", "arkoselabs.com",
                           "funcaptcha.com", "geetest.com", "captcha-delivery.com",
                           "perimeterx.net", "px-cloud.net", "px-cdn.net"))
_SECOND_LEVEL = frozenset(("co", "com", "org", "net", "ac", "gov", "edu", "ne", "or", "go"))
# Hosting domains whose subdomains belong to different owners: each
# `<name>.github.io` is its own site, never one site with every other.
_SHARED_HOSTING = frozenset((
    "github.io", "herokuapp.com", "azurewebsites.net", "vercel.app", "netlify.app",
    "pages.dev", "workers.dev", "web.app", "firebaseapp.com", "appspot.com",
    "cloudfront.net", "amazonaws.com", "blogspot.com", "wixsite.com", "webflow.io",
    "onrender.com", "fly.dev", "glitch.me", "ngrok.io", "ngrok-free.app", "surge.sh",
    "wordpress.com", "sharepoint.com", "notion.site", "weebly.com", "squarespace.com",
    "myshopify.com", "wixstudio.io", "editorx.io", "jimdosite.com", "webnode.com",
    "square.site", "carrd.co", "framer.website", "framer.app", "godaddysites.com",
    "wpengine.com", "wpcomstaging.com", "gitlab.io", "bitbucket.io", "readthedocs.io",
    "repl.co", "replit.app", "railway.app", "deno.dev", "azurestaticapps.net",
    "cloudapp.net", "windows.net", "ondigitalocean.app", "digitaloceanspaces.com",
    "hs-sites.com", "hubspotpagebuilder.com", "myportfolio.com", "tumblr.com",
    "substack.com", "googleusercontent.com", "zohosites.com",
    "strikingly.com", "mystrikingly.com", "webstarts.com", "site123.me", "yolasite.com"))
# Hosts with many owners on the one host, each under its own path
# (`sites.google.com/view/<name>`): each such host is a site of its own, never
# one site with the rest of its domain (a Google sign-in, a Google Doc).
_SHARED_HOSTS = frozenset((
    "sites.google.com", "docs.google.com", "drive.google.com", "storage.googleapis.com",
    "forms.office.com", "forms.microsoft.com", "s3.amazonaws.com"))
# Shared hosting whose names nest under one another
# (`bucket.s3.amazonaws.com`, `account.blob.core.windows.net`): a host there is
# its own site, the whole name.
_NESTED_HOSTING = frozenset(("amazonaws.com", "windows.net", "googleusercontent.com"))
# Oracle's recruiting hosts (`<tenant>.fa.<region>.oraclecloud.com`). Oracle's
# other cloud hosts (object storage, any customer's app) serve anyone's pages
# and are never the platform.
_ORACLE_RECRUITING = re.compile(r"^[a-z0-9-]+\.fa\.[a-z0-9-]+\.oraclecloud\.com$")
# One vendor's sites: a job's account on one of them may move between them
# (Workday's careers and sign-in hosts, SuccessFactors' career site and its
# recruiting pages).
_PLATFORMS = (
    frozenset(("myworkdayjobs.com", "myworkdaysite.com", "myworkday.com", "workday.com")),
    frozenset(("successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu",
               "jobs2web.com")),
    frozenset(("ultipro.com", "ukg.com", "saashr.com")),
    frozenset(("applytojob.com", "jazzhr.com", "jazz.co")),
    frozenset(("paycor.com", "recruitingbypaycor.com")),
    frozenset(("personio.de", "personio.com")),
    frozenset(("oraclecloud.com", "taleo.net")),
)
# Host labels that name no company on an ATS platform: the boards, sign-in,
# API and asset hosts every company there shares
# (`boards.greenhouse.io`, `login.icims.com`, `workforcenow.adp.com`).
_SHARED_TENANT_LABELS = frozenset((
    "www", "jobs", "job", "careers", "career", "apply", "boards", "job-boards", "boards-api",
    "app", "apps", "api", "login", "auth", "sso", "accounts", "account", "secure", "my",
    "cdn", "static", "assets", "recruiting", "recruit", "hire", "hiring", "talent", "portal",
    "mail", "embed", "id", "signin", "candidate", "candidates", "external", "fa", "identity",
    "ats", "workforcenow", "myjobs", "sjobs", "en", "us", "eu", "uk", "ca", "au", "de"))
# a platform's numbered data-centre and pod labels (`wd5`, `career4`, `us58`)
_NUMBERED_LABEL = re.compile(r"^(?:wd|career|performancemanager|recruiting|hcm|dc|us|eu|ca|"
                             r"uk|au|ats|jobs|secure|pod|na|emea|apac)\d+[a-z]?$")
_LOOPBACK_HOSTS = frozenset(("127.0.0.1", "::1", "localhost"))
# Senders whose mail is never the job's code or link: an identity provider's
# sign-in and security mail (beside LinkedIn and the inbox provider's own)
_IDENTITY_SITES = frozenset((
    "okta.com", "oktapreview.com", "auth0.com", "onelogin.com", "microsoftonline.com",
    "microsoft.com", "live.com", "office.com", "office365.com", "outlook.com", "hotmail.com",
    "google.com", "gmail.com", "googlemail.com", "apple.com", "icloud.com", "id.me",
    "login.gov", "duosecurity.com", "duo.com", "pingidentity.com", "pingone.com",
    "jumpcloud.com", "yahoo.com", "facebook.com", "facebookmail.com", "github.com",
    "amazon.com", "amazonaws.com"))
# the inbox providers' own mail domains, by the provider `apply_inbox` names
_INBOX_PROVIDER_SITES = {
    "gmail": frozenset(("google.com", "gmail.com", "googlemail.com")),
    "outlook": frozenset(("microsoft.com", "outlook.com", "office.com", "office365.com",
                          "live.com", "hotmail.com", "microsoftonline.com")),
}

# Hosts whose requests are never an application's send: analytics, ads,
# tag managers, error reporting, consent logging (by registrable site). A
# POST there after the submit click proves nothing either way.
_TRACKING_SITES = frozenset((
    "google-analytics.com", "googletagmanager.com", "doubleclick.net", "googleadservices.com",
    "googlesyndication.com", "facebook.com", "facebook.net", "bing.com", "clarity.ms",
    "hotjar.com", "hotjar.io", "segment.io", "segment.com", "mixpanel.com", "amplitude.com",
    "fullstory.com", "heapanalytics.com", "nr-data.net", "newrelic.com", "sentry.io",
    "datadoghq.com", "datadoghq.eu", "quantserve.com", "quantcast.com", "adroll.com",
    "tiktok.com", "twitter.com", "ads-twitter.com", "snapchat.com", "pinterest.com",
    "reddit.com", "criteo.com", "taboola.com", "outbrain.com", "hs-analytics.net",
    "hsadspixel.net", "licdn.com", "cookielaw.org", "onetrust.com", "cookiebot.com",
    "trustarc.com", "usercentrics.eu", "osano.com", "didomi.io", "bugsnag.com",
    "rollbar.com", "logrocket.io", "logrocket.com", "mouseflow.com", "crazyegg.com",
    "optimizely.com", "demdex.net", "omtrdc.net", "adobedc.net", "everesttech.net",
    "adsrvr.org", "adnxs.com", "rubiconproject.com", "pubmatic.com", "casalemedia.com",
    "criteo.net", "scorecardresearch.com", "intercom.io", "intercomcdn.com", "drift.com",
    "driftt.com", "crisp.chat", "tawk.to", "livechatinc.com", "olark.com", "zdassets.com",
    "sc-static.net", "t.co", "cloudflareinsights.com"))
# Google's own tag and ad paths; a form on docs.google.com is a real send
_GOOGLE_TRACKING = ("/ccm/", "/pagead/", "/ads/", "/g/collect", "/j/collect", "/recaptcha")
# Cloudflare's beacon and challenge paths, served on the site's own host
_CLOUDFLARE_PATHS = ("/cdn-cgi/rum", "/cdn-cgi/challenge-platform/")


def _tracking(url: str) -> bool:
    parts = urlsplit(str(url or ""))
    site = _site(parts.hostname or "")
    if site in _TRACKING_SITES or parts.path.startswith(_CLOUDFLARE_PATHS):
        return True
    return site == "google.com" and parts.path.startswith(_GOOGLE_TRACKING)


def _host(url_or_netloc: str) -> str:
    """The lowercased hostname of a URL or a netloc, without a port."""
    raw = str(url_or_netloc or "").strip()
    if "://" in raw:
        return (urlsplit(raw).hostname or "").lower()
    return raw.split("/")[0].rsplit("@", 1)[-1].split(":")[0].lower()


# the one create-account link `_Accounts._click_link` clicks, marked for its
# locator; the mark moves to the link it is set on
_MARKED_LINK = "[data-apply-signup-link='1']"
_MARK_LINK_JS = """el => {
  document.querySelectorAll('[data-apply-signup-link]')
    .forEach((n) => n.removeAttribute('data-apply-signup-link'));
  el.setAttribute('data-apply-signup-link', '1');
}"""


def _scripted_link(target: str, page_url: str) -> bool:
    """Is a link's resolved `target` no address of its own: a scheme other
    than http(s) (`javascript:void(0)`), or the page itself with only a
    fragment added ("#", "#signup")? Such a link is the page's script's to
    follow."""
    parts = urlsplit(str(target or ""))
    if parts.scheme.lower() not in ("http", "https"):
        return True
    here = urlsplit(str(page_url or ""))
    return (bool(parts.fragment) or str(target).endswith("#")) and \
        parts._replace(fragment="") == here._replace(fragment="")


def _known_sites() -> frozenset[str]:
    """Every site the module names in a list (ATS, tracker, aggregator,
    bot-check and analytics hosts): `_site` reads a host under one of them
    as that site."""
    global _KNOWN_SITES
    if _KNOWN_SITES is None:
        _KNOWN_SITES = (ATS_SITES | TRACKER_SITES | AGGREGATOR_SITES | CAPTCHA_SITES
                        | _TRACKING_SITES)
    return _KNOWN_SITES


_KNOWN_SITES: frozenset[str] | None = None


def _site(url_or_host: str) -> str:
    """The site a URL or host belongs to, for every same-site rule (the
    master password, the allowed hosts, a send's destination), read with no
    public suffix list and so leaning to the narrower answer:
    - a host under a site the module lists (`_known_sites`) is that site
      (`jobs.lever.co` -> `lever.co`);
    - a host on shared hosting (`_SHARED_HOSTING`) is its own name there
      (`careers.acme.github.io` -> `acme.github.io`), since every name has
      its own owner; on Amazon's, whose names nest (`bucket.s3.amazonaws.com`),
      the whole host;
    - under a two-letter country code, three labels when the second level
      is generic (`jobs.example.co.uk` -> `example.co.uk`), else the whole
      host: `ltd.uk`, `gc.ca`, `gouv.fr` or `tx.us` are suffixes that
      hold many owners, and a host there is never grouped with another;
    - a host many owners share by path (`_SHARED_HOSTS`: `sites.google.com`)
      is its own site;
    - on Oracle's cloud, only a recruiting host (`_ORACLE_RECRUITING`) is the
      platform's site; any other Oracle cloud host is its own;
    - else the last two labels."""
    host = _host(url_or_host)
    if not host or host.replace(".", "").isdigit() or "." not in host or ":" in host:
        return host             # an IP address, `localhost`
    if host in _SHARED_HOSTS:
        return host
    if host.endswith(".oraclecloud.com") and not _ORACLE_RECRUITING.match(host):
        return host
    labels = [p for p in host.split(".") if p]
    known = _known_sites()
    for i in range(len(labels) - 1):
        tail = ".".join(labels[i:])
        if tail in known:
            return tail
    if len(labels) >= 3 and ".".join(labels[-2:]) in _SHARED_HOSTING:
        if ".".join(labels[-2:]) in _NESTED_HOSTING:
            return ".".join(labels)
        return ".".join(labels[-3:])
    if len(labels) >= 3 and len(labels[-1]) == 2:
        if labels[-2] in _SECOND_LEVEL:
            return ".".join(labels[-3:])
        return ".".join(labels)
    return ".".join(labels[-2:])


def _platform(site: str) -> frozenset[str]:
    """The sites of the vendor `site` belongs to (`_PLATFORMS`); a site no
    vendor shares stands alone."""
    return next((p for p in _PLATFORMS if site in p), frozenset((site,)))


def _ats_tenant(url_or_host: str) -> str:
    """The company account a host on an ATS platform names, "" for a host
    every company there shares or a host off the platforms: the tenant
    `ats_accounts.tenant_key` reads (Workday, iCIMS), else the host's labels
    left of its site without the shared and numbered ones
    (`acme.bamboohr.com` -> "acme", `ehxx.fa.us2.oraclecloud.com` ->
    "ehxx", `boards.greenhouse.io` and `wd5.myworkdaysite.com` -> "")."""
    host = _host(url_or_host)
    site = _site(host)
    if site not in ATS_SITES:
        return ""
    key = ats_accounts.tenant_key(host)
    if key:
        return key.split("/", 1)[1]
    left = host[:-len(site)].rstrip(".") if host.endswith("." + site) else ""
    names = [label for label in left.split(".") if label
             and label not in _SHARED_TENANT_LABELS and not _NUMBERED_LABEL.match(label)]
    return ".".join(names)


def _insecure(url: str) -> bool:
    """Does `url` reach its site unencrypted? An `http` address anywhere but
    this machine's own loopback (`127.0.0.1`, `::1`, `localhost`). A bare
    host, or a scheme that names no host (`about:`, `blob:`), is no
    such address."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme.lower() != "http":
        return False
    host = (parts.hostname or "").lower()
    return not (host in _LOOPBACK_HOSTS or host.endswith(".localhost"))


def _sender_site(sender: str) -> str:
    """The site of a listed message's sender address ("" when the row shows
    a name alone)."""
    found = re.findall(r"[\w.+'-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)", str(sender or "").lower())
    return _site(found[-1]) if found else ""


def _is_captcha_url(url: str) -> bool:
    """A bot-check provider's page: hCaptcha, reCAPTCHA (Google's `/recaptcha`
    paths too), Cloudflare's challenge host, Arkose, GeeTest."""
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if not host:
        return False
    if _site(host) in CAPTCHA_SITES or host == "challenges.cloudflare.com":
        return True
    return _site(host) == "google.com" and parts.path.startswith("/recaptcha")


def _link_challenge(answer) -> tuple[str, str]:
    """What the answer to a verification link's fetch is in place of the
    link's page: ("check", what) for the site's bot check, Cloudflare's `cf-mitigated: challenge` header or a 403, 429 or
    503 whose page asks for one (`LINK_BOT_WORDS`); ("status", what) for
    any other 403, 429 or 503, with the page's words when it says the site
    is down or busy (`LINK_DOWN_WORDS`, read before a check's words); ("",
    "") for any other answer, and for a 403, 429 or 503 whose page says the
    link was refused (`LINK_FAILED_WORDS`) or the address is verified
    already (`LINK_VERIFIED_WORDS`): that page loads and is read as the
    link's own."""
    headers = {str(k).lower(): str(v) for k, v in dict(answer.headers or {}).items()}
    if headers.get("cf-mitigated", "").strip().lower() == "challenge":
        return "check", "cf-mitigated: challenge"
    if answer.status not in LINK_CHALLENGE_STATUS:
        return "", ""
    try:
        body = answer.text()
    except Exception:           # noqa: BLE001  (a body that does not decode says nothing)
        body = ""
    body = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", body)
    words = " ".join(unescape(re.sub(r"<[^>]*>", " ", body)).split())
    if LINK_FAILED_WORDS.search(words) or LINK_VERIFIED_WORDS.search(words):
        return "", ""
    status = f"HTTP {answer.status}"
    down = LINK_DOWN_WORDS.search(words)
    if down:
        return "status", f"{status} (the page says {down.group(0)!r})"
    bot = LINK_BOT_WORDS.search(words)
    if bot:
        return "check", f"{status}; the page says {bot.group(0)!r}"
    return "status", status


# a verification link's settled page as a bot check reads it: the main
# frame's own text, on a page with no box to fill, as a
# check's page is; "" for a page with a box. A CAPTCHA widget in a child
# frame, or a box beside one, belongs to the page's next step (a sign-in
# with its own CAPTCHA box once the link verified the address)
_LINK_CHECK_JS = """() => {
  const box = [...document.querySelectorAll('input, textarea, select')].some(el => {
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'checkbox', 'submit', 'button', 'image', 'reset'].includes(type)) return false;
    return el.getClientRects().length > 0;
  });
  return box || !document.body ? '' : (document.body.innerText || '');
}"""


def _link_check_text(tab) -> str:
    """The words of a verification link's settled page a bot check is read
    from (`_LINK_CHECK_JS`); "" for a page that cannot be read."""
    try:
        return str(tab.main_frame.evaluate(_LINK_CHECK_JS) or "")
    except Exception:           # noqa: BLE001  (a page gone shows no check)
        return ""


def _tracker(url_or_host: str) -> bool:
    """A programmatic-ad tracker or a link shortener (`TRACKER_SITES`)."""
    return bool(_host(url_or_host)) and _site(url_or_host) in TRACKER_SITES


def _aggregator(url_or_host: str) -> bool:
    """A job board or aggregator (`AGGREGATOR_SITES`)."""
    return bool(_host(url_or_host)) and _site(url_or_host) in AGGREGATOR_SITES


# The control on an aggregator's posting that leads to the company's own site
_COMPANY_SITE = re.compile(
    r"\bapply\s+(?:on|at|via|through)\s+(?:the\s+)?(?:company|employer)(?:'s|s)?\s+"
    r"(?:site|website|page)\b|\b(?:continue|go)\s+to\s+(?:the\s+)?(?:company|employer)"
    r"(?:'s|s)?\s+(?:site|website)\b|\bvisit\s+(?:the\s+)?(?:company|employer)(?:'s|s)?\s+"
    r"(?:site|website)\b|\bapply\s+externally\b", re.I)


def company_site_control(digest: apply_form.FormDigest, *, board: str = "",
                         targets: Mapping[int, str] | None = None) -> apply_form.Button | None:
    """The control of an aggregator's posting that leads to the company's own
    site: one that says so ("Apply on company site", "Continue to the
    employer's website"), else the one Apply-worded link (`targets`: each
    button's link target) that leads off the board (`board`: the board's
    host; M7: a board whose off-site control reads just "Apply"). Never a
    form's own button or the site's chrome; None when there is none."""
    said = next((b for b in digest.buttons if _COMPANY_SITE.search(
        str(b.text or "").translate(jev.APOSTROPHES)) and not b.in_form), None)
    if said is not None:
        return said
    off = [b for b in digest.buttons
           if apply_judge.entry_worded(b.text) and not b.in_form and not b.chrome
           and (targets or {}).get(b.n) and _site((targets or {})[b.n]) != _site(board)]
    return off[0] if len(off) == 1 else None


_LINK_TARGET_JS = ("el => { const a = el.closest('a[href]'); "
                   "return a ? String(a.href || '') : ''; }")


def link_targets(page, digest: apply_form.FormDigest) -> dict[int, str]:
    """The link each Apply-worded control leads to (its own `a[href]` or its
    enclosing one), by button number; a control that is no link is left
    out."""
    out: dict[int, str] = {}
    for b in digest.buttons:
        if not apply_judge.entry_worded(b.text):
            continue
        try:
            href = str(apply_form.resolve(page, b.locator).first.evaluate(
                _LINK_TARGET_JS, timeout=apply_click.ACTION_TIMEOUT_MS) or "")
        except Exception:       # noqa: BLE001  (a page double, a detached control)
            continue
        if href.lower().startswith(("http://", "https://")):
            out[b.n] = href
    return out


def content_frame_site(frame_url: str, page_url: str, hosts=()) -> bool:
    """May a child frame at `frame_url` be read before its page? Only a frame of the page's own site, a known ATS platform
    (`ATS_SITES`) or an admitted application host (`hosts`): an iCIMS
    content frame, a Greenhouse embed; never an embedded video or an ad. A
    blank or srcdoc frame is the page's own."""
    host = _host(frame_url)
    if not host:
        return True
    site = _site(host)
    return site == _site(page_url) or site in ATS_SITES or any(site == _site(h) for h in hosts)


def _on_linkedin_redirector(url: str) -> bool:
    """Is `url` LinkedIn's `/safety/go/` hop to an off-site Apply page (on
    any LinkedIn host)?"""
    return apply_linkedin.url_kind(url) == "redirector"


def _easy_apply(entry: Mapping[str, Any]) -> bool:
    """Is the queue entry an Easy Apply job? A legacy entry may carry the
    flag as text."""
    flag = entry.get("is_easy_apply")
    return flag is True or str(flag or "").strip().lower() in ("true", "1", "yes")
