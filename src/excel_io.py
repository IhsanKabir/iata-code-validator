"""Read input IATA numbers from Excel and write timestamped result file."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter

from .config import (
    BD_OUTPUT_COLUMNS_FULL,
    BD_OUTPUT_COLUMNS_LOOKUP,
    OEP_OUTPUT_COLUMNS_CATEGORY_SUMMARY,
    OEP_OUTPUT_COLUMNS_COUNTRY_RAW,
    OEP_OUTPUT_COLUMNS_COUNTRY_SUMMARY,
    OEP_OUTPUT_COLUMNS_DIVISION_RAW,
    OEP_OUTPUT_COLUMNS_DIVISION_SUMMARY,
    OEP_OUTPUT_COLUMNS_GENDER_SUMMARY,
    OUTPUT_COLUMNS,
)
from .parser import LookupResult


def list_sheet_names(path: Path) -> list[str]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def list_columns(path: Path, sheet: str) -> list[str]:
    """Return header row values for the chosen sheet (row 1)."""
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet]
        first_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
        cols: list[str] = []
        for idx, val in enumerate(first_row):
            label = str(val).strip() if val is not None else ""
            if not label:
                label = f"Column {get_column_letter(idx + 1)}"
            cols.append(label)
        return cols
    finally:
        wb.close()


def read_iata_numbers(
    path: Path,
    sheet: str,
    column_index: int,
    start_row: int = 2,
    end_row: int | None = None,
) -> list[tuple[int, str]]:
    """Read IATA numbers from `column_index` (0-based) on `sheet`.

    Returns list of (excel_row_number, iata_number) tuples, skipping blanks.
    Numbers are normalized to digit-only strings.
    """
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet]
        rows: list[tuple[int, str]] = []
        for row_idx, row in enumerate(
            ws.iter_rows(min_row=start_row, max_row=end_row, values_only=True),
            start=start_row,
        ):
            if column_index >= len(row):
                continue
            value = row[column_index]
            if value is None:
                continue
            normalized = _normalize(value)
            if not normalized:
                continue
            rows.append((row_idx, normalized))
        return rows
    finally:
        wb.close()


def _normalize(value: object) -> str:
    """Strip whitespace; for floats from Excel that look like ints, drop the decimal."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    s = str(value).strip()
    # Some users paste codes with spaces or hyphens
    s = s.replace(" ", "").replace("-", "")
    return s


def build_output_path(folder: Path, stem: str = "iata_results") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return folder / f"{stem}_{timestamp}.xlsx"


class ResultWriter:
    """Streaming writer — flushes to disk every 10 rows.

    Always use as a context manager so the final flush runs even if the
    surrounding code raises:

        with ResultWriter(path) as writer:
            writer.append(result)
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._wb = Workbook()
        ws = self._wb.active
        ws.title = "Results"
        ws.append(OUTPUT_COLUMNS)
        self._row_count = 0
        self._save()

    def __enter__(self) -> "ResultWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def append(self, result: LookupResult) -> None:
        ws = self._wb.active
        ws.append([
            result.iata_number,
            result.trading_name,
            result.country,
            result.accredited,
            result.status,
            result.checked_at,
            result.notes,
        ])
        self._row_count += 1
        # Save every 10 rows to balance durability vs speed
        if self._row_count % 10 == 0:
            self._save()

    def append_many(self, results: Iterable[LookupResult]) -> None:
        for r in results:
            self.append(r)

    def close(self) -> None:
        self._save()

    def _save(self) -> None:
        self._wb.save(self.path)


# ---------------------------------------------------------------------------
# BD Travel Agency exports
# ---------------------------------------------------------------------------


def write_bd_full_list(path: Path, agencies: list) -> None:
    """Dump the full BD agency list to a fresh Excel file."""
    wb = Workbook()
    ws = wb.active
    ws.title = "BD Agencies"
    ws.append(BD_OUTPUT_COLUMNS_FULL)
    for a in agencies:
        ws.append([
            a.agency_name,
            a.license_no,
            a.email,
            a.mobile,
            a.website,
            a.address,
            a.license_expired_date,
            a.status,
        ])
    wb.save(path)


def write_bd_lookup_results(path: Path, results: list) -> None:
    """Write BD lookup results (one row per input)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Lookup Results"
    ws.append(BD_OUTPUT_COLUMNS_LOOKUP)
    for r in results:
        a = r.agency
        ws.append([
            r.searched_input,
            r.match_method,
            r.matched_field,
            r.match_score,
            a.agency_name if a else "",
            a.license_no if a else "",
            a.email if a else "",
            a.mobile if a else "",
            a.website if a else "",
            a.address if a else "",
            a.license_expired_date if a else "",
            a.status if a else "",
            r.other_matches,
        ])
    wb.save(path)


