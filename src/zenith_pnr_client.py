"""Resolve a 6-character PNR code into Zenith's full Dossier (booking).

The History Analyzer flat-files each modification with the *one* leg
being changed — but a PNR can span multiple legs. To answer the user's
"what was the total route?" question we have to walk each PNR's full
Dossier and stitch together every segment + its coupon status.

Two-step HTTP flow (mirrors what the Zenith UI does when you paste a
PNR into the dashboard search):

  1. GET /newui/dash/quickSearchRights.json.asp?vaction=VERIF&Id=PNR
     → 302 to /TTIDotNet/.../SyntheseDossier.aspx?Id_Dossier=NNN
     (we let requests follow this for us)

  2. GET /TTIDotNet/.../Dossier.aspx?view=UsrDossierSynthese&...
     → 200 with the 200-300 KB ASPX HTML carrying every booking detail

The parser is purely structural — it anchors on the ASP.NET WebForms
control naming pattern (`*_lblFieldName`, `*_hlEtat`) which is stable
across booking variations. No JS execution is needed.

Field map (from the actual Dossier HTML):

  *_lblPNRCode          → 'XXXXXX'         the 6-char PNR
  *_lblCustomerName     → '…'              the customer / agency
  *_lblNomProprio       → '…'              the traveler's surname
  *_lblTelMobile        → '+880…'          contact number
  *_lblNbPaxForPNR      → '1 pax'          passenger count
  *_lblEtatDossier      → 'Issued'         overall PNR status
  *_lblPaiement         → 'On account'     payment method
  *_lblPrixTotalTTC     → '…,… BDT'        total amount
  *_lblTaxesTotal       → '…,… BDT'        total taxes
  *_lblDeviseDossier    → 'BDT'            currency

Per segment (rptSegments → rptVols → rptPassagers):

  *_lblDepartVol        → 'From DAC 22:30'
  *_lblArriveeVol       → '- To SIN 04:40+1'
  *_lblDatesAller       → 'Departure Tue 19/05/26'   (outbound only)
  *_lblDatesRetour      → 'Departure Sun 24/05/26'   (return only)
  *_lblLegAller         → 'DAC - SIN'
  *_lblLegRetour        → 'SIN - DAC'
  *_lblAircraftNumber   → 'Boeing 737-800 (S2-AJH)'
  *_lblClasse           → 'XBDSG6M-AD'    fare basis (first char = RBD)
  *_hlEtat              → 'Flown' / 'Issued' / 'Refunded' / 'Voided'
  *_lblPrixHT           → '…,… BDT'       fare ex-tax for this segment
  *_lblPrixTTC          → '…,… BDT'       fare inc-tax
  *_lblTicketNumber     → '7792XXXXXXXXX 6/1'
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

import requests

from .zenith_client import (
    BASE_URL,
    USER_AGENT,
    RateLimitedError,
    SessionExpiredError,
    ZenithError,
    ZenithSession,
    _backoff_with_jitter,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Endpoint constants
# ---------------------------------------------------------------------------

QUICK_SEARCH_URL = f"{BASE_URL}/newui/dash/quickSearchRights.json.asp"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PNRNotFoundError(ZenithError):
    """The PNR code didn't resolve to a booking."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PNRSegment:
    """One coupon row: a (passenger, leg) pair.

    A two-leg round-trip × one passenger = 2 segments. A two-leg round-trip
    × three passengers = 6 segments.
    """

    leg_route: str            # 'DAC-SIN'
    leg_direction: str        # 'OUT' / 'RETURN' / '' (single-leg)
    departure_date: str       # 'Tue 19/05/26' (rendered)
    departure_text: str       # 'From DAC 22:30'
    arrival_text: str         # '- To SIN 04:40+1'
    aircraft: str             # 'Boeing 737-800 (S2-AJH)'
    fare_basis: str           # e.g. 'XBDSG6M-AD'
    rbd_class: str            # 'S' (first char of fare_basis)
    coupon_status: str        # 'Flown' / 'Issued' / 'Refunded' / 'Voided' / 'No Show' / ...
    price_ht: str             # '15,413 BDT' (raw — taxes excluded)
    price_ttc: str            # '29,884 BDT' (raw — all-in)
    ticket_number: str        # 13-digit e-ticket + coupon, e.g. '7792XXXXXXXXX 6/1'
    passenger: str            # 'AD Mr. SURNAME GIVENNAME'


