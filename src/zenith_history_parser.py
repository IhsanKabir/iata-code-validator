"""Flight history log parser for Zenith ModificationHistory exports.

Zenith's "Inventory → Flight list → History → Download" produces files named
like `ModificationHistory 1 JAN DAC-DXB.xls`. The `.xls` extension is a lie
— each file is actually HTML containing one large `<table>` with eight
columns:

  Date | Created by | Description | Type | PNR | Customer | Flight | Passenger

This module parses one file (or a folder of them) into a stream of
`HistoryEvent` records, normalising the free-text `Created by` and
`Description` fields so the analyzer can group by agent, RBD class,
status transition, etc., without re-parsing prose.

The parser is deliberately stdlib-only (uses `html.parser`) so it can run
inside the bundled PyInstaller exe without pulling pandas/lxml.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Iterator

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Agent:
    """Normalised actor info from the 'Created by' column.

    `display_name` is the human name, `user_id` is the login shown in
    parentheses, and `department` is whatever follows a `/` inside the
    parens (Zenith adds it for staff like `/BO-3 Revenue Management`).
    """

    raw: str
    display_name: str
    user_id: str
    department: str

    @property
    def is_system(self) -> bool:
        return self.user_id.lower() == "system" or "/tti" in self.raw.lower()

    @property
    def is_api(self) -> bool:
        return self.user_id.lower().startswith("api_")


@dataclass(frozen=True)
class FlightRef:
    """Parsed flight cell. Empty strings if the cell was blank."""

    raw: str
    flight_number: str         # e.g. 'BS341'
    origin: str                # 3-letter airport
    destination: str           # 3-letter airport
    flight_date: str           # DD/MM/YYYY
    departure_time: str        # HH:MM


@dataclass(frozen=True)
class HistoryEvent:
    """One row from a ModificationHistory file, normalised.

    Source-file path is preserved so the analyzer can report which log a
    suspicious event came from.
    """

    # Source
    source_file: str
    row_index: int

    # Raw columns
    raw_date: str
    raw_created_by: str
    raw_description: str
    event_type: str
    pnr: str
    customer: str
    raw_flight: str
    passenger: str

    # Derived
    timestamp: datetime | None
    agent: Agent
    flight: FlightRef
    rbd_class: str = ""          # Single-letter booking class, e.g. 'Y', 'G'
    old_status: str = ""         # For coupon status transitions
    new_status: str = ""
    capacity_class: str = ""     # For 'Booking class: X' capacity events
    capacity_before: int | None = None
    capacity_after: int | None = None
    ticket_number: str = ""      # The 13-digit BS ticket number, if visible


# ---------------------------------------------------------------------------
# HTML table reader
# ---------------------------------------------------------------------------


class _TableReader(HTMLParser):
    """Pulls every <tr> out of the file as a list of cell strings.

    Treats <br> inside cells as ' | ' so multi-line descriptions stay
    on one row downstream (and the original breaks remain visible).
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] = []
        self._cell_buf: list[str] = []
        self._in_cell = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "tr":
            self._current_row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cell_buf = []
        elif tag == "br" and self._in_cell:
            self._cell_buf.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            text = " ".join("".join(self._cell_buf).split()).strip()
            self._current_row.append(text)
            self._in_cell = False
        elif tag == "tr" and self._current_row:
            self.rows.append(self._current_row)

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_buf.append(data)


# ---------------------------------------------------------------------------
# Cell-level extractors
# ---------------------------------------------------------------------------


_AGENT_RE = re.compile(
    # Either   `Display Name (userid)`   or   `Display Name (userid/dept)`
    r"^(?P<name>.+?)\s*\((?P<user>[^)/]+)(?:/(?P<dept>[^)]+))?\)\s*$"
)


def parse_agent(raw: str) -> Agent:
    """Parse a 'Created by' cell into structured parts.

    Falls back to using the whole string as display_name if the format
    doesn't match — never raises.
    """
    raw = raw.strip()
    m = _AGENT_RE.match(raw)
    if not m:
        return Agent(raw=raw, display_name=raw, user_id="", department="")
    return Agent(
        raw=raw,
        display_name=m.group("name").strip(),
        user_id=m.group("user").strip(),
        department=(m.group("dept") or "").strip(),
    )


