"""Singapore — Changi air traffic (data.gov.sg open data).

`datastore_search` returns monthly Changi rows. Verified live fields:
Year, Month ("2015-01"), Passenger_Arrival/Departures/Transit/Total,
Aircraft_Total, Cargo_*, AirMail_* — numbers are comma-formatted strings.
Open data, no key. We emit total passengers + total aircraft movements per
month (no arrival/departure split rows, to avoid double-counting the total).
"""

from __future__ import annotations

import requests

from ..traffic_client import ProgressCallback, TrafficError, TrafficRow, http_get

API_URL = "https://data.gov.sg/api/action/datastore_search"
_RESOURCE_ID = "d_744e62bfb1c524508bce0a64a2488243"
_SOURCE_ID = "singapore_changi"
_SOURCE_LABEL = "Singapore — Changi traffic (data.gov.sg)"
_AIRPORT = "SIN"


def _num(s: object) -> float:
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def parse_changi(records: "list[dict]") -> list[TrafficRow]:
    rows: list[TrafficRow] = []
    for rec in records:
        month = str(rec.get("Month") or "").strip()
        if month and "-" in month:
            period, gran = month, "month"
        else:
            year = str(rec.get("Year") or "").strip()
            if not year:
                continue
            period, gran = year, "year"
        base = dict(
            source=_SOURCE_ID, source_label=_SOURCE_LABEL,
            period=period, period_granularity=gran,
            country="Singapore", airport=_AIRPORT,
            flight_type="international", raw_label="Changi",
        )
        pax = _num(rec.get("Passenger_Total"))
        if pax:
            rows.append(TrafficRow(metric="passengers", unit="passengers", value=pax, **base))
        mov = _num(rec.get("Aircraft_Total"))
        if mov:
            rows.append(TrafficRow(metric="movements", unit="aircraft", value=mov, **base))
    return rows


class _SingaporeChangi:
    id = _SOURCE_ID
    label = _SOURCE_LABEL
    granularity = "airport"
    needs_credentials = False
    needs_file = False

    def list_filter_options(self, session: requests.Session) -> dict:
        return {}

    def fetch(
        self,
        filters: dict,
        *,
        session: requests.Session | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> list[TrafficRow]:
        if progress_cb:
            progress_cb(0, 1, "Fetching Changi traffic…")
        resp = http_get(
            API_URL, session=session,
            params={"resource_id": _RESOURCE_ID, "limit": 10000}, timeout=45.0,
        )
        try:
            data = resp.json()
        except ValueError as exc:
            raise TrafficError(f"Singapore: invalid JSON: {exc}") from exc
        records = ((data or {}).get("result") or {}).get("records")
        if not isinstance(records, list):
            raise TrafficError("Singapore: unexpected payload shape")

        rows = parse_changi(records)
        df = (filters.get("date_from") or "")[:7]
        dt = (filters.get("date_to") or "")[:7]
        if df:
            rows = [r for r in rows if r.period >= df]
        if dt:
            rows = [r for r in rows if r.period <= dt]
        if progress_cb:
            progress_cb(1, 1, f"{len(rows):,} rows")
        return rows


SOURCE = _SingaporeChangi()
