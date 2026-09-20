"""Which agencies grew and which fell away, against their own average.

Fixtures are synthetic: the warehouse carries real agency names and revenue.
Every case mirrors something the Jul/Aug 2026 data actually does -- above all
that 30,066 of 35,130 active customers had no sales on ONE side of the
comparison, so for most of them a percentage does not exist at all.
"""
from datetime import date

import pytest

from src import sales_movement as sm


def _row(customer, window, amount, *, tickets=1, **meta):
    base = {"customer": customer, "window": window, "amount": amount,
            "tickets": tickets, "customer_id": f"C-{customer}",
            "iata": "", "zone": "Dhaka", "station": "DAC",
            "sales_person": "Rep A", "agent_type": "BSP", "channel": "AGENCY"}
    base.update(meta)
    return base


AUG = sm.Settings(period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
                  trailing=3, threshold=0.20, floor=0.0)


# --------------------------------------------------------------------------
# the baseline windows
# --------------------------------------------------------------------------
def test_a_whole_calendar_month_gets_calendar_month_baselines():
    """Picking 'August' should compare against May, June and July -- not
    against three 31-day slices that start on the 31st of May."""
    got = sm.windows(AUG)
    assert got[0] == (date(2026, 8, 1), date(2026, 8, 31))
    assert got[1] == (date(2026, 7, 1), date(2026, 7, 31))
    assert got[2] == (date(2026, 6, 1), date(2026, 6, 30))
    assert got[3] == (date(2026, 5, 1), date(2026, 5, 31))


def test_an_arbitrary_range_gets_equal_length_windows():
    """A 10-day range is compared with the 10 days before it, and the 10
    before that -- never with a calendar month of a different length."""
    s = sm.Settings(period_from=date(2026, 8, 10), period_to=date(2026, 8, 19),
                    trailing=2)
    got = sm.windows(s)
    assert got[0] == (date(2026, 8, 10), date(2026, 8, 19))
    assert got[1] == (date(2026, 7, 31), date(2026, 8, 9))
    assert got[2] == (date(2026, 7, 21), date(2026, 7, 30))
    assert all((b - a).days == 9 for a, b in got)


def test_trailing_one_is_simply_the_previous_period():
    s = sm.Settings(period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
                    trailing=1)
    assert sm.windows(s)[1] == (date(2026, 7, 1), date(2026, 7, 31))


def test_the_baseline_span_is_reported_for_the_heading():
    lo, hi = sm.baseline_span(AUG)
    assert (lo, hi) == (date(2026, 5, 1), date(2026, 7, 31))


# --------------------------------------------------------------------------
# the average itself
# --------------------------------------------------------------------------
def test_the_baseline_is_the_mean_of_the_trailing_windows():
    rows = [_row("Alpha", 0, 900_000), _row("Alpha", 1, 1_000_000),
            _row("Alpha", 2, 1_100_000), _row("Alpha", 3, 900_000)]
    m = sm.build(rows, AUG).by_name("Alpha")
    assert m.baseline == pytest.approx(1_000_000)
    assert m.current == pytest.approx(900_000)


def test_a_month_with_no_sales_counts_as_a_zero_in_the_average():
    """Averaging only over the months an agency traded flatters it: an agency
    that bought once in three months would show a baseline as high as a
    steady one. The divisor is the number of windows ASKED FOR."""
    rows = [_row("Quiet", 0, 300_000), _row("Quiet", 2, 900_000)]
    m = sm.build(rows, AUG).by_name("Quiet")
    assert m.baseline == pytest.approx(300_000)     # 900k over three windows
    assert m.windows_traded == 1
    assert m.thin_baseline is True                  # said out loud on the sheet


def test_an_agency_trading_every_window_is_not_flagged_thin():
    rows = [_row("Steady", i, 500_000) for i in range(4)]
    assert sm.build(rows, AUG).by_name("Steady").thin_baseline is False


