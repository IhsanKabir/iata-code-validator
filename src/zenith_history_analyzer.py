"""Audit reports over Zenith ModificationHistory events.

The analyzer takes a stream of `HistoryEvent` records (from
`zenith_history_parser`) and produces structured `AuditReport`s that
feed both the on-screen renderer and the multi-sheet Excel writer.

Each audit answers one question the user asked:

  1. ClassDowngradeAudit          — which PNRs got moved to a lower fare tier
  2. DowngradeLeaderboard         — which agent does downgrades most often
  3. GClassIssuanceAudit          — every G-class ticket event, by agent
  4. AgentActivityAudit           — per-agent action counts by event type
  5. RevenueMgmtAudit             — capacity changes per class per flight per agent
  6. SuspiciousActivityAudit      — heuristic flags worth a human look

Phase-2 enrichment (PNR → customer name) is left as an opt-in step the
GUI calls separately, so a cold run stays fast and the audit object
shape is stable whether or not customer names are filled in.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from .zenith_history_parser import (
    HistoryEvent,
    RBD_FARE_RANK,
    downgrade_severity,
    is_downgrade,
)
from .zenith_loads_index import (
    VERDICT_UNKNOWN,
    LoadLookup,
    load_verdict,
)
from .zenith_pnr_client import PNRDetails, PNRSegment

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-audit result rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassTrajectory:
    """One PNR's class-tier path over time."""

    pnr: str
    passenger: str
    flight_number: str
    flight_date: str
    classes_seen: tuple[str, ...]         # ('Y', 'Y', 'T', 'G')
    starting_class: str
    ending_class: str
    total_downgrade_severity: int          # 0 if no downgrade
    downgrade_steps: int                   # how many separate down-moves
    last_changed_by: str                   # user_id of agent that last touched it
    last_changed_at: datetime | None
    customer_name: str = ""                # Filled by Phase-2 enrichment


@dataclass(frozen=True)
class DowngradeLeader:
    agent_user_id: str
    agent_display_name: str
    agent_department: str
    downgrade_event_count: int
    total_severity: int
    distinct_pnrs: int


@dataclass(frozen=True)
class GClassEvent:
    timestamp: datetime | None
    agent_user_id: str
    agent_display_name: str
    agent_department: str
    pnr: str
    passenger: str
    flight_number: str
    flight_date: str
    event_type: str
    ticket_number: str
    customer_name: str = ""                # Phase-2 enrichment


@dataclass(frozen=True)
class AgentActivityRow:
    agent_user_id: str
    agent_display_name: str
    agent_department: str
    total_events: int
    by_type: dict[str, int]                # event_type → count


@dataclass(frozen=True)
class RevenueMgmtChange:
    timestamp: datetime | None
    agent_user_id: str
    agent_display_name: str
    flight_number: str
    flight_date: str
    route: str                              # e.g. 'CGP-DXB'
    booking_class: str
    seats_before: int
    seats_after: int
    delta: int                              # negative = capacity closed


@dataclass(frozen=True)
class SuspiciousFlag:
    timestamp: datetime | None
    agent_user_id: str
    pnr: str
    passenger: str
    flight_number: str
    event_type: str
    reason: str                             # e.g. 'Off-hours downgrade (02:13)'
    severity: str                           # 'low' / 'medium' / 'high'


@dataclass(frozen=True)
class PNRRouteRow:
    """One PNR's full booking — used for the 'correct route' audit.

    Replaces the per-leg row that the History file gives us with the
    PNR's complete route plus per-segment status, so the auditor can
    see "DAC-SIN-DAC booked, only DAC-SIN flown, SIN-DAC refunded".
    """

    pnr_code: str
    customer_name: str
    traveler_surname: str
    phone: str
    pnr_status: str
    pax_count: int
    booked_route: str        # 'DAC-SIN-DAC'
    flown_route: str         # 'DAC-SIN' if return refunded; '' if all voided
    segment_count: int
    flown_count: int
    refunded_count: int
    voided_count: int
    other_status_count: int
    total_amount: str        # raw '99,325 BDT'
    currency: str
    payment_method: str
    # Compact per-segment summary for human scanning. Each entry shaped
    # like 'DAC-SIN/S/Flown/29,884 BDT'.
    segments_summary: str


