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
from datetime import datetime, timedelta
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
# Flight ModificationHistory is exported via `excel=1`, whose timestamps are GMT/UTC
# (verified by the Step-0 probe + the corpus hour-histogram). US-Bangla operates in
# Asia/Dhaka (UTC+6, no DST), so we localise before ANY hour-of-day reasoning — otherwise
# "off-hours" fires on Dhaka morning business hours (e.g. 04:21 GMT == 10:21 DAC).
LOCAL_UTC_OFFSET_HOURS = 6    # Asia/Dhaka, fixed (Bangladesh has no daylight saving)
OFF_HOURS_START = 23          # an event at/after 23:00 LOCAL ...
OFF_HOURS_END = 6             # ... or before 06:00 LOCAL is "off hours"
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
    # Corpus coverage — so the workbook can show whether the ticket-lifecycle detectors
    # could even run. flown_events==0 means refund_of_flown is inactive on this corpus;
    # a high fallback_groups share means most groups lacked a parseable ticket number.
    flown_events: int = 0
    real_ticket_groups: int = 0
    fallback_groups: int = 0


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


def _local(ts: datetime) -> datetime:
    """GMT/UTC corpus timestamp -> Asia/Dhaka local (for hour-of-day reasoning)."""
    return ts + timedelta(hours=LOCAL_UTC_OFFSET_HOURS)


def _is_off_hours(ts: datetime | None) -> bool:
    if ts is None:
        return False
    h = _local(ts).hour
    return h >= OFF_HOURS_START or h < OFF_HOURS_END


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
    """Group events by ticket number, falling back to PNR when the ticket isn't
    parseable.

    The ModificationHistory description often glues the coupon suffix to the e-ticket
    (e.g. ``7792000000001C1``), so the parser's ``_TICKET_NUMBER_RE`` (which requires a
    word boundary after 13 digits) yields nothing. PNR is then the best grouping key —
    a PNR is one ticket for the vast majority of bookings, so the per-ticket detectors
    still hold.
    """
    out: dict[str, list[HistoryEvent]] = defaultdict(list)
    for e in events:
        key = e.ticket_number or e.pnr
        if key:
            out[key].append(e)
    for evs in out.values():
        evs.sort(key=lambda e: e.timestamp or datetime.min)
    return out


