"""PNR-centric misuse audit re-pivoted from the flight ModificationHistory corpus.

The Flight History Analyzer already downloads per-flight ModificationHistory files and
parses them into `zenith_history_parser.HistoryEvent` rows — each carrying PNR, agent
(user_id + department), ticket number, coupon-status transition (Issued→Refunded/Voided),
RBD class, and timestamp. One flight file covers every PNR that touched that flight, so
re-pivoting that EXISTING corpus by PNR/ticket/agent yields ~85-90% of the misuse audit
at zero extra GDS load (vs scraping each PNR's dossier event tabs).

This module consumes a stream of `HistoryEvent`, classifies each event's action from its
coupon-status transition, runs STRUCTURAL detectors (no fragile free-text regexes), and
produces a composite **risk worklist** ranking the PNRs and agents most worth a human
look. Detectors exclude system/API logins. Flags are framed as *observations needing
review with evidence + a confidence*, not accusations.

Gaps this corpus can't see (payment transaction ids, contact/name changes) are deferred
to the Phase-2 per-PNR dossier scrape; this module is the cheap, offline, first pass.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from .zenith_history_parser import (
    HistoryEvent,
    downgrade_severity,
    is_downgrade,
)

# ---------------------------------------------------------------------------
# Tunable thresholds (module-level so the GUI can override; baseline-calibration
# replaces the count-based ones in a later pass — see plan).
# ---------------------------------------------------------------------------
OFF_HOURS_START = 23          # an event at/after 23:00 ...
OFF_HOURS_END = 6             # ... or before 06:00 is "off hours"
REPEATED_CHANGE_MIN = 3       # >= this many RBD/class changes on one ticket
REFUND_VOID_BURST_PER_DAY = 8  # >= this many refunds+voids by one agent in a day

_SEV_WEIGHT = {"low": 1, "medium": 2, "high": 4, "critical": 8}

# Coupon statuses, normalised.
_REFUNDED = "refunded"
_VOIDED = {"voided", "void", "cancelled", "canceled"}
_ISSUED = "issued"
_FLOWN = "flown"


# ---------------------------------------------------------------------------
# Result value objects (immutable)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PNRFlag:
    """One observation worth a human review — never an assertion of guilt."""

    detector: str                 # 'refund_of_flown' / 'self_refund_sod' / ...
    severity: str                 # low | medium | high | critical
    confidence: float             # 0..1 (structural detectors ~1.0)
    pnr: str
    ticket_number: str
    agent_user_id: str
    agent_department: str
    timestamp: datetime | None
    reason: str                   # observation ("Refund on a coupon that was Flown — verify involuntary")
    evidence: str                 # the triggering event(s), verbatim-ish


@dataclass(frozen=True)
class AgentActivityRow:
    agent_user_id: str
    agent_display_name: str
    department: str
    total_events: int
    issues: int
    reissues: int                 # RBD/class changes on issued tickets (reissue proxy)
    refunds: int
    voids: int
    downgrades: int
    off_hours: int
    distinct_pnrs: int


@dataclass(frozen=True)
class RiskRow:
    grain: str                    # 'pnr' | 'agent'
    entity: str
    score: float
    families: tuple[str, ...]     # distinct detectors that fired
    flag_count: int
    top_reasons: tuple[str, ...]


@dataclass(frozen=True)
class PNRMisuseReport:
    event_count: int
    pnr_count: int
    agent_count: int
    date_range: tuple[datetime | None, datetime | None]
    flags: tuple[PNRFlag, ...]
    agent_activity: tuple[AgentActivityRow, ...]
    risk_worklist: tuple[RiskRow, ...]   # PNR + agent grains, highest score first


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def classify_action(event: HistoryEvent) -> str:
    """Map one event to a coarse action from its coupon-status transition.

    Returns one of: refund | void | issue | flown | capacity | modify. Reissues are
    a cross-event pattern (an RBD change on an issued ticket) detected during grouping,
    not from a single row, so they are NOT classified here.
    """
    new = _norm(event.new_status)
    if new == _REFUNDED:
        return "refund"
    if new in _VOIDED:
        return "void"
    if new == _ISSUED:
        return "issue"
    if new == _FLOWN:
        return "flown"
    if event.capacity_class:
        return "capacity"
    return "modify"


def _is_off_hours(ts: datetime | None) -> bool:
    if ts is None:
        return False
    return ts.hour >= OFF_HOURS_START or ts.hour < OFF_HOURS_END


def _excluded(event: HistoryEvent, whitelist: set[str]) -> bool:
    """System/API logins and explicitly whitelisted user_ids don't generate flags."""
    a = event.agent
    return a.is_system or a.is_api or (a.user_id and a.user_id in whitelist)