@dataclass(frozen=True)
class DowngradeJustification:
    """A downgrade event + the flight's load% at the time → verdict.

    Only emitted when a Flight Loads Excel is supplied to the audit.
    `load_pct = None` means we had loads data but couldn't match this
    flight (so the verdict is UNKNOWN).
    """

    timestamp: datetime | None
    agent_user_id: str
    agent_display_name: str
    pnr: str
    passenger: str
    flight_number: str
    flight_date: str
    route: str                              # 'DAC-DXB'
    old_class: str
    new_class: str
    severity: int                           # number of fare tiers dropped
    load_pct: float | None                  # None = no matching load row
    seats_capacity: int | None
    inventory_status: str
    verdict: str                            # QUESTIONABLE / SITUATIONAL / JUSTIFIED / UNKNOWN


# ---------------------------------------------------------------------------
# Combined report container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryAuditReport:
    """Bundle of every audit + cover metadata for downstream rendering."""

    # Cover
    file_count: int
    event_count: int
    date_range: tuple[datetime | None, datetime | None]
    top_agents: list[tuple[str, int]]
    top_rbds: list[tuple[str, int]]

    # Audits
    class_trajectories: list[ClassTrajectory]
    downgrade_leaders: list[DowngradeLeader]
    g_class_events: list[GClassEvent]
    agent_activity: list[AgentActivityRow]
    revenue_mgmt_changes: list[RevenueMgmtChange]
    suspicious_flags: list[SuspiciousFlag]
    # Empty unless a Flight Loads Excel was provided to run_history_audit.
    downgrade_justifications: list[DowngradeJustification] = field(default_factory=list)

    # Empty unless PNR enrichment was run after the initial audit.
    # Use `apply_pnr_enrichment` to populate.
    pnr_routes: list[PNRRouteRow] = field(default_factory=list)

    # Pass-throughs that the Excel writer wants for the Raw Events sheet.
    # We keep this last because it can be huge.
    raw_events: list[HistoryEvent] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trajectory_summary(classes: Sequence[str]) -> tuple[int, int]:
    """Total downgrade severity + number of separate downgrade steps."""
    seen = [c for c in classes if c in RBD_FARE_RANK]
    severity = 0
    steps = 0
    for prev, curr in zip(seen, seen[1:]):
        if is_downgrade(prev, curr):
            steps += 1
            severity += downgrade_severity(prev, curr)
    return severity, steps


# ---------------------------------------------------------------------------
# Individual audits
# ---------------------------------------------------------------------------


def audit_class_trajectories(events: Iterable[HistoryEvent]) -> list[ClassTrajectory]:
    """Group events by PNR and track each PNR's class-tier path.

    Only PNRs with at least one observed class are returned; we don't
    invent trajectories from blank data.
    """
    by_pnr: dict[str, list[HistoryEvent]] = defaultdict(list)
    for e in events:
        if e.pnr and e.rbd_class:
            by_pnr[e.pnr].append(e)
    out: list[ClassTrajectory] = []
    for pnr, evs in by_pnr.items():
        evs_sorted = sorted(
            evs, key=lambda e: e.timestamp or datetime.min,
        )
        classes = tuple(e.rbd_class for e in evs_sorted)
        severity, steps = _trajectory_summary(classes)
        last = evs_sorted[-1]
        out.append(ClassTrajectory(
            pnr=pnr,
            passenger=last.passenger,
            flight_number=last.flight.flight_number,
            flight_date=last.flight.flight_date,
            classes_seen=classes,
            starting_class=classes[0],
            ending_class=classes[-1],
            total_downgrade_severity=severity,
            downgrade_steps=steps,
            last_changed_by=last.agent.user_id,
            last_changed_at=last.timestamp,
        ))
    # Most severely downgraded first
    out.sort(key=lambda t: t.total_downgrade_severity, reverse=True)
    return out