# --------------------------------------------------------------------------
# the buckets -- a percentage does not exist for most rows
# --------------------------------------------------------------------------
def test_a_fall_past_the_threshold_is_declined():
    rows = [_row("Falling", 0, 700_000)] + \
           [_row("Falling", i, 1_000_000) for i in (1, 2, 3)]
    m = sm.build(rows, AUG).by_name("Falling")
    assert m.bucket == sm.DECLINED
    assert m.change_pct == pytest.approx(-0.30)
    assert m.change == pytest.approx(-300_000)


def test_a_rise_past_the_threshold_is_growth():
    rows = [_row("Rising", 0, 1_500_000)] + \
           [_row("Rising", i, 1_000_000) for i in (1, 2, 3)]
    assert sm.build(rows, AUG).by_name("Rising").bucket == sm.GREW


def test_a_move_inside_the_band_is_stable():
    rows = [_row("Flat", 0, 1_050_000)] + \
           [_row("Flat", i, 1_000_000) for i in (1, 2, 3)]
    assert sm.build(rows, AUG).by_name("Flat").bucket == sm.STABLE


def test_the_threshold_boundary_counts_as_a_move():
    rows = [_row("Edge", 0, 800_000)] + \
           [_row("Edge", i, 1_000_000) for i in (1, 2, 3)]
    assert sm.build(rows, AUG).by_name("Edge").bucket == sm.DECLINED


def test_an_agency_that_stopped_buying_is_lapsed_not_minus_one_hundred():
    """14,577 customers bought in July and not in August. Printing '-100%'
    against them buries the only fact that matters: they are gone.

    The percentage assertion is the point of this test. Guarding only on the
    baseline let -1.0 through -- a lapsed customer HAS a baseline, that is
    what makes them lapsed -- and the workbook printed '-100%' on the very
    sheet captioned 'no percentage is printed'.
    """
    rows = [_row("Gone", i, 1_000_000) for i in (1, 2, 3)]
    m = sm.build(rows, AUG).by_name("Gone")
    assert m.bucket == sm.LAPSED
    assert m.current == 0
    assert m.change_pct is None
    assert m.change == -1_000_000       # the baseline IS the loss


# --------------------------------------------------------------------------
# a period that nets below zero is not an empty one
# --------------------------------------------------------------------------
def test_taking_more_money_back_than_you_spent_is_its_own_group():
    """9 of the 59 August 'stopped buying' agencies had really done this.
    They were trading; the net ran backwards."""
    rows = [_row("Refunder", 0, -250_000)] + \
           [_row("Refunder", i, 1_000_000) for i in (1, 2, 3)]
    m = sm.build(rows, AUG).by_name("Refunder")
    assert m.bucket == sm.REFUNDED
    assert m.verdict == "REFUNDED MORE THAN SOLD"


def test_a_refunder_gets_no_percentage_because_the_loss_exceeds_the_baseline():
    rows = [_row("Refunder", 0, -250_000)] + \
           [_row("Refunder", i, 1_000_000) for i in (1, 2, 3)]
    m = sm.build(rows, AUG).by_name("Refunder")
    assert m.change_pct is None
    assert m.change == -1_250_000       # bigger than the 1,000,000 baseline


def test_a_refunder_counts_as_a_drop_and_against_the_money_lost():
    s = sm.Settings(period_from=AUG.period_from, period_to=AUG.period_to,
                    direction=sm.DROPPED)
    rows = [_row("Refunder", 0, -250_000)] + \
           [_row("Refunder", i, 1_000_000) for i in (1, 2, 3)]
    res = sm.build(rows, s)
    assert [m.customer for m in res.reported] == ["Refunder"]
    assert res.money_lost == -1_250_000


def test_a_refunder_with_no_baseline_is_judged_on_the_size_of_the_refund():
    """A floor compares against the baseline, but a refunder may have none --
    and the size of a refund is its magnitude, not its sign."""
    s = sm.Settings(period_from=AUG.period_from, period_to=AUG.period_to,
                    floor=500_000)
    res = sm.build([_row("Big", 0, -900_000), _row("Small", 0, -1_000)], s)
    assert res.by_name("Big") is not None
    assert res.by_name("Small") is None


