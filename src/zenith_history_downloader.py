"""Fetch ModificationHistory .xls files from Zenith for a date range.

Two-step flow (mirrors the Zenith UI):

  1. List flights in a date range
     `POST /newui/aerien/gestionregulation/Liste_vols_newStock.asp?Nav=N`
     Returns HTML with one row per (flight, leg) — each row carries an
     `id_vol` we need for step 2.

  2. For each (flight, leg), POST the Search_Event form with `excel=1`
     `POST /newui/aerien/commun/Search_Event.asp`
     Returns the same HTML-disguised-as-.xls file the user gets when
     they click the Download button in Zenith.

The downloader reuses `zenith_client.ZenithSession` so the user only
logs in once for both the existing Customer/Flight Loads features and
this downloader.

Files are saved with the same naming convention Zenith's manual export
uses — `ModificationHistory {D} {MMM} {ORIG}-{DEST}.xls` — so the
existing analyzer picks them up without configuration.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator

import requests

from .zenith_client import (
    BASE_URL,
    USER_AGENT,
    SessionExpiredError,
    ZenithError,
    ZenithSession,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FLIGHT_LIST_URL = (
    f"{BASE_URL}/newui/aerien/gestionregulation/Liste_vols_newStock.asp"
)
HISTORY_VIEW_URL = f"{BASE_URL}/newui/aerien/commun/search_event.asp"
HISTORY_DOWNLOAD_URL = f"{BASE_URL}/newui/aerien/commun/Search_Event.asp"

# Zenith caps the flight list at ~10 pages per search before timing out.
LIST_MAX_PAGES = 10
DEFAULT_PAGE_SIZE = 100

_MONTH_ABBR = (
    "", "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DownloaderError(ZenithError):
    """Generic failure during a downloader run."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlightRef:
    """Minimal flight metadata needed to download + name a history file."""

    id_vol: str                # Internal Zenith flight id, passed to Search_Event
    flight_number: str         # e.g. 'BS326'
    flight_date: str           # 'DD/MM/YYYY'
    origin: str                # IATA code, e.g. 'CAN'
    destination: str           # IATA code, e.g. 'DAC'

    @property
    def filename(self) -> str:
        """Match Zenith's manual-export filename convention.

        Existing files look like 'ModificationHistory 1 JAN DAC-DXB.xls'.
        """
        day, month, _year = self.flight_date.split("/")
        mon = _MONTH_ABBR[int(month)] if 1 <= int(month) <= 12 else month
        return (
            f"ModificationHistory {int(day)} {mon} "
            f"{self.origin}-{self.destination}.xls"
        )


@dataclass(frozen=True)
class DownloadResult:
    """Per-flight outcome of a download attempt."""

    flight: FlightRef
    status: str                # 'OK', 'SKIP_EXISTS', 'EMPTY', 'ERROR'
    output_path: Path | None = None
    error: str = ""
    bytes_written: int = 0


# ---------------------------------------------------------------------------
# Flight-list parser
# ---------------------------------------------------------------------------


# Each flight row in the gestionregulation listing carries a JS call:
#   visu_PlanVolLeg(NNNNNN,73,'[BS  XXX     ].[24 DD/MM/YYYY].[ORG-DST].[HH:MM-->HH:MM].[Aircraft type]')
# We use that as our anchor — it has every field we need in one place,
# and one occurrence per (flight, leg).
_PLANVOL_RE = re.compile(
    r"visu_PlanVolLeg\("
    r"(?P<id_vol>\d+),"
    r"\d+,\s*"
    r"'\[(?P<airline>[A-Z]+)\s*(?P<number>\d+)\s*\]"
    r"\.\[\d+\s+(?P<date>\d{2}/\d{2}/\d{4})\]"
    r"\.\[(?P<orig>[A-Z]{3})-(?P<dest>[A-Z]{3})\]"
)


def parse_flight_list(html: str) -> list[FlightRef]:
    """Pull every (flight, leg) row from the gestionregulation listing.

    De-duplicates on `id_vol` since each flight may appear multiple times
    (multi-leg flights share the parent id_vol across rows).
    """
    seen: set[str] = set()
    out: list[FlightRef] = []
    for m in _PLANVOL_RE.finditer(html):
        id_vol = m.group("id_vol")
        if id_vol in seen:
            continue
        seen.add(id_vol)
        out.append(FlightRef(
            id_vol=id_vol,
            flight_number=f"{m.group('airline')}{m.group('number')}",
            flight_date=m.group("date"),
            origin=m.group("orig"),
            destination=m.group("dest"),
        ))
    return out


def _validate_date(value: str, field: str) -> None:
    if not re.match(r"^\d{2}/\d{2}/\d{4}$", value or ""):
        raise ValueError(f"{field} must be DD/MM/YYYY, got {value!r}")
    try:
        datetime.strptime(value, "%d/%m/%Y")
    except ValueError as exc:
        raise ValueError(f"{field} is not a real date: {value!r}") from exc


# ---------------------------------------------------------------------------
# Listing fetcher
# ---------------------------------------------------------------------------