def audit_downgrade_leaderboard(
    trajectories: Iterable[ClassTrajectory],
    events: Iterable[HistoryEvent],
) -> list[DowngradeLeader]:
    """For each agent, count how many downgrades they performed.

    We attribute a downgrade to whichever agent's modification produced
    the lower-class observation — so we walk per-PNR events again here
    rather than re-deriving from trajectories.
    """
    agent_meta: dict[str, tuple[str, str]] = {}      # user_id → (name, dept)
    by_pnr: dict[str, list[HistoryEvent]] = defaultdict(list)
    for e in events:
        if e.pnr and e.rbd_class:
            by_pnr[e.pnr].append(e)
        if e.agent.user_id and e.agent.user_id not in agent_meta:
            agent_meta[e.agent.user_id] = (
                e.agent.display_name, e.agent.department,
            )

    counts: Counter[str] = Counter()
    severity_total: Counter[str] = Counter()
    pnrs_seen: dict[str, set[str]] = defaultdict(set)

    for pnr, evs in by_pnr.items():
        evs_sorted = sorted(
            evs, key=lambda e: e.timestamp or datetime.min,
        )
        for prev, curr in zip(evs_sorted, evs_sorted[1:]):
            if not is_downgrade(prev.rbd_class, curr.rbd_class):
                continue
            uid = curr.agent.user_id or "(unknown)"
            counts[uid] += 1
            severity_total[uid] += downgrade_severity(
                prev.rbd_class, curr.rbd_class,
            )
            pnrs_seen[uid].add(pnr)

    out: list[DowngradeLeader] = []
    for uid, count in counts.items():
        name, dept = agent_meta.get(uid, ("(unknown)", ""))
        out.append(DowngradeLeader(
            agent_user_id=uid,
            agent_display_name=name,
            agent_department=dept,
            downgrade_event_count=count,
            total_severity=severity_total[uid],
            distinct_pnrs=len(pnrs_seen[uid]),
        ))
    out.sort(key=lambda d: (d.total_severity, d.downgrade_event_count), reverse=True)
    return out


def audit_g_class_issuance(events: Iterable[HistoryEvent]) -> list[GClassEvent]:
    """Every event that touched a G-class coupon."""
    out: list[GClassEvent] = []
    for e in events:
        if e.rbd_class != "G":
            continue
        out.append(GClassEvent(
            timestamp=e.timestamp,
            agent_user_id=e.agent.user_id,
            agent_display_name=e.agent.display_name,
            agent_department=e.agent.department,
            pnr=e.pnr,
            passenger=e.passenger,
            flight_number=e.flight.flight_number,
            flight_date=e.flight.flight_date,
            event_type=e.event_type,
            ticket_number=e.ticket_number,
        ))
    out.sort(key=lambda g: g.timestamp or datetime.min, reverse=True)
    return out


def audit_agent_activity(events: Iterable[HistoryEvent]) -> list[AgentActivityRow]:
    """Per-agent breakdown of action counts by event type."""
    agent_meta: dict[str, tuple[str, str]] = {}
    by_agent: dict[str, Counter[str]] = defaultdict(Counter)
    for e in events:
        uid = e.agent.user_id or "(unknown)"
        by_agent[uid][e.event_type] += 1
        if uid not in agent_meta:
            agent_meta[uid] = (e.agent.display_name, e.agent.department)
    out: list[AgentActivityRow] = []
    for uid, counter in by_agent.items():
        name, dept = agent_meta[uid]
        out.append(AgentActivityRow(
            agent_user_id=uid,
            agent_display_name=name,
            agent_department=dept,
            total_events=sum(counter.values()),
            by_type=dict(counter),
        ))
    out.sort(key=lambda a: a.total_events, reverse=True)
    return out


def audit_revenue_mgmt(events: Iterable[HistoryEvent]) -> list[RevenueMgmtChange]:
    """Every capacity-change event with structured before/after counts."""
    out: list[RevenueMgmtChange] = []
    for e in events:
        if not e.capacity_class or e.capacity_before is None or e.capacity_after is None:
            continue
        route = ""
        if e.flight.origin and e.flight.destination:
            route = f"{e.flight.origin}-{e.flight.destination}"
        out.append(RevenueMgmtChange(
            timestamp=e.timestamp,
            agent_user_id=e.agent.user_id,
            agent_display_name=e.agent.display_name,
            flight_number=e.flight.flight_number,
            flight_date=e.flight.flight_date,
            route=route,
            booking_class=e.capacity_class,
            seats_before=e.capacity_before,
            seats_after=e.capacity_after,
            delta=e.capacity_after - e.capacity_before,
        ))
    # Biggest closures first
    out.sort(key=lambda r: r.delta)
    return out


# ---------------------------------------------------------------------------
# Suspicious-activity heuristics
# ---------------------------------------------------------------------------

# Off-hours = before 6 AM or after 11 PM local time on the modification.
_OFF_HOURS_START = 23
_OFF_HOURS_END = 6

# Burst threshold — N+ downgrades by the same agent in the same day
# is worth flagging, regardless of legitimacy.
_DOWNGRADE_BURST_PER_DAY = 20


