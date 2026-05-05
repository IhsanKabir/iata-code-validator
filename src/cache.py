"""SQLite cache to avoid re-querying IATA codes already checked.

Local to each laptop; nothing transmitted anywhere.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .parser import LookupResult


_SCHEMA = """
CREATE TABLE IF NOT EXISTS lookups (
    iata_number TEXT PRIMARY KEY,
    trading_name TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    accredited TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_lookups_status ON lookups(status);
"""


class Cache:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get(self, iata: str) -> LookupResult | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM lookups WHERE iata_number = ?",
                (iata,),
            ).fetchone()
        if row is None:
            return None
        return LookupResult(
            iata_number=row["iata_number"],
            trading_name=row["trading_name"],
            country=row["country"],
            accredited=row["accredited"],
            status=row["status"],
            checked_at=row["checked_at"],
            notes=row["notes"],
        )

    def put(self, result: LookupResult) -> None:
        # Don't cache ERROR rows — they should be retried next run.
        if result.status == "ERROR":
            return
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO lookups
                    (iata_number, trading_name, country, accredited, status, checked_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(iata_number) DO UPDATE SET
                    trading_name=excluded.trading_name,
                    country=excluded.country,
                    accredited=excluded.accredited,
                    status=excluded.status,
                    checked_at=excluded.checked_at,
                    notes=excluded.notes
                """,
                (
                    result.iata_number,
                    result.trading_name,
                    result.country,
                    result.accredited,
                    result.status,
                    result.checked_at,
                    result.notes,
                ),
            )

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM lookups").fetchone()[0]
