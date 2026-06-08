"""SQLite send-log so a Bulk Mailer re-run never double-sends.

A "send" is keyed by (campaign, email, subject). `campaign` defaults to
the mapping-file name so two different blasts don't collide. Only
genuinely SENT messages are recorded as sent — drafts and failures are
logged too but don't block a retry.

The point: if the run dies at row 25 of 40, re-running it skips the 24
already SENT and resumes — no recipient gets two copies.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_log (
    campaign   TEXT NOT NULL,
    email      TEXT NOT NULL,
    subject    TEXT NOT NULL,
    status     TEXT NOT NULL,   -- SENT | DRAFTED | FAILED
    sent_at    TEXT NOT NULL,
    error      TEXT DEFAULT '',
    PRIMARY KEY (campaign, email, subject)
);
"""


class MailerLog:
    """Resume-safe record of what has already gone out."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def already_sent(self, campaign: str, email: str, subject: str) -> bool:
        """True only when this exact (campaign, email, subject) was SENT."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM sent_log WHERE campaign=? AND email=? AND subject=?",
                (campaign, email, subject),
            ).fetchone()
        return bool(row) and row[0] == "SENT"

    def record(
        self, campaign: str, email: str, subject: str, status: str, error: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sent_log "
                "(campaign, email, subject, status, sent_at, error) "
                "VALUES (?,?,?,?,?,?)",
                (campaign, email, subject, status,
                 datetime.now().isoformat(timespec="seconds"), error),
            )

    def sent_count(self, campaign: str) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM sent_log WHERE campaign=? AND status='SENT'",
                (campaign,),
            ).fetchone()[0]

    def clear_campaign(self, campaign: str) -> None:
        """Forget a campaign so the next run re-sends everything."""
        with self._connect() as conn:
            conn.execute("DELETE FROM sent_log WHERE campaign=?", (campaign,))