def audit_suspicious(
    events: Iterable[HistoryEvent],
    trajectories: Iterable[ClassTrajectory],
) -> list[SuspiciousFlag]:
    """Heuristic flags worth a human pass.

    These are intentionally conservative — false positives are cheap
    here, false negatives mean a bad pattern goes unnoticed.
    """
    flags: list[SuspiciousFlag] = []
    event_list = list(events)
    trajectories = list(trajectories)

    # 1. Off-hours downgrades — operator activity outside normal shifts.
    by_pnr: dict[str, list[HistoryEvent]] = defaultdict(list)
    for e in event_list:
        if e.pnr and e.rbd_class:
            by_pnr[e.pnr].append(e)
    for pnr, evs in by_pnr.items():
        evs_sorted = sorted(evs, key=lambda e: e.timestamp or datetime.min)
        for prev, curr in zip(evs_sorted, evs_sorted[1:]):
            if not is_downgrade(prev.rbd_class, curr.rbd_class):
                continue
            ts = curr.timestamp
            if ts is None:
                continue
            hour = ts.hour
            off_hours = hour >= _OFF_HOURS_START or hour < _OFF_HOURS_END
            if off_hours:
                flags.append(SuspiciousFlag(
                    timestamp=ts,
                    agent_user_id=curr.agent.user_id,
                    pnr=pnr,
                    passenger=curr.passenger,
                    flight_number=curr.flight.flight_number,
                    event_type=curr.event_type,
                    reason=f"Off-hours downgrade at {ts.strftime('%H:%M')}",
                    severity="medium",
                ))

    # 2. Downgrade bursts — one agent doing many downgrades in one day.
    bursts: Counter[tuple[str, str]] = Counter()    # (agent_uid, YYYY-MM-DD)
    burst_examples: dict[tuple[str, str], HistoryEvent] = {}
    for pnr, evs in by_pnr.items():
        evs_sorted = sorted(evs, key=lambda e: e.timestamp or datetime.min)
        for prev, curr in zip(evs_sorted, evs_sorted[1:]):
            if not is_downgrade(prev.rbd_class, curr.rbd_class):
                continue
            if curr.timestamp is None or not curr.agent.user_id:
                continue
            key = (curr.agent.user_id, curr.timestamp.strftime("%Y-%m-%d"))
            bursts[key] += 1
            burst_examples.setdefault(key, curr)
    for (uid, day), count in bursts.items():
        if count >= _DOWNGRADE_BURST_PER_DAY:
            ev = burst_examples[(uid, day)]
            flags.append(SuspiciousFlag(
                timestamp=ev.timestamp,
                agent_user_id=uid,
                pnr="(multiple)",
                passenger="(multiple)",
                flight_number=ev.flight.flight_number,
                event_type=ev.event_type,
                reason=f"Burst: {count} downgrades on {day}",
                severity="high",
            ))

    # 3. Severe drops on a single ticket (≥ 6 tiers in one move).
    for traj in trajectories:
        for prev, curr in zip(traj.classes_seen, traj.classes_seen[1:]):
            if downgrade_severity(prev, curr) >= 6:
                flags.append(SuspiciousFlag(
                    timestamp=traj.last_changed_at,
                    agent_user_id=traj.last_changed_by,
                    pnr=traj.pnr,
                    passenger=traj.passenger,
                    flight_number=traj.flight_number,
                    event_type="Class downgrade",
                    reason=f"Steep drop: {prev} -> {curr}",
                    severity="high",
                ))

    flags.sort(
        key=lambda f: (
            {"high": 0, "medium": 1, "low": 2}.get(f.severity, 3),
            f.timestamp or datetime.min,
        )
    )
    return flags