def list_flights(
    session: ZenithSession,
    date_from: str,
    date_to: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = LIST_MAX_PAGES,
    timeout_s: float = 90.0,
) -> list[FlightRef]:
    """Paginate the gestionregulation listing and return every flight.

    Date inputs are DD/MM/YYYY. Stops when a page returns fewer flights
    than `page_size` (the last page) or when `max_pages` is hit.
    """
    _validate_date(date_from, "date_from")
    _validate_date(date_to, "date_to")
    sess = session.session
    sess.headers.setdefault("User-Agent", USER_AGENT)

    out: list[FlightRef] = []
    seen_ids: set[str] = set()
    for page in range(1, max_pages + 1):
        body = {
            "hidAction": "aff",
            "CodeCompagnie": "BS",
            "date_depart_vol": date_from,
            "date_fin_vol": date_to,
            "VolsOuverts": "VolsOuverts",
            "DisplayOppositeLeg": "DisplayOppositeLeg",
            "NbReponse": str(page_size),
        }
        url = f"{FLIGHT_LIST_URL}?Nav={page}"
        log.info("Zenith flight-list page %d (%s..%s)", page, date_from, date_to)
        try:
            resp = sess.post(url, data=body, timeout=timeout_s)
        except requests.RequestException as exc:
            raise DownloaderError(
                f"Network error fetching flight list page {page}: {exc}",
            ) from exc
        if resp.status_code in (401, 403):
            raise SessionExpiredError(
                f"Zenith returned {resp.status_code} on flight list — session expired.",
            )
        resp.raise_for_status()
        page_flights = parse_flight_list(resp.text)
        new_flights = [f for f in page_flights if f.id_vol not in seen_ids]
        for f in new_flights:
            seen_ids.add(f.id_vol)
        out.extend(new_flights)
        log.info(
            "  page %d: %d flights (new: %d, total: %d)",
            page, len(page_flights), len(new_flights), len(out),
        )
        # Last page reached — Zenith returns fewer rows than NbReponse
        # when there are no more pages.
        if len(page_flights) < page_size:
            break
    return out


# ---------------------------------------------------------------------------
# Per-flight history download
# ---------------------------------------------------------------------------


def _build_search_event_form(id_vol: str) -> dict[str, str]:
    """Full Search_Event POST form, matching the browser exactly.

    Zenith refuses partial forms — sending only the populated fields
    returns HTTP 500 (the ASP page assumes every name exists). All 37
    inputs from the page are included; empties stay empty.
    """
    return {
        # Hidden fields (the H-prefixed ones — Zenith reads these on
        # subsequent navigations).
        "HDateDebut": "",
        "HDateFin": "",
        "Hid_Dossier_Vol": "",
        "Hid_Point_Of_Sale": "",
        "Hid_facture": "",
        "id_Vol": id_vol,
        "ID_FlightSchedulePeriode": "",
        "HFlightNumber": "",
        "HFlightDate": "",
        "HId_Personne": "",
        "HCodeEventNiveau": "",
        "HCodeEventType": "",
        "HCategorieEvent": "1",
        "Hid_Creator": "",
        "eventcodes": "",
        "contexte": "Liste_vols",
        "filterChecked": "",
        "Hid_EMD": "",
        "HFlightEventFrom": "",
        "HFlightEventTo": "",
        "action": "Search",
        # `excel=1` is the switch that flips the response from HTML view
        # to the .xls download.
        "excel": "1",
        "id_facture": "",
        "id_Fournisseur": "2035",
        "NAV": "1",
        # User-facing form fields (mostly empty when we drive it).
        "FlightNumber": "",
        "FlightDate": "",
        "DateDebut": "",
        "DateFin": "",
        "id_Dossier_Vol": "",
        "id_EMD": "",
        "id_Personne": "",
        "SrcID_Creator": "",
        "SrcNomCreator": "",
        "FlightEventDateFrom": "",
        "FlightEventDateTo": "",
        "Recherche": "Search",
    }


def _build_search_event_headers(id_vol: str) -> dict[str, str]:
    """The Referer the page would have set when the user clicks Export."""
    return {
        "Referer": (
            f"{BASE_URL}/newui/aerien/commun/search_event.asp?"
            f"contexte=Liste_vols&id_vol={id_vol}&CategorieEvent=1"
        ),
        "Origin": BASE_URL,
    }


