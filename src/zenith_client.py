"""HTTP client for Zenith TT Interactive (asia.ttinteractive.com).

Bulk extracts customer records (name, email, phone, address, etc.) given
a list of Customer IDs. Read-only — never POSTs to update/create.

Flow (one-time, per session):

  1. POST  /otds/index.asp?action=TESTLOGIN           credentials
  2. Follow 4-5 redirects → /NewUI/aerien/f_index.asp
  3. Parse `var stateValues = {...}` for ID_ADMIN, ID_SOCIETE, ID_APPLICATION
  4. GET   /TTIDotNet/.../InitUserContext.aspx?...    initializes ASPX session

Flow (per customer):

  5. GET   /TTIDOTNET/.../FinalCustomer.ashx?IdCustomer={id}    302 →
  6. GET   /TTIDOTNET/.../Customer.aspx?taskId={new_uuid}&...   ~150 KB HTML

The HTML is parsed by regex — ASP.NET WebForms field names like
`mUsrMain$UsrFinalCustomer$UsrFinalCustomer_MainInformation1$txtLastName`
are stable enough across customers to anchor extraction.

Concurrency model: `fetch_many` uses a ThreadPoolExecutor with N workers,
each making serial calls with a configurable inter-call delay. Per-worker
delay × N workers = effective rate. Tests at N=1 first, then dial up.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable

import requests

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://asia.ttinteractive.com"
LOGIN_URL = f"{BASE_URL}/otds/index.asp?action=TESTLOGIN"
LANDING_URL = f"{BASE_URL}/NewUI/aerien/f_index.asp"
INIT_CONTEXT_URL = (
    f"{BASE_URL}/TTIDotNet/Transport/TransportNetBO2/Sales/InitUserContext.aspx"
)
CUSTOMER_LOOKUP_URL = (
    f"{BASE_URL}/TTIDOTNET/TRANSPORT/TRANSPORTNETBO2/SALES2/CustomerViews/FinalCustomer.ashx"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Lookup status codes recorded in the cache.
STATUS_OK = "OK"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_ERROR = "ERROR"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ZenithError(Exception):
    """Base class — any failure talking to Zenith."""


class LoginError(ZenithError):
    """Credentials rejected, or company code wrong."""


class SessionExpiredError(ZenithError):
    """A previously-good session is no longer valid. Caller should re-login."""


class CustomerNotFoundError(ZenithError):
    """Zenith found no customer with the given ID."""


class RateLimitedError(ZenithError):
    """Zenith returned 429 / 503 / a known rate-limit signal."""


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CustomerRecord:
    """One parsed customer page.

    The schema mirrors the on-screen "Edit Final Customer" form so users
    recognize the columns immediately. All fields are strings (no
    parsing/normalization) — Zenith's own format is what hits the Excel.
    """

    customer_id: str
    title: str = ""
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    date_of_birth: str = ""
    email: str = ""
    home_phone: str = ""
    home_phone_international: str = ""
    mobile_phone: str = ""
    mobile_phone_international: str = ""
    office_phone: str = ""
    nationality: str = ""
    language: str = ""
    spoken_language: str = ""
    address: str = ""
    city: str = ""
    postal_code: str = ""
    country: str = ""
    registration_date: str = ""


@dataclass(frozen=True)
class LookupResult:
    """Outcome of one fetch attempt — success, not-found, or error."""

    customer_id: str
    status: str  # STATUS_OK / STATUS_NOT_FOUND / STATUS_ERROR
    record: CustomerRecord | None = None
    error: str = ""
    checked_at: str = ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# ASP.NET WebForms prefixes every input with a long control hierarchy;
# we anchor on the leaf `txtFieldName` / `ddlFieldName` only.
_TEXT_FIELD_RE = re.compile(
    r'name="[^"]*\$(txt[A-Za-z]+)"[^>]*?\bvalue="([^"]*)"',
    re.IGNORECASE,
)
_SELECT_BLOCK_RE = re.compile(
    r'<select[^>]+name="[^"]*\$(ddl[A-Za-z]+)"[^>]*>(.*?)</select>',
    re.IGNORECASE | re.DOTALL,
)
_SELECTED_OPTION_RE = re.compile(
    r'<option[^>]+selected[^>]*>([^<]+)</option>',
    re.IGNORECASE,
)
_REG_DATE_RE = re.compile(
    r'id="[^"]*_lblRegistrationDate"[^>]*>([^<]+)</span>',
    re.IGNORECASE,
)

# A NOT_FOUND page typically lacks any txtFirstName / txtLastName field.
# We treat "no first AND no last name AND no email" as missing.
_MARKERS_OF_PRESENCE = ("txtFirstName", "txtLastName", "txtEmail")


def parse_customer_html(html: str, customer_id: str) -> CustomerRecord:
    """Extract a CustomerRecord from the Customer.aspx HTML.

    Raises CustomerNotFoundError if the page doesn't look like a real
    customer page (e.g. Zenith silently redirects to an empty form when
    the ID doesn't exist).
    """
    if not any(marker in html for marker in _MARKERS_OF_PRESENCE):
        raise CustomerNotFoundError(f"No customer fields in page for ID {customer_id}")

    text_fields = dict(_TEXT_FIELD_RE.findall(html))
    select_fields: dict[str, str] = {}
    for name, block in _SELECT_BLOCK_RE.findall(html):
        m = _SELECTED_OPTION_RE.search(block)
        if m:
            select_fields[name] = m.group(1).strip()

    reg_date_match = _REG_DATE_RE.search(html)
    registration_date = reg_date_match.group(1).strip() if reg_date_match else ""

    def t(name: str) -> str:
        return text_fields.get(name, "").strip()

    def s(name: str) -> str:
        v = select_fields.get(name, "").strip()
        # Zenith uses "Select..." for empty dropdowns — treat as blank.
        return "" if v in ("Select...", "Select…") else v

    # `txtCountry` lives on the address card and is sometimes a text field;
    # the dropdown form uses ddlCountry. Prefer the dropdown when set.
    country = s("ddlCountry") or t("txtCountry")

    record = CustomerRecord(
        customer_id=customer_id,
        title=s("ddlTitle"),
        first_name=t("txtFirstName"),
        middle_name=t("txtMiddleName"),
        last_name=t("txtLastName"),
        date_of_birth=t("txtDateOfBirth"),
        email=t("txtEmail"),
        home_phone=t("txtHomePhoneNumber"),
        home_phone_international=t("txtHomePhoneNumberInternational"),
        mobile_phone=t("txtMobilePhoneNumber"),
        mobile_phone_international=t("txtMobilePhoneNumberInternational"),
        office_phone=t("txtOfficePhoneNumber"),
        nationality=s("ddlNationality"),
        language=s("ddlLanguage"),
        spoken_language=s("ddlSpokenLanguage"),
        address=t("txtAddress"),
        city=t("txtCity"),
        postal_code=t("txtPostalCode"),
        country=country,
        registration_date=registration_date,
    )
    return record


_STATE_VALUES_RE = re.compile(
    r'var\s+stateValues\s*=\s*\{(.*?)\}\s*;',
    re.DOTALL,
)
_STATE_KV_RE = re.compile(r'"([^"]+)"\s*:\s*"([^"]*)"')


def parse_state_values(landing_html: str) -> dict[str, str]:
    """Extract the `var stateValues = { ... }` block from f_index.asp."""
    m = _STATE_VALUES_RE.search(landing_html)
    if not m:
        raise LoginError(
            "Landing page didn't contain stateValues — login may have failed "
            "or the page layout changed."
        )
    return dict(_STATE_KV_RE.findall(m.group(1)))


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class ZenithSession:
    """One logged-in Zenith session.

    Use `from_credentials` to log in; call `fetch_customer` to look up
    individual IDs. The session is thread-safe for read-only customer
    lookups because requests.Session locks per-host connection pools —
    but each fetch creates its own request, no shared mutable state.
    """

    session: requests.Session
    state_values: dict[str, str] = field(default_factory=dict)
    company_code: str = "usba"

    @classmethod
    def from_credentials(
        cls,
        username: str,
        password: str,
        *,
        company_code: str = "usba",
        timeout_s: float = 30.0,
    ) -> "ZenithSession":
        """Run the full login + init-context handshake."""
        sess = requests.Session()
        sess.headers["User-Agent"] = USER_AGENT

        # Step 1: TESTLOGIN POST. Allow redirects — Zenith chains 4-5 of them.
        log.info("Zenith login as %r (company=%s)", username, company_code)
        try:
            resp = sess.post(
                LOGIN_URL,
                data={
                    "Secured": "1",
                    "login": username,
                    "pwd": password,
                    "LoginCompanyIdentificationCode": company_code,
                    "imageFond": "/otds/images/aeropack.jpg",
                    "smartclient": "",
                    "extranetagencyURL": "",
                    "RadioApplication": "Aeropack",
                },
                allow_redirects=True,
                timeout=timeout_s,
            )
        except requests.RequestException as exc:
            raise LoginError(f"Network error during login: {exc}") from exc

        # If the final page is still on /otds/, the login failed.
        if "/otds/" in resp.url and "f_index" not in resp.url:
            raise LoginError(
                f"Login rejected by Zenith — landed at {resp.url} instead of "
                "the dashboard. Check username, password, and company code."
            )

        # Step 2: Make sure we have the landing page (which carries stateValues).
        if "f_index" not in resp.url:
            log.info("Following extra redirect to landing page")
            resp = sess.get(LANDING_URL, timeout=timeout_s)
            resp.raise_for_status()

        state_values = parse_state_values(resp.text)
        log.info(
            "Zenith state values: user=%s company=%s app=%s",
            state_values.get("ID_ADMIN"),
            state_values.get("ID_SOCIETE"),
            state_values.get("ID_APPLICATION"),
        )

        # Step 3: InitUserContext — primes the .NET BO session.
        init_resp = sess.get(
            INIT_CONTEXT_URL,
            params={
                "Id_Utilisateur": state_values.get("ID_ADMIN", ""),
                "Id_Societe": state_values.get("ID_SOCIETE", ""),
                "Id_Application": state_values.get("ID_APPLICATION", "3"),
                "Id_Langue": state_values.get("ID_LANGUE", "2"),
            },
            timeout=timeout_s,
        )
        init_resp.raise_for_status()

        return cls(session=sess, state_values=state_values, company_code=company_code)

    def fetch_customer(
        self, customer_id: str, *, timeout_s: float = 45.0,
    ) -> CustomerRecord:
        """Fetch and parse one customer record.

        Raises CustomerNotFoundError, SessionExpiredError, RateLimitedError,
        or ZenithError on failures.
        """
        params = {"IdCustomer": str(customer_id).strip()}
        try:
            resp = self.session.get(
                CUSTOMER_LOOKUP_URL,
                params=params,
                allow_redirects=True,
                timeout=timeout_s,
            )
        except requests.RequestException as exc:
            raise ZenithError(f"Network error fetching {customer_id}: {exc}") from exc

        if resp.status_code in (401, 403):
            raise SessionExpiredError(
                f"Zenith returned {resp.status_code} for ID {customer_id} — "
                "session expired or never authenticated."
            )
        if resp.status_code in (429, 503):
            raise RateLimitedError(
                f"Zenith returned {resp.status_code} for ID {customer_id} — "
                "back off and retry."
            )
        if resp.status_code >= 500:
            raise ZenithError(
                f"Zenith returned {resp.status_code} for ID {customer_id}."
            )
        # 200 means we got the customer page (or a not-found surrogate).
        if "/otds/" in resp.url:
            raise SessionExpiredError(
                f"Zenith redirected ID {customer_id} back to login — session lost."
            )
        return parse_customer_html(resp.text, str(customer_id))


# ---------------------------------------------------------------------------
# Concurrent bulk fetcher
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def fetch_many(
    session: ZenithSession,
    customer_ids: Iterable[str],
    *,
    concurrency: int = 3,
    delay_s: float = 0.8,
    progress_cb: Callable[[LookupResult, int, int], None] | None = None,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    on_session_expired: Callable[[], bool] | None = None,
) -> list[LookupResult]:
    """Look up many customer IDs concurrently.

    Args:
      session: a logged-in ZenithSession.
      customer_ids: iterable of strings.
      concurrency: number of worker threads (1..10 is the sane range).
      delay_s: per-worker delay between calls — politeness to Zenith.
      progress_cb: called after each result with (result, completed, total).
      stop_event: setting this causes new work to stop and the call to return.
      pause_event: when set, workers block in a tight loop until it clears.
      on_session_expired: called when a SessionExpiredError is caught.
                          Returns True to retry after caller re-logs in;
                          False to abort.

    Returns:
      LookupResult list in completion order — caller is expected to keep
      a reference to the input list if they need positional order.
    """
    ids = [str(i).strip() for i in customer_ids if str(i).strip()]
    results: list[LookupResult] = []
    total = len(ids)
    if total == 0:
        return results

    concurrency = max(1, min(int(concurrency), 10))
    delay_s = max(0.0, float(delay_s))

    def _worker(cid: str) -> LookupResult:
        # Cooperative pause
        if pause_event is not None:
            while pause_event.is_set():
                if stop_event is not None and stop_event.is_set():
                    return LookupResult(
                        customer_id=cid, status=STATUS_ERROR,
                        error="cancelled", checked_at=_now_iso(),
                    )
                time.sleep(0.2)
        if stop_event is not None and stop_event.is_set():
            return LookupResult(
                customer_id=cid, status=STATUS_ERROR,
                error="cancelled", checked_at=_now_iso(),
            )
        try:
            record = session.fetch_customer(cid)
            result = LookupResult(
                customer_id=cid, status=STATUS_OK, record=record,
                checked_at=_now_iso(),
            )
        except CustomerNotFoundError as exc:
            result = LookupResult(
                customer_id=cid, status=STATUS_NOT_FOUND,
                error=str(exc), checked_at=_now_iso(),
            )
        except SessionExpiredError as exc:
            # Surface to caller and stop.
            log.warning("Session expired during fetch of %s", cid)
            if on_session_expired is not None:
                handled = on_session_expired()
                if not handled and stop_event is not None:
                    stop_event.set()
            elif stop_event is not None:
                stop_event.set()
            result = LookupResult(
                customer_id=cid, status=STATUS_ERROR,
                error=f"SessionExpired: {exc}", checked_at=_now_iso(),
            )
        except RateLimitedError as exc:
            # Sleep longer than the delay then continue.
            log.warning("Rate limited on %s — sleeping 10s", cid)
            time.sleep(10.0)
            result = LookupResult(
                customer_id=cid, status=STATUS_ERROR,
                error=f"RateLimited: {exc}", checked_at=_now_iso(),
            )
        except Exception as exc:  # noqa: BLE001 — surface any leak
            log.exception("Unexpected error fetching %s", cid)
            result = LookupResult(
                customer_id=cid, status=STATUS_ERROR,
                error=f"{type(exc).__name__}: {exc}", checked_at=_now_iso(),
            )
        # Per-worker polite delay AFTER each call.
        if delay_s > 0:
            time.sleep(delay_s)
        return result

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_worker, cid): cid for cid in ids}
        completed = 0
        for fut in as_completed(futures):
            completed += 1
            r = fut.result()
            results.append(r)
            if progress_cb is not None:
                try:
                    progress_cb(r, completed, total)
                except Exception:  # noqa: BLE001 — never let callback kill the run
                    log.exception("progress callback raised")
            if stop_event is not None and stop_event.is_set():
                # Drain any remaining futures as cancelled
                for remaining in futures:
                    if not remaining.done():
                        remaining.cancel()
                break
    return results