def build_pnr_routes(
    pnr_details: dict[str, PNRDetails],
) -> list[PNRRouteRow]:
    """Roll up enriched PNR data into route-audit rows.

    One row per PNR. Sorted so PNRs with the most refund/void activity
    surface first (those are the ones whose route reporting from the
    raw history is most misleading).
    """
    from collections import Counter

    not_flown = {"voided", "refunded", "cancelled", "canceled", "no show"}
    rows: list[PNRRouteRow] = []
    for code, d in pnr_details.items():
        statuses = Counter(s.coupon_status for s in d.segments)
        flown = sum(
            c for st, c in statuses.items()
            if st.lower() not in not_flown and st != ""
        )
        refunded = sum(c for st, c in statuses.items() if st.lower() == "refunded")
        voided = sum(c for st, c in statuses.items() if st.lower() == "voided")
        cancelled_other = sum(
            c for st, c in statuses.items()
            if st.lower() in {"cancelled", "canceled", "no show"}
        )
        other_status_count = cancelled_other
        # Compact summary: 'DAC-SIN/S/Flown/29,884 BDT ; SIN-DAC/M/Refunded/69,441 BDT'
        parts = [
            f"{s.leg_route or '?'}/{s.rbd_class or '?'}/"
            f"{s.coupon_status or '?'}/{s.price_ttc or '?'}"
            for s in d.segments
        ]
        rows.append(PNRRouteRow(
            pnr_code=code,
            customer_name=d.customer_name,
            traveler_surname=d.traveler_surname,
            phone=d.phone,
            pnr_status=d.pnr_status,
            pax_count=d.pax_count,
            booked_route=d.booked_route,
            flown_route=d.flown_route,
            segment_count=len(d.segments),
            flown_count=flown,
            refunded_count=refunded,
            voided_count=voided,
            other_status_count=other_status_count,
            total_amount=d.total_amount,
            currency=d.currency,
            payment_method=d.payment_method,
            segments_summary=" ; ".join(parts),
        ))
    # Sort: most-disrupted PNRs first (refunds + voids), then by segment count.
    rows.sort(
        key=lambda r: (-(r.refunded_count + r.voided_count), -r.segment_count),
    )
    return rows


def apply_pnr_enrichment(
    report: HistoryAuditReport,
    pnr_details: dict[str, PNRDetails],
) -> HistoryAuditReport:
    """Return a new report with PNR-derived customer names attached.

    Walks every existing audit row that carries a `customer_name` slot
    (G-class events, class trajectories) and fills it from the enriched
    PNR data when available. Adds the PNR Routes audit.
    """
    # Index PNR → customer (the agency in CustomerName) + surname.
    def cust_for(pnr: str) -> str:
        d = pnr_details.get(pnr)
        if d is None:
            return ""
        # Prefer customer_name; fall back to traveler_surname for direct bookings.
        return d.customer_name or d.traveler_surname

    enriched_trajs = [
        ClassTrajectory(
            pnr=t.pnr, passenger=t.passenger,
            flight_number=t.flight_number, flight_date=t.flight_date,
            classes_seen=t.classes_seen,
            starting_class=t.starting_class, ending_class=t.ending_class,
            total_downgrade_severity=t.total_downgrade_severity,
            downgrade_steps=t.downgrade_steps,
            last_changed_by=t.last_changed_by,
            last_changed_at=t.last_changed_at,
            customer_name=cust_for(t.pnr) or t.customer_name,
        )
        for t in report.class_trajectories
    ]
    enriched_g = [
        GClassEvent(
            timestamp=g.timestamp,
            agent_user_id=g.agent_user_id,
            agent_display_name=g.agent_display_name,
            agent_department=g.agent_department,
            pnr=g.pnr, passenger=g.passenger,
            flight_number=g.flight_number, flight_date=g.flight_date,
            event_type=g.event_type, ticket_number=g.ticket_number,
            customer_name=cust_for(g.pnr) or g.customer_name,
        )
        for g in report.g_class_events
    ]
    routes = build_pnr_routes(pnr_details)

    return HistoryAuditReport(
        file_count=report.file_count,
        event_count=report.event_count,
        date_range=report.date_range,
        top_agents=report.top_agents,
        top_rbds=report.top_rbds,
        class_trajectories=enriched_trajs,
        downgrade_leaders=report.downgrade_leaders,
        g_class_events=enriched_g,
        agent_activity=report.agent_activity,
        revenue_mgmt_changes=report.revenue_mgmt_changes,
        suspicious_flags=report.suspicious_flags,
        downgrade_justifications=report.downgrade_justifications,
        pnr_routes=routes,
        raw_events=report.raw_events,
    )


