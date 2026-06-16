"""Qatar — Hamad International (DOH) arrivals/departures (data.gov.qa).

Opendatasoft Explore API v2.1. Verified live record shape:
{"year":"2019","month":"February","type":"Arrivals","total":1429733}. Open
data, no key. Paginated (limit ≤ 100). Month is an English name we map to a
number; `type` (Arrivals/Departures) becomes the direction.
"""

from __future__ import annotations

import requests

from ..traffic_client import ProgressCallback, TrafficError, TrafficRow, http_get

API_URL = (
    "https://www.data.gov.qa/api/explore/v2.1/catalog/datasets/"
    "arrival-and-departures-via-hamad-international-airport-by-month-and-year/records"
)
_SOURCE_ID = "qatar_hamad"
_SOURCE_LABEL = "Qatar — Hamad (DOH) (data.gov.qa)"
_AIRPORT = "DOH"
_MONTHS = {
    m.lower(): i for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], start=1)
}


def _period(year: object, month_name: object) -> "tuple[str, str]":
    y = str(year or "").strip()[:4]
    mi = _MONTHS.get(str(month_name or "").strip().lower())
    if y and mi:
        return f"{y}-{mi:02d}", "month"
    if y:
        return y, "year"
    return "", "year"


def parse_hamad(records: "list[dict]") -> list[TrafficRow]:
    rows: list[TrafficRow] = []
    for rec in records:
        period, gran = _period(rec.get("year"), rec.get("month"))
        if not period:
            continue
        typ = str(rec.get("type") or "").strip().lower()
        direction = "arrival" if typ.startswith("arr") else (
            "departure" if typ.startswith("dep") else "")
        try:
            total = float(rec.get("total") or 0)
        except (TypeError, ValueError):
            total = 0.0
        rows.append(TrafficRow(
            source=_SOURCE_ID, source_label=_SOURCE_LABEL,
            period=period, period_granularity=gran,
            metric="passengers", unit="passengers", value=total,
            country="Qatar", airport=_AIRPORT, direction=direction,
            flight_type="international", raw_label=str(rec.get("type") or ""),
        ))
    return rows


class _QatarHamad:
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
        all_recs: list[dict] = []
        offset, limit = 0, 100
        while True:
            if progress_cb:
                progress_cb(offset, offset + limit, f"Hamad records {offset}…")
            resp = http_get(
                API_URL, session=session,
                params={"limit": limit, "offset": offset}, timeout=45.0,
            )
            try:
                data = resp.json()
            except ValueError as exc:
                raise TrafficError(f"Qatar: invalid JSON: {exc}") from exc
            recs = (data or {}).get("results")
            if not isinstance(recs, list) or not recs:
                break
            all_recs.extend(recs)
            total_count = int((data or {}).get("total_count") or 0)
            offset += limit
            if offset >= total_count or len(recs) < limit or offset > 10000:
                break

        rows = parse_hamad(all_recs)
        df = (filters.get("date_from") or "")[:7]
        dt = (filters.get("date_to") or "")[:7]
        if df:
            rows = [r for r in rows if r.period >= df]
        if dt:
            rows = [r for r in rows if r.period <= dt]
        if progress_cb:
            progress_cb(1, 1, f"{len(rows):,} rows")
        return rows


SOURCE = _QatarHamad()
