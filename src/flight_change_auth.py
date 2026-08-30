"""Flight Change Authenticator — was a free (FOC) reissue actually earned?

A passenger is entitled to a free reissue when WE moved their flight far enough:
the schedule shifted by at least the sector's threshold (30 min domestic, 1 hour
international by default — INCLUSIVE, so an exact 30-minute move qualifies), and
the reissue happened within the reissue window (1 month by default).

This module is the pure decision engine: it takes a PNR's change-history events
(``zenith_pnr_history_parser.DossierEvent``) and returns one verdict per reissue.
No network, no Excel — all of it is unit-tested against real, sanitised strings
taken from the manual investigation workbook.

WHEN IS THE REISSUE?
--------------------
Not from description prose, which varies by agent. The authoritative marker is the
coupon transition ``IATA Coupon status : I -> E`` (Issued -> Exchanged), already
parsed upstream into ``DossierEvent.is_reissue``; a Type containing "Exchang" is
the fallback. The reissue TIME is that event's timestamp.

WHICH CHANGE GOVERNS IT?
------------------------
A PNR is often moved several times, so both readings are computed:

  governing   the latest schedule change BEFORE the reissue — what the agent was
              acting on at the time;
  cumulative  earliest original departure -> latest revised departure among the
              changes before the reissue — because several small moves can add up
              to a genuine entitlement.

The verdict uses the CUMULATIVE shift by default: it is the reading most
favourable to the agent, so a flag raised against it is defensible.

UNKNOWN MARKERS ARE NOT "CLEAN"
-------------------------------
Only three change grammars are known (see the parsers below). A reissue whose PNR
carries no recognised change, but does carry unclassified events, is reported as
NEEDS_REVIEW — never as justified and never as a clean miss. Those event types are
listed so new grammars can be found and added.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Both endpoints here => the leg is domestic (30-minute rule); anything else is
# international (1-hour rule).
BD_AIRPORTS = frozenset({"DAC", "CGP", "CXB", "ZYL", "JSR", "RJH", "BZL", "SPD"})

# --- verdicts ---------------------------------------------------------------
JUSTIFIED = "JUSTIFIED"
BELOW_THRESHOLD = "BELOW_THRESHOLD"
OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
NO_CHANGE_FOUND = "NO_CHANGE_FOUND"
NEEDS_REVIEW = "NEEDS_REVIEW"

# --- change kinds -----------------------------------------------------------
KIND_TIME = "TIME_REVISION"       # departure time moved
KIND_TRANSFER = "FLIGHT_TRANSFER"  # moved to another flight/date
KIND_CANCEL = "CANCELLATION"      # flight cancelled / NOOP

#: "DACSHJ/10/10/2025/ 20:45->00:30/1 ==> 10/10/2025/ 21:25->00:30/1"
#: route(6) / date / dep->arr [/day-offset]  ==>  date / dep->arr [/day-offset]
_TIME_RE = re.compile(
    r"(?P<route>[A-Z]{6})\s*/\s*(?P<d1>\d{1,2}/\d{1,2}/\d{4})\s*/\s*"
    r"(?P<t1>\d{1,2}:\d{2})\s*->\s*(?P<a1>\d{1,2}:\d{2})(?:\s*/\s*\d+)?"
    r"\s*==>\s*"
    r"(?P<d2>\d{1,2}/\d{1,2}/\d{4})\s*/\s*"
    r"(?P<t2>\d{1,2}:\d{2})\s*->\s*(?P<a2>\d{1,2}:\d{2})(?:\s*/\s*\d+)?",
    re.IGNORECASE)

#: "Flight:BS 381 01/10/2025->BS 381 03/10/2025"
_TRANSFER_RE = re.compile(
    r"Flight\s*:\s*(?P<f1>[A-Z0-9]{2}\s*\d{2,4}[A-Z]?)\s+(?P<d1>\d{1,2}/\d{1,2}/\d{4})"
    r"\s*->\s*(?P<f2>[A-Z0-9]{2}\s*\d{2,4}[A-Z]?)\s+(?P<d2>\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE)

_CANCEL_RE = re.compile(
    r"(cancellation\s+modifications?\s+in\s+progress|NOOP\s+FLIGHT|"
    r"CANCELLED\s+FLIGHT)", re.IGNORECASE)

#: "Addition of new ticket(s) to be exchanged : flight BS345 25/10/2025 21:25:00
#: (DAC -> SHJ)" — a File Modification event. This, not the coupon line, is how
#: most reissues actually appear: a live 2,286-event run carried 46 of these but
#: only 2 "Issued->Exchanged" coupon lines. It also carries the route and the new
#: departure, which the coupon line does not.
_EXCHANGE_ADD_RE = re.compile(
    r"Addition\s+of\s+new\s+ticket\(s\)\s+to\s+be\s+exchanged\s*:?\s*"
    r"flight\s+(?P<flight>[A-Z0-9]{2}\s*\d{2,4}[A-Z]?)\s+"
    r"(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+(?P<time>\d{1,2}:\d{2})(?::\d{2})?\s*"
    r"\(\s*(?P<orig>[A-Z]{3})\s*->\s*(?P<dest>[A-Z]{3})\s*\)",
    re.IGNORECASE)

_EXCHANGE_TYPE_RE = re.compile(r"exchang", re.IGNORECASE)

_TRANSFER_HINT_RE = re.compile(r"flight\s*transfer", re.IGNORECASE)
_TYPE_TIME_RE = re.compile(r"chang\w*\s+flight\s+time", re.IGNORECASE)


@dataclass(frozen=True)
class AuthConfig:
    """Every rule number is a setting — the desk changes these, not the code."""

    domestic_minutes: int = 30
    international_minutes: int = 60
    reissue_window_days: int = 30
    inclusive: bool = True          # an exact 30-minute move QUALIFIES
    basis: str = "cumulative"       # "cumulative" | "governing"
    bd_airports: frozenset = BD_AIRPORTS

    def threshold_for(self, sector: str) -> int:
        return (self.domestic_minutes if sector == "Domestic"
                else self.international_minutes)

    def qualifies(self, shift_minutes: float | None, sector: str) -> bool:
        if shift_minutes is None:
            return False
        need = self.threshold_for(sector)
        got = abs(shift_minutes)
        return got >= need if self.inclusive else got > need


@dataclass(frozen=True)
class FlightChange:
    """One schedule move parsed out of an event description."""

    kind: str
    route: str = ""
    sector: str = ""                    # Domestic | International | Unknown
    original_dep: datetime | None = None
    revised_dep: datetime | None = None
    shift_minutes: float | None = None  # signed: + = later, - = earlier
    detail: str = ""

    @property
    def is_qualifying_by_nature(self) -> bool:
        """Cancellations and flight/date transfers entitle a reissue outright —
        there is no 'small' version of losing your flight."""
        return self.kind in (KIND_CANCEL, KIND_TRANSFER)


@dataclass
class ReissueCase:
    """One reissue, with the evidence for or against it."""

    pnr: str
    reissue_at: datetime | None
    reissued_by: str = ""
    reissued_by_login: str = ""
    reissued_by_dept: str = ""
    verdict: str = NEEDS_REVIEW
    reason: str = ""
    sector: str = "Unknown"
    route: str = ""
    governing_change: FlightChange | None = None
    cumulative_shift: float | None = None
    change_at: datetime | None = None
    changed_by: str = ""
    changed_by_login: str = ""
    days_to_reissue: float | None = None
    same_agent: bool = False            # schedule mover == reissuer
    minutes_change_to_reissue: float | None = None
    informed_by: str = ""
    unclassified_types: tuple = ()
    changes: tuple = ()


def sector_of_route(route: str, cfg: AuthConfig | None = None) -> str:
    cfg = cfg or AuthConfig()
    parts = _route_parts(route)
    if len(parts) != 2:
        return "Unknown"
    return "Domestic" if all(p in cfg.bd_airports for p in parts) else "International"


def _route_parts(route: str) -> list[str]:
    r = re.sub(r"[^A-Z]", "", str(route or "").upper())
    if len(r) == 6:
        return [r[:3], r[3:]]
    parts = [p for p in re.split(r"[-/ ]+", str(route or "").upper()) if p]
    return parts if len(parts) == 2 else []


def _dt(date_s: str, time_s: str) -> datetime | None:
    for fmt in ("%d/%m/%Y %H:%M",):
        try:
            return datetime.strptime(f"{date_s.strip()} {time_s.strip()}", fmt)
        except ValueError:
            continue
    return None


def parse_time_revisions(description: str, cfg: AuthConfig | None = None) -> list[FlightChange]:
    """Every 'A ==> B' departure move in one description cell.

    A single cell can chain several hops (the investigation file has a PNR moved
    20:45 -> 21:35 -> 21:25 in one comment), so each is returned in order.
    """
    cfg = cfg or AuthConfig()
    out: list[FlightChange] = []
    for m in _TIME_RE.finditer(description or ""):
        route = m.group("route").upper()
        o = _dt(m.group("d1"), m.group("t1"))
        n = _dt(m.group("d2"), m.group("t2"))
        if not o or not n:
            continue
        pretty = f"{route[:3]}-{route[3:]}"
        out.append(FlightChange(
            kind=KIND_TIME, route=pretty, sector=sector_of_route(pretty, cfg),
            original_dep=o, revised_dep=n,
            shift_minutes=(n - o).total_seconds() / 60.0,
            detail=m.group(0).strip()))
    return out


def parse_flight_transfer(description: str) -> FlightChange | None:
    """'Flight:BS 381 01/10/2025->BS 381 03/10/2025' — moved to another flight/date."""
    m = _TRANSFER_RE.search(description or "")
    if not m:
        return None
    o = _dt(m.group("d1"), "00:00")
    n = _dt(m.group("d2"), "00:00")
    shift = (n - o).total_seconds() / 60.0 if o and n else None
    f1 = re.sub(r"\s+", "", m.group("f1")).upper()
    f2 = re.sub(r"\s+", "", m.group("f2")).upper()
    return FlightChange(kind=KIND_TRANSFER, original_dep=o, revised_dep=n,
                        shift_minutes=shift,
                        detail=f"{f1} {m.group('d1')} -> {f2} {m.group('d2')}")


def changes_in_event(event, cfg: AuthConfig | None = None) -> list[FlightChange]:
    """Schedule moves carried by one history event, from any known grammar."""
    cfg = cfg or AuthConfig()
    desc = getattr(event, "raw_description", "") or ""
    etype = getattr(event, "event_type", "") or ""
    found = parse_time_revisions(desc, cfg)
    if found:
        return found
    transfer = parse_flight_transfer(desc)
    if transfer is not None and (_TRANSFER_HINT_RE.search(desc) or _CANCEL_RE.search(desc)
                                 or "transfer" in etype.lower()):
        return [transfer]
    if _CANCEL_RE.search(desc):
        return [FlightChange(kind=KIND_CANCEL, detail=desc.strip()[:160])]
    if transfer is not None:
        return [transfer]
    if _TYPE_TIME_RE.search(etype):
        # the Type says the schedule moved but no grammar matched — surface it
        # rather than dropping it, so the missing grammar gets noticed
        return [FlightChange(kind=KIND_TIME, detail=desc.strip()[:160])]
    return []


def reissue_marker(event) -> dict | None:
    """The reissue evidence on one event, or None.

    Two forms, both meaning "this ticket was exchanged":
      * the coupon transition Issued -> Exchanged, already flagged upstream as
        ``is_reissue``; authoritative but carries no route;
      * "Addition of new ticket(s) to be exchanged : flight … (DAC -> SHJ)",
        which is how the great majority appear and which DOES carry the route and
        the new departure.
    Returns the flight/route/new-departure when the text form matched, so the
    caller can fill in a sector the schedule change may not have supplied.
    """
    desc = getattr(event, "raw_description", "") or ""
    m = _EXCHANGE_ADD_RE.search(desc)
    if m:
        route = f"{m.group('orig').upper()}-{m.group('dest').upper()}"
        return {
            "flight": re.sub(r"\s+", "", m.group("flight")).upper(),
            "route": route,
            "new_departure": _dt(m.group("date"), m.group("time")),
            "source": "exchange-addition",
        }
    if getattr(event, "is_reissue", False):
        return {"flight": "", "route": "", "new_departure": None,
                "source": "coupon I->E"}
    if _EXCHANGE_TYPE_RE.search(getattr(event, "event_type", "") or ""):
        return {"flight": "", "route": "", "new_departure": None,
                "source": "event type"}
    return None


def is_reissue_event(event) -> bool:
    return reissue_marker(event) is not None


def _agent_bits(event) -> tuple[str, str, str]:
    agent = getattr(event, "agent", None)
    name = getattr(agent, "name", "") or getattr(agent, "display_name", "") or ""
    login = getattr(agent, "user_id", "") or ""
    dept = getattr(agent, "department", "") or ""
    return str(name), str(login), str(dept)


def authenticate_pnr(pnr: str, events, cfg: AuthConfig | None = None) -> list[ReissueCase]:
    """Judge every reissue on one PNR against its schedule-change history."""
    cfg = cfg or AuthConfig()
    ordered = sorted(
        [e for e in events if getattr(e, "timestamp", None) is not None],
        key=lambda e: e.timestamp)
    undated = [e for e in events if getattr(e, "timestamp", None) is None]

    change_events: list[tuple] = []      # (timestamp, event, FlightChange)
    unclassified: list[str] = []
    for e in ordered:
        found = changes_in_event(e, cfg)
        if found:
            change_events.extend((e.timestamp, e, c) for c in found)
        elif not is_reissue_event(e):
            t = (getattr(e, "event_type", "") or "").strip()
            if t:
                unclassified.append(t)

    cases: list[ReissueCase] = []
    seen_reissues: set = set()
    for e in ordered + undated:
        marker = reissue_marker(e)
        if marker is None:
            continue
        t_r = getattr(e, "timestamp", None)
        # One exchange transaction is logged once per ticket/coupon, so the same
        # reissue appears several times with identical text. Count the transaction
        # once, or a two-coupon booking looks like two reissues.
        fingerprint = (t_r, marker.get("flight", ""), marker.get("route", ""))
        if fingerprint in seen_reissues:
            continue
        seen_reissues.add(fingerprint)
        name, login, dept = _agent_bits(e)
        case = ReissueCase(pnr=pnr, reissue_at=t_r, reissued_by=name,
                           reissued_by_login=login, reissued_by_dept=dept,
                           unclassified_types=tuple(sorted(set(unclassified))))
        prior = [(ts, ev, c) for ts, ev, c in change_events
                 if t_r is None or ts <= t_r]
        case.changes = tuple(c for _ts, _ev, c in prior)
        if not prior:
            case.verdict = NEEDS_REVIEW if unclassified else NO_CHANGE_FOUND
            case.reason = ("no recognised schedule change before the reissue; "
                           f"unclassified event types present: "
                           f"{', '.join(case.unclassified_types)}"
                           if unclassified else
                           "no schedule change found before the reissue")
            cases.append(case)
            continue

        gov_ts, gov_ev, gov = prior[-1]
        case.governing_change = gov
        case.change_at = gov_ts
        case.changed_by, case.changed_by_login, _d = _agent_bits(gov_ev)
        # a transfer/cancellation carries no route, but the reissue marker usually
        # does — so the sector (and therefore the threshold) is still known
        case.route = gov.route or next(
            (c.route for _t, _e, c in reversed(prior) if c.route), "") \
            or marker.get("route", "")
        case.sector = (gov.sector if gov.sector
                       else sector_of_route(case.route, cfg) if case.route
                       else "Unknown")

        timed = [c for _t, _e, c in prior
                 if c.original_dep and c.revised_dep and c.kind == KIND_TIME]
        if timed:
            case.cumulative_shift = (
                max(c.revised_dep for c in timed)
                - min(c.original_dep for c in timed)).total_seconds() / 60.0
        outright = any(c.is_qualifying_by_nature for _t, _e, c in prior)

        if t_r is not None and gov_ts is not None:
            gap = t_r - gov_ts
            case.days_to_reissue = gap.total_seconds() / 86400.0
            case.minutes_change_to_reissue = gap.total_seconds() / 60.0
        case.same_agent = bool(login and login == case.changed_by_login)

        basis = (case.cumulative_shift if cfg.basis == "cumulative"
                 else gov.shift_minutes)
        if outright:
            qualifies, why = True, "flight cancelled or transferred to another flight/date"
        else:
            qualifies = cfg.qualifies(basis, case.sector)
            why = (f"{abs(basis):.0f} min shift vs {cfg.threshold_for(case.sector)} min "
                   f"{case.sector.lower()} threshold" if basis is not None
                   else "no measurable shift")

        if not qualifies:
            case.verdict = BELOW_THRESHOLD
            case.reason = why
        elif (case.days_to_reissue is not None
              and case.days_to_reissue > cfg.reissue_window_days):
            case.verdict = OUTSIDE_WINDOW
            case.reason = (f"{why}; reissued {case.days_to_reissue:.0f} days later, "
                           f"window is {cfg.reissue_window_days} days")
        else:
            case.verdict = JUSTIFIED
            case.reason = why
        cases.append(case)
    return cases


def authenticate(events_by_pnr: dict, cfg: AuthConfig | None = None) -> list[ReissueCase]:
    """Judge many PNRs. `events_by_pnr` maps PNR -> its history events."""
    cfg = cfg or AuthConfig()
    out: list[ReissueCase] = []
    for pnr, events in events_by_pnr.items():
        out.extend(authenticate_pnr(pnr, events, cfg))
    return out


def suspicious_flags(case: ReissueCase, cfg: AuthConfig | None = None,
                     quick_minutes: int = 15) -> list[str]:
    """Human-checkable observations — never accusations."""
    cfg = cfg or AuthConfig()
    out: list[str] = []
    if case.same_agent:
        out.append("SAME AGENT moved the schedule and did the reissue "
                   "(segregation of duties)")
    if (case.minutes_change_to_reissue is not None
            and 0 <= case.minutes_change_to_reissue <= quick_minutes
            and case.verdict != JUSTIFIED):
        out.append(f"reissued {case.minutes_change_to_reissue:.0f} min after the "
                   "schedule change")
    if case.verdict == BELOW_THRESHOLD:
        out.append("free reissue on a shift below the entitlement threshold")
    if case.verdict == OUTSIDE_WINDOW:
        out.append("free reissue after the entitlement window closed")
    if case.reissue_at is not None and case.reissue_at.hour in range(0, 6):
        out.append(f"reissued at {case.reissue_at:%H:%M} (off hours)")
    return out
