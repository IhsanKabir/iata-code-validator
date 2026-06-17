"""SQLite cache of RAW Zenith dossier event-history HTML, keyed by (dossier_id, tab).

The PNR History / Audit feature scrapes each dossier's event-history tabs (Tickets'
history, Changes history, Reissue history, …). This cache stores the **raw gzipped
HTML of each tab exactly as scraped — not parsed rows** — so a later parser fix (new
regex, changed column layout) re-runs entirely offline against the cache and never
re-hits the (fragile, recently-504-storming) GDS.

One row per (dossier_id, tab). A *bundle* is the set of tab rows for one dossier; the
downloader writes a bundle ATOMICALLY (all tabs at once) so a half-scraped dossier is
never mistaken for "done" on a resumed run.

Mirrors `zenith_pnr_cache.ZenithPNRCache` (one table, WAL, `check_same_thread=False`)
but the payload is a gzipped-HTML BLOB plus scrape metadata (http_status, byte_size,
fetched_at, scrape_version) instead of JSON. Re-parsing thousands of dossiers from this
cache is offline and sub-minute; re-scraping them is hours of GDS traffic.
"""
from __future__ import annotations

import gzip
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# Bump when the SCRAPE shape changes (e.g. a new tab is added) so older rows are
# distinguishable and can be force-refreshed. Independent of any PARSER version.
SCRAPE_VERSION = 1


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pnr_history (
    dossier_id     TEXT NOT NULL,
    tab            TEXT NOT NULL,
    fetched_at     TEXT NOT NULL,
    http_status    INTEGER NOT NULL,
    byte_size      INTEGER NOT NULL,
    scrape_version INTEGER NOT NULL,
    html_gz        BLOB NOT NULL,
    PRIMARY KEY (dossier_id, tab)
);

CREATE INDEX IF NOT EXISTS idx_pnr_history_fetched_at
    ON pnr_history(fetched_at);
"""


@dataclass(frozen=True)
class RawTab:
    """One scraped history tab for one dossier (HTML decompressed on read)."""

    dossier_id: str
    tab: str
    html: str
    http_status: int
    byte_size: int            # original (UNCOMPRESSED) HTML size — drives the "<2 KB = empty" check
    fetched_at: datetime | None
    scrape_version: int = SCRAPE_VERSION


class ZenithPNRHistoryCache:
    """Persistent raw-HTML cache keyed by (dossier_id, tab)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False so the scraper worker and the UI thread can both
        # touch the DB; SQLite serialises writes, reads are concurrent.
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_bundle(self, dossier_id: str) -> dict[str, RawTab] | None:
        """All cached tabs for a dossier as ``{tab: RawTab}``, or None if none cached."""
        key = str(dossier_id).strip()
        if not key:
            return None
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tab, fetched_at, http_status, byte_size, scrape_version, html_gz "
                "FROM pnr_history WHERE dossier_id = ?",
                (key,),
            ).fetchall()
        if not rows:
            return None
        out: dict[str, RawTab] = {}
        for tab, fetched, status, size, ver, html_gz in rows:
            try:
                html = gzip.decompress(html_gz).decode("utf-8", errors="replace")
            except (OSError, EOFError) as exc:  # corrupt blob — skip, don't crash a run
                log.warning("Corrupt cached tab %s/%s: %s", key, tab, exc)
                continue
            out[tab] = RawTab(
                dossier_id=key, tab=str(tab), html=html,
                http_status=int(status), byte_size=int(size),
                fetched_at=_parse_dt(fetched), scrape_version=int(ver),
            )
        return out or None

    def is_fresh(self, dossier_id: str, *, stale_after_days: float,
                 now: datetime | None = None) -> bool:
        """True iff EVERY cached tab for the dossier was fetched within the window.

        A partially-cached dossier (or one with any stale tab) is not fresh, so the
        downloader re-scrapes it rather than serving an incomplete/old bundle.
        """
        key = str(dossier_id).strip()
        if not key:
            return False
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT fetched_at FROM pnr_history WHERE dossier_id = ?",
                (key,),
            ).fetchall()
        if not rows:
            return False
        cutoff = (now or datetime.now()) - timedelta(days=stale_after_days)
        for (fetched,) in rows:
            dt = _parse_dt(fetched)
            if dt is None or dt < cutoff:
                return False
        return True

    def count_dossiers(self) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(DISTINCT dossier_id) FROM pnr_history",
            ).fetchone()[0]

    def count_tabs(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM pnr_history").fetchone()[0]

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def put_bundle(self, dossier_id: str,
                   tabs: dict[str, tuple[str, int]],
                   *, now: datetime | None = None) -> None:
        """Atomically REPLACE all tabs for one dossier.

        ``tabs`` maps ``tab -> (html, http_status)``. Existing rows for the dossier are
        deleted and the new set written in one transaction, so a resumed run never sees
        a half-written bundle.
        """
        key = str(dossier_id).strip()
        if not key or not tabs:
            return
        stamp = (now or datetime.now()).isoformat(timespec="seconds")
        rows = []
        for tab, (html, status) in tabs.items():
            raw = (html or "").encode("utf-8")
            rows.append((
                key, str(tab), stamp, int(status), len(raw),
                SCRAPE_VERSION, gzip.compress(raw),
            ))
        with self._connect() as conn:
            conn.execute("DELETE FROM pnr_history WHERE dossier_id = ?", (key,))
            conn.executemany(
                "INSERT INTO pnr_history "
                "(dossier_id, tab, fetched_at, http_status, byte_size, scrape_version, html_gz) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pnr_history")

    def purge_older_than(self, days: float, *, now: datetime | None = None) -> int:
        """Delete tabs fetched more than ``days`` ago. Returns rows removed (retention)."""
        cutoff = ((now or datetime.now()) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM pnr_history WHERE fetched_at < ?", (cutoff,))
            return cur.rowcount


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