@dataclass(frozen=True)
class PNRDetails:
    """Full Dossier view of one PNR."""

    pnr_code: str             # six-char alphanumeric PNR
    dossier_id: str           # numeric Zenith internal id
    customer_name: str        # the customer / agency
    traveler_surname: str     # traveler surname
    phone: str                # '+8801949555333'
    payment_method: str       # 'On account'
    pax_count: int            # 1
    pnr_status: str           # 'Issued' / 'Cancelled' / etc.
    currency: str             # 'BDT'
    total_amount: str         # '99,325 BDT' (raw, formatted)
    total_taxes: str          # '25,343 BDT'
    segments: tuple[PNRSegment, ...] = field(default_factory=tuple)
    fetched_at: datetime | None = None

    @property
    def booked_route(self) -> str:
        """Origin → ... → Destination → ... using every unique leg in order.

        Multi-leg trips collapse repeats: DAC-SIN, SIN-DAC → 'DAC-SIN-DAC'.
        """
        if not self.segments:
            return ""
        legs: list[str] = []
        for s in self.segments:
            if not s.leg_route or "-" not in s.leg_route:
                continue
            a, b = (p.strip() for p in s.leg_route.split("-", 1))
            if not legs:
                legs.extend([a, b])
            else:
                if legs[-1] != a:
                    legs.append(a)
                legs.append(b)
        return "-".join(legs)

    @property
    def flown_route(self) -> str:
        """Same as booked_route but only segments that were actually flown.

        A segment is "flown" if its coupon status is anything other than
        Voided / Refunded / Cancelled / No Show. Lets the auditor see
        how the trip actually ended up vs how it was booked.
        """
        not_flown = {"voided", "refunded", "cancelled", "canceled", "no show"}
        flown_segments = [
            s for s in self.segments
            if s.coupon_status.lower() not in not_flown
        ]
        if not flown_segments:
            return ""
        legs: list[str] = []
        for s in flown_segments:
            if not s.leg_route or "-" not in s.leg_route:
                continue
            a, b = (p.strip() for p in s.leg_route.split("-", 1))
            if not legs:
                legs.extend([a, b])
            else:
                if legs[-1] != a:
                    legs.append(a)
                legs.append(b)
        return "-".join(legs)


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


def _strip_html_entities(s: str) -> str:
    """Lightweight entity decode for the strings we care about."""
    return (
        s.replace("&nbsp;", " ")
         .replace("&amp;", "&")
         .replace("&#45;", "-")
         .replace("&lt;", "<")
         .replace("&gt;", ">")
         .strip()
    )


