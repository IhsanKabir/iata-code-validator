"""Parse the IATA CheckACode result HTML into structured data.

The page renders a "details" block after validation. Layout:

    32302491 is a Valid IATA Numeric Code
    AGENCY DETAILS
    Trading Name    TRAVEL POINT PTE. LTD.    Country  SINGAPORE
    This is an IATA Accredited Agent. ...

Or for invalid codes:

    99999999 is not a valid IATA Numeric Code
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class LookupResult:
    iata_number: str
    trading_name: str
    country: str
    accredited: str  # "Y", "N", or ""
    status: str      # VALID / INVALID / ERROR
    checked_at: str
    notes: str


_VALID_RE = re.compile(r"is a Valid IATA", re.IGNORECASE)
_INVALID_RE = re.compile(r"is not a valid IATA|Invalid IATA Numeric", re.IGNORECASE)
_ACCREDITED_RE = re.compile(r"IATA Accredited Agent", re.IGNORECASE)
_NON_ACCREDITED_RE = re.compile(r"not an IATA Accredited|non-accredited", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def parse_result(iata: str, page_text: str) -> LookupResult:
    """Parse visible text from the result block."""
    text = (page_text or "").strip()

    if not text:
        return _error(iata, "empty page text")

    if _INVALID_RE.search(text):
        return LookupResult(
            iata_number=iata,
            trading_name="",
            country="",
            accredited="",
            status="INVALID",
            checked_at=now_iso(),
            notes="Not a valid IATA Numeric Code",
        )

    if not _VALID_RE.search(text):
        return _error(iata, "could not detect valid/invalid marker")

    trading_name = _extract_after(text, ["Trading Name"])
    country = _extract_after(text, ["Country"])

    if _ACCREDITED_RE.search(text) and not _NON_ACCREDITED_RE.search(text):
        accredited = "Y"
    elif _NON_ACCREDITED_RE.search(text):
        accredited = "N"
    else:
        accredited = ""

    return LookupResult(
        iata_number=iata,
        trading_name=trading_name,
        country=country,
        accredited=accredited,
        status="VALID",
        checked_at=now_iso(),
        notes="",
    )


def _error(iata: str, reason: str) -> LookupResult:
    return LookupResult(
        iata_number=iata,
        trading_name="",
        country="",
        accredited="",
        status="ERROR",
        checked_at=now_iso(),
        notes=reason,
    )


_LABELS_TO_STOP_AT = ("Country", "Trading Name", "AGENCY DETAILS", "This is an", "This is not")


def _extract_after(text: str, labels: list[str]) -> str:
    """Find a label, return the value that follows it.

    Works for both layouts:
      A) inline:  "Trading Name    SOME NAME    Country  USA"
      B) lines :  "Trading Name\\nSOME NAME\\nCountry\\nUSA"

    Strategy: locate the label position in the full text (case-insensitive),
    take everything after it, then stop at the next known label, double newline,
    or end of string.
    """
    if not text:
        return ""
    haystack = text
    haystack_lower = haystack.lower()
    for label in labels:
        idx = haystack_lower.find(label.lower())
        if idx == -1:
            continue
        after = haystack[idx + len(label):]
        return _take_first_field(after, exclude_label=label)
    return ""


def _take_first_field(s: str, exclude_label: str = "") -> str:
    """Trim leading whitespace/newlines, then return up to the next label."""
    s = s.strip()
    if not s:
        return ""
    # Stop at any other known label
    earliest = len(s)
    for label in _LABELS_TO_STOP_AT:
        if label == exclude_label:
            continue
        i = s.lower().find(label.lower())
        if 0 < i < earliest:
            earliest = i
    candidate = s[:earliest].strip()
    # Collapse whitespace so multi-line "Country\nUSA" → "USA"
    # but multi-word "TRAVEL POINT PTE. LTD." stays intact.
    # Take just the first non-empty line if multi-line and the first line is empty.
    lines = [ln.strip() for ln in candidate.splitlines() if ln.strip()]
    if not lines:
        return ""
    # If layout B (label\nvalue), the first line *is* the value.
    # If layout A, the value was on the same line as the label and is line[0].
    return lines[0]
