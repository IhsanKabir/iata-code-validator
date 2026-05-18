"""HTTP client for the Overseas Employment Platform (oep.gov.bd).

Two public reports power the "BD Overseas Movement" tab:

  /reports/country-clearance        DataTables JSON endpoint.
                                    Returns one row per (country, job category)
                                    pair with `total_employee`. Pull
                                    everything in one shot via `length=-1`.

  /reports/geo-clearance-count      Server-rendered HTML.
                                    Returns a table of Division+District counts
                                    for the chosen date range / country filter.

Both endpoints are public — no session, no CSRF. The desktop just GETs them
with a normal browser User-Agent.

Field reference (from the page form):

  gender_id   1=Male, 2=Female, 3=Other, ""=All
  division    numeric id (1..8) corresponding to the select option
  country_id  numeric id from /reports/country-clearance country picker
  date_from   YYYY-MM-DD
  date_to     YYYY-MM-DD

The select option lists (countries, divisions) live on the geo-clearance-count
page, so we scrape them once and cache in memory.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Iterable

import requests

log = logging.getLogger(__name__)


BASE_URL = "https://www.oep.gov.bd"
COUNTRY_REPORT_URL = f"{BASE_URL}/reports/country-clearance"
GEO_REPORT_URL = f"{BASE_URL}/reports/geo-clearance-count"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

GENDER_OPTIONS = (
    ("", "All"),
    ("1", "Male"),
    ("2", "Female"),
    ("3", "Other"),
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CountryClearance:
    """One (country, job category) row from /reports/country-clearance."""

    country_id: int
    country_name: str
    category_name: str
    total_employee: int


@dataclass(frozen=True)
class DivisionClearance:
    """One Division+District row parsed from /reports/geo-clearance-count."""

    division: str
    district: str
    total_employee: int


@dataclass(frozen=True)
class Option:
    """A `<select>` option scraped from the report form."""

    value: str
    label: str


# ---------------------------------------------------------------------------
# Country / division form-option scraping
# ---------------------------------------------------------------------------


class _SelectParser(HTMLParser):
    """Collect `<option value=...>label</option>` pairs grouped by parent select id.

    Stdlib only — keeps the desktop dependency footprint flat.
    """

    def __init__(self) -> None:
        super().__init__()
        self._stack: list[tuple[str, str | None]] = []  # (tag, select_id)
        self._current_select: str | None = None
        self._current_value: str | None = None
        self._current_label: list[str] = []
        self.results: dict[str, list[Option]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "select":
            select_id = attr.get("id") or attr.get("name") or ""
            self._current_select = select_id
            self.results.setdefault(select_id, [])
        elif tag == "option" and self._current_select is not None:
            self._current_value = attr.get("value") or ""
            self._current_label = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._current_select is not None and self._current_value is not None:
            label = re.sub(r"\s+", " ", "".join(self._current_label)).strip()
            value = self._current_value
            if label and value != "":
                self.results[self._current_select].append(Option(value=value, label=label))
            self._current_value = None
            self._current_label = []
        elif tag == "select":
            self._current_select = None

    def handle_data(self, data: str) -> None:
        if self._current_select is not None and self._current_value is not None:
            self._current_label.append(data)


def parse_form_options(html: str) -> dict[str, list[Option]]:
    """Parse the geo-clearance-count form for `<select>` options."""
    parser = _SelectParser()
    parser.feed(html)
    return parser.results


_form_cache: dict[str, list[Option]] | None = None


def fetch_form_options(
    *,
    session: requests.Session | None = None,
    timeout_s: float = 30.0,
) -> dict[str, list[Option]]:
    """Fetch + cache the form-option lists once per process."""
    global _form_cache
    if _form_cache is not None:
        return _form_cache
    sess = session or requests.Session()
    sess.headers["User-Agent"] = USER_AGENT
    resp = sess.get(GEO_REPORT_URL, timeout=timeout_s)
    resp.raise_for_status()
    _form_cache = parse_form_options(resp.text)
    return _form_cache


def list_countries(
    *, session: requests.Session | None = None
) -> list[Option]:
    return fetch_form_options(session=session).get("country_name", [])


def list_divisions(
    *, session: requests.Session | None = None
) -> list[Option]:
    return fetch_form_options(session=session).get("division_name", [])


def reset_form_cache() -> None:
    """Drop the cached select-option lists. Used in tests."""
    global _form_cache
    _form_cache = None


# ---------------------------------------------------------------------------
# Country-clearance JSON endpoint
# ---------------------------------------------------------------------------


def fetch_country_clearance(
    date_from: str,
    date_to: str,
    *,
    gender_id: str | None = None,
    country_id: str | None = None,
    country_ids: Iterable[str] | None = None,
    category_id: str | None = None,
    category_ids: Iterable[str] | None = None,
    session: requests.Session | None = None,
    timeout_s: float = 90.0,
) -> list[CountryClearance]:
    """One-shot pull of all country/category rows for the date range.

    `length=-1` makes the DataTables backend stream the entire dataset
    (~4k rows / 400 KB for a 4-month window).

    `country_ids` / `category_ids` send the values as `country_id[]` /
    `category_id[]` (multi-select). The single-value `country_id` /
    `category_id` keep working for back-compat.
    """
    _validate_date(date_from, "date_from")
    _validate_date(date_to, "date_to")
    sess = session or requests.Session()
    sess.headers["User-Agent"] = USER_AGENT

    params: list[tuple[str, str]] = [
        ("draw", "1"),
        ("start", "0"),
        ("length", "-1"),
        ("approval_date_from", date_from),
        ("approval_date_to", date_to),
        ("gender_id", gender_id or ""),
        # `all_skills=1` matches the page default; `all_skills=0` makes the
        # backend return the rendered HTML page instead of JSON.
        ("all_skills", "1"),
    ]
    if country_ids:
        for cid in country_ids:
            if cid:
                params.append(("country_id[]", str(cid)))
    elif country_id:
        params.append(("country_id", country_id))
    if category_ids:
        for cid in category_ids:
            if cid:
                params.append(("category_id[]", str(cid)))
    elif category_id:
        params.append(("category_id", category_id))

    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": GEO_REPORT_URL,
    }
    log.info("OEP country-clearance %s..%s gender=%s countries=%s",
             date_from, date_to, gender_id or "all",
             list(country_ids) if country_ids else (country_id or "all"))
    resp = sess.get(COUNTRY_REPORT_URL, params=params, headers=headers, timeout=timeout_s)
    resp.raise_for_status()
    body = resp.json()
    payload = body.get("payload") or {}
    rows = payload.get("data") or []
    out: list[CountryClearance] = []
    for raw in rows:
        try:
            out.append(CountryClearance(
                country_id=int(raw.get("country_id") or 0),
                country_name=str(raw.get("country_name") or "").strip(),
                category_name=str(raw.get("category_name") or "").strip(),
                total_employee=int(raw.get("total_employee") or 0),
            ))
        except (ValueError, TypeError) as e:
            log.warning("Skipping malformed country row %r: %s", raw, e)
    log.info("OEP country-clearance got %d rows (total employees=%s)",
             len(out), payload.get("totalEmployee"))
    return out


# ---------------------------------------------------------------------------
# Geo-clearance HTML endpoint
# ---------------------------------------------------------------------------


class _GeoTableParser(HTMLParser):
    """Pull rows from the single <tbody> on /reports/geo-clearance-count."""

    def __init__(self) -> None:
        super().__init__()
        self._in_body = False
        self._in_row = False
        self._cells: list[str] = []
        self._buf: list[str] = []
        self._in_cell = False
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tbody":
            self._in_body = True
        elif tag == "tr" and self._in_body:
            self._in_row = True
            self._cells = []
        elif tag == "td" and self._in_row:
            self._in_cell = True
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._in_cell:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self._cells.append(text)
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            if self._cells:
                self.rows.append(self._cells)
            self._in_row = False
        elif tag == "tbody":
            self._in_body = False

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._buf.append(data)


def parse_division_table(html: str) -> list[DivisionClearance]:
    """Parse the Division+District table from the rendered report HTML.

    Expected columns: SL | Division | District | Count.
    Tolerates rows missing the leading SL column.
    """
    p = _GeoTableParser()
    p.feed(html)
    out: list[DivisionClearance] = []
    for cells in p.rows:
        if len(cells) >= 4:
            _sl, division, district, count = cells[0], cells[1], cells[2], cells[3]
        elif len(cells) == 3:
            division, district, count = cells
        else:
            continue
        try:
            total = int(count.replace(",", ""))
        except ValueError:
            continue
        out.append(DivisionClearance(
            division=division.strip(),
            district=district.strip(),
            total_employee=total,
        ))
    return out


class OEPRateLimitedError(RuntimeError):
    """Raised when the geo endpoint returns 401 — usually IP rate-limiting."""


def fetch_division_clearance(
    date_from: str,
    date_to: str,
    *,
    gender_id: str | None = None,
    country_ids: Iterable[str] | None = None,
    division_id: str | None = None,
    district_id: str | None = None,
    session: requests.Session | None = None,
    timeout_s: float = 90.0,
) -> list[DivisionClearance]:
    """Fetch and parse the geo-clearance-count report.

    Retries once on transient 401/429 with a 5-second backoff — OEP's
    site occasionally rate-limits after dozens of rapid calls.
    """
    import time

    _validate_date(date_from, "date_from")
    _validate_date(date_to, "date_to")
    sess = session or requests.Session()
    sess.headers["User-Agent"] = USER_AGENT

    params: list[tuple[str, str]] = [
        ("date_from", date_from),
        ("date_to", date_to),
        ("gender_id", gender_id or ""),
        ("division_name", division_id or ""),
        ("district_name", district_id or ""),
    ]
    for cid in (country_ids or []):
        params.append(("country_name[]", str(cid)))

    log.info("OEP geo-clearance %s..%s gender=%s div=%s countries=%s",
             date_from, date_to, gender_id or "all", division_id or "all",
             list(country_ids or []))

    for attempt in (1, 2):
        resp = sess.get(GEO_REPORT_URL, params=params, timeout=timeout_s)
        if resp.status_code in (401, 429):
            if attempt == 1:
                log.warning(
                    "OEP geo-clearance got %d — backing off 5s before retry",
                    resp.status_code,
                )
                time.sleep(5)
                continue
            raise OEPRateLimitedError(
                f"oep.gov.bd returned {resp.status_code}. The site appears to "
                "be rate-limiting your IP. Wait 5-10 minutes and try again, "
                "or run a smaller selection."
            )
        resp.raise_for_status()
        break

    rows = parse_division_table(resp.text)
    log.info("OEP geo-clearance got %d rows", len(rows))
    return rows


# ---------------------------------------------------------------------------
# Date validation
# ---------------------------------------------------------------------------


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_date(value: str, field: str) -> None:
    if not _DATE_RE.match(value or ""):
        raise ValueError(f"{field} must be YYYY-MM-DD, got {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} is not a real date: {value!r}") from exc


# ---------------------------------------------------------------------------
# Aggregation helpers (used by the GUI)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CountryTotal:
    country_name: str
    total_employee: int
    category_count: int


def aggregate_by_country(rows: Iterable[CountryClearance]) -> list[CountryTotal]:
    """Collapse (country, category) rows into a per-country sorted list."""
    by_country: dict[str, list[CountryClearance]] = {}
    for r in rows:
        by_country.setdefault(r.country_name, []).append(r)
    out = [
        CountryTotal(
            country_name=name,
            total_employee=sum(r.total_employee for r in group),
            category_count=len({r.category_name for r in group if r.category_name}),
        )
        for name, group in by_country.items()
    ]
    out.sort(key=lambda c: c.total_employee, reverse=True)
    return out


@dataclass(frozen=True)
class CategoryTotal:
    category_name: str
    total_employee: int
    country_count: int


def aggregate_by_category(rows: Iterable[CountryClearance]) -> list[CategoryTotal]:
    by_cat: dict[str, list[CountryClearance]] = {}
    for r in rows:
        if not r.category_name:
            continue
        by_cat.setdefault(r.category_name, []).append(r)
    out = [
        CategoryTotal(
            category_name=name,
            total_employee=sum(r.total_employee for r in group),
            country_count=len({r.country_name for r in group}),
        )
        for name, group in by_cat.items()
    ]
    out.sort(key=lambda c: c.total_employee, reverse=True)
    return out


@dataclass(frozen=True)
class DivisionTotal:
    division: str
    total_employee: int
    district_count: int


def aggregate_by_division(rows: Iterable[DivisionClearance]) -> list[DivisionTotal]:
    by_div: dict[str, list[DivisionClearance]] = {}
    for r in rows:
        by_div.setdefault(r.division, []).append(r)
    out = [
        DivisionTotal(
            division=name,
            total_employee=sum(r.total_employee for r in group),
            district_count=len({r.district for r in group}),
        )
        for name, group in by_div.items()
    ]
    out.sort(key=lambda d: d.total_employee, reverse=True)
    return out


@dataclass(frozen=True)
class GenderBreakdown:
    country_name: str
    male: int
    female: int
    other: int
    total: int


# ---------------------------------------------------------------------------
# Time-series fetch (monthly per country)
# ---------------------------------------------------------------------------


# Earliest month with data, found via probe (anything before December 2023
# returns zero rows). Used so "all historical" doesn't pull empty months.
EARLIEST_YEAR_MONTH = "2023-12"


@dataclass(frozen=True)
class MonthlyTotal:
    """One row per (year_month, country_name) in a time series."""

    year_month: str           # YYYY-MM
    country_name: str
    total_employee: int


def iter_year_months(date_from: str, date_to: str) -> list[str]:
    """Inclusive list of YYYY-MM strings between two ISO dates."""
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        return []
    months: list[str] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            m = 1
            y += 1
    return months


def _month_bounds(ym: str) -> tuple[str, str]:
    """Return ISO-date bounds for the calendar month `YYYY-MM`."""
    y, m = int(ym[:4]), int(ym[5:7])
    first = date(y, m, 1)
    last_m = m + 1
    last_y = y
    if last_m == 13:
        last_m = 1
        last_y = y + 1
    from datetime import timedelta
    last = date(last_y, last_m, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def fetch_monthly_timeseries(
    date_from: str,
    date_to: str,
    *,
    country_ids: Iterable[str],
    gender_id: str | None = None,
    session: requests.Session | None = None,
    progress_cb=None,
    timeout_s: float = 90.0,
) -> list[MonthlyTotal]:
    """One call per month with all selected countries, summed across categories.

    `progress_cb(idx, total, label)` is invoked between calls — used by the
    GUI for a determinate progress bar.
    """
    cids = [c for c in country_ids if c]
    if not cids:
        raise ValueError("country_ids must contain at least one id")
    sess = session or requests.Session()
    sess.headers["User-Agent"] = USER_AGENT

    months = iter_year_months(date_from, date_to)
    out: list[MonthlyTotal] = []
    for idx, ym in enumerate(months, start=1):
        if progress_cb:
            progress_cb(idx, len(months), f"Fetching {ym}")
        m_from, m_to = _month_bounds(ym)
        rows = fetch_country_clearance(
            m_from, m_to,
            gender_id=gender_id,
            country_ids=cids,
            session=sess,
            timeout_s=timeout_s,
        )
        # Sum the (country, category) rows down to one row per country.
        by_country: dict[str, int] = {}
        for r in rows:
            by_country[r.country_name] = by_country.get(r.country_name, 0) + r.total_employee
        for name, total in by_country.items():
            out.append(MonthlyTotal(year_month=ym, country_name=name, total_employee=total))
    return out


def pivot_timeseries(rows: Iterable[MonthlyTotal]) -> tuple[list[str], dict[str, list[int]]]:
    """Convert a flat list of MonthlyTotal into chart-friendly columns.

    Returns (sorted_year_months, {country_name: [count_for_each_month]}).
    Missing (country, month) cells are filled with 0.
    """
    months_set: set[str] = set()
    countries: dict[str, dict[str, int]] = {}
    for r in rows:
        months_set.add(r.year_month)
        countries.setdefault(r.country_name, {})[r.year_month] = r.total_employee
    months = sorted(months_set)
    series = {
        name: [vals.get(ym, 0) for ym in months]
        for name, vals in countries.items()
    }
    return months, series


# ---------------------------------------------------------------------------
# Country × Division pivot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CountryDivisionCell:
    country_name: str
    division: str
    total_employee: int


def fetch_country_division_pivot(
    date_from: str,
    date_to: str,
    *,
    country_options: Iterable[Option],
    gender_id: str | None = None,
    session: requests.Session | None = None,
    progress_cb=None,
    timeout_s: float = 90.0,
) -> list[CountryDivisionCell]:
    """For each destination, fetch the division/district breakdown and roll up.

    `country_options` is a list of (id, label) — usually the top-N
    countries from a prior country-clearance pull. We need both the id
    (passed as `country_name[]` on the geo endpoint) and the label (used
    as the row key, since the API echoes country names, not ids).
    """
    sess = session or requests.Session()
    sess.headers["User-Agent"] = USER_AGENT

    import time

    opts = [o for o in country_options if o.value]
    out: list[CountryDivisionCell] = []
    for idx, opt in enumerate(opts, start=1):
        if progress_cb:
            progress_cb(idx, len(opts), f"Fetching {opt.label}")
        rows = fetch_division_clearance(
            date_from, date_to,
            gender_id=gender_id,
            country_ids=[opt.value],
            session=sess,
            timeout_s=timeout_s,
        )
        by_div: dict[str, int] = {}
        for r in rows:
            by_div[r.division] = by_div.get(r.division, 0) + r.total_employee
        for div, total in by_div.items():
            out.append(CountryDivisionCell(
                country_name=opt.label,
                division=div,
                total_employee=total,
            ))
        # Be polite to oep.gov.bd — pause briefly to avoid IP rate-limiting.
        time.sleep(0.3)
    return out


@dataclass(frozen=True)
class CountryDivisionMonthCell:
    """One (country, division, month) value for the mega-sheet view."""

    country_name: str
    division: str
    year_month: str
    total_employee: int


def fetch_country_division_timeseries(
    date_from: str,
    date_to: str,
    *,
    country_options: Iterable[Option],
    gender_id: str | None = None,
    session: requests.Session | None = None,
    progress_cb=None,
    timeout_s: float = 90.0,
) -> list[CountryDivisionMonthCell]:
    """Walk every (country, month) pair and aggregate per division.

    Heavy: N_countries × N_months HTTP calls. Use a bounded country
    selection (5-20) unless you're prepared to wait many minutes.
    """
    opts = [o for o in country_options if o.value]
    months = iter_year_months(date_from, date_to)
    if not opts or not months:
        return []
    sess = session or requests.Session()
    sess.headers["User-Agent"] = USER_AGENT

    import time

    total_calls = len(opts) * len(months)
    out: list[CountryDivisionMonthCell] = []
    step = 0
    for opt in opts:
        for ym in months:
            step += 1
            if progress_cb:
                progress_cb(step, total_calls, f"{opt.label}  {ym}")
            m_from, m_to = _month_bounds(ym)
            rows = fetch_division_clearance(
                m_from, m_to,
                gender_id=gender_id,
                country_ids=[opt.value],
                session=sess,
                timeout_s=timeout_s,
            )
            by_div: dict[str, int] = {}
            for r in rows:
                by_div[r.division] = by_div.get(r.division, 0) + r.total_employee
            for div, total in by_div.items():
                out.append(CountryDivisionMonthCell(
                    country_name=opt.label,
                    division=div,
                    year_month=ym,
                    total_employee=total,
                ))
            # Pace ourselves to avoid IP rate-limiting on heavy runs.
            time.sleep(0.3)
    return out


@dataclass(frozen=True)
class CountryDistrictMonthCell:
    """One (country, division, district, month) value for the deep-detail view."""

    country_name: str
    division: str
    district: str
    year_month: str
    total_employee: int


def fetch_country_district_timeseries(
    date_from: str,
    date_to: str,
    *,
    country_options: Iterable[Option],
    gender_id: str | None = None,
    session: requests.Session | None = None,
    progress_cb=None,
    timeout_s: float = 90.0,
) -> list[CountryDistrictMonthCell]:
    """Same call pattern as `fetch_country_division_timeseries`, but the
    output preserves district-level granularity.

    Cost is identical: N_countries × N_months calls. The only difference
    is that we don't collapse the per-division dict — we emit one row per
    (division, district) cell.
    """
    import time

    opts = [o for o in country_options if o.value]
    months = iter_year_months(date_from, date_to)
    if not opts or not months:
        return []
    sess = session or requests.Session()
    sess.headers["User-Agent"] = USER_AGENT

    total_calls = len(opts) * len(months)
    out: list[CountryDistrictMonthCell] = []
    step = 0
    for opt in opts:
        for ym in months:
            step += 1
            if progress_cb:
                progress_cb(step, total_calls, f"{opt.label}  {ym}")
            m_from, m_to = _month_bounds(ym)
            rows = fetch_division_clearance(
                m_from, m_to,
                gender_id=gender_id,
                country_ids=[opt.value],
                session=sess,
                timeout_s=timeout_s,
            )
            for r in rows:
                out.append(CountryDistrictMonthCell(
                    country_name=opt.label,
                    division=r.division,
                    district=r.district,
                    year_month=ym,
                    total_employee=r.total_employee,
                ))
            time.sleep(0.3)
    return out


def pivot_country_district_timeseries(
    cells: Iterable[CountryDistrictMonthCell],
) -> tuple[
    list[str],
    list[tuple[str, str, str]],
    dict[tuple[str, str, str, str], int],
]:
    """Pack district-level mega-sheet cells into rendering-friendly structures.

    Returns:
      months  — sorted list of YYYY-MM strings
      triples — (country, division, district) row keys, sorted by country
                in input order, then division alphabetically, then district
      table   — {(country, division, district, ym): total}
    """
    months_set: set[str] = set()
    triples_set: set[tuple[str, str, str]] = set()
    country_order: list[str] = []
    seen_countries: set[str] = set()
    table: dict[tuple[str, str, str, str], int] = {}
    for c in cells:
        months_set.add(c.year_month)
        triples_set.add((c.country_name, c.division, c.district))
        if c.country_name not in seen_countries:
            country_order.append(c.country_name)
            seen_countries.add(c.country_name)
        table[(c.country_name, c.division, c.district, c.year_month)] = c.total_employee
    months = sorted(months_set)
    rank = {name: i for i, name in enumerate(country_order)}
    triples = sorted(triples_set, key=lambda t: (rank.get(t[0], 999), t[1], t[2]))
    return months, triples, table


def pivot_country_division_timeseries(
    cells: Iterable[CountryDivisionMonthCell],
) -> tuple[list[str], list[tuple[str, str]], dict[tuple[str, str, str], int]]:
    """Pack mega-sheet cells into rendering-friendly structures.

    Returns:
      months              — sorted list of YYYY-MM strings
      country_division    — (country, division) row keys, sorted by country
                            in input order then division alphabetically
      table               — {(country, division, ym): total}
    """
    months_set: set[str] = set()
    pairs_set: set[tuple[str, str]] = set()
    country_order: list[str] = []
    seen_countries: set[str] = set()
    table: dict[tuple[str, str, str], int] = {}
    for c in cells:
        months_set.add(c.year_month)
        pairs_set.add((c.country_name, c.division))
        if c.country_name not in seen_countries:
            country_order.append(c.country_name)
            seen_countries.add(c.country_name)
        table[(c.country_name, c.division, c.year_month)] = c.total_employee
    months = sorted(months_set)
    # Sort: country in input order, then division alphabetically
    rank = {name: i for i, name in enumerate(country_order)}
    pairs = sorted(pairs_set, key=lambda p: (rank.get(p[0], 999), p[1]))
    return months, pairs, table


def pivot_country_division(
    cells: Iterable[CountryDivisionCell],
) -> tuple[list[str], list[str], dict[tuple[str, str], int]]:
    """Pack pivot cells into (divisions_sorted, countries_in_input_order, lookup)."""
    div_set: set[str] = set()
    countries: list[str] = []
    seen_countries: set[str] = set()
    table: dict[tuple[str, str], int] = {}
    for c in cells:
        div_set.add(c.division)
        if c.country_name not in seen_countries:
            countries.append(c.country_name)
            seen_countries.add(c.country_name)
        table[(c.division, c.country_name)] = c.total_employee
    divisions = sorted(div_set)
    return divisions, countries, table


# ---------------------------------------------------------------------------
# Category drilldown for a single country
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryTotal_PerCountry:
    category_name: str
    total_employee: int


def categories_for_country(
    rows: Iterable[CountryClearance], country_name: str
) -> list[CategoryTotal_PerCountry]:
    """Filter + sort the country-clearance rows down to one country."""
    by_cat: dict[str, int] = {}
    for r in rows:
        if r.country_name == country_name and r.category_name:
            by_cat[r.category_name] = by_cat.get(r.category_name, 0) + r.total_employee
    out = [CategoryTotal_PerCountry(name, total) for name, total in by_cat.items()]
    out.sort(key=lambda x: x.total_employee, reverse=True)
    return out


def merge_gender_breakdowns(
    all_rows: Iterable[CountryClearance],
    male_rows: Iterable[CountryClearance],
    female_rows: Iterable[CountryClearance],
) -> list[GenderBreakdown]:
    """Build one row per destination with male/female/other counts.

    "Other" is derived as total - male - female so we only need three API
    calls instead of four.
    """
    totals: dict[str, int] = {}
    for r in all_rows:
        totals[r.country_name] = totals.get(r.country_name, 0) + r.total_employee
    males: dict[str, int] = {}
    for r in male_rows:
        males[r.country_name] = males.get(r.country_name, 0) + r.total_employee
    females: dict[str, int] = {}
    for r in female_rows:
        females[r.country_name] = females.get(r.country_name, 0) + r.total_employee

    out: list[GenderBreakdown] = []
    for name, total in totals.items():
        m = males.get(name, 0)
        f = females.get(name, 0)
        other = max(0, total - m - f)
        out.append(GenderBreakdown(
            country_name=name,
            male=m,
            female=f,
            other=other,
            total=total,
        ))
    out.sort(key=lambda g: g.total, reverse=True)
    return out