_FLIGHT_RE = re.compile(
    # Examples:
    #   `BS 341 DAC CGP 01/01/2026 20:45`
    #   `BS341 01/01/2026 20:45 (DAC -> DXB)`  (not present in Flight col but be lenient)
    r"^(?P<flight>BS\s*\d+)\s+(?P<orig>[A-Z]{3})\s+(?P<dest>[A-Z]{3})\s+"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<time>\d{2}:\d{2})"
)


def parse_flight(raw: str) -> FlightRef:
    """Parse the 'Flight' cell. Returns empty FlightRef when blank."""
    raw = raw.strip()
    if not raw:
        return FlightRef(raw="", flight_number="", origin="",
                        destination="", flight_date="", departure_time="")
    m = _FLIGHT_RE.match(raw)
    if not m:
        return FlightRef(raw=raw, flight_number="", origin="",
                        destination="", flight_date="", departure_time="")
    flight = re.sub(r"\s+", "", m.group("flight"))
    return FlightRef(
        raw=raw,
        flight_number=flight,
        origin=m.group("orig"),
        destination=m.group("dest"),
        flight_date=m.group("date"),
        departure_time=m.group("time"),
    )


_TS_FORMATS = ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y")


def parse_timestamp(raw: str) -> datetime | None:
    """Parse Zenith's `DD/MM/YYYY HH:MM` date cell. Returns None on failure."""
    raw = raw.strip()
    if not raw:
        return None
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


# Description patterns we care about for the audit
_RBD_IN_COUPON_RE = re.compile(
    # `Coupon information: [37103384] 7792411394795C1 BS 341 /E DAC CGP 01/01/2026`
    r"BS\s*\d+\s*/(?P<rbd>[A-Z])\s+[A-Z]{3}\s+[A-Z]{3}"
)
_TICKET_NUMBER_RE = re.compile(r"\b(7792\d{9})\b")  # 13-digit BS ticket
_STATUS_TRANSITION_RE = re.compile(
    # Matches `Old status->New status` or `Previous status = X , New status = Y`
    r"(?:Coupon status\s*:\s*(?P<old1>[A-Za-z ]+?)\s*->\s*(?P<new1>[A-Za-z ]+?)(?:$|<|\s\|))"
    r"|(?:Previous status\s*=\s*(?P<old2>\w+)\s*,\s*New status\s*=\s*(?P<new2>\w+))"
)
_CAPACITY_RE = re.compile(
    # `Flight Capacity change: (CGP -DXB ) Cabine Eco (Booking class: H) 2 -> 0`
    r"Booking class:\s*(?P<cls>[A-Z])\)\s*(?P<before>\d+)\s*->\s*(?P<after>\d+)"
)


def extract_description_fields(desc: str) -> dict[str, object]:
    """Pull structured fields out of the Description column.

    Returns a dict so the dataclass constructor can splat it without
    every-field-or-empty checks at the call site.
    """
    out: dict[str, object] = {
        "rbd_class": "",
        "old_status": "",
        "new_status": "",
        "capacity_class": "",
        "capacity_before": None,
        "capacity_after": None,
        "ticket_number": "",
    }
    if not desc:
        return out

    m = _RBD_IN_COUPON_RE.search(desc)
    if m:
        out["rbd_class"] = m.group("rbd")

    m = _TICKET_NUMBER_RE.search(desc)
    if m:
        out["ticket_number"] = m.group(1)

    m = _STATUS_TRANSITION_RE.search(desc)
    if m:
        out["old_status"] = (m.group("old1") or m.group("old2") or "").strip()
        out["new_status"] = (m.group("new1") or m.group("new2") or "").strip()

    m = _CAPACITY_RE.search(desc)
    if m:
        out["capacity_class"] = m.group("cls")
        out["capacity_before"] = int(m.group("before"))
        out["capacity_after"] = int(m.group("after"))

    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_EXPECTED_HEADERS = (
    "Date", "Created by", "Description", "Type",
    "PNR", "Customer", "Flight", "Passenger",
)


