"""Parser for the per-PNR dossier CHANGES-history tab (Phase 2).

Step-0 discovery (docs/pnr_history_phase2_discovery.md) established that the
``search_event.asp?contexte=recap_dossier&CategorieEvent=3&id_dossier_vol=<id>&excel=1``
view returns a clean columnar table:

    Date | Created by | Description | Type | PNR | Customer | Flight | Passenger

The Description cell is free text with facts joined by ``<br>`` (which ``_TableReader``
flattens to `` | ``). This module turns that table into ``DossierEvent`` rows and extracts
the signals the flight ModificationHistory corpus CANNOT see — the Phase-2 gap:

  * payment method + transaction id   (``BKASH PAYMENT//Transaction ID-XXXX//``)
  * passenger contact set / change     (``PAX CONTACT-<old> -> PAX CONTACT-<new>``)
  * explicit reissue / exchange        (``IATA Coupon status : I -> E``)
  * coupon-status transition           (``IATA Coupon status : <from> -> <to>``)

Everything is offline + verified against the real (sanitised) format; the live scrape that
feeds it is `zenith_pnr_history_downloader`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .zenith_history_parser import Agent, _TableReader, parse_agent, parse_timestamp

# The CHANGES tab (CategorieEvent=3) is the one carrying these comments.
CHANGES_TAB = "changes"
TICKETS_TAB = "tickets"

# --- comment extractors (validated against the Step-0 sample) ----------------
# Contact: full change "PAX CONTACT-<old> -> PAX CONTACT-<new>" OR initial set
# " -> PAX CONTACT-<new>" (old side absent). Values are phone/email tokens.
_CONTACT_RE = re.compile(
    r"(?:PAX\s*CONTACT-(?P<old>[^\s|>]+))?\s*->\s*PAX\s*CONTACT-(?P<new>[^\s|>]+)",
    re.IGNORECASE,
)
# Payment: "<METHOD> PAYMENT//Transaction ID-<txn>//"  (BKASH / NAGAD / CARD / ...)
_PAYMENT_RE = re.compile(
    r"(?P<method>[A-Z][A-Za-z]*)\s+PAYMENT//\s*Transaction\s*ID-(?P<txn>[A-Za-z0-9]+)//",
    re.IGNORECASE,
)
# Coupon status transition, e.g. "IATA Coupon status : I -> E" (E = Exchanged = reissue).
_COUPON_RE = re.compile(
    r"IATA\s*Coupon\s*status\s*:\s*(?P<from>[A-Za-z]{1,3})\s*->\s*(?P<to>[A-Za-z]{1,3})",
    re.IGNORECASE,
)

_HEADER_KEYS = ("date", "created by", "description", "type", "pnr")


@dataclass(frozen=True)
class DossierEvent:
    """One row of a dossier's CHANGES history, with the gap signals derived."""

    dossier_id: str
    row_index: int
    raw_date: str
    timestamp: datetime | None        # GMT (excel view), like the flight corpus
    agent: Agent
    raw_description: str
    event_type: str
    pnr: str
    customer: str
    raw_flight: str
    passenger: str
    # derived signals
    payment_method: str = ""
    payment_txn_id: str = ""
    contact_old: str = ""
    contact_new: str = ""
    is_reissue: bool = False          # exchange (coupon I->E) or Type contains "Exchang"
    coupon_from: str = ""
    coupon_to: str = ""

    @property
    def contact_changed(self) -> bool:
        """A real change (not a first-time set, not a no-op re-save)."""
        return bool(self.contact_old and self.contact_new
                    and self.contact_old != self.contact_new)


def _find_header(rows: list[list[str]]) -> tuple[int, dict[str, int]] | None:
    """Locate the column header row and map our fields to column indices."""
    for i, row in enumerate(rows):
        low = [c.strip().lower() for c in row]
        if sum(1 for k in _HEADER_KEYS if k in low) >= 4:
            idx = {}
            for col, name in enumerate(low):
                if name in ("date", "created by", "description", "type", "pnr",
                            "customer", "flight", "passenger"):
                    idx[name] = col
            return i, idx
    return None


def _cell(row: list[str], idx: dict[str, int], name: str) -> str:
    col = idx.get(name)
    return row[col].strip() if col is not None and col < len(row) else ""


def _extract(desc: str) -> dict:
    """Pull payment / contact / reissue signals out of one Description cell."""
    out = {"payment_method": "", "payment_txn_id": "", "contact_old": "", "contact_new": "",
           "is_reissue": False, "coupon_from": "", "coupon_to": ""}
    m = _PAYMENT_RE.search(desc)
    if m:
        out["payment_method"] = m.group("method").upper()
        out["payment_txn_id"] = m.group("txn")
    m = _CONTACT_RE.search(desc)
    if m:
        out["contact_old"] = (m.group("old") or "").strip()
        out["contact_new"] = (m.group("new") or "").strip()
    m = _COUPON_RE.search(desc)
    if m:
        out["coupon_from"] = m.group("from").upper()
        out["coupon_to"] = m.group("to").upper()
        out["is_reissue"] = out["coupon_to"] == "E" or out["coupon_from"] == "E"
    return out


def parse_dossier_changes(html: str, dossier_id: str) -> list[DossierEvent]:
    """Parse one CHANGES-history (excel) HTML blob into DossierEvent rows.

    Tolerant: returns [] for an empty/short/error page, skips rows that don't have a
    PNR + a parseable header. Never raises on malformed input.
    """
    if not html or len(html) < 200:
        return []
    reader = _TableReader()
    try:
        reader.feed(html)
    except Exception:  # noqa: BLE001 — malformed HTML must not crash a bulk run
        return []
    found = _find_header(reader.rows)
    if not found:
        return []
    hdr_i, idx = found
    events: list[DossierEvent] = []
    for n, row in enumerate(reader.rows[hdr_i + 1:]):
        pnr = _cell(row, idx, "pnr")
        created = _cell(row, idx, "created by")
        if not pnr and not created:
            continue                                   # spacer / total row
        desc = _cell(row, idx, "description")
        sig = _extract(desc)
        etype = _cell(row, idx, "type")
        if "exchang" in etype.lower():
            sig["is_reissue"] = True
        events.append(DossierEvent(
            dossier_id=str(dossier_id), row_index=n,
            raw_date=_cell(row, idx, "date"),
            timestamp=parse_timestamp(_cell(row, idx, "date")),
            agent=parse_agent(created),
            raw_description=desc, event_type=etype, pnr=pnr,
            customer=_cell(row, idx, "customer"),
            raw_flight=_cell(row, idx, "flight"),
            passenger=_cell(row, idx, "passenger"),
            **sig,
        ))
    return events
