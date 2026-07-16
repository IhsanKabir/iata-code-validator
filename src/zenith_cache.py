"""SQLite cache for Zenith customer lookups.

Two responsibilities:

  1. **Resume safety** — every successful fetch is committed before the
     next call starts. If the laptop reboots, the run resumes from the
     first un-cached ID with zero work lost.
  2. **De-dup across runs** — the user might rerun the same Excel input
     to fill in newly-added rows; cached IDs are skipped automatically.

Schema (single table):

    zenith_customers(
      customer_id TEXT PK,
      status TEXT,                       -- 'OK' / 'NOT_FOUND' / 'ERROR'
      title, first_name, ... country,    -- empty strings on NOT_FOUND/ERROR
      registration_date,
      error TEXT,                        -- non-empty when status != 'OK'
      checked_at TEXT                    -- ISO-ish timestamp
    )

A second `zenith_meta(key, value)` table tracks last-run info for the UI.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .zenith_client import (
    STATUS_ERROR,
    STATUS_NOT_FOUND,
    STATUS_OK,
    CustomerRecord,
    LookupResult,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS zenith_customers (
    customer_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    first_name TEXT NOT NULL DEFAULT '',
    middle_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    date_of_birth TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    home_phone TEXT NOT NULL DEFAULT '',
    home_phone_international TEXT NOT NULL DEFAULT '',
    mobile_phone TEXT NOT NULL DEFAULT '',
    mobile_phone_international TEXT NOT NULL DEFAULT '',
    office_phone TEXT NOT NULL DEFAULT '',
    nationality TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    spoken_language TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    postal_code TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    registration_date TEXT NOT NULL DEFAULT '',
    customer_type TEXT NOT NULL DEFAULT '',
    company_name TEXT NOT NULL DEFAULT '',
    administrative_name TEXT NOT NULL DEFAULT '',
    iata_number TEXT NOT NULL DEFAULT '',
    resolved_id TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_zenith_status ON zenith_customers(status);

CREATE TABLE IF NOT EXISTS zenith_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# All CustomerRecord fields in DB column order — keep in sync with _SCHEMA.
# customer_type/company_name/administrative_name/iata_number were ADDED in a
# later schema version (they were silently dropped before — agency rows came
# back from the cache with an empty Agency Name / IATA Number); _ensure_columns
# migrates older cache files in place.
_RECORD_COLUMNS = (
    "customer_type",
    "company_name",
    "administrative_name",
    "iata_number",
    "title",
    "first_name",
    "middle_name",
    "last_name",
    "date_of_birth",
    "email",
    "home_phone",
    "home_phone_international",
    "mobile_phone",
    "mobile_phone_international",
    "office_phone",
    "nationality",
    "language",
    "spoken_language",
    "address",
    "city",
    "postal_code",
    "country",
    "registration_date",
)


class ZenithCache:
    """Resume-safe cache for Zenith customer lookups."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            self._ensure_columns(conn)

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection) -> None:
        """In-place migration: CREATE TABLE IF NOT EXISTS never adds columns to an
        existing file, so add any missing ones (idempotent, data preserved)."""
        have = {row[1] for row in conn.execute("PRAGMA table_info(zenith_customers)")}
        for col in (*_RECORD_COLUMNS, "resolved_id"):
            if col not in have:
                conn.execute(
                    f"ALTER TABLE zenith_customers ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- writes ----

    def save_result(self, result: LookupResult) -> None:
        """Persist one lookup outcome. Idempotent — re-running replaces."""
        record = result.record or CustomerRecord(customer_id=result.customer_id)
        values: list = [result.customer_id, result.status]
        for col in _RECORD_COLUMNS:
            values.append(getattr(record, col, ""))
        # For name lookups the row is keyed by the query but the record carries
        # the REAL numeric id — persist it so the round-trip doesn't lose it.
        values.append(record.customer_id if record.customer_id != result.customer_id else "")
        values.append(result.error)
        values.append(result.checked_at)

        cols = ("customer_id, status, " + ", ".join(_RECORD_COLUMNS)
                + ", resolved_id, error, checked_at")
        placeholders = ", ".join("?" * len(values))
        sql = f"""
            INSERT INTO zenith_customers ({cols})
            VALUES ({placeholders})
            ON CONFLICT(customer_id) DO UPDATE SET
                status=excluded.status,
                {", ".join(f"{c}=excluded.{c}" for c in _RECORD_COLUMNS)},
                resolved_id=excluded.resolved_id,
                error=excluded.error,
                checked_at=excluded.checked_at
        """
        with self._conn() as conn:
            conn.execute(sql, values)

    def save_many(self, results: Iterable[LookupResult]) -> int:
        """Bulk insert / upsert. Returns count written."""
        count = 0
        for r in results:
            self.save_result(r)
            count += 1
        return count

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO zenith_meta(key, value) VALUES (?, ?)",
                (key, value),
            )

    # ---- reads ----

    def get_result(self, customer_id: str) -> LookupResult | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM zenith_customers WHERE customer_id = ?",
                (str(customer_id),),
            ).fetchone()
        return _row_to_result(row) if row else None

    def get_meta(self, key: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM zenith_meta WHERE key = ?", (key,),
            ).fetchone()
        return row["value"] if row else None

    def cached_ids(self, *, only_ok: bool = False) -> set[str]:
        """Set of IDs already in the cache.

        `only_ok=True` excludes NOT_FOUND/ERROR rows — useful when the user
        wants to retry the failures from a previous run.
        """
        with self._conn() as conn:
            if only_ok:
                rows = conn.execute(
                    "SELECT customer_id FROM zenith_customers WHERE status = ?",
                    (STATUS_OK,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT customer_id FROM zenith_customers",
                ).fetchall()
        return {r["customer_id"] for r in rows}

    def counts_by_status(self) -> dict[str, int]:
        """Returns {'OK': n, 'NOT_FOUND': m, 'ERROR': k}."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM zenith_customers GROUP BY status",
            ).fetchall()
        out = {STATUS_OK: 0, STATUS_NOT_FOUND: 0, STATUS_ERROR: 0}
        for r in rows:
            out[r["status"]] = r["n"]
        return out

    def iter_all(self, *, only_ok: bool = False) -> Iterable[LookupResult]:
        """Stream every cached row — used by the Excel exporter."""
        with self._conn() as conn:
            if only_ok:
                rows = conn.execute(
                    "SELECT * FROM zenith_customers WHERE status = ? ORDER BY customer_id",
                    (STATUS_OK,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM zenith_customers ORDER BY customer_id",
                ).fetchall()
        for row in rows:
            yield _row_to_result(row)

    # ---- maintenance ----

    def clear_errors(self) -> int:
        """Drop ERROR rows so the next run retries them. Returns count deleted."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM zenith_customers WHERE status = ?",
                (STATUS_ERROR,),
            )
            return cur.rowcount

    def reset(self) -> None:
        """Wipe everything. Used when the user wants a fresh start."""
        with self._conn() as conn:
            conn.executescript(
                "DELETE FROM zenith_customers; DELETE FROM zenith_meta;"
            )


def _row_to_result(row: sqlite3.Row) -> LookupResult:
    """Convert a DB row back to a LookupResult."""
    cid = row["customer_id"]
    status = row["status"]
    record = None
    if status in (STATUS_OK, STATUS_NOT_FOUND):
        kwargs = {col: row[col] for col in _RECORD_COLUMNS}
        # Name-lookup rows are keyed by the query; the record gets the real id back.
        record = CustomerRecord(customer_id=row["resolved_id"] or cid, **kwargs)
    return LookupResult(
        customer_id=cid,
        status=status,
        record=record,
        error=row["error"],
        checked_at=row["checked_at"],
    )


def utc_now_iso() -> str:
    """Lightweight timestamp used in meta rows."""
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