def parse_history_file(path: str | Path) -> list[HistoryEvent]:
    """Parse one ModificationHistory .xls file into HistoryEvent records.

    Skips the header row. Rows with the wrong column count are dropped
    with a warning rather than aborting the whole file.
    """
    path = Path(path)
    with open(path, "rb") as f:
        body = f.read().decode("utf-8", errors="replace")

    reader = _TableReader()
    reader.feed(body)

    if not reader.rows:
        log.warning("History file has no rows: %s", path)
        return []

    header = reader.rows[0]
    if tuple(header[:8]) != _EXPECTED_HEADERS:
        log.warning(
            "History file header mismatch in %s — got %r, expected %r",
            path, header, list(_EXPECTED_HEADERS),
        )

    events: list[HistoryEvent] = []
    for idx, row in enumerate(reader.rows[1:], start=1):
        # Pad short rows to 8 cells so indexing is safe.
        if len(row) < 8:
            row = row + [""] * (8 - len(row))
        date_raw, by_raw, desc, etype, pnr, customer, flight_raw, passenger = row[:8]
        derived = extract_description_fields(desc)
        events.append(HistoryEvent(
            source_file=path.name,
            row_index=idx,
            raw_date=date_raw,
            raw_created_by=by_raw,
            raw_description=desc,
            event_type=etype,
            pnr=pnr,
            customer=customer,
            raw_flight=flight_raw,
            passenger=passenger,
            timestamp=parse_timestamp(date_raw),
            agent=parse_agent(by_raw),
            flight=parse_flight(flight_raw),
            **derived,
        ))
    return events


def parse_history_folder(
    folder: str | Path,
    *,
    pattern: str = "ModificationHistory*.xls",
    progress_cb=None,
) -> Iterator[HistoryEvent]:
    """Yield events from every matching file in `folder`.

    The generator shape lets the analyzer stream-process a folder of
    millions of rows without materialising everything at once.
    """
    folder = Path(folder)
    files = sorted(folder.glob(pattern))
    log.info("History folder %s — %d files matched %s", folder, len(files), pattern)
    for i, path in enumerate(files, start=1):
        if progress_cb is not None:
            try:
                progress_cb(i, len(files), path.name)
            except Exception:  # noqa: BLE001 — never let UI callback break the run
                log.exception("history progress callback raised")
        try:
            events = parse_history_file(path)
        except Exception as exc:  # noqa: BLE001 — keep going on a bad file
            log.exception("Failed to parse %s: %s", path, exc)
            continue
        yield from events


def collect_history(
    folder: str | Path,
    *,
    pattern: str = "ModificationHistory*.xls",
    progress_cb=None,
) -> list[HistoryEvent]:
    """Eager helper for callers (the analyzer + tests) that want a list."""
    return list(parse_history_folder(folder, pattern=pattern, progress_cb=progress_cb))


# ---------------------------------------------------------------------------
# Fare-tier proxy (until real fares are wired in)
# ---------------------------------------------------------------------------

# Rough ordering of US-Bangla economy booking classes from highest fare
# (full-fare Y) to lowest (deep-discount U). Used by the analyzer to
# detect "downgrades" — when a PNR moves from a higher-tier class to a
# lower one. Order is editable in one place if user feedback says
# otherwise.
RBD_FARE_RANK: dict[str, int] = {
    "Y": 0, "H": 1, "R": 2, "N": 3, "V": 4, "X": 5,
    "B": 6, "T": 7, "G": 8, "S": 9, "L": 10, "K": 11,
    "O": 12, "I": 13, "E": 14, "M": 15, "U": 16,
}


def is_downgrade(old_class: str, new_class: str) -> bool:
    """True when new_class is a lower fare tier than old_class.

    Unknown classes never count as downgrades — we can't rank them.
    """
    if not old_class or not new_class or old_class == new_class:
        return False
    if old_class not in RBD_FARE_RANK or new_class not in RBD_FARE_RANK:
        return False
    return RBD_FARE_RANK[new_class] > RBD_FARE_RANK[old_class]


def downgrade_severity(old_class: str, new_class: str) -> int:
    """How many fare tiers a class change drops (0 if not a downgrade)."""
    if not is_downgrade(old_class, new_class):
        return 0
    return RBD_FARE_RANK[new_class] - RBD_FARE_RANK[old_class]
