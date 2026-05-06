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