def test_an_agency_with_no_baseline_is_new_and_has_no_percentage():
    """15,489 customers bought in August and not in July. There is no
    denominator, so there is no percentage -- not an infinity, not a zero."""
    m = sm.build([_row("Fresh", 0, 400_000)], AUG).by_name("Fresh")
    assert m.bucket == sm.NEW
    assert m.change_pct is None
    assert m.change == pytest.approx(400_000)


def test_nobody_is_reported_twice():
    rows = [_row("Falling", 0, 100), _row("Falling", 1, 1_000),
            _row("Fresh", 0, 400_000), _row("Gone", 1, 900_000)]
    res = sm.build(rows, AUG)
    names = [m.customer for m in res.all]
    assert len(names) == len(set(names)) == 3


# --------------------------------------------------------------------------
# the floor -- without it the list is 3,981 rows of noise
# --------------------------------------------------------------------------
def test_an_agency_under_the_floor_is_left_out():
    s = sm.Settings(period_from=AUG.period_from, period_to=AUG.period_to,
                    trailing=3, threshold=0.20, floor=500_000)
    rows = [_row("Tiny", 0, 1_000)] + [_row("Tiny", i, 30_000) for i in (1, 2, 3)]
    res = sm.build(rows, s)
    assert res.by_name("Tiny") is None
    assert res.below_floor == 1


def test_the_floor_reads_the_current_period_for_a_new_agency():
    """A new agency has no baseline, so a floor applied to the baseline would
    silently delete every new agency -- the opposite of what it is for."""
    s = sm.Settings(period_from=AUG.period_from, period_to=AUG.period_to,
                    trailing=3, threshold=0.20, floor=500_000)
    res = sm.build([_row("BigNew", 0, 900_000), _row("SmallNew", 0, 1_000)], s)
    assert res.by_name("BigNew") is not None
    assert res.by_name("SmallNew") is None


# --------------------------------------------------------------------------
# direction
# --------------------------------------------------------------------------
def _mixed():
    rows = []
    rows += [_row("Falling", 0, 700_000)] + [_row("Falling", i, 1_000_000)
                                             for i in (1, 2, 3)]
    rows += [_row("Rising", 0, 1_500_000)] + [_row("Rising", i, 1_000_000)
                                              for i in (1, 2, 3)]
    rows += [_row("Gone", i, 1_000_000) for i in (1, 2, 3)]
    rows += [_row("Fresh", 0, 400_000)]
    return rows


def test_dropped_reports_the_fallers_and_the_lapsed():
    s = sm.Settings(period_from=AUG.period_from, period_to=AUG.period_to,
                    direction=sm.DROPPED)
    res = sm.build(_mixed(), s)
    assert {m.customer for m in res.reported} == {"Falling", "Gone"}


def test_increased_reports_the_risers_and_the_new():
    s = sm.Settings(period_from=AUG.period_from, period_to=AUG.period_to,
                    direction=sm.INCREASED)
    res = sm.build(_mixed(), s)
    assert {m.customer for m in res.reported} == {"Rising", "Fresh"}


def test_either_reports_every_mover_but_never_the_stable():
    rows = _mixed() + [_row("Flat", 0, 1_000_000)] + \
        [_row("Flat", i, 1_000_000) for i in (1, 2, 3)]
    res = sm.build(rows, AUG)
    assert "Flat" not in {m.customer for m in res.reported}
    assert res.counts[sm.STABLE] == 1


# --------------------------------------------------------------------------
# ranking -- money, not percentage
# --------------------------------------------------------------------------
def test_the_list_is_ranked_by_money_lost_not_by_percentage():
    """A 3.2M fall at -31% matters more than a 40k fall at -95%, and sorting
    on the percentage puts the trivial one on top."""
    rows = [_row("Big", 0, 7_000_000)] + [_row("Big", i, 10_000_000)
                                          for i in (1, 2, 3)]
    rows += [_row("Small", 0, 2_000)] + [_row("Small", i, 40_000)
                                         for i in (1, 2, 3)]
    res = sm.build(rows, AUG)
    assert [m.customer for m in res.declined] == ["Big", "Small"]