def download_history_file(
    session: ZenithSession,
    flight: FlightRef,
    output_dir: Path,
    *,
    skip_if_exists: bool = True,
    timeout_s: float = 120.0,
) -> DownloadResult:
    """Download one flight's history .xls and save it to `output_dir`."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / flight.filename
    if skip_if_exists and out_path.exists() and out_path.stat().st_size > 0:
        return DownloadResult(
            flight=flight, status="SKIP_EXISTS", output_path=out_path,
            bytes_written=out_path.stat().st_size,
        )

    sess = session.session
    sess.headers.setdefault("User-Agent", USER_AGENT)

    # Step A: prime the server-side ASP Session for this id_vol by
    # loading the HTML view first. Zenith's Search_Event.asp handler
    # appears to read Session("id_Vol") set by this GET — without it,
    # the export returns the generic ASP 500 error page.
    try:
        prime = sess.get(
            HISTORY_VIEW_URL,
            params={
                "contexte": "Liste_vols",
                "id_vol": flight.id_vol,
                "CategorieEvent": "1",
            },
            timeout=timeout_s,
        )
        if prime.status_code in (401, 403):
            raise SessionExpiredError(
                f"Zenith returned {prime.status_code} priming id_vol={flight.id_vol}.",
            )
        if prime.status_code != 200:
            log.warning(
                "Prime GET HTTP %d for id_vol=%s", prime.status_code, flight.id_vol,
            )
    except SessionExpiredError:
        raise
    except requests.RequestException as exc:
        log.warning("Prime network error for id_vol=%s: %s", flight.id_vol, exc)
        return DownloadResult(
            flight=flight, status="ERROR",
            error=f"Prime GET failed: {exc}",
        )

    # Step B: hit the same URL via GET with `excel=1` in the query string.
    # The commented-out window.open() in the page's JS reveals the real
    # download URL pattern. Browser-side ExportExcel() flips the hidden
    # `excel` form input to "1" then POSTs to Search_Event.asp; we use
    # the GET variant because it's stateless and doesn't depend on the
    # 37-field form being a perfect match.
    try:
        resp = sess.get(
            HISTORY_VIEW_URL,
            params={
                "contexte": "Liste_vols",
                "id_vol": flight.id_vol,
                "CategorieEvent": "1",
                "excel": "1",
            },
            headers=_build_search_event_headers(flight.id_vol),
            timeout=timeout_s,
        )
    except requests.RequestException as exc:
        log.warning("Network error for id_vol=%s: %s", flight.id_vol, exc)
        return DownloadResult(
            flight=flight, status="ERROR",
            error=f"Network error: {exc}",
        )
    if resp.status_code in (401, 403):
        raise SessionExpiredError(
            f"Zenith returned {resp.status_code} for id_vol={flight.id_vol}.",
        )
    if resp.status_code != 200:
        # Log the server's response prefix to make Zenith-side errors
        # diagnosable. The first ~700 chars are CSS boilerplate; the
        # actual error message starts after that, so dump 2 KB.
        body_preview = resp.content[:2000].decode("utf-8", errors="replace")
        log.warning(
            "HTTP %d for id_vol=%s; body[0:2000]=%s",
            resp.status_code, flight.id_vol, body_preview,
        )
        return DownloadResult(
            flight=flight, status="ERROR",
            error=f"HTTP {resp.status_code}",
        )
    # Skip empty / unusable responses (Zenith sometimes returns ~1 KB
    # boilerplate when a flight has no events yet).
    if len(resp.content) < 2000:
        log.info(
            "EMPTY response for id_vol=%s (%d bytes)",
            flight.id_vol, len(resp.content),
        )
        return DownloadResult(
            flight=flight, status="EMPTY",
            error=f"Response too small ({len(resp.content)} bytes)",
            bytes_written=len(resp.content),
        )
    out_path.write_bytes(resp.content)
    log.info(
        "OK id_vol=%s wrote %d bytes to %s",
        flight.id_vol, len(resp.content), out_path.name,
    )
    return DownloadResult(
        flight=flight, status="OK",
        output_path=out_path,
        bytes_written=len(resp.content),
    )


# ---------------------------------------------------------------------------
# Batch driver
# ---------------------------------------------------------------------------


def download_history_batch(
    session: ZenithSession,
    flights: Iterable[FlightRef],
    output_dir: Path,
    *,
    delay_s: float = 1.0,
    skip_if_exists: bool = True,
    progress_cb: Callable[[DownloadResult, int, int], None] | None = None,
    stop_event: threading.Event | None = None,
) -> list[DownloadResult]:
    """Download every flight's history serially with a polite delay.

    Serial on purpose — the Search_Event endpoint isn't designed for
    parallel calls and downloads are large (3-4 MB each). One worker
    avoids overwhelming the server and keeps memory predictable.
    """
    flights = list(flights)
    total = len(flights)
    results: list[DownloadResult] = []
    for idx, flight in enumerate(flights, start=1):
        if stop_event is not None and stop_event.is_set():
            log.info("Stop requested at %d/%d", idx, total)
            break
        try:
            r = download_history_file(
                session, flight, output_dir, skip_if_exists=skip_if_exists,
            )
        except SessionExpiredError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep going past one bad row
            log.exception("download failed for %s", flight.flight_number)
            r = DownloadResult(
                flight=flight, status="ERROR",
                error=f"{type(exc).__name__}: {exc}",
            )
        results.append(r)
        if progress_cb is not None:
            try:
                progress_cb(r, idx, total)
            except Exception:  # noqa: BLE001 — UI callback must never abort
                log.exception("progress callback raised")
        # Skip the polite delay after the last item.
        if idx < total and r.status == "OK" and delay_s > 0:
            time.sleep(delay_s)
    return results
