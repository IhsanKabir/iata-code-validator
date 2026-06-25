"""SQLite-backed cache for PNR Dossier lookups.

A single audit can touch 10,000+ PNRs. Re-running the audit (after a
new history download, or after tuning verdict thresholds) should not
re-hit Zenith for PNRs we already know. The cache stores the parsed
PNRDetails as JSON per PNR code; rebuild from cache is sub-second.

Schema is intentionally simple — one table, no migrations. The JSON
column lets us evolve PNRDetails fields without altering the schema.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .zenith_pnr_client import PNRDetails, PNRSegment

log = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pnr_details (
    pnr_code TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL  -- JSON-serialised PNRDetails
);

CREATE INDEX IF NOT EXISTS idx_pnr_fetched_at
    ON pnr_details(fetched_at);
"""


class ZenithPNRCache:
    """Persistent lookup cache keyed by uppercase PNR code."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False so the worker thread that fetches and
        # the UI thread that renders can both touch the DB. SQLite serialises
        # writes internally; reads are concurrent.
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, pnr_code: str) -> PNRDetails | None:
        if not pnr_code:
            return None
        key = pnr_code.strip().upper()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM pnr_details WHERE pnr_code = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0])
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("Cache row for %s is unreadable: %s", key, exc)
            return None
        return _details_from_dict(data)

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM pnr_details",
            ).fetchone()[0]

    def iter_all(self) -> list[PNRDetails]:
        """Every cached PNRDetails, ordered by PNR code — the source of truth for an
        export-from-cache (a storm-killed run still yields a usable sheet). Unreadable
        rows are skipped, never fatal."""
        out: list[PNRDetails] = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM pnr_details ORDER BY pnr_code",
            ).fetchall()
        for (payload,) in rows:
            try:
                out.append(_details_from_dict(json.loads(payload)))
            except (json.JSONDecodeError, TypeError) as exc:
                log.warning("Skipping unreadable PNR cache row: %s", exc)
        return out

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def put(self, details: PNRDetails) -> None:
        if not details.pnr_code:
            return
        payload = json.dumps(_details_to_dict(details))
        fetched = (details.fetched_at or datetime.now()).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pnr_details "
                "(pnr_code, fetched_at, payload) VALUES (?, ?, ?)",
                (details.pnr_code.strip().upper(), fetched, payload),
            )

    def put_many(self, items: list[PNRDetails]) -> None:
        rows = []
        for d in items:
            if not d.pnr_code:
                continue
            rows.append((
                d.pnr_code.strip().upper(),
                (d.fetched_at or datetime.now()).isoformat(timespec="seconds"),
                json.dumps(_details_to_dict(d)),
            ))
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO pnr_details "
                "(pnr_code, fetched_at, payload) VALUES (?, ?, ?)",
                rows,
            )

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pnr_details")


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _details_to_dict(d: PNRDetails) -> dict:
    out = asdict(d)
    # `fetched_at` is a datetime — JSON needs a string.
    if d.fetched_at is not None:
        out["fetched_at"] = d.fetched_at.isoformat(timespec="seconds")
    # `segments` becomes a list of dicts automatically via asdict.
    return out


def _details_from_dict(data: dict) -> PNRDetails:
    fetched_raw = data.get("fetched_at")
    fetched = None
    if fetched_raw:
        try:
            fetched = datetime.fromisoformat(fetched_raw)
        except ValueError:
            fetched = None
    segments_raw = data.get("segments") or []
    segments = tuple(
        PNRSegment(
            leg_route=s.get("leg_route", ""),
            leg_direction=s.get("leg_direction", ""),
            departure_date=s.get("departure_date", ""),
            departure_text=s.get("departure_text", ""),
            arrival_text=s.get("arrival_text", ""),
            aircraft=s.get("aircraft", ""),
            fare_basis=s.get("fare_basis", ""),
            rbd_class=s.get("rbd_class", ""),
            coupon_status=s.get("coupon_status", ""),
            price_ht=s.get("price_ht", ""),
            price_ttc=s.get("price_ttc", ""),
            ticket_number=s.get("ticket_number", ""),
            passenger=s.get("passenger", ""),
        )
        for s in segments_raw
    )
    return PNRDetails(
        pnr_code=data.get("pnr_code", ""),
        dossier_id=data.get("dossier_id", ""),
        customer_name=data.get("customer_name", ""),
        traveler_surname=data.get("traveler_surname", ""),
        phone=data.get("phone", ""),
        payment_method=data.get("payment_method", ""),
        pax_count=int(data.get("pax_count") or 0),
        pnr_status=data.get("pnr_status", ""),
        currency=data.get("currency", ""),
        total_amount=data.get("total_amount", ""),
        total_taxes=data.get("total_taxes", ""),
        segments=segments,
        fetched_at=fetched,
    )