# --------------------------------------------------------------------------
# things the report must refuse to get wrong
# --------------------------------------------------------------------------
def test_a_period_running_past_the_data_is_flagged_not_reported_as_a_collapse():
    """The warehouse ends on 19 Sep. Asking for September and comparing it
    with whole months shows every agency down by a third, which is an artefact
    of the calendar, not a fact about anyone."""
    s = sm.Settings(period_from=date(2026, 9, 1), period_to=date(2026, 9, 30))
    res = sm.build([], s, data_last_day=date(2026, 9, 19))
    assert res.period_incomplete is True
    assert any("19 Sep" in w for w in res.warnings)


def test_a_complete_period_raises_no_such_warning():
    res = sm.build([], AUG, data_last_day=date(2026, 9, 19))
    assert res.period_incomplete is False


def test_a_baseline_reaching_past_the_start_of_the_data_is_flagged():
    """Averaging over three windows when the warehouse only holds one makes
    the baseline a third of its true size, and every agency a hero."""
    s = sm.Settings(period_from=date(2025, 6, 1), period_to=date(2025, 6, 30),
                    trailing=3)
    res = sm.build([], s, data_first_day=date(2025, 5, 1))
    assert res.baseline_truncated is True
    assert any("1 May 2025" in w for w in res.warnings)


def test_the_settings_are_stated_in_words_for_whoever_gets_the_sheet():
    s = sm.Settings(period_from=AUG.period_from, period_to=AUG.period_to,
                    trailing=3, threshold=0.20, floor=500_000,
                    direction=sm.DROPPED, measure=sm.MEASURE_NET)
    said = sm.build([], s).describe()
    assert "20%" in said and "500,000" in said
    assert "Aug 2026" in said and "May 2026" in said
    assert "net of refunds" in said.lower()


def test_zero_on_both_sides_is_not_a_row_at_all():
    assert sm.build([_row("Dormant", 0, 0), _row("Dormant", 1, 0)],
                    AUG).all == []


def test_a_customer_keeps_the_details_the_sheet_needs():
    rows = [_row("Alpha", 0, 900_000, zone="Chattogram", station="CGP",
                 sales_person="Rep B", iata="1234567")]
    m = sm.build(rows, AUG).by_name("Alpha")
    assert (m.zone, m.station, m.sales_person, m.iata) == \
        ("Chattogram", "CGP", "Rep B", "1234567")


def test_ticket_counts_ride_along_so_volume_can_be_read_beside_value():
    rows = [_row("Alpha", 0, 900_000, tickets=30)] + \
           [_row("Alpha", i, 1_000_000, tickets=40) for i in (1, 2, 3)]
    m = sm.build(rows, AUG).by_name("Alpha")
    assert m.tickets_current == 30
    assert m.tickets_baseline == pytest.approx(40)


# --------------------------------------------------------------------------
# identity: the account number, not the trading name
# --------------------------------------------------------------------------
def test_two_agencies_sharing_a_name_stay_two_rows():
    """281 agency names in this warehouse map to more than one Customer ID.
    Keying on the name merges distinct agencies into one silent total."""
    rows = [_row("Sky Travels", 0, 900_000, customer_id="A-1"),
            _row("Sky Travels", 0, 400_000, customer_id="A-2")]
    res = sm.build(rows, AUG)
    assert len(res.all) == 2
    assert {m.customer_id for m in res.all} == {"A-1", "A-2"}


def test_one_account_renamed_stays_one_row_under_its_current_name():
    """652 Customer IDs carry more than one name -- the same account, renamed.
    Those SHOULD merge, and should show what they trade as now."""
    rows = [_row("Old Name", 1, 1_000_000, customer_id="A-1",
                 last_bought=date(2026, 5, 4)),
            _row("New Name", 0, 900_000, customer_id="A-1",
                 last_bought=date(2026, 8, 30))]
    res = sm.build(rows, AUG)
    assert len(res.all) == 1
    assert res.all[0].customer == "New Name"


def test_a_customer_with_no_id_still_groups_on_its_name():
    rows = [_row("No Id Co", 0, 900_000, customer_id=""),
            _row("No Id Co", 1, 1_000_000, customer_id="")]
    assert len(sm.build(rows, AUG).all) == 1


