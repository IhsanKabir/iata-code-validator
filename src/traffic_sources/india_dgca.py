"""India — DGCA international air traffic (Vonter/india-aviation-traffic mirror).

Pulls `aggregated/international/country.csv` (ODbL — attribute DGCA & MoCA).
Verified header: Year(2-digit),Quarter,Country,PaxToIndia,PaxFromIndia,Freight*.
India↔country quarterly passengers; PaxToIndia = arrival into India, PaxFromIndia
= departure from India.

NOTE: fetched from raw.githubusercontent.com, so this source only works where
GitHub is reachable. Corporate networks that block GitHub will get a graceful
TrafficError (other sources still work). The cleanest machine-readable India
source available; the official DGCA portal is HTML/PDF only.
"""

from __future__ import annotations

import csv
import io

import requests

from ..traffic_client import ProgressCallback, TrafficError, TrafficRow, http_get

CSV_URL = (
    "https://raw.githubusercontent.com/Vonter/india-aviation-traffic/"
    "main/aggregated/international/country.csv"
)
_SOURCE_ID = "india_dgca"
_SOURCE_LABEL = "India — DGCA intl (Vonter mirror · needs GitHub)"


def _period(year: object, quarter: object) -> "tuple[str, str]":
    y = str(year or "").strip()
    q = str(quarter or "").strip()
    if y.isdigit():
        yy = int(y)
        full = 2000 + yy if yy < 100 else yy
        if q.isdigit():
            return f"{full}-Q{int(q)}", "quarter"
        return str(full), "year"
    return "", "quarter"


def _num(s: object) -> float:
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def parse_country_csv(text: str) -> list[TrafficRow]:
    rows: list[TrafficRow] = []
    for rec in csv.DictReader(io.StringIO(text)):
        period, gran = _period(rec.get("Year"), rec.get("Quarter"))
        if not period:
            continue
        country = (rec.get("Country") or "").strip().title()
        if not country:
            continue
        base = dict(
            source=_SOURCE_ID, source_label=_SOURCE_LABEL,
            period=period, period_granularity=gran,
            metric="passengers", unit="passengers",
            country=country, flight_type="international",
            raw_label=(rec.get("Country") or "").strip(),
        )
        to_india = _num(rec.get("PaxToIndia"))
        if to_india:
            rows.append(TrafficRow(
                value=to_india, origin=country, destination="India",
                direction="arrival", **base))
        from_india = _num(rec.get("PaxFromIndia"))
        if from_india:
            rows.append(TrafficRow(
                value=from_india, origin="India", destination=country,
                direction="departure", **base))
    return rows


class _IndiaDgca:
    id = _SOURCE_ID
    label = _SOURCE_LABEL
    granularity = "country"
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
            progress_cb(0, 1, "Fetching India DGCA (GitHub)…")
        resp = http_get(CSV_URL, session=session, timeout=45.0)
        rows = parse_country_csv(resp.text)

        # period is "YYYY-Qn" or "YYYY" — filter by the year prefix.
        df = (filters.get("date_from") or "")[:4]
        dt = (filters.get("date_to") or "")[:4]
        if df:
            rows = [r for r in rows if r.period[:4] >= df]
        if dt:
            rows = [r for r in rows if r.period[:4] <= dt]
        if progress_cb:
            progress_cb(1, 1, f"{len(rows):,} rows")
        return rows


SOURCE = _IndiaDgca()
