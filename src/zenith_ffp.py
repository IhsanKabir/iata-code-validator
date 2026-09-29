"""Zenith's frequent-flyer (FFP) account search.

The FFP accounts live in the MODERN Zenith back office, not the legacy
customer screens: `CustomerAccount/IframeSearch`, a small form the customer
page embeds, searching by last name, FFP number, email, phone or FFP level.
Found from a browser HAR; every field name and marker below is as captured.

Two properties of that search shape everything that uses it:

* It shows at most 50 rows. A search for level "Silver" reported 119,847
  matches in its badge ("FFP Results 119847"), listed 50, and said "Results
  list not displaying all results. Please restrict your search criterias."
  There is no next page -- the page has no paging at all.
* The badge always carries the FULL count, even when only 50 are listed.
  So a search that is too broad says by how much, and can be narrowed until
  every row fits (see ffp_collect).

It is a modern-app page, so the session needs the PollSession bootstrap the
app's login skips (see zenith_passenger); a POST also has to send back the
anti-forgery token the form was served with.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from html import unescape

log = logging.getLogger(__name__)

#: The levels the programme has, lowest first.
LEVELS = ("Silver", "Gold", "Platinum", "Titanium")

#: Most rows one search will list, whatever its badge says.
PAGE_CAP = 50

#: The company segment of the modern back-office URLs.
DEFAULT_COMPANY = "USBangla"

_FORM_MARKER = 'id="formIframeSearch"'
_TRUNCATED_MARKER = "Results list not displaying all results"
_TOKEN_RE = re.compile(
    r'name="__RequestVerificationToken"[^>]*value="([^"]*)"', re.IGNORECASE)
_BADGE_RE = re.compile(
    r'FFP Results\s*<span class="badge">\s*([\d,]+)\s*</span>', re.IGNORECASE)
_ROW_RE = re.compile(
    r'<tr class="customerRow">(.*?)</tr>', re.IGNORECASE | re.DOTALL)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


class FFPSearchError(Exception):
    """The response was not the FFP search page (an error or timeout page)."""


class FFPSessionError(FFPSearchError):
    """Zenith no longer accepts the session -- sign in again."""


@dataclass(frozen=True)
class FFPMember:
    ffp_number: str
    customer_id: str
    last_name: str
    first_name: str
    birth_date: str
    email: str
    phone: str
    id_number: str
    level: str
    miles: str


@dataclass(frozen=True)
class SearchPage:
    #: What the badge says matched -- the full count, not what is listed.
    total: int
    members: tuple
    #: Zenith said it is not listing everything.
    truncated: bool

    @property
    def complete(self) -> bool:
        """Every match is listed, so the rows can be taken as they are.

        Zenith's own warning decides, not a count of the rows read: if one
        row were laid out differently and could not be read, counting would
        call the search incomplete for ever and split it down 30 levels --
        the same runaway the trailing-space bug caused.
        """
        return not self.truncated

    @property
    def short(self) -> int:
        """Rows Zenith said it listed but that could not be read."""
        return max(0, self.total - len(self.members)) if self.complete else 0


def _text(fragment: str) -> str:
    return unescape(" ".join(_TAG_RE.sub(" ", fragment).split())).strip()


def _span(row: str, cls: str) -> str:
    m = re.search(r'<span class="' + cls + r'"[^>]*>(.*?)</span>',
                  row, re.IGNORECASE | re.DOTALL)
    return _text(m.group(1)) if m else ""


def _hidden(row: str, cls: str) -> str:
    m = re.search(r'<input[^>]*class="' + cls + r'"[^>]*value="([^"]*)"',
                  row, re.IGNORECASE)
    return unescape(m.group(1)).strip() if m else ""


def parse_token(html: str) -> str:
    """The anti-forgery token a search POST must send back."""
    m = _TOKEN_RE.search(html or "")
    if not m:
        raise FFPSearchError("The FFP search form carried no security token.")
    return m.group(1)


def parse_member(row: str) -> FFPMember:
    """One `customerRow`. Columns, as captured: FFP number, last name,
    first name, date of birth, email, phone, ID number, level, total miles.
    Most carry a class; level and miles do not, so they are read by place."""
    cells = [_text(c) for c in _TD_RE.findall(row)]
    at = (lambda i: cells[i] if i < len(cells) else "")
    customer_id = _hidden(row, "idCustomer")
    number = re.search(r"\d{4,}", at(0))
    return FFPMember(
        # the FFP number is the customer code; a row whose number cannot be
        # read keeps its customer id rather than being dropped
        ffp_number=number.group(0) if number else customer_id,
        customer_id=customer_id,
        last_name=_span(row, "surname"),
        first_name=_span(row, "firstname"),
        birth_date=_span(row, "birthdate"),
        email=_span(row, "email"),
        phone=_span(row, "phone"),
        id_number=_span(row, "documentId"),
        level=at(7),
        miles=at(8),
    )


def parse_search_results(html: str) -> SearchPage:
    """Read a search response: the badge count, the listed rows, and whether
    Zenith said it is holding rows back.

    A page without the search form is not "no results" -- it is an error or
    a timeout page, and is raised so the caller retries rather than
    recording a gap as empty.
    """
    if _FORM_MARKER not in (html or ""):
        raise FFPSearchError("Response is not the FFP search page.")
    rows = tuple(parse_member(r) for r in _ROW_RE.findall(html))
    badge = _BADGE_RE.search(html)
    total = int(badge.group(1).replace(",", "")) if badge else len(rows)
    return SearchPage(total=total, members=rows,
                      truncated=_TRUNCATED_MARKER in html)


class FFPSearcher:
    """Runs FFP searches on a signed-in ZenithSession, one at a time.

    Retries a timeout or a server error with a growing pause; a page that
    has lost the session is re-bootstrapped once, then reported.
    """

    def __init__(self, session, *, base_url: str,
                 company: str = DEFAULT_COMPANY,
                 timeout_s: float = 180.0, attempts: int = 4):
        self.session = session
        self.base = base_url.rstrip("/")
        self.company = company
        self.timeout_s = timeout_s
        self.attempts = max(1, attempts)
        self._token = ""
        self.searches = 0
        # several workers share one searcher: one bootstrap, one counter
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        return (f"{self.base}/Zenith/BackOffice/{self.company}/en-GB/"
                f"CustomerAccount/IframeSearch")

    def _headers(self, **extra) -> dict:
        from .zenith_client import USER_AGENT
        return {"User-Agent": USER_AGENT, **extra}

    def bootstrap(self) -> None:
        """PollSession once, then fetch the empty form for its token."""
        sv = getattr(self.session, "state_values", None) or {}
        poll = (f"{self.base}/Zenith/BackOffice/{self.company}/BookingEngine/"
                f"PollSession?idUser={sv.get('ID_ADMIN', '')}"
                f"&idCompany={sv.get('ID_SOCIETE', '')}")
        r = self.session.session.get(
            poll, timeout=30, headers=self._headers(
                **{"X-Requested-With": "XMLHttpRequest"}))
        if "PollSession Successful" not in (r.text or ""):
            raise FFPSessionError(
                "Zenith did not open the FFP side of the back office "
                "(PollSession refused). Sign in to Zenith again.")
        page = self.session.session.get(self.url, timeout=self.timeout_s,
                                        headers=self._headers())
        if _FORM_MARKER not in (page.text or ""):
            raise FFPSessionError(
                "Zenith did not return the FFP search form. Sign in to "
                "Zenith again.")
        self._token = parse_token(page.text)

    def search(self, *, level: str = "", ffp_number: str = "",
               last_name: str = "", email: str = "",
               phone: str = "", extra_form: dict | None = None,
               extra_query: dict | None = None) -> SearchPage:
        """One search. `extra_form` / `extra_query` add fields the page does
        not show -- used to test whether Zenith honours a larger page."""
        with self._lock:
            if not self._token:
                self.bootstrap()
        data = {"__RequestVerificationToken": self._token,
                "LastName": last_name, "FFPNumber": ffp_number,
                "Email": email, "PhoneNumber": phone, "LevelName": level,
                **(extra_form or {})}
        rebooted = False
        for attempt in range(1, self.attempts + 1):
            last = attempt >= self.attempts
            try:
                r = self.session.session.post(
                    self.url, data=data, params=extra_query or None,
                    timeout=self.timeout_s,
                    headers=self._headers(Referer=self.url, Origin=self.base))
            except Exception as exc:          # noqa: BLE001 - network
                if last:
                    raise FFPSearchError(
                        f"Network error searching FFP ({exc}).") from exc
                time.sleep(5 * attempt)
                continue
            with self._lock:
                self.searches += 1
            if "/otds/" in (r.url or "") or r.status_code in (401, 403):
                raise FFPSessionError(
                    "Zenith signed the session out. Sign in again, then "
                    "press Collect -- it carries on where it stopped.")
            if r.status_code >= 500:
                if last:
                    raise FFPSearchError(
                        f"Zenith returned {r.status_code} for an FFP search "
                        f"{attempt} times in a row.")
                time.sleep(10 * attempt)
                continue
            try:
                page = parse_search_results(r.text)
            except FFPSearchError:
                if rebooted:
                    # a fresh bootstrap was accepted, so the sign-in is fine:
                    # this is Zenith having a bad moment, not a lost session
                    raise FFPSearchError(
                        "Zenith answered the FFP search with an error page "
                        "twice.")
                rebooted = True               # lost the modern session
                with self._lock:
                    self.bootstrap()
                data["__RequestVerificationToken"] = self._token
                continue
            self._token = parse_token(r.text) if _TOKEN_RE.search(
                r.text) else self._token
            return page
        raise FFPSearchError("FFP search did not complete.")