def build_bd_output_path(folder: Path, kind: str = "lookup") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = "bd_agency_full_list" if kind == "full" else "bd_agency_lookup"
    return folder / f"{stem}_{timestamp}.xlsx"


# ---------------------------------------------------------------------------
# OEP overseas-movement exports
# ---------------------------------------------------------------------------


def _safe_share(n: int, total: int) -> float:
    return round(100.0 * n / total, 2) if total > 0 else 0.0


def write_oep_country_report(
    path: Path,
    *,
    date_from: str,
    date_to: str,
    summary: list,
    raw_rows: list,
) -> None:
    """Write the "Top destinations" report (summary + raw)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "By Country"
    ws.append([f"Range: {date_from} → {date_to}"])
    ws.append([])
    ws.append(OEP_OUTPUT_COLUMNS_COUNTRY_SUMMARY)
    grand = sum(c.total_employee for c in summary) or 1
    for rank, c in enumerate(summary, start=1):
        ws.append([
            rank,
            c.country_name,
            c.total_employee,
            c.category_count,
            _safe_share(c.total_employee, grand),
        ])

    raw_ws = wb.create_sheet("Raw")
    raw_ws.append(OEP_OUTPUT_COLUMNS_COUNTRY_RAW)
    for r in raw_rows:
        raw_ws.append([
            r.country_id,
            r.country_name,
            r.category_name,
            r.total_employee,
        ])
    wb.save(path)


def write_oep_division_report(
    path: Path,
    *,
    date_from: str,
    date_to: str,
    summary: list,
    raw_rows: list,
) -> None:
    """Write the "Top source districts" report (summary + raw)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "By Division"
    ws.append([f"Range: {date_from} → {date_to}"])
    ws.append([])
    ws.append(OEP_OUTPUT_COLUMNS_DIVISION_SUMMARY)
    grand = sum(d.total_employee for d in summary) or 1
    for rank, d in enumerate(summary, start=1):
        ws.append([
            rank,
            d.division,
            d.total_employee,
            d.district_count,
            _safe_share(d.total_employee, grand),
        ])

    raw_ws = wb.create_sheet("Raw")
    raw_ws.append(OEP_OUTPUT_COLUMNS_DIVISION_RAW)
    for r in raw_rows:
        raw_ws.append([r.division, r.district, r.total_employee])
    wb.save(path)


def write_oep_category_report(
    path: Path,
    *,
    date_from: str,
    date_to: str,
    summary: list,
    raw_rows: list,
) -> None:
    """Write the "Top job categories" report (summary + raw)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "By Category"
    ws.append([f"Range: {date_from} → {date_to}"])
    ws.append([])
    ws.append(OEP_OUTPUT_COLUMNS_CATEGORY_SUMMARY)
    grand = sum(c.total_employee for c in summary) or 1
    for rank, c in enumerate(summary, start=1):
        ws.append([
            rank,
            c.category_name,
            c.total_employee,
            c.country_count,
            _safe_share(c.total_employee, grand),
        ])

    raw_ws = wb.create_sheet("Raw")
    raw_ws.append(OEP_OUTPUT_COLUMNS_COUNTRY_RAW)
    for r in raw_rows:
        raw_ws.append([
            r.country_id,
            r.country_name,
            r.category_name,
            r.total_employee,
        ])
    wb.save(path)


def write_oep_gender_report(
    path: Path,
    *,
    date_from: str,
    date_to: str,
    summary: list,
) -> None:
    """Write the "Gender breakdown by destination" report."""
    wb = Workbook()
    ws = wb.active
    ws.title = "By Gender"
    ws.append([f"Range: {date_from} → {date_to}"])
    ws.append([])
    ws.append(OEP_OUTPUT_COLUMNS_GENDER_SUMMARY)
    for rank, g in enumerate(summary, start=1):
        ws.append([
            rank,
            g.country_name,
            g.male,
            g.female,
            g.other,
            g.total,
            _safe_share(g.female, g.total),
        ])
    wb.save(path)


def write_oep_timeseries_report(
    path: Path,
    *,
    date_from: str,
    date_to: str,
    months: list,
    series: dict,
) -> None:
    """Write the monthly time-series report.

    Sheet "Time Series" — wide format with months as columns; useful for
    pasting into pivot charts. Sheet "Raw" — long format (month, country,
    total) for downstream analysis.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Time Series"
    ws.append([f"Range: {date_from} → {date_to}"])
    ws.append([])
    header = ["Country"] + list(months) + ["Total"]
    ws.append(header)
    # Sort countries by their grand total, descending.
    sorted_countries = sorted(
        series.items(), key=lambda kv: sum(kv[1]), reverse=True,
    )
    for name, values in sorted_countries:
        ws.append([name, *values, sum(values)])

    raw_ws = wb.create_sheet("Raw")
    raw_ws.append(["Year-Month", "Country", "Total Employees"])
    for name, values in sorted_countries:
        for ym, total in zip(months, values):
            raw_ws.append([ym, name, total])
    wb.save(path)


