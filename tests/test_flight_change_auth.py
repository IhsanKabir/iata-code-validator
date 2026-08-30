"""Flight Change Authenticator — was a free reissue actually earned?

Fixtures use the REAL description strings from the manual investigation workbook
(PNRs/agents anonymised), because the whole tool hinges on parsing those grammars
exactly:

  time revision   "DACSHJ/10/10/2025/ 20:45->00:30/1 ==> 10/10/2025/ 21:25->00:30/1"
  chained hops    two "==>" segments inside one comment
  flight transfer "Flight:BS 381 01/10/2025->BS 381 03/10/2025"

Confirmed rule: an EXACT 30-minute domestic move qualifies (inclusive), and the
reissue must still land inside the one-month window.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.flight_change_auth import (
    BELOW_THRESHOLD,
    JUSTIFIED,
    KIND_TIME,
    KIND_TRANSFER,
    NEEDS_REVIEW,
    NO_CHANGE_FOUND,
    OUTSIDE_WINDOW,
    AuthConfig,
    authenticate_pnr,
    changes_in_event,
    parse_flight_transfer,
    parse_time_revisions,
    sector_of_route,
    suspicious_flags,
)


class _Agent:
    def __init__(self, name, user_id, department=""):
        self.name, self.user_id, self.department = name, user_id, department


class _Event:
    """Stands in for zenith_pnr_history_parser.DossierEvent."""

    def __init__(self, ts, desc="", etype="", agent=None, is_reissue=False):
        self.timestamp = ts
        self.raw_description = desc
        self.event_type = etype
        self.agent = agent or _Agent("", "")
        self.is_reissue = is_reissue


# --- grammar A: time revision ----------------------------------------------------

def test_parses_a_single_time_revision():
    (c,) = parse_time_revisions(
        "DACSHJ/10/10/2025/ 20:45->00:30/1 ==> 10/10/2025/ 21:25->00:30/1")
    assert c.kind == KIND_TIME and c.route == "DAC-SHJ"
    assert c.sector == "International"
    assert c.original_dep == datetime(2025, 10, 10, 20, 45)
    assert c.revised_dep == datetime(2025, 10, 10, 21, 25)
    assert c.shift_minutes == 40                    # +40 min


def test_parses_every_hop_in_a_chained_comment():
    """One comment can carry several moves; each must be kept in order."""
    desc = ("/DACSHJ/10/10/2025/ 20:45->00:30/1 ==> 10/10/2025/ 21:35->00:30/1"
            "//DACSHJ/10/10/2025/ 21:35->00:30/1 ==> 10/10/2025/ 21:25->00:30/1"
            "/MULTIPLE TIMES DEL")
    hops = parse_time_revisions(desc)
    assert [h.shift_minutes for h in hops] == [50, -10]


def test_domestic_route_is_recognised():
    (c,) = parse_time_revisions(
        "CXBDAC/11/11/2025/ 10:50->11:55 ==> 11/11/2025/ 10:20->11:25")
    assert c.sector == "Domestic" and c.shift_minutes == -30      # 30 min EARLIER


def test_sector_of_route_forms():
    assert sector_of_route("DACCGP") == "Domestic"
    assert sector_of_route("DAC-CGP") == "Domestic"
    assert sector_of_route("DACSHJ") == "International"
    assert sector_of_route("") == "Unknown"


# --- grammar B: flight transfer --------------------------------------------------

def test_parses_a_flight_transfer():
    c = parse_flight_transfer("Flight:BS 381 01/10/2025->BS 381 03/10/2025")
    assert c is not None and c.kind == KIND_TRANSFER
    assert c.is_qualifying_by_nature                 # losing your flight always counts


def test_cancellation_text_is_a_qualifying_change():
    ev = _Event(datetime(2025, 10, 1, 9, 0),
                desc="BS381 01OCT NOOP FLIGHT | Flight:BS 381 01/10/2025->BS 381 03/10/2025",
                etype="Ticket Modification")
    (c,) = changes_in_event(ev)
    assert c.is_qualifying_by_nature


def test_type_says_flight_time_but_grammar_unknown_is_still_a_change():
    """A missing grammar must not read as 'no change' — it must surface."""
    ev = _Event(datetime(2025, 10, 1, 9, 0), desc="rescheduled, see notes",
                etype="Changing flight time")
    (c,) = changes_in_event(ev)
    assert c.kind == KIND_TIME and c.shift_minutes is None


# --- verdicts --------------------------------------------------------------------

def _case(change_desc, shift_days_to_reissue=1.0, etype="Changing flight time",
          mover=("Tanni", "tanni7196"), reissuer=("Akhter", "alaya1751"), cfg=None):
    t0 = datetime(2025, 10, 1, 10, 0)
    ev_change = _Event(t0, desc=change_desc, etype=etype,
                       agent=_Agent(mover[0], mover[1], "BO-3 Revenue Management"))
    ev_reissue = _Event(t0 + timedelta(days=shift_days_to_reissue),
                        desc="Issued->Exchanged | IATA Coupon status :I ->E",
                        etype="Ticket Modification",
                        agent=_Agent(reissuer[0], reissuer[1], "BO-1 Central Reservation"),
                        is_reissue=True)
    return authenticate_pnr("08200H", [ev_change, ev_reissue], cfg or AuthConfig())


def test_international_40_minute_move_is_below_threshold():
    """The real anomaly from the file: DAC-SHJ moved 40 min, free reissue given."""
    (case,) = _case("DACSHJ/10/10/2025/ 20:45->00:30/1 ==> 10/10/2025/ 21:25->00:30/1")
    assert case.verdict == BELOW_THRESHOLD
    assert "40 min" in case.reason and "60 min" in case.reason


def test_exact_30_minute_domestic_move_qualifies():
    """Confirmed by the desk: exactly 30 minutes IS an entitlement."""
    (case,) = _case("CXBDAC/11/11/2025/ 10:50->11:55 ==> 11/11/2025/ 10:20->11:25")
    assert case.verdict == JUSTIFIED


def test_exact_30_minutes_can_be_made_strict_by_config():
    cfg = AuthConfig(inclusive=False)
    (case,) = _case("CXBDAC/11/11/2025/ 10:50->11:55 ==> 11/11/2025/ 10:20->11:25",
                    cfg=cfg)
    assert case.verdict == BELOW_THRESHOLD


def test_qualifying_move_reissued_too_late_is_outside_the_window():
    (case,) = _case("DACSHJ/10/10/2025/ 20:45->00:30/1 ==> 10/10/2025/ 22:45->00:30/1",
                    shift_days_to_reissue=45)
    assert case.verdict == OUTSIDE_WINDOW and "45 days" in case.reason


def test_small_moves_accumulate_into_an_entitlement():
    """Two 35-minute hops on an international sector = 70 min net -> justified."""
    desc = ("DACSHJ/10/10/2025/ 20:00->00:30/1 ==> 10/10/2025/ 20:35->00:30/1"
            "/DACSHJ/10/10/2025/ 20:35->00:30/1 ==> 10/10/2025/ 21:10->00:30/1")
    (case,) = _case(desc)
    assert case.cumulative_shift == 70 and case.verdict == JUSTIFIED


def test_governing_basis_judges_only_the_last_hop():
    desc = ("DACSHJ/10/10/2025/ 20:00->00:30/1 ==> 10/10/2025/ 20:35->00:30/1"
            "/DACSHJ/10/10/2025/ 20:35->00:30/1 ==> 10/10/2025/ 21:10->00:30/1")
    (case,) = _case(desc, cfg=AuthConfig(basis="governing"))
    assert case.verdict == BELOW_THRESHOLD          # last hop alone is only 35 min


def test_reissue_with_no_change_at_all():
    t0 = datetime(2025, 10, 1, 10, 0)
    ev = _Event(t0, desc="Issued->Exchanged | IATA Coupon status :I ->E",
                etype="Ticket Modification", is_reissue=True)
    (case,) = authenticate_pnr("08DS6J", [ev])
    assert case.verdict == NO_CHANGE_FOUND


def test_unrecognised_events_make_it_needs_review_not_clean():
    t0 = datetime(2025, 10, 1, 10, 0)
    other = _Event(t0, desc="something we have never seen", etype="Mystery Type")
    reissue = _Event(t0 + timedelta(hours=2),
                     desc="Issued->Exchanged | IATA Coupon status :I ->E",
                     etype="Ticket Modification", is_reissue=True)
    (case,) = authenticate_pnr("08HA2G", [other, reissue])
    assert case.verdict == NEEDS_REVIEW
    assert case.unclassified_types == ("Mystery Type",)


def test_changes_after_the_reissue_do_not_justify_it():
    """A move made AFTER the reissue cannot be what earned it."""
    t0 = datetime(2025, 10, 1, 10, 0)
    reissue = _Event(t0, desc="IATA Coupon status :I ->E", is_reissue=True,
                     etype="Ticket Modification")
    later = _Event(t0 + timedelta(days=1),
                   desc="DACSHJ/10/10/2025/ 20:00->00:30/1 ==> 10/10/2025/ 23:00->00:30/1",
                   etype="Changing flight time")
    (case,) = authenticate_pnr("08DV0C", [reissue, later])
    assert case.verdict == NO_CHANGE_FOUND


# --- people and patterns ----------------------------------------------------------

def test_same_agent_moved_and_reissued_is_flagged():
    (case,) = _case("DACSHJ/10/10/2025/ 20:45->00:30/1 ==> 10/10/2025/ 23:45->00:30/1",
                    mover=("Robin", "robin5914"), reissuer=("Robin", "robin5914"))
    assert case.same_agent
    assert any("SAME AGENT" in f for f in suspicious_flags(case))


def test_different_agents_are_not_flagged_for_duties():
    (case,) = _case("DACSHJ/10/10/2025/ 20:45->00:30/1 ==> 10/10/2025/ 23:45->00:30/1")
    assert not case.same_agent
    assert not any("SAME AGENT" in f for f in suspicious_flags(case))


def test_reissue_minutes_after_the_change_is_flagged_when_unjustified():
    (case,) = _case("DACSHJ/10/10/2025/ 20:45->00:30/1 ==> 10/10/2025/ 21:25->00:30/1",
                    shift_days_to_reissue=5 / (24 * 60))      # 5 minutes later
    assert case.verdict == BELOW_THRESHOLD
    assert any("min after the schedule change" in f for f in suspicious_flags(case))


def test_agents_and_route_are_carried_onto_the_case():
    (case,) = _case("DACSHJ/10/10/2025/ 20:45->00:30/1 ==> 10/10/2025/ 21:25->00:30/1")
    assert case.reissued_by == "Akhter" and case.reissued_by_login == "alaya1751"
    assert case.changed_by == "Tanni" and case.changed_by_login == "tanni7196"
    assert case.route == "DAC-SHJ" and case.sector == "International"
    assert round(case.days_to_reissue, 2) == 1.0


# --- the marker the live data actually uses -------------------------------------
# A 2,286-event run found 46 "Addition of new ticket(s) to be exchanged" events but
# only 2 "Issued->Exchanged" coupon lines. Keying solely on the coupon line made the
# tool report 2 reissues and look clean — the failure these tests prevent.

_REAL_EXCHANGE = ("Addition of new ticket(s) to be exchanged : "
                  "flight BS345 25/10/2025 21:25:00 (DAC -> SHJ)")


def test_exchange_addition_is_recognised_as_a_reissue():
    from src.flight_change_auth import is_reissue_event, reissue_marker
    ev = _Event(None, _REAL_EXCHANGE, "File Modification")
    assert is_reissue_event(ev)
    m = reissue_marker(ev)
    assert m["flight"] == "BS345" and m["route"] == "DAC-SHJ"
    assert m["new_departure"] == datetime(2025, 10, 25, 21, 25)


def test_coupon_line_still_counts():
    from src.flight_change_auth import is_reissue_event
    assert is_reissue_event(_Event(datetime(2025, 10, 1), "IATA Coupon status :I ->E",
                                   "Ticket Modification", is_reissue=True))


def test_ordinary_events_are_not_reissues():
    from src.flight_change_auth import is_reissue_event
    for desc, etype in (("Coupon status change:32003073 Old status:Boarded "
                         "New status:Flown", "Coupon Status Change"),
                        ("Seat map change", "Seat map change"),
                        ("", "Cute Event")):
        assert not is_reissue_event(_Event(datetime(2025, 10, 1), desc, etype))


def test_route_comes_from_the_marker_when_the_change_has_none():
    """A transfer/cancellation carries no route, so the sector used to be Unknown
    and the threshold silently defaulted to international."""
    t0 = datetime(2025, 10, 1, 10, 0)
    evs = [
        _Event(t0, "BS345 01OCT NOOP FLIGHT Flight:BS 345 01/10/2025->BS 345 03/10/2025",
               "Ticket Modification", _Agent("Tanni", "t1")),
        _Event(t0 + timedelta(days=1), _REAL_EXCHANGE, "File Modification",
               _Agent("Akhter", "a1")),
    ]
    (case,) = authenticate_pnr("08200H", evs)
    assert case.route == "DAC-SHJ" and case.sector == "International"
    assert case.verdict == JUSTIFIED          # a cancelled flight earns it outright


def test_one_transaction_logged_per_coupon_is_counted_once():
    """The same exchange is written once per ticket/coupon; two coupons must not
    read as two reissues."""
    t0 = datetime(2025, 10, 1, 10, 0)
    evs = [
        _Event(t0, "DACSHJ/10/10/2025/ 20:45->00:30/1 ==> 10/10/2025/ 23:45->00:30/1",
               "Changing flight time", _Agent("T", "t1")),
        _Event(t0 + timedelta(days=1), _REAL_EXCHANGE, "File Modification",
               _Agent("A", "a1")),
        _Event(t0 + timedelta(days=1), _REAL_EXCHANGE, "File Modification",
               _Agent("A", "a1")),
    ]
    assert len(authenticate_pnr("093RQ7", evs)) == 1


def test_separate_exchanges_are_still_separate_cases():
    """Dedupe must not collapse two genuinely different reissues."""
    t0 = datetime(2025, 10, 1, 10, 0)
    other = ("Addition of new ticket(s) to be exchanged : "
             "flight BS333 02/04/2026 19:30:00 (DAC -> DOH)")
    evs = [
        _Event(t0, "DACSHJ/10/10/2025/ 20:45->00:30/1 ==> 10/10/2025/ 23:45->00:30/1",
               "Changing flight time", _Agent("T", "t1")),
        _Event(t0 + timedelta(days=1), _REAL_EXCHANGE, "File Modification"),
        _Event(t0 + timedelta(days=2), other, "File Modification"),
    ]
    assert len(authenticate_pnr("09F069", evs)) == 2
