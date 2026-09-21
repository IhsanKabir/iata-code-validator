"""Which Customer IDs are the same agency.

The warehouse gives one row per ACCOUNT, and a business can hold several.
Be Fresh Limited holds four: two under IATA code 42306773, one under the
placeholder code with its own account number, and one more. Reported
separately they understate the agency and misplace it in any ranking --
measured over May to September 2026, 206 agencies are split this way, and
correcting them moves ranks by as much as 491 places.

The key is the NAME, not the IATA code, and that is a deliberate choice
against the obvious one:

* Grouping on IATA code looks safer until you see the data. 2,545 of the
  4,688 agency accounts carry '0' -- a placeholder, not a code -- so that key
  would fuse thousands of unrelated businesses into one enormous fake agency.
* It also splits what it should join. Be Fresh's accounts carry 42306773 AND
  the placeholder, so an IATA key files the same business under two agencies.

The name key therefore does the grouping, and the IATA code does the
CHECKING: where the accounts under one name carry two different real codes,
that is flagged rather than merged in silence. Every group lists its members,
so a wrong merge is visible on the sheet instead of buried in a total -- the
same reason the call list marks an ambiguous contact rather than hiding it.

Location accounts never reach here: every real agency account number is
numeric, while a counter is written 'DAC-07 Baridhara', so the shape of the
identifier separates them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .visit_master import identity

#: Codes that mean "no IATA code" rather than naming one. 2,545 of 4,688
#: agency accounts carry the zero placeholder.
PLACEHOLDER_CODES = {"", "0", "00", "000", "none", "null", "n/a", "-"}


def clean_iata(code) -> str:
    """A real IATA code, or "" where the field is a placeholder."""
    text = str(code or "").strip()
    return "" if text.lower() in PLACEHOLDER_CODES else text


def is_agency_account(customer_id) -> bool:
    """True for a real agency account number.

    Agency accounts are numeric -- all 4,688 of them. A sales counter is
    written as a station code and a place name ('DAC-07 Baridhara'), and
    carries the agency's name in the customer column, so grouping on the
    name alone would pull a US-Bangla counter into the agency's total.
    """
    text = str(customer_id or "").strip()
    return bool(text) and text.isdigit()


@dataclass
class AgencyGroup:
    """One business, and the accounts it trades under."""

    key: str
    name: str = ""
    #: (customer_id, name, value), biggest first.
    members: list = field(default_factory=list)
    codes: set = field(default_factory=set)

    @property
    def ids(self) -> list:
        return [m[0] for m in self.members]

    @property
    def value(self) -> float:
        return sum(m[2] for m in self.members)

    @property
    def is_group(self) -> bool:
        """More than one account, so the sheet shows a '+' to expand it."""
        return len(self.members) > 1

    @property
    def code_conflict(self) -> bool:
        """Accounts here carry two different REAL IATA codes.

        Not proof of a wrong merge -- a business can hold two accreditations
        -- but it is the shape a wrong merge takes, so it is shown.
        """
        return len(self.codes) > 1

    @property
    def iata(self) -> str:
        return sorted(self.codes)[0] if self.codes else ""

    def label(self) -> str:
        if not self.is_group:
            return self.name
        return f"{self.name}  ({len(self.members)} accounts)"


def resolve(rows) -> tuple:
    """Group accounts into businesses.

    `rows` are mappings with customer_id, customer and optionally iata and
    value. Returns (groups, skipped) where groups is {key: AgencyGroup},
    ordered biggest first, and skipped lists identifiers that are not agency
    accounts -- reported rather than silently folded in.
    """
    from collections import defaultdict

    groups: dict = {}
    skipped: list = []
    seen: dict = defaultdict(dict)      # key -> {customer_id: value}
    names: dict = defaultdict(dict)     # key -> {customer_id: name}
    for r in rows:
        cid = str(r.get("customer_id") or "").strip()
        name = str(r.get("customer") or "").strip()
        if not is_agency_account(cid):
            if cid or name:
                skipped.append((cid, name))
            continue
        key = identity(name) or f"id:{cid}"
        g = groups.get(key)
        if g is None:
            g = groups[key] = AgencyGroup(key=key)
        # One row per ACCOUNT, however many times it is fed in. Callers
        # resolve over two windows at once so an agency that changed account
        # between them stays one business, and that hands the same account
        # in twice -- appending blindly listed it twice and doubled the total.
        seen[key][cid] = (seen[key].get(cid, 0.0)
                          + float(r.get("value") or 0.0))
        names[key][cid] = name or names[key].get(cid, "")
        code = clean_iata(r.get("iata"))
        if code:
            g.codes.add(code)
    for key, g in groups.items():
        g.members = [(cid, names[key][cid], value)
                     for cid, value in seen[key].items()]
        g.members.sort(key=lambda m: -m[2])
        # the name the biggest account trades under, so the sheet shows the
        # one a reader will recognise
        g.name = g.members[0][1] if g.members else ""
    ordered = dict(sorted(groups.items(), key=lambda kv: -kv[1].value))
    return ordered, skipped


def find(groups: dict, term: str, limit: int = 25) -> list:
    """Groups matching a typed name or account number, best first.

    Never returns one silently: 'TRAVELS' matches 1,825 agency names and
    'AIR' matches 666, so the caller is handed candidates to choose from.
    An exact account number is matched first, because that is unambiguous.
    """
    text = str(term or "").strip()
    if not text:
        return []
    exact = [g for g in groups.values() if text in g.ids]
    if exact:
        return exact[:limit]
    key = identity(text)
    lowered = text.lower()
    scored = []
    for g in groups.values():
        name = g.name.lower()
        if key and identity(g.name) == key:
            rank = 0                      # same business, spelled differently
        elif name.startswith(lowered):
            rank = 1
        elif lowered in name:
            rank = 2
        elif any(lowered in m[1].lower() for m in g.members):
            rank = 3                      # matched a member's own name
        else:
            continue
        scored.append((rank, -g.value, g))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [g for _rank, _v, g in scored[:limit]]