def write_oep_pivot_report(
    path: Path,
    *,
    date_from: str,
    date_to: str,
    divisions: list,
    countries: list,
    table: dict,
) -> None:
    """Write the country × division pivot report."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Pivot"
    ws.append([f"Range: {date_from} → {date_to}"])
    ws.append([])
    ws.append(["Division"] + list(countries) + ["Total"])
    grand_total = 0
    for div in divisions:
        row = [div]
        row_total = 0
        for country in countries:
            v = table.get((div, country), 0)
            row.append(v)
            row_total += v
        row.append(row_total)
        grand_total += row_total
        ws.append(row)
    totals = ["Total"]
    for country in countries:
        totals.append(sum(table.get((d, country), 0) for d in divisions))
    totals.append(grand_total)
    ws.append(totals)

    raw_ws = wb.create_sheet("Raw")
    raw_ws.append(["Country", "Division", "Total Employees"])
    for div in divisions:
        for country in countries:
            v = table.get((div, country), 0)
            if v:
                raw_ws.append([country, div, v])
    wb.save(path)


def write_oep_full_report(
    path: Path,
    *,
    date_from: str,
    date_to: str,
    gender_id: str,
    country_summary: list,
    category_summary: list,
    division_summary: list,
    gender_summary: list,
    raw_country: list,
    raw_division: list,
    months: list,
    series: dict,
    divisions: list,
    pivot_countries: list,
    table: dict,
    country_labels: list,
    cdt_months: list | None = None,
    cdt_pairs: list | None = None,
    cdt_table: dict | None = None,
) -> None:
    """Mega-report — one workbook with Cover + 6 data sheets + 2 raw sheets.

    Order of sheets (left to right) is what someone scanning the workbook
    will read first. Cover comes first so the file opens to the headline
    numbers; raw sheets are last for power users.
    """
    wb = Workbook()

    # ----- Cover -----
    cover = wb.active
    cover.title = "Cover"
    grand_total = sum(c.total_employee for c in country_summary)
    gender_label = {"1": "Male only", "2": "Female only", "3": "Other only"}.get(
        gender_id, "All genders"
    )

    cover.append(["BD Overseas Workforce Movement — Full Report"])
    cover.append([])
    cover.append(["Date range", f"{date_from}  →  {date_to}"])
    cover.append(["Gender filter", gender_label])
    cover.append(["Countries in time-series / pivot",
                  ", ".join(country_labels) or "—"])
    cover.append(["Generated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    cover.append([])
    cover.append(["Headline numbers"])
    cover.append(["Total workers cleared", grand_total])
    cover.append(["Unique destinations", len(country_summary)])
    cover.append(["Unique job categories", len(category_summary)])
    cover.append(["BD divisions covered", len(division_summary)])
    cover.append([])
    cover.append(["Top 5 destinations"])
    cover.append(["Rank", "Country", "Workers", "Share %"])
    for rank, c in enumerate(country_summary[:5], start=1):
        cover.append([
            rank, c.country_name, c.total_employee,
            _safe_share(c.total_employee, grand_total),
        ])

    # ----- By Country -----
    ws = wb.create_sheet("By Country")
    ws.append(OEP_OUTPUT_COLUMNS_COUNTRY_SUMMARY)
    for rank, c in enumerate(country_summary, start=1):
        ws.append([
            rank, c.country_name, c.total_employee, c.category_count,
            _safe_share(c.total_employee, grand_total),
        ])

    # ----- By Division -----
    ws = wb.create_sheet("By Division")
    div_grand = sum(d.total_employee for d in division_summary) or 1
    ws.append(OEP_OUTPUT_COLUMNS_DIVISION_SUMMARY)
    for rank, d in enumerate(division_summary, start=1):
        ws.append([
            rank, d.division, d.total_employee, d.district_count,
            _safe_share(d.total_employee, div_grand),
        ])

    # ----- By Category -----
    ws = wb.create_sheet("By Category")
    cat_grand = sum(c.total_employee for c in category_summary) or 1
    ws.append(OEP_OUTPUT_COLUMNS_CATEGORY_SUMMARY)
    for rank, c in enumerate(category_summary, start=1):
        ws.append([
            rank, c.category_name, c.total_employee, c.country_count,
            _safe_share(c.total_employee, cat_grand),
        ])

    # ----- By Gender -----
    ws = wb.create_sheet("By Gender")
    ws.append(OEP_OUTPUT_COLUMNS_GENDER_SUMMARY)
    for rank, g in enumerate(gender_summary, start=1):
        ws.append([
            rank, g.country_name, g.male, g.female, g.other, g.total,
            _safe_share(g.female, g.total),
        ])

    # ----- Time Series -----
    ws = wb.create_sheet("Time Series")
    ws.append(["Country", *months, "Total"])
    sorted_ts = sorted(series.items(), key=lambda kv: sum(kv[1]), reverse=True)
    for name, values in sorted_ts:
        ws.append([name, *values, sum(values)])

    # ----- Pivot -----
    ws = wb.create_sheet("Country x Division")
    ws.append(["Division", *pivot_countries, "Total"])
    pivot_grand = 0
    for div in divisions:
        row = [div]
        row_total = 0
        for country in pivot_countries:
            v = table.get((div, country), 0)
            row.append(v)
            row_total += v
        row.append(row_total)
        pivot_grand += row_total
        ws.append(row)
    totals = ["Total"]
    for country in pivot_countries:
        totals.append(sum(table.get((d, country), 0) for d in divisions))
    totals.append(pivot_grand)
    ws.append(totals)

    # ----- Country × Division × Month (flat) -----
    if cdt_pairs and cdt_months and cdt_table is not None:
        ws = wb.create_sheet("Country×Division×Month")
        ws.append(["Country", "Division", *cdt_months, "Total"])
        col_totals = [0] * len(cdt_months)
        cdt_grand = 0
        for country, division in cdt_pairs:
            row = [country, division]
            row_total = 0
            for ci, ym in enumerate(cdt_months):
                v = cdt_table.get((country, division, ym), 0)
                row.append(v)
                row_total += v
                col_totals[ci] += v
            row.append(row_total)
            cdt_grand += row_total
            ws.append(row)
        ws.append(["Total", "", *col_totals, cdt_grand])

    # ----- Raw sheets -----
    raw_c = wb.create_sheet("Raw Country×Category")
    raw_c.append(OEP_OUTPUT_COLUMNS_COUNTRY_RAW)
    for r in raw_country:
        raw_c.append([r.country_id, r.country_name, r.category_name, r.total_employee])

    raw_d = wb.create_sheet("Raw Division")
    raw_d.append(OEP_OUTPUT_COLUMNS_DIVISION_RAW)
    for r in raw_division:
        raw_d.append([r.division, r.district, r.total_employee])

    wb.save(path)


def write_oep_country_district_timeseries(
    path: Path,
    *,
    date_from: str,
    date_to: str,
    months: list,
    triples: list,
    table: dict,
) -> None:
    """One flat sheet at district granularity.

    Schema:
      A: Country | B: Division | C: District | D..N: month columns | last: Total
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Country×District×Month"
    ws.append([f"Range: {date_from} → {date_to}"])
    ws.append([])
    ws.append(["Country", "Division", "District", *months, "Total"])
    col_totals = [0] * len(months)
    grand_total = 0
    for country, division, district in triples:
        row_values = [country, division, district]
        row_total = 0
        for ci, ym in enumerate(months):
            v = table.get((country, division, district, ym), 0)
            row_values.append(v)
            row_total += v
            col_totals[ci] += v
        row_values.append(row_total)
        grand_total += row_total
        ws.append(row_values)
    ws.append(["Total", "", "", *col_totals, grand_total])
    wb.save(path)


