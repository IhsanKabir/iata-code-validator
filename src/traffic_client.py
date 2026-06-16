"""Shared core for the Traffic-movement tab.

Each data source (Malaysia data.gov.my, Oman NCSI, US BTS T-100, ...) lives in
its own module under `traffic_sources/` and normalizes its data into the ONE
`TrafficRow` schema below — so a single results Treeview and a single Excel
writer serve them all. Adding a source = drop in one `traffic_sources/<x>.py`
that returns `list[TrafficRow]` and register it in `traffic_sources.SOURCES`;
no GUI / worker / export changes.

Mirrors oep_client.py: stateless module functions + frozen dataclasses, a
browser User-Agent session, and retry with capped backoff + jitter (real
backoff, not the OEP single-shot, so polite per-source loops don't hammer
rate-limited open-data APIs).
"""

from __future__ import annotations

import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

ProgressCallback = Callable[[int, int, str], None]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TrafficError(RuntimeError):
    """Any traffic-source request or parse failure."""


class TrafficRateLimitedError(TrafficError):
    """Source returned 429/503 — back off and retry later."""


# ---------------------------------------------------------------------------
# Unified row + aggregation dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrafficRow:
    """One normalized traffic / passenger-movement datapoint.

    Heterogeneous sources land in this single shape. Optional dimensions
    default to ""/0.0 so a per-airport-only source leaves route fields blank
    while a route source fills them — without ever changing the schema.
    `metric` and `unit` travel together so passengers/seats/freight never
    silently merge. `period` is a normalized sortable string (e.g. "2024-03"
    or "2024"); `period_granularity` drives aggregation.
    """

    source: str            # registry id, e.g. "malaysia_arrivals"
    source_label: str      # human label for the source
    period: str            # "2024", "2024-Q1", or "2024-03"
    period_granularity: str  # "year" | "quarter" | "month"
    metric: str            # "arrivals" | "passengers" | "seats" | "movements" | "freight"
    value: float
    unit: str = "count"
    country: str = ""
    airport: str = ""
    origin: str = ""
    destination: str = ""
    carrier: str = ""
    direction: str = ""    # "arrival" | "departure" | ""
    flight_type: str = ""  # "international" | "domestic" | ""
    nationality: str = ""
    gender: str = ""
    raw_label: str = ""    # the source's own label, kept for the Raw sheet / audit


@dataclass(frozen=True)
class CountryTotal:
    country: str
    metric: str
    unit: str
    value: float


@dataclass(frozen=True)
class AirportTotal:
    airport: str
    metric: str
    unit: str
    value: float


@dataclass(frozen=True)
class RouteTotal:
    origin: str
    destination: str
    metric: str
    unit: str
    value: float


@dataclass(frozen=True)
class PeriodTotal:
    period: str
    metric: str
    unit: str
    value: float


# ---------------------------------------------------------------------------
# HTTP — session + GET with capped backoff + jitter
# ---------------------------------------------------------------------------


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


_BACKOFF_SCHEDULE_S = (2.0, 5.0, 10.0)


def http_get(
    url: str,
    *,
    session: requests.Session | None = None,
    params: dict | None = None,
    timeout: float = 30.0,
    attempts: int = 3,
) -> requests.Response:
    """GET with capped backoff + equal jitter between retries.

    Raises TrafficRateLimitedError on persistent 429/503, TrafficError on
    other HTTP/network failures. Real backoff (not a single fixed sleep) so a
    multi-period loop against a keyless open-data API doesn't trip rate limits.
    """
    sess = session or build_session()
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = sess.get(url, params=params, timeout=timeout, allow_redirects=True)
        except requests.RequestException as exc:
            last = exc
            if attempt == attempts:
                raise TrafficError(f"Network error GET {url}: {exc}") from exc
        else:
            if resp.status_code in (429, 503):
                last = TrafficRateLimitedError(f"HTTP {resp.status_code} from {url}")
                if attempt == attempts:
                    raise last
            elif resp.status_code >= 400:
                raise TrafficError(f"HTTP {resp.status_code} GET {url}")
            else:
                return resp
        base = _BACKOFF_SCHEDULE_S[min(attempt - 1, len(_BACKOFF_SCHEDULE_S) - 1)]
        time.sleep(base / 2.0 + random.uniform(0.0, base / 2.0))
    raise TrafficError(f"GET {url} exhausted retries: {last}")  # unreachable


# ---------------------------------------------------------------------------
# Aggregation helpers (pure) — mirror oep_client's aggregate_by_*/pivot_*
# ---------------------------------------------------------------------------


def aggregate_by_country(rows: Iterable[TrafficRow]) -> list[CountryTotal]:
    acc: dict[tuple[str, str, str], float] = defaultdict(float)
    for r in rows:
        if r.country:
            acc[(r.country, r.metric, r.unit)] += r.value
    return sorted(
        (CountryTotal(c, m, u, v) for (c, m, u), v in acc.items()),
        key=lambda t: t.value, reverse=True,
    )


def aggregate_by_airport(rows: Iterable[TrafficRow]) -> list[AirportTotal]:
    acc: dict[tuple[str, str, str], float] = defaultdict(float)
    for r in rows:
        if r.airport:
            acc[(r.airport, r.metric, r.unit)] += r.value
    return sorted(
        (AirportTotal(a, m, u, v) for (a, m, u), v in acc.items()),
        key=lambda t: t.value, reverse=True,
    )


def aggregate_by_route(rows: Iterable[TrafficRow]) -> list[RouteTotal]:
    acc: dict[tuple[str, str, str, str], float] = defaultdict(float)
    for r in rows:
        if r.origin and r.destination:
            acc[(r.origin, r.destination, r.metric, r.unit)] += r.value
    return sorted(
        (RouteTotal(o, d, m, u, v) for (o, d, m, u), v in acc.items()),
        key=lambda t: t.value, reverse=True,
    )


def aggregate_by_period(rows: Iterable[TrafficRow]) -> list[PeriodTotal]:
    acc: dict[tuple[str, str, str], float] = defaultdict(float)
    for r in rows:
        acc[(r.period, r.metric, r.unit)] += r.value
    return sorted(
        (PeriodTotal(p, m, u, v) for (p, m, u), v in acc.items()),
        key=lambda t: t.period,
    )
