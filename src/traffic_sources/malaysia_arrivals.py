"""Malaysia international arrivals — data.gov.my open data (commercial-OK).

`GET https://api.data.gov.my/data-catalogue?id=arrivals` returns a JSON array of
monthly records, by country ("ALL" = grand total). Verified live shape:

    {"date": "2020-01-01", "country": "ALL",
     "arrivals": 2923053, "arrivals_male": 1598823, "arrivals_female": 1324230}

No API key. We emit one TrafficRow per (country, month) carrying the total
arrivals; the male/female split is kept on the record for a future gender view
but NOT emitted as separate rows (which would double-count aggregations).
"""

from __future__ import annotations

import requests

from ..traffic_client import (
    ProgressCallback,
    TrafficError,
    TrafficRow,
    http_get,
)

API_URL = "https://api.data.gov.my/data-catalogue"
_DATASET_ID = "arrivals"
_SOURCE_ID = "malaysia_arrivals"
_SOURCE_LABEL = "Malaysia — Arrivals (data.gov.my)"


def _month(date_str: str) -> str:
    """'2020-01-01' -> '2020-01' (the record's monthly period)."""
    return (date_str or "")[:7]


def _to_float(v: object) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def parse_arrivals(records: list[dict]) -> list[TrafficRow]:
    """Map data.gov.my arrivals records into unified TrafficRow list."""
    rows: list[TrafficRow] = []
    for rec in records:
        period = _month(str(rec.get("date", "")))
        if not period:
            continue
        raw_country = str(rec.get("country") or "").strip()
        country = "All countries" if raw_country.upper() == "ALL" else raw_country
        rows.append(TrafficRow(
            source=_SOURCE_ID,
            source_label=_SOURCE_LABEL,
            period=period,
            period_granularity="month",
            metric="arrivals",
            unit="passengers",
            value=_to_float(rec.get("arrivals")),
            country=country,
            direction="arrival",
            flight_type="international",
            raw_label=raw_country,
        ))
    return rows


class _MalaysiaArrivals:
    """Singleton conforming to the TrafficSource Protocol."""

    id = _SOURCE_ID
    label = _SOURCE_LABEL
    granularity = "country"
    needs_credentials = False
    needs_file = False

    def list_filter_options(self, session: requests.Session) -> dict:
        # Countries/periods are discovered from the data itself; the API has
        # no server-side filter form, so no pre-fetched options.
        return {}

    def fetch(
        self,
        filters: dict,
        *,
        session: requests.Session | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> list[TrafficRow]:
        if progress_cb:
            progress_cb(0, 1, "Fetching Malaysia arrivals…")
        resp = http_get(
            API_URL, session=session, params={"id": _DATASET_ID}, timeout=45.0,
        )
        try:
            data = resp.json()
        except ValueError as exc:
            raise TrafficError(f"Malaysia arrivals: invalid JSON: {exc}") from exc
        if isinstance(data, dict):  # some catalogue items wrap rows in {"data": [...]}
            data = data.get("data")
        if not isinstance(data, list):
            raise TrafficError("Malaysia arrivals: unexpected payload shape")

        rows = parse_arrivals(data)

        # Optional client-side period filtering (YYYY-MM-DD or YYYY-MM bounds).
        date_from = (filters.get("date_from") or "")[:7]
        date_to = (filters.get("date_to") or "")[:7]
        if date_from:
            rows = [r for r in rows if r.period >= date_from]
        if date_to:
            rows = [r for r in rows if r.period <= date_to]

        if progress_cb:
            progress_cb(1, 1, f"{len(rows):,} rows")
        return rows


SOURCE = _MalaysiaArrivals()