def write_oep_country_division_timeseries(
    path: Path,
    *,
    date_from: str,
    date_to: str,
    months: list,
    pairs: list,
    table: dict,
) -> None:
    """One flat sheet: each row is (Country, Division) × monthly columns.

    Schema:
      A: Country | B: Division | C..N: month columns | last: Total

    Tidy/long data — pasteable straight into a PivotTable for any cut
    (heatmaps, % change, sum-over-time, etc.).
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Country×Division×Month"
    ws.append([f"Range: {date_from} → {date_to}"])
    ws.append([])
    ws.append(["Country", "Division", *months, "Total"])
    grand_total = 0
    # Column totals row collected as we go
    col_totals = [0] * len(months)
    for country, division in pairs:
        row_values = [country, division]
        row_total = 0
        for ci, ym in enumerate(months):
            v = table.get((country, division, ym), 0)
            row_values.append(v)
            row_total += v
            col_totals[ci] += v
        row_values.append(row_total)
        grand_total += row_total
        ws.append(row_values)
    # Totals row
    totals_row = ["Total", "", *col_totals, grand_total]
    ws.append(totals_row)
    wb.save(path)


def build_oep_output_path(folder: Path, kind: str) -> Path:
    """`kind` ∈ {country, division, category, gender, timeseries, pivot}."""
    folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return folder / f"oep_{kind}_{timestamp}.xlsx"