# --------------------------------------------------------------------------
# the settings refuse what would reach SQL as text
# --------------------------------------------------------------------------
def test_a_date_written_as_a_string_is_refused_at_the_boundary():
    """These dates are interpolated into SQL. The GUI parses them first, but
    a CLI or scheduled caller would not necessarily."""
    with pytest.raises(TypeError, match="must be a datetime.date"):
        sm.Settings(period_from="2026-08-01", period_to=date(2026, 8, 31))


def test_a_backwards_period_is_refused_when_the_settings_are_made():
    with pytest.raises(ValueError, match="falls before"):
        sm.Settings(period_from=date(2026, 8, 31), period_to=date(2026, 8, 1))


# --------------------------------------------------------------------------
# the unit follows the measure
# --------------------------------------------------------------------------
def test_the_unit_is_tickets_when_tickets_are_what_is_counted():
    s = sm.Settings(period_from=AUG.period_from, period_to=AUG.period_to,
                    measure=sm.MEASURE_TICKETS, floor=50)
    assert s.unit == "tickets"
    assert "50 tickets" in sm.build([], s).describe()


def test_the_unit_is_bdt_for_both_money_measures():
    for measure in (sm.MEASURE_NET, sm.MEASURE_GROSS):
        s = sm.Settings(period_from=AUG.period_from, period_to=AUG.period_to,
                        measure=measure)
        assert s.unit == "BDT"


# --------------------------------------------------------------------------
# the same period a year earlier -- Hajj, Umrah and the winter peak move the
# whole book together, and a trailing average cannot see that
# --------------------------------------------------------------------------
LAST_YEAR = sm.Settings(period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
                        baseline=sm.BASELINE_LAST_YEAR, trailing=3)


def test_last_year_compares_with_the_same_month_a_year_before():
    got = sm.windows(LAST_YEAR)
    assert got[0] == (date(2026, 8, 1), date(2026, 8, 31))
    assert got[1] == (date(2025, 8, 1), date(2025, 8, 31))
    assert len(got) == 2               # one window, never an average of years


def test_last_year_ignores_the_trailing_setting_when_dividing():
    """Reading `trailing` directly would divide one window's total by three
    and make every agency look like it tripled."""
    rows = [_row("Alpha", 0, 900_000), _row("Alpha", 1, 1_000_000)]
    m = sm.build(rows, LAST_YEAR).by_name("Alpha")
    assert m.baseline == pytest.approx(1_000_000)
    assert m.trailing == 1
    assert m.thin_baseline is False


def test_last_year_on_an_arbitrary_range_shifts_both_ends():
    s = sm.Settings(period_from=date(2026, 8, 10), period_to=date(2026, 8, 19),
                    baseline=sm.BASELINE_LAST_YEAR)
    assert sm.windows(s)[1] == (date(2025, 8, 10), date(2025, 8, 19))


def test_a_leap_day_steps_back_to_the_twenty_eighth():
    s = sm.Settings(period_from=date(2028, 2, 29), period_to=date(2028, 2, 29),
                    baseline=sm.BASELINE_LAST_YEAR)
    assert sm.windows(s)[1] == (date(2027, 2, 28), date(2027, 2, 28))


def test_the_last_year_sentence_does_not_claim_an_average():
    said = sm.build([], LAST_YEAR).describe()
    assert "same period a year earlier" in said
    assert "Aug 2025" in said
    assert "average" not in said


def test_the_trailing_sentence_still_says_average():
    assert "own average for" in sm.build([], AUG).describe()


def test_an_unknown_baseline_mode_is_refused():
    with pytest.raises(ValueError, match="unknown baseline"):
        sm.Settings(period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
                    baseline="vibes")


def test_a_last_year_baseline_before_the_data_starts_is_flagged():
    """The warehouse begins in May 2025, so asking for early 2026 against
    last year silently compares with nothing at all."""
    s = sm.Settings(period_from=date(2026, 2, 1), period_to=date(2026, 2, 28),
                    baseline=sm.BASELINE_LAST_YEAR)
    res = sm.build([], s, data_first_day=date(2025, 5, 1))
    assert res.baseline_truncated is True