def audit_downgrade_justification(
    events: Iterable[HistoryEvent],
    load_lookup: LoadLookup,
    *,
    high_threshold: float | None = None,
    low_threshold: float | None = None,
) -> list[DowngradeJustification]:
    """For every class downgrade, join the flight's load% + verdict.

    A downgrade on a 95%-full flight is hard to justify on revenue
    grounds; on a 30%-full flight it's a reasonable load-management
    move. This audit surfaces the imbalance.
    """
    out: list[DowngradeJustification] = []
    by_pnr: dict[str, list[HistoryEvent]] = defaultdict(list)
    for e in events:
        if e.pnr and e.rbd_class:
            by_pnr[e.pnr].append(e)

    for pnr, evs in by_pnr.items():
        evs_sorted = sorted(evs, key=lambda e: e.timestamp or datetime.min)
        for prev, curr in zip(evs_sorted, evs_sorted[1:]):
            if not is_downgrade(prev.rbd_class, curr.rbd_class):
                continue
            entry = load_lookup.find(
                curr.flight.flight_number,
                curr.flight.flight_date,
                curr.flight.origin,
                curr.flight.destination,
            )
            load_pct = entry.load_pct if entry else None
            verdict_kwargs = {}
            if high_threshold is not None:
                verdict_kwargs["high_threshold"] = high_threshold
            if low_threshold is not None:
                verdict_kwargs["low_threshold"] = low_threshold
            verdict = load_verdict(load_pct, **verdict_kwargs)
            route = (
                f"{curr.flight.origin}-{curr.flight.destination}"
                if curr.flight.origin and curr.flight.destination else ""
            )
            out.append(DowngradeJustification(
                timestamp=curr.timestamp,
                agent_user_id=curr.agent.user_id,
                agent_display_name=curr.agent.display_name,
                pnr=pnr,
                passenger=curr.passenger,
                flight_number=curr.flight.flight_number,
                flight_date=curr.flight.flight_date,
                route=route,
                old_class=prev.rbd_class,
                new_class=curr.rbd_class,
                severity=downgrade_severity(prev.rbd_class, curr.rbd_class),
                load_pct=load_pct,
                seats_capacity=entry.seats_capacity if entry else None,
                inventory_status=entry.inventory_status if entry else "",
                verdict=verdict,
            ))
    # Most questionable first (high load + steep severity).
    verdict_rank = {
        "QUESTIONABLE": 0, "SITUATIONAL": 1,
        "JUSTIFIED": 2, "UNKNOWN": 3,
    }
    out.sort(key=lambda d: (
        verdict_rank.get(d.verdict, 9),
        -(d.load_pct if d.load_pct is not None else -1),
        -d.severity,
    ))
    return out


# ---------------------------------------------------------------------------
# Top-level audit composer
# ---------------------------------------------------------------------------


def run_history_audit(
    events: Iterable[HistoryEvent],
    *,
    include_raw: bool = True,
    load_lookup: LoadLookup | None = None,
    high_threshold: float | None = None,
    low_threshold: float | None = None,
) -> HistoryAuditReport:
    """Run every audit in one pass and return the bundled report."""
    events = list(events)
    if not events:
        log.warning("run_history_audit called with zero events")
        return HistoryAuditReport(
            file_count=0,
            event_count=0,
            date_range=(None, None),
            top_agents=[],
            top_rbds=[],
            class_trajectories=[],
            downgrade_leaders=[],
            g_class_events=[],
            agent_activity=[],
            revenue_mgmt_changes=[],
            suspicious_flags=[],
            raw_events=[],
        )

    files = {e.source_file for e in events}
    timestamps = [e.timestamp for e in events if e.timestamp is not None]
    date_range = (min(timestamps), max(timestamps)) if timestamps else (None, None)
    top_agents = Counter(e.agent.user_id for e in events if e.agent.user_id).most_common(10)
    top_rbds = Counter(e.rbd_class for e in events if e.rbd_class).most_common(10)

    trajectories = audit_class_trajectories(events)
    leaders = audit_downgrade_leaderboard(trajectories, events)
    g_events = audit_g_class_issuance(events)
    activity = audit_agent_activity(events)
    rm_changes = audit_revenue_mgmt(events)
    flags = audit_suspicious(events, trajectories)
    justifications: list[DowngradeJustification] = []
    if load_lookup is not None:
        justifications = audit_downgrade_justification(
            events, load_lookup,
            high_threshold=high_threshold,
            low_threshold=low_threshold,
        )
        log.info(
            "Downgrade justification: %d rows; verdicts=%s",
            len(justifications),
            {v: sum(1 for d in justifications if d.verdict == v)
             for v in ("QUESTIONABLE", "SITUATIONAL", "JUSTIFIED", "UNKNOWN")},
        )

    return HistoryAuditReport(
        file_count=len(files),
        event_count=len(events),
        date_range=date_range,
        top_agents=top_agents,
        top_rbds=top_rbds,
        class_trajectories=trajectories,
        downgrade_leaders=leaders,
        g_class_events=g_events,
        agent_activity=activity,
        revenue_mgmt_changes=rm_changes,
        suspicious_flags=flags,
        downgrade_justifications=justifications,
        raw_events=events if include_raw else [],
    )
