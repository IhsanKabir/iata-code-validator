"""US DOT BTS T-100 Segment — route-level passengers/seats (US public domain).

BTS publishes T-100 (and T-100f for foreign carriers to/from the US) as a
form-driven CSV download from TranStats, not a clean keyless API. So this is a
FILE-BASED source: the user downloads the T-100 Segment CSV from
https://www.bts.gov/airline-data-downloads and points the app at it. Zero ToS
risk (US government public-domain data), works offline, and gives true
per-route + per-carrier granularity.

We parse the standard T-100 Segment columns (case-insensitive): ORIGIN, DEST,
CARRIER/UNIQUE_CARRIER, PASSENGERS, SEATS, YEAR, MONTH — emitting one
TrafficRow per (segment, metric) for passengers and seats.
"""

from __future__ import annotations

import csv
from pathlib import Path

import requests

from ..traffic_client import ProgressCallback, TrafficError, TrafficRow

_SOURCE_ID = "bts_t100"
_SOURCE_LABEL = "US BTS T-100 Segment (load CSV)"

# Accept common header spellings (TranStats varies by download options).
_COL = {
    "origin": ("ORIGIN",),
    "dest": ("DEST", "DESTINATION"),
    "carrier": ("CARRIER", "UNIQUE_CARRIER", "OP_UNIQUE_CARRIER"),
    "carrier_name": ("CARRIER_NAME", "UNIQUE_CARRIER_NAME"),
    "passengers": ("PASSENGERS",),
    "seats": ("SEATS",),
    "year": ("YEAR",),
    "month": ("MONTH",),
}


def _pick(row: dict, keys: tuple) -> str:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return str(row[k])
    return ""


def _to_float(v: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _period(year: str, month: str) -> tuple[str, str]:
    """Return (period, granularity). Month -> 'YYYY-MM'/month, else year."""
    y = (year or "").strip()[:4]
    m = (month or "").strip()
    if y and m and m.isdigit():
        return f"{y}-{int(m):02d}", "month"
    if y:
        return y, "year"
    return "", "year"


def parse_t100_rows(records: "list[dict]") -> list[TrafficRow]:
    """Map T-100 Segment dict rows (DictReader) into unified TrafficRow list.

    Headers are matched case-insensitively. Emits a passengers row and a seats
    row per segment (separate (metric,unit) so they never merge).
    """
    rows: list[TrafficRow] = []
    for rec in records:
        up = {(k or "").strip().upper(): v for k, v in rec.items()}
        origin = _pick(up, _COL["origin"]).strip().upper()
        dest = _pick(up, _COL["dest"]).strip().upper()
        if not origin or not dest:
            continue
        period, gran = _period(_pick(up, _COL["year"]), _pick(up, _COL["month"]))
        if not period:
            continue
        carrier = _pick(up, _COL["carrier"]).strip().upper()
        base = dict(
            source=_SOURCE_ID, source_label=_SOURCE_LABEL,
            period=period, period_granularity=gran,
            origin=origin, destination=dest, carrier=carrier,
            airport=origin, flight_type="international",
            raw_label=f"{origin}-{dest} {carrier}".strip(),
        )
        pax = _to_float(_pick(up, _COL["passengers"]))
        rows.append(TrafficRow(metric="passengers", unit="passengers", value=pax, **base))
        seats = _to_float(_pick(up, _COL["seats"]))
        if seats:
            rows.append(TrafficRow(metric="seats", unit="seats", value=seats, **base))
    return rows


class _BtsT100:
    """Singleton conforming to the TrafficSource Protocol (file-based)."""

    id = _SOURCE_ID
    label = _SOURCE_LABEL
    granularity = "route"
    needs_credentials = False
    needs_file = True

    def list_filter_options(self, session: requests.Session) -> dict:
        return {}

    def fetch(
        self,
        filters: dict,
        *,
        session: requests.Session | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> list[TrafficRow]:
        path_str = (filters or {}).get("csv_path") or ""
        path = Path(path_str)
        if not path_str or not path.is_file():
            raise TrafficError(
                "BTS T-100 needs a downloaded Segment CSV. Get one from "
                "bts.gov/airline-data-downloads and pick it in the file box."
            )
        if progress_cb:
            progress_cb(0, 1, f"Parsing {path.name}…")
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                rows = parse_t100_rows(list(csv.DictReader(f)))
        except OSError as exc:
            raise TrafficError(f"Could not read {path}: {exc}") from exc

        df = (filters.get("date_from") or "")[:7]
        dt = (filters.get("date_to") or "")[:7]
        if df:
            rows = [r for r in rows if r.period >= df]
        if dt:
            rows = [r for r in rows if r.period <= dt]

        if progress_cb:
            progress_cb(1, 1, f"{len(rows):,} rows")
        return rows


SOURCE = _BtsT100()