def detect_flags(events: Iterable[HistoryEvent], *, whitelist: set[str]) -> list[PNRFlag]:
    evs = [e for e in events if e.pnr]
    flags: list[PNRFlag] = []

    for key, tevs in _by_ticket(evs).items():
        # A group keyed by a REAL ticket number is one coupon's lifecycle. A PNR-fallback
        # group (the ticket number didn't parse) can span several passengers/tickets, so
        # cross-lifecycle detectors (refund-of-flown, self-refund, downgrade, churn) would
        # falsely link one passenger's flown coupon to another's refund — they only run on
        # real-ticket groups. Per-event detectors (off-hours, burst) are safe on any group.
        is_real_ticket = bool(tevs and tevs[0].ticket_number)
        actions = [classify_action(e) for e in tevs]
        # Issuer set + flown index from NON-excluded events only — never attribute a
        # system-set Flown or a whitelisted issue to a human refunder.
        issuers = {e.agent.user_id for e, a in zip(tevs, actions)
                   if a == "issue" and e.agent.user_id and not _excluded(e, whitelist)}
        flown_idx = next((i for i, e in enumerate(tevs)
                          if not _excluded(e, whitelist)
                          and _FLOWN in (_norm(e.old_status), _norm(e.new_status))), None)

        prev_rbd = ""
        rbd_changes = 0
        rbd_agents: set[str] = set()
        for i, (e, act) in enumerate(zip(tevs, actions)):
            if _excluded(e, whitelist):
                if e.rbd_class:
                    prev_rbd = e.rbd_class          # keep the class chain continuous
                continue

            if is_real_ticket and act == "refund" and flown_idx is not None and i >= flown_idx:
                flags.append(PNRFlag(
                    detector="refund_of_flown", severity="critical", confidence=0.7,
                    pnr=e.pnr, ticket_number=key, agent_user_id=e.agent.user_id,
                    agent_department=e.agent.department, timestamp=e.timestamp,
                    reason="Refund on a coupon that was Flown — verify it isn't an involuntary refund.",
                    evidence=_evidence(e)))

            if is_real_ticket and act in ("refund", "void") and e.agent.user_id in issuers:
                flags.append(PNRFlag(
                    detector="self_refund_sod", severity="high", confidence=1.0,
                    pnr=e.pnr, ticket_number=key, agent_user_id=e.agent.user_id,
                    agent_department=e.agent.department, timestamp=e.timestamp,
                    reason=f"Same login ({e.agent.user_id}) both issued and {act}ed this ticket "
                           "(no segregation of duties).",
                    evidence=_evidence(e)))

            if act in ("refund", "void") and _is_off_hours(e.timestamp):
                flags.append(PNRFlag(
                    detector="off_hours_value", severity="medium", confidence=1.0,
                    pnr=e.pnr, ticket_number=key, agent_user_id=e.agent.user_id,
                    agent_department=e.agent.department, timestamp=e.timestamp,
                    reason=f"{act.title()} at {_local(e.timestamp).strftime('%H:%M')} DAC "
                           "(off-hours).",
                    evidence=_evidence(e)))

            # Downgrade vs the previous NON-excluded class on a REAL ticket.
            if prev_rbd and e.rbd_class and prev_rbd != e.rbd_class:
                rbd_changes += 1
                if e.agent.user_id:
                    rbd_agents.add(e.agent.user_id)
                sev = downgrade_severity(prev_rbd, e.rbd_class)
                if is_real_ticket and sev > 0:
                    flags.append(PNRFlag(
                        detector="downgrade", severity="high" if sev >= 6 else "medium",
                        confidence=1.0, pnr=e.pnr, ticket_number=key,
                        agent_user_id=e.agent.user_id, agent_department=e.agent.department,
                        timestamp=e.timestamp,
                        reason=f"Class downgrade {prev_rbd}->{e.rbd_class} ({sev} tiers).",
                        evidence=_evidence(e)))
            if e.rbd_class:
                prev_rbd = e.rbd_class

        # Repeated class changes on one ticket — attribute to the agent(s) who drove it.
        if is_real_ticket and rbd_changes >= REPEATED_CHANGE_MIN:
            last = next((e for e in reversed(tevs) if not _excluded(e, whitelist)), tevs[-1])
            multi = len(rbd_agents) > 1
            flags.append(PNRFlag(
                detector="repeated_class_change", severity="high", confidence=0.7,
                pnr=last.pnr, ticket_number=key,
                agent_user_id="(multiple)" if multi else next(iter(rbd_agents), last.agent.user_id),
                agent_department=last.agent.department, timestamp=last.timestamp,
                reason=f"{rbd_changes} class changes on one ticket by {len(rbd_agents)} agent(s) "
                       "— possible reissue churn.",
                evidence=f"agents: {', '.join(sorted(rbd_agents))} · {_evidence(last)}"))

    # Refund/void burst by one agent in a day (per-event; safe on any grouping).
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
                detector="refund_void_burst", severity="medium", confidence=0.7,
                pnr="(multiple)", ticket_number="(multiple)", agent_user_id=uid,
                agent_department=ex.agent.department, timestamp=ex.timestamp,
                reason=f"{n} refunds/voids by {uid} on {day} — verify "
                       "(central desks / group ops do this legitimately).",
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
        reissues = 0
        for tevs in _by_ticket(evs).values():       # one pass: count both
            for prev, curr in zip(tevs, tevs[1:]):
                if prev.rbd_class and curr.rbd_class and prev.rbd_class != curr.rbd_class:
                    reissues += 1
                    if is_downgrade(prev.rbd_class, curr.rbd_class):
                        downgrades += 1
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
        # Cross-family corroboration: many distinct detectors > one noisy one. Capped so
        # an entity lit by overlapping detectors on the same events can't run away.
        score = base * min(2.0, 1.0 + 0.5 * (len(families) - 1))
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
    groups = _by_ticket([e for e in evs if e.pnr])
    real_groups = sum(1 for g in groups.values() if g and g[0].ticket_number)
    flown_events = sum(
        1 for e in evs if _FLOWN in (_norm(e.old_status), _norm(e.new_status)))
    return PNRMisuseReport(
        event_count=len(evs),
        pnr_count=len({e.pnr for e in evs if e.pnr}),
        agent_count=len({e.agent.user_id for e in evs if e.agent.user_id}),
        date_range=(min(times) if times else None, max(times) if times else None),
        flags=tuple(flags),
        agent_activity=tuple(activity),
        risk_worklist=tuple(worklist),
        flown_events=flown_events,
        real_ticket_groups=real_groups,
        fallback_groups=len(groups) - real_groups,
    )
