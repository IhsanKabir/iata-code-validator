"""Read-only access to Zenith's pricing screens, for the season check.

Four screens are read, each the way the browser reads it (from HARs):

* Fare Editor -- the page (embedded legs, seasons, airports, passenger
  types), FareBasisEditor.aspx/GetFareBasisList and FareEditor.aspx/LoadFares.
* Quick Sale -- InitTask.aspx opens a session (302 to a taskId), then
  SalesCrypticRequest.ashx runs one terminal command.
* Penalties -- saisie_penalite.asp, searched with Action=Appliquer/Made=Seek.
* Taxes -- saisie_IATAtaxes.asp, hAction=Seek (the list for a route on a
  date) or Affiche_Result (one tax's details).

The same screens also SAVE: the Fare Editor has SaveFares and an Excel
import; the penalty and tax forms have Update and Delete; Quick Sale books,
tickets, voids and cancels (BS, TKT, VOID, XP, XS, FOP...). This client can
do none of that. Every request passes `check_allowed`, which knows exactly
the reads above -- and for the forms, that no penalty or tax value is being
sent -- and refuses anything else BEFORE it leaves the machine.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from urllib.parse import parse_qs, urlsplit

FARE_EDITOR = "/TTIDotNet/Transport/TransportNetBO2/Pricing/FareEditor.aspx"
FARE_BASIS_LIST = ("/TTIDotNet/Transport/TransportNetBO2/Pricing/"
                   "FareBasisEditor.aspx/GetFareBasisList")
LOAD_FARES = FARE_EDITOR + "/LoadFares"
QS_INIT = "/TTIDotNet/Transport/TransportNetBO2/Sales/InitTask.aspx"
QS_PAGE = "/TTIDotNet/Transport/TransportNetBO2/Sales/SalesCryptic.aspx"
QS_COMMAND = ("/TTIDotNet/Transport/TransportNetBO2/Sales/"
              "SalesCrypticRequest.ashx")
PENALTIES = "/newui/aerien/commercial/saisie_penalite.asp"
TAXES = "/newui/aerien/commercial/saisie_IATAtaxes.asp"

#: A Quick Sale fare display and nothing else: FDI, a six-letter city pair,
#: a dated day with its year, and only the display options.
FDI_RE = re.compile(
    r"^FDI[A-Z]{6}/D\d{2}[A-Z]{3}\d{2}"
    r"(/R(,(NP|N|WT|AT|FC-[A-Z]{3}))+)?$")

#: Penalty-form fields that carry a VALUE. A search sends them all empty.
_PENALTY_VALUES = ("DelaiMin", "DelaiMax", "penMod", "penDevModif", "penRemb",
                   "penDevRemb", "penNoShowMod", "penDevNoShowModif",
                   "penNoShowRemb", "penDevNoShowRemb")
_TAX_READS = ("Seek", "Affiche_Result")
_MAX_REDIRECTS = 5


class ReadOnlyViolation(Exception):
    """A request that could change Zenith was about to be sent."""


class PricingError(Exception):
    """Zenith did not answer as a pricing read should."""


class SessionLost(PricingError):
    """Zenith no longer accepts the session -- sign in again."""


def check_allowed(method: str, url: str, data=None) -> None:
    """Raise ReadOnlyViolation unless this is one of the known reads."""
    parts = urlsplit(url)
    path = parts.path
    lower = path.lower()
    method = method.upper()
    form = data if isinstance(data, dict) else {}
    if method == "GET" and lower in (FARE_EDITOR.lower(), QS_INIT.lower(),
                                     QS_PAGE.lower(), PENALTIES.lower(),
                                     TAXES.lower()):
        return
    if method == "POST" and lower in (FARE_BASIS_LIST.lower(),
                                      LOAD_FARES.lower()):
        return
    if method == "GET" and lower == QS_COMMAND.lower():
        cmd = (parse_qs(parts.query).get("txtRequest") or [""])[0].strip()
        if FDI_RE.match(cmd):
            return
        raise ReadOnlyViolation(
            f"Quick Sale command {cmd!r} refused: only fare displays (FDI) "
            f"are allowed.")
    if method == "POST" and lower == PENALTIES.lower():
        filled = [k for k in _PENALTY_VALUES if str(form.get(k, "")).strip()]
        if (form.get("Action") == "Appliquer" and form.get("Made") == "Seek"
                and not filled):
            return
        raise ReadOnlyViolation(
            "Penalty form refused: only a search (Made=Seek with every "
            "penalty field empty) is allowed.")
    if method == "POST" and lower == TAXES.lower():
        if form.get("hAction") in _TAX_READS:
            return
        raise ReadOnlyViolation(
            f"Tax form refused: hAction {form.get('hAction')!r} is not a "
            f"read.")
    raise ReadOnlyViolation(f"{method} {path} is not a pricing read.")


class PricingClient:
    """Pricing reads on a signed-in ZenithSession, one request at a time."""

    def __init__(self, session, *, base_url: str, timeout_s: float = 180.0,
                 attempts: int = 3, pause_s: float = 0.0):
        self.session = session
        self.base = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.attempts = max(1, attempts)
        self.pause_s = max(0.0, pause_s)
        self.requests = 0
        self._task_id = ""

    # ---- transport ---------------------------------------------------------
    def _send(self, method: str, path: str, *, params=None, data=None,
              json_body=None, headers=None):
        url = self.base + path
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        check_allowed(method, url, data)
        from .zenith_client import USER_AGENT
        hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
        for attempt in range(1, self.attempts + 1):
            last = attempt >= self.attempts
            try:
                if method == "GET":
                    r = self.session.session.get(
                        url, headers=hdrs, timeout=self.timeout_s,
                        allow_redirects=False)
                elif json_body is not None:
                    r = self.session.session.post(
                        url, headers={**hdrs, "Content-Type":
                                      "application/json; charset=UTF-8"},
                        data=json.dumps(json_body), timeout=self.timeout_s,
                        allow_redirects=False)
                else:
                    r = self.session.session.post(
                        url, headers=hdrs, data=data, timeout=self.timeout_s,
                        allow_redirects=False)
                r = self._follow(r, url, hdrs)
            except (ReadOnlyViolation, SessionLost):
                raise
            except Exception as exc:               # noqa: BLE001 - network
                if last:
                    raise PricingError(f"Network error: {exc}") from exc
                time.sleep(5 * attempt)
                continue
            self.requests += 1
            if "/otds/" in (getattr(r, "url", "") or "") or \
                    r.status_code in (401, 403):
                raise SessionLost("Zenith signed the session out — sign in "
                                  "again and re-run.")
            if r.status_code >= 500:
                if last:
                    raise PricingError(
                        f"Zenith returned {r.status_code} {attempt} times.")
                time.sleep(10 * attempt)
                continue
            if self.pause_s:
                time.sleep(self.pause_s)
            return r
        raise PricingError("Request did not complete.")

    def _follow(self, r, url: str, hdrs: dict):
        """Follow redirects by hand, each one passing check_allowed first --
        left to `requests`, a redirect to a saving page would be followed
        with the signed-in session and never checked."""
        from urllib.parse import urljoin
        for _hop in range(_MAX_REDIRECTS):
            if r.status_code not in (301, 302, 303, 307, 308):
                return r
            target = urljoin(getattr(r, "url", "") or url,
                             r.headers.get("Location", ""))
            if "/otds/" in target:
                raise SessionLost("Zenith signed the session out — sign in "
                                  "again and re-run.")
            check_allowed("GET", target)
            url = target
            r = self.session.session.get(target, headers=hdrs,
                                         timeout=self.timeout_s,
                                         allow_redirects=False)
        raise PricingError("Zenith redirected too many times.")

    # ---- Fare Editor -------------------------------------------------------
    def fare_editor_page(self, id_fare_basis: int) -> str:
        r = self._send("GET", FARE_EDITOR,
                       params={"idFarebasis": id_fare_basis})
        return r.text or ""

    def fare_basis_list(self) -> list:
        r = self._send("POST", FARE_BASIS_LIST, json_body={})
        return _d(r)

    def load_fares(self, id_fare_basis: int, id_period: int) -> list:
        r = self._send("POST", LOAD_FARES, json_body={
            "idFareBasis": int(id_fare_basis), "idPeriod": int(id_period),
            "idDepartureAirport": None})
        return _d(r)

    # ---- Quick Sale --------------------------------------------------------
    def quick_sale(self, command: str) -> str:
        """Run one FDI command; opens the Quick Sale session the first time."""
        if not self._task_id:
            self._task_id = self._open_quick_sale()
        r = self._send("GET", QS_COMMAND, params={
            "taskId": self._task_id, "txtRequest": command, "color": "true"},
            headers={"X-Requested-With": "XMLHttpRequest"})
        return r.text or ""

    def _open_quick_sale(self) -> str:
        r = self._send("GET", QS_INIT, params={
            "taskName": "SalesCryptic", "ViewName": "SalesCryptic",
            "isGraph": "1", "reuseOtherTaskIfIsSameName": "1",
            "isCreateTaskIfNotFound": "1"})
        m = re.search(r"taskId=([0-9a-fA-F-]{20,})", getattr(r, "url", "")
                      or "") or re.search(r"taskId=([0-9a-fA-F-]{20,})",
                                          r.text or "")
        if not m:
            raise PricingError("Quick Sale did not open (no session id).")
        uuid.UUID(m.group(1))                      # a real id, not junk
        return m.group(1)

    # ---- penalties ---------------------------------------------------------
    def penalty_page(self) -> str:
        return self._send("GET", PENALTIES).text or ""

    def penalty_search(self, form: dict) -> str:
        return self._send("POST", PENALTIES, data=form).text or ""

    # ---- taxes -------------------------------------------------------------
    def tax_page(self) -> str:
        return self._send("GET", TAXES).text or ""

    def tax_search(self, form: dict) -> str:
        return self._send("POST", TAXES, data=form).text or ""


def _d(response) -> list:
    try:
        got = response.json()
    except ValueError as exc:
        raise PricingError("Zenith did not answer with data.") from exc
    got = got.get("d", got) if isinstance(got, dict) else got
    if not isinstance(got, list):
        raise PricingError("Zenith answered with an unexpected shape.")
    return got
