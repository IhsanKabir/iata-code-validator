"""SQLite cache for the BD agency list.

The list endpoint returns ~6,113 records in one round-trip. We cache them
locally so:
  - The app opens with data immediately, no spinner on launch.
  - Lookups don't hit the network on every search.
  - The "Refresh" button is opt-in, not mandatory.

Cache layout (single table):
    agencies(id PK, agency_name, license_no, email, mobile, website,
             address, license_expired_date, status)

One side table holds metadata so the UI can show "Last fetched at ...".
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .bd_agency_client import Agency


_SCHEMA = """
CREATE TABLE IF NOT EXISTS bd_agencies (
    id INTEGER PRIMARY KEY,
    agency_name TEXT NOT NULL DEFAULT '',
    license_no TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    mobile TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    license_expired_date TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_bd_name ON bd_agencies(agency_name);
CREATE INDEX IF NOT EXISTS ix_bd_license ON bd_agencies(license_no);

CREATE TABLE IF NOT EXISTS bd_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class BDAgencyCache:
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

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM bd_agencies").fetchone()[0]

    def all(self) -> list[Agency]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM bd_agencies").fetchall()
        return [
            Agency(
                agency_name=r["agency_name"],
                license_no=r["license_no"],
                email=r["email"],
                mobile=r["mobile"],
                website=r["website"],
                address=r["address"],
                license_expired_date=r["license_expired_date"],
                status=r["status"],
                raw_id=r["id"],
            )
            for r in rows
        ]

    def last_refresh(self) -> str:
        """ISO timestamp string, or empty if never refreshed."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM bd_meta WHERE key='last_refresh'"
            ).fetchone()
        return row["value"] if row else ""

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def replace_all(self, agencies: list[Agency]) -> None:
        """Atomically swap the cache contents to the given list."""
        now = (
            datetime.now(timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        with self._conn() as conn:
            conn.execute("DELETE FROM bd_agencies")
            conn.executemany(
                """
                INSERT INTO bd_agencies (
                    id, agency_name, license_no, email, mobile, website,
                    address, license_expired_date, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        a.raw_id,
                        a.agency_name,
                        a.license_no,
                        a.email,
                        a.mobile,
                        a.website,
                        a.address,
                        a.license_expired_date,
                        a.status,
                    )
                    for a in agencies
                ],
            )
            conn.execute(
                """
                INSERT INTO bd_meta (key, value) VALUES ('last_refresh', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (now,),
            )