def _evidence(event: HistoryEvent) -> str:
    when = event.timestamp.strftime("%d/%m/%Y %H:%M") if event.timestamp else event.raw_date
    return (f"{when} · {event.agent.user_id or '?'} · {event.event_type} · "
            f"{event.raw_description[:120]}").strip()


# ---------------------------------------------------------------------------
# Detectors (structural — no free-text regexes)
# ---------------------------------------------------------------------------
def _by_ticket(events: list[HistoryEvent]) -> dict[str, list[HistoryEvent]]:
    out: dict[str, list[HistoryEvent]] = defaultdict(list)
    for e in events:
        if e.ticket_number:
            out[e.ticket_number].append(e)
    for evs in out.values():
        evs.sort(key=lambda e: e.timestamp or datetime.min)
    return out


def detect_flags(events: Iterable[HistoryEvent], *, whitelist: set[str]) -> list[PNRFlag]:
    evs = [e for e in events if e.pnr]
    flags: list[PNRFlag] = []
    by_ticket = _by_ticket(evs)

    for ticket, tevs in by_ticket.items():
        actions = [classify_action(e) for e in tevs]
        statuses = [(_norm(e.old_status), _norm(e.new_status)) for e in tevs]
        flown_seen_idx = next(
            (i for i, (o, n) in enumerate(statuses) if _FLOWN in (o, n)), None)
        issuers = {e.agent.user_id for e, a in zip(tevs, actions)
                   if a == "issue" and e.agent.user_id}
        rbd_changes = 0

        for i, (e, act) in enumerate(zip(tevs, actions)):
            if _excluded(e, whitelist):
                continue

            # 1. Refund of a coupon that was Flown (critical, cross-event on ticket).
            if act == "refund" and flown_seen_idx is not None and i >= flown_seen_idx:
                flags.append(PNRFlag(
                    detector="refund_of_flown", severity="critical", confidence=0.9,
                    pnr=e.pnr, ticket_number=ticket, agent_user_id=e.agent.user_id,
                    agent_department=e.agent.department, timestamp=e.timestamp,
                    reason="Refund on a coupon that was Flown — verify it isn't an involuntary refund.",
                    evidence=_evidence(e)))

            # 2. Self-refund / segregation-of-duties: same agent issued AND refunded/voided.
            if act in ("refund", "void") and e.agent.user_id in issuers:
                flags.append(PNRFlag(
                    detector="self_refund_sod", severity="high", confidence=1.0,
                    pnr=e.pnr, ticket_number=ticket, agent_user_id=e.agent.user_id,
                    agent_department=e.agent.department, timestamp=e.timestamp,
                    reason=f"Same login ({e.agent.user_id}) both issued and {act}ed this ticket "
                           "(no segregation of duties).",
                    evidence=_evidence(e)))

            # 3. Off-hours value-moving event (refund/void outside shift hours).
            if act in ("refund", "void") and _is_off_hours(e.timestamp):
                flags.append(PNRFlag(
                    detector="off_hours_value", severity="medium", confidence=1.0,
                    pnr=e.pnr, ticket_number=ticket, agent_user_id=e.agent.user_id,
                    agent_department=e.agent.department, timestamp=e.timestamp,
                    reason=f"{act.title()} at {e.timestamp.strftime('%H:%M')} (off-hours).",
                    evidence=_evidence(e)))

            # 4. Downgrade across consecutive RBD on the ticket (severity-scaled).
            if i > 0:
                prev = tevs[i - 1]
                if prev.rbd_class and e.rbd_class and prev.rbd_class != e.rbd_class:
                    rbd_changes += 1
                    sev = downgrade_severity(prev.rbd_class, e.rbd_class)
                    if sev > 0:
                        flags.append(PNRFlag(
                            detector="downgrade", severity="high" if sev >= 6 else "medium",
                            confidence=1.0, pnr=e.pnr, ticket_number=ticket,
                            agent_user_id=e.agent.user_id, agent_department=e.agent.department,
                            timestamp=e.timestamp,
                            reason=f"Class downgrade {prev.rbd_class}->{e.rbd_class} ({sev} tiers).",
                            evidence=_evidence(e)))

        # 5. Repeated class changes on one ticket (reissue churn proxy).
        if rbd_changes >= REPEATED_CHANGE_MIN:
            last = tevs[-1]
            if not _excluded(last, whitelist):
                flags.append(PNRFlag(
                    detector="repeated_class_change", severity="high", confidence=0.7,
                    pnr=last.pnr, ticket_number=ticket, agent_user_id=last.agent.user_id,
                    agent_department=last.agent.department, timestamp=last.timestamp,
                    reason=f"{rbd_changes} class changes on one ticket — possible reissue churn.",
                    evidence=_evidence(last)))

    # 6. Refund/void burst by one agent in a day.
    burst: Counter[tuple[str, str]] = Counter()
    burst_ex: dict[tuple[str, str], HistoryEvent] = {}
    for e in evs:
        if _excluded(e, whitelist) or e.timestamp is None:
            continue
        if classify_action(e) in ("refund", "void") and e.agent.user_id:
            k = (e.agent.user_id, e.timestamp.strftime("%Y-%m-%d"))
            burst[k] += 1
            burst_ex.setdefault(k, e)
    for (uid, day), n in burst.items():
        if n >= REFUND_VOID_BURST_PER_DAY:
            ex = burst_ex[(uid, day)]
            flags.append(PNRFlag(
                detector="refund_void_burst", severity="high", confidence=0.8,
                pnr="(multiple)", ticket_number="(multiple)", agent_user_id=uid,
                agent_department=ex.agent.department, timestamp=ex.timestamp,
                reason=f"{n} refunds/voids by {uid} on {day}.",
                evidence=_evidence(ex)))

    flags.sort(key=lambda f: (
        {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(f.severity, 4),
        f.timestamp or datetime.min))
    return flags


# ---------------------------------------------------------------------------
# Trends + risk scoring
# ---------------------------------------------------------------------------
def agent_activity(events: Iterable[HistoryEvent]) -> list[AgentActivityRow]:
    by_agent: dict[str, list[HistoryEvent]] = defaultdict(list)
    for e in events:
        if e.agent.user_id:
            by_agent[e.agent.user_id].append(e)
    rows = []
    for uid, evs in by_agent.items():
        acts = Counter(classify_action(e) for e in evs)
        downgrades = 0
        for tevs in _by_ticket(evs).values():
            for prev, curr in zip(tevs, tevs[1:]):
                if is_downgrade(prev.rbd_class, curr.rbd_class):
                    downgrades += 1
        reissues = sum(
            1 for tevs in _by_ticket(evs).values()
            for prev, curr in zip(tevs, tevs[1:])
            if prev.rbd_class and curr.rbd_class and prev.rbd_class != curr.rbd_class)
        rows.append(AgentActivityRow(
            agent_user_id=uid,
            agent_display_name=evs[0].agent.display_name,
            department=evs[0].agent.department,
            total_events=len(evs),
            issues=acts.get("issue", 0), reissues=reissues,
            refunds=acts.get("refund", 0), voids=acts.get("void", 0),
            downgrades=downgrades,
            off_hours=sum(1 for e in evs if _is_off_hours(e.timestamp)),
            distinct_pnrs=len({e.pnr for e in evs if e.pnr}),
        ))
    rows.sort(key=lambda r: (r.refunds + r.voids, r.total_events), reverse=True)
    return rows


def _score_grain(grain: str, key_fn, flags: list[PNRFlag]) -> list[RiskRow]:
    groups: dict[str, list[PNRFlag]] = defaultdict(list)
    for f in flags:
        k = key_fn(f)
        if k and k != "(multiple)":
            groups[k].append(f)
    rows: list[RiskRow] = []
    for entity, fs in groups.items():
        base = sum(_SEV_WEIGHT.get(f.severity, 1) * f.confidence for f in fs)
        families = sorted({f.detector for f in fs})
        # Cross-family corroboration: many distinct detectors > one noisy one.
        score = base * (1.0 + 0.5 * (len(families) - 1))
        top = tuple(f.reason for f in sorted(
            fs, key=lambda f: _SEV_WEIGHT.get(f.severity, 1) * f.confidence,
            reverse=True)[:3])
        rows.append(RiskRow(grain=grain, entity=entity, score=round(score, 2),
                            families=tuple(families), flag_count=len(fs), top_reasons=top))
    return rows


def run_pnr_misuse_audit(
    events: Iterable[HistoryEvent],
    *,
    whitelist_user_ids: Iterable[str] = (),
) -> PNRMisuseReport:
    """Re-pivot the flight history corpus into a PNR-centric misuse report."""
    evs = list(events)
    whitelist = {u for u in whitelist_user_ids if u}
    flags = detect_flags(evs, whitelist=whitelist)
    activity = agent_activity(evs)

    worklist = (_score_grain("pnr", lambda f: f.pnr, flags)
                + _score_grain("agent", lambda f: f.agent_user_id, flags))
    worklist.sort(key=lambda r: r.score, reverse=True)

    times = [e.timestamp for e in evs if e.timestamp]
    return PNRMisuseReport(
        event_count=len(evs),
        pnr_count=len({e.pnr for e in evs if e.pnr}),
        agent_count=len({e.agent.user_id for e in evs if e.agent.user_id}),
        date_range=(min(times) if times else None, max(times) if times else None),
        flags=tuple(flags),
        agent_activity=tuple(activity),
        risk_worklist=tuple(worklist),
    )