# Map field → list of all occurrences in document order. We keep the
# order because per-segment fields repeat (one entry per coupon) and we
# need to zip them together to rebuild each segment.
_LBL_SPAN_RE = re.compile(
    r'id="[^"]*_lbl([A-Za-z]+)"[^>]*>([^<]*)</span>',
    re.IGNORECASE,
)
_LINK_ETAT_RE = re.compile(
    r'id="[^"]*_hlEtat"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)
_PASSENGER_LINK_RE = re.compile(
    r'id="[^"]*_linkPassager"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)


def _collect_label_values(html: str) -> dict[str, list[str]]:
    """Pull every `lblXxx` span, group by field name in source order."""
    out: dict[str, list[str]] = {}
    for m in _LBL_SPAN_RE.finditer(html):
        field_name = m.group(1)
        value = _strip_html_entities(m.group(2))
        if value == "":
            continue
        out.setdefault(field_name, []).append(value)
    return out


def _collect_coupon_statuses(html: str) -> list[str]:
    """`<a id="..._hlEtat">Flown</a>` repeats once per segment in order."""
    return [
        _strip_html_entities(m.group(1)) for m in _LINK_ETAT_RE.finditer(html)
    ]


def _collect_passenger_names(html: str) -> list[str]:
    return [
        _strip_html_entities(m.group(1)) for m in _PASSENGER_LINK_RE.finditer(html)
    ]


_PNR_HEADER_RE = re.compile(
    r"PNR\s*:\s*(?P<dossier>\d+)\s*\|\s*(?P<pnr>[A-Z0-9]+)",
)


def _first(values: list[str], default: str = "") -> str:
    return values[0] if values else default


def _pax_count(text: str) -> int:
    """`'1 pax'` → 1, `'3 pax'` → 3. Falls back to 0 on garbage."""
    m = re.match(r"(\d+)", text)
    return int(m.group(1)) if m else 0


def parse_dossier_html(html: str) -> PNRDetails:
    """Parse one Dossier.aspx page into a PNRDetails record.

    Raises PNRNotFoundError if the page lacks the marker fields that
    every real PNR page carries — guards against silent-redirect cases
    where Zenith returns an empty dashboard rather than the booking.
    """
    labels = _collect_label_values(html)

    if "PNRCode" not in labels:
        # Try the header text — older skins put PNR only in `PNR : N | CODE`.
        m = _PNR_HEADER_RE.search(html)
        if not m:
            raise PNRNotFoundError(
                "Dossier page has no PNRCode field or PNR header — "
                "this booking may not exist.",
            )
        labels.setdefault("PNRCode", []).append(m.group("pnr"))

    statuses = _collect_coupon_statuses(html)
    passengers = _collect_passenger_names(html)

    # Per-segment fields — keep parallel by position. If counts mismatch
    # (very rare), we pad with empties so we don't crash a whole audit
    # over one weird PNR.
    classe = labels.get("Classe", [])
    prix_ht = labels.get("PrixHT", [])
    prix_ttc = labels.get("PrixTTC", [])
    tickets = labels.get("TicketNumber", [])
    departs = labels.get("DepartVol", [])
    arrives = labels.get("ArriveeVol", [])
    aircraft = labels.get("AircraftNumber", [])

    # Legs split into outbound + return. Most PNRs have one of each;
    # multi-leg trips can have more. We zip departures with arrivals.
    leg_aller = _first(labels.get("LegAller", []))
    leg_retour = _first(labels.get("LegRetour", []))
    dates_aller = _first(labels.get("DatesAller", []))
    dates_retour = _first(labels.get("DatesRetour", []))

    # For each coupon we record which direction it belongs to. Aller =
    # outbound (first half of segments). Retour = return (second half).
    n_segments = max(
        len(statuses), len(classe), len(tickets),
        len(prix_ht), len(prix_ttc),
    )
    # If only one direction exists, every segment is OUT.
    if leg_retour and leg_aller:
        # Heuristic: first half outbound, second half return.
        half = n_segments // 2
        directions = ["OUT"] * half + ["RETURN"] * (n_segments - half)
        routes = [leg_aller] * half + [leg_retour] * (n_segments - half)
        dates = [dates_aller] * half + [dates_retour] * (n_segments - half)
    elif leg_aller:
        directions = ["OUT"] * n_segments
        routes = [leg_aller] * n_segments
        dates = [dates_aller] * n_segments
    elif leg_retour:
        directions = ["RETURN"] * n_segments
        routes = [leg_retour] * n_segments
        dates = [dates_retour] * n_segments
    else:
        directions = [""] * n_segments
        routes = [""] * n_segments
        dates = [""] * n_segments

    def at(seq: list[str], i: int) -> str:
        return seq[i] if i < len(seq) else ""

    segments: list[PNRSegment] = []
    for i in range(n_segments):
        fare_basis = at(classe, i)
        rbd = fare_basis[:1] if fare_basis and fare_basis[0].isalpha() else ""
        segments.append(PNRSegment(
            leg_route=routes[i].replace(" - ", "-").replace(" ", ""),
            leg_direction=directions[i],
            departure_date=dates[i],
            departure_text=at(departs, i),
            arrival_text=at(arrives, i),
            aircraft=at(aircraft, i),
            fare_basis=fare_basis,
            rbd_class=rbd,
            coupon_status=at(statuses, i),
            price_ht=at(prix_ht, i),
            price_ttc=at(prix_ttc, i),
            ticket_number=at(tickets, i),
            passenger=at(passengers, i),
        ))

    header = _PNR_HEADER_RE.search(html)
    return PNRDetails(
        pnr_code=_first(labels.get("PNRCode", [])),
        dossier_id=header.group("dossier") if header else "",
        customer_name=_first(labels.get("CustomerName", [])),
        traveler_surname=_first(labels.get("NomProprio", [])),
        phone=_first(labels.get("TelMobile", [])),
        payment_method=_first(labels.get("Paiement", [])),
        pax_count=_pax_count(_first(labels.get("NbPaxForPNR", []))),
        pnr_status=_first(labels.get("EtatDossier", [])),
        currency=_first(labels.get("DeviseDossier", [])),
        total_amount=_first(labels.get("PrixTotalTTC", [])),
        total_taxes=_first(labels.get("TaxesTotal", [])),
        segments=tuple(segments),
        fetched_at=datetime.now(),
    )


# ---------------------------------------------------------------------------
# Network fetch
# ---------------------------------------------------------------------------


def lookup_pnr(
    session: ZenithSession,
    pnr_code: str,
    *,
    timeout_s: float = 60.0,
    max_attempts: int = 3,
) -> PNRDetails:
    """Resolve `pnr_code` to a parsed PNRDetails record, retrying transients.

    Goes through the same quickSearch redirect chain the Zenith UI uses — that
    gives us automatic 302-following to the Dossier page without us needing the
    internal Id_Dossier ahead of time.

    Zenith intermittently returns 502/503/504 (gateway timeout / overload) on
    bulk PNR runs; those — and network blips — are retried with capped backoff
    + jitter instead of failing the PNR on the first hiccup (the dominant cause
    of bulk-run errors). Session loss (401/403 or a login redirect) is NOT
    retried — it needs a fresh login. The read timeout stays generous because
    the Dossier page is a large, slow ASPX render. Mirrors fetch_customer.
    """
    if not pnr_code:
        raise ValueError("pnr_code must be a non-empty string")
    sess = session.session
    sess.headers.setdefault("User-Agent", USER_AGENT)

    params = {
        "id_langue": "2",
        "GDSCRSPartnerRCIRLoc": "",
        "vaction": "VERIF",
        "Id": pnr_code.strip().upper(),
    }
    import time as _t
    attempts = max(1, int(max_attempts))
    for attempt in range(1, attempts + 1):
        is_last = attempt >= attempts
        try:
            resp = sess.get(QUICK_SEARCH_URL, params=params, timeout=timeout_s)
        except requests.RequestException as exc:
            if is_last:
                raise ZenithError(
                    f"Network error looking up PNR {pnr_code} after "
                    f"{attempts} attempts: {exc}"
                ) from exc
            _t.sleep(_backoff_with_jitter(attempt, base_s=1.5, cap_s=8.0))
            continue

        # Session loss — never retry; the caller must re-login.
        if resp.status_code in (401, 403) or "/otds/" in resp.url:
            raise SessionExpiredError(
                f"Zenith returned {resp.status_code} for PNR {pnr_code} — "
                "session expired or never authenticated."
            )
        # Rate limited / unavailable — back off longer + retry.
        if resp.status_code in (429, 503):
            if is_last:
                raise RateLimitedError(
                    f"Zenith returned {resp.status_code} for PNR {pnr_code} "
                    f"after {attempts} attempts — back off."
                )
            _t.sleep(_backoff_with_jitter(attempt, base_s=4.0, cap_s=12.0))
            continue
        # Transient gateway/server errors — the 502/503/504/500 storm seen on
        # bulk runs. Retry; only fail this PNR if it sticks.
        if resp.status_code >= 500:
            if is_last:
                raise ZenithError(
                    f"Zenith returned {resp.status_code} for PNR {pnr_code} "
                    f"after {attempts} attempts."
                )
            _t.sleep(_backoff_with_jitter(attempt, base_s=1.5, cap_s=8.0))
            continue

        # 2xx/3xx (not a login redirect). The dashboard HTML (no Dossier
        # fields) means the PNR is unknown — NOT a transient, do not retry.
        if "_lblPNRCode" not in resp.text and "PNR :" not in resp.text:
            raise PNRNotFoundError(
                f"Zenith couldn't resolve PNR {pnr_code!r}.",
            )
        return parse_dossier_html(resp.text)
    # The loop always returns or raises on the final attempt; guard anyway.
    raise ZenithError(f"Exhausted retries looking up PNR {pnr_code}.")


def lookup_many(
    session: ZenithSession,
    pnr_codes: Iterable[str],
    *,
    concurrency: int = 3,
    delay_s: float = 0.8,
    skip_cached=None,
    on_result=None,
    progress_cb=None,
    stop_event=None,
) -> dict[str, PNRDetails]:
    """Look up many PNRs with bounded concurrency + per-result checkpointing.

    Mirrors zenith_client.fetch_many: a ThreadPoolExecutor (1..10 workers),
    each making serial calls with a polite per-worker delay. lookup_pnr already
    retries transient 504/5xx, so a flaky server costs a retry, not a lost PNR —
    and overlapping the (slow) waits across workers is what beats the serial
    wall-clock.

    `skip_cached(pnr) -> PNRDetails|None` short-circuits the network call.
    `on_result(code, details_or_None, status)` fires as EACH result lands (in
    the caller's thread, serially) so the caller can checkpoint to its cache
    immediately — making a stopped/crashed run resume-safe. `status` is one of
    OK / NOT_FOUND / CACHED / ERROR: … / STOPPED. `progress_cb(done, total,
    code, status)` gets a running completion count. `stop_event` cancels the
    rest.

    An adaptive governor tracks the running transient-failure streak: it slows
    the workers once a 504-storm starts and ABORTS the run (rather than grinding
    for hours) if Zenith stays down — every completed PNR is already reported
    via on_result, so a re-run resumes from the cache.
    """
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out: dict[str, PNRDetails] = {}
    codes = [c.strip().upper() for c in pnr_codes if c and c.strip()]
    total = len(codes)
    if total == 0:
        return out
    concurrency = max(1, min(int(concurrency), 10))
    delay_s = max(0.0, float(delay_s))

    # One stop signal for the whole run — the caller's if supplied, else an
    # internal one. Session loss / governor-abort set this; the as_completed
    # loop then CANCELS the still-queued futures before the pool shuts down, so
    # we never block draining thousands of doomed calls (mirrors fetch_many).
    _stop = stop_event if stop_event is not None else threading.Event()

    # Adaptive governor — consecutive transient (5xx / timeout) failures.
    SLOW_AT, ABORT_AT = 10, 25
    gov = {"streak": 0, "aborted": False, "session_lost": None}
    gov_lock = threading.Lock()

    def _worker(code: str):
        if _stop.is_set():
            return code, None, "STOPPED"
        cached = skip_cached(code) if skip_cached else None
        if cached is not None:
            return code, cached, "CACHED"
        with gov_lock:
            if gov["aborted"]:
                return code, None, "ERROR: Zenith overloaded — run aborted"
            slow = gov["streak"] >= SLOW_AT
        if slow:
            time.sleep(_backoff_with_jitter(2, base_s=2.0, cap_s=8.0))
        try:
            details = lookup_pnr(session, code)
            with gov_lock:
                gov["streak"] = 0
            time.sleep(delay_s)
            return code, details, "OK"
        except PNRNotFoundError:
            with gov_lock:
                gov["streak"] = 0
            time.sleep(delay_s)
            return code, None, "NOT_FOUND"
        except SessionExpiredError as exc:
            # Not transient — stop the run cleanly (do NOT re-raise from the
            # worker: that would make the pool drain every queued future).
            with gov_lock:
                gov["session_lost"] = exc
            _stop.set()
            return code, None, "SESSION_EXPIRED"
        except ZenithError as exc:  # incl. RateLimitedError — transient server
            with gov_lock:
                gov["streak"] += 1
                if gov["streak"] >= ABORT_AT:
                    gov["aborted"] = True
                    _stop.set()
            time.sleep(delay_s)
            return code, None, f"ERROR: {exc}"
        except Exception as exc:  # noqa: BLE001
            time.sleep(delay_s)
            return code, None, f"ERROR: {type(exc).__name__}: {exc}"

    completed = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_worker, c): c for c in codes}
        for fut in as_completed(futures):
            code, details, status = fut.result()
            completed += 1
            if status in ("OK", "CACHED") and details is not None:
                out[code] = details
            if on_result is not None:
                try:
                    on_result(code, details, status)
                except Exception:  # noqa: BLE001 — never let the callback kill the run
                    log.exception("on_result raised for %s", code)
            if progress_cb is not None:
                try:
                    progress_cb(completed, total, code, status)
                except Exception:  # noqa: BLE001
                    log.exception("progress_cb raised")
            if _stop.is_set():
                for rem in futures:
                    if not rem.done():
                        rem.cancel()
                break

    # Surface a lost session to the caller AFTER the pool is cleanly drained,
    # so the GUI's SessionExpiredError handler still fires (re-login + resume).
    if gov["session_lost"] is not None:
        raise gov["session_lost"]
    return out
