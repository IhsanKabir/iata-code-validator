"""Where one agency stands with BS.

Fixtures mirror Be Fresh Limited, which trades through a Non-IATA account
and two BSP ones -- the reason "growth in BSP" is a question about accounts
rather than about the business.
"""
from datetime import date

import pytest

from src import agency_identity as ai
from src import agency_scorecard as asc


def _acct(cid, name, atype, value, iata="0"):
    return {"customer_id": cid, "customer": name, "iata": iata,
            "agent_type": atype, "value": value}


NOW = [_acct("10000277", "BE FRESH LIMITED", "Non-IATA", 582_034_422),
       _acct("11662412", "Be Fresh Limited (IATA)", "BSP", 86_948_326,
             "42306773"),
       _acct("900", "Big Rival", "BSP", 5_000_000_000),
       _acct("901", "Small Co", "Non-IATA", 1_000)]
BEFORE = [_acct("10000277", "BE FRESH LIMITED", "Non-IATA", 422_336_509),
          _acct("11662412", "Be Fresh Limited (IATA)", "BSP", 82_437_721,
                "42306773"),
          _acct("900", "Big Rival", "BSP", 4_000_000_000)]


def _card(now=None, before=None, term="be fresh"):
    now = NOW if now is None else now
    before = BEFORE if before is None else before
    groups, _ = ai.resolve(now + before)
    agency = ai.find(groups, term)[0]
    return asc.build(agency, groups, asc._totals(now, groups),
                     asc._totals(before, groups),
                     period=(date(2026, 5, 1), date(2026, 9, 19)),
                     prior=(date(2025, 5, 1), date(2025, 9, 19)))


# --------------------------------------------------------------------------
# the six
# --------------------------------------------------------------------------
def test_market_share_is_against_the_whole_agency_book():
    c = _card()
    total = 582_034_422 + 86_948_326 + 5_000_000_000 + 1_000
    assert c.share_now == pytest.approx((582_034_422 + 86_948_326) / total)


def test_last_year_share_uses_last_year_market():
    c = _card()
    total = 422_336_509 + 82_437_721 + 4_000_000_000
    assert c.share_before == pytest.approx(
        (422_336_509 + 82_437_721) / total)


def test_growth_with_bs_is_the_whole_business():
    assert _card().growth == pytest.approx(
        (582_034_422 + 86_948_326) / (422_336_509 + 82_437_721) - 1)


def test_growth_in_bsp_counts_only_the_bsp_accounts():
    """Be Fresh grew 32.5% overall while its BSP arm grew 5.5%. Reporting
    one number would have hidden which half moved."""
    c = _card()
    assert c.growth_bsp == pytest.approx(86_948_326 / 82_437_721 - 1)
    assert c.growth_bsp < c.growth


def test_the_non_iata_arm_is_carried_too():
    c = _card()
    assert c.growth_non_iata == pytest.approx(582_034_422 / 422_336_509 - 1)


def test_ranking_counts_grouped_agencies_not_accounts():
    c = _card()
    assert c.rank_now == 2             # behind Big Rival, ahead of Small Co
    assert c.now.agencies == 3         # Be Fresh counts once, not twice


def test_last_year_ranking_is_its_own_window():
    c = _card()
    assert c.rank_before == 2
    assert c.before.agencies == 2      # Small Co did not trade last year


def test_rank_movement_is_positive_when_they_climb():
    c = _card()
    assert c.rank_move == 0
    other = _card(now=NOW, before=[_acct("10000277", "BE FRESH LIMITED",
                                         "Non-IATA", 1_000),
                                   _acct("900", "Big Rival", "BSP", 9_000),
                                   _acct("902", "Mid Co", "BSP", 5_000)])
    assert other.rank_move == 1        # 3rd last year, 2nd now


# --------------------------------------------------------------------------
# what does not exist is not zero
# --------------------------------------------------------------------------
def test_an_agency_with_no_last_year_has_no_share_growth_or_rank():
    """1,558 of 3,471 agencies trading this year did not trade in the same
    window last year."""
    c = _card(now=NOW + [_acct("950", "Brand New Co", "BSP", 9_000_000)],
              term="Brand New")
    assert c.comparable is False
    assert c.share_before is None
    assert c.growth is None
    assert c.rank_before is None
    assert any("do not exist" in w for w in c.warnings)


def test_an_agency_with_no_bsp_account_has_no_bsp_growth():
    c = _card(now=[_acct("1", "Solo Tours", "Non-IATA", 500)],
              before=[_acct("1", "Solo Tours", "Non-IATA", 400)],
              term="Solo")
    assert c.has_bsp is False
    assert c.growth_bsp is None


# --------------------------------------------------------------------------
# a rank in the tail is a band, not a position
# --------------------------------------------------------------------------
def test_a_rank_near_the_top_is_not_flagged_as_noise():
    c = _card()
    assert c.now.rank_is_noise is False


def test_the_band_is_a_percentile_not_a_position():
    """Rank 7 of 3,238 is 'top 1%'; rank 2 of 3 is the bottom half. The band
    describes where they sit in the book, not how small the number is."""
    now = [_acct(str(9000 + i), f"Filler {i}", "BSP", 10_000_000 - i)
           for i in range(200)]
    now.append(_acct("1", "Near Top", "BSP", 11_000_000))
    c = _card(now=now, before=now, term="Near Top")
    assert c.rank_now == 1
    assert c.now.rank_band == "top 1%"


def test_a_rank_deep_in_the_tail_is_flagged_as_noise():
    """At rank 500 the gap to the next agency is 1,874 BDT on 5.9M."""
    now = [_acct(str(9000 + i), f"Filler {i}", "BSP", 10_000_000 - i)
           for i in range(300)]
    now.append(_acct("1", "Tail Co", "Non-IATA", 5))
    c = _card(now=now, before=now, term="Tail Co")
    assert c.now.rank_is_noise is True
    assert c.now.rank_band == "bottom half"
    assert any("one ticket moves it" in w for w in c.warnings)


# --------------------------------------------------------------------------
# both sides cover the same days
# --------------------------------------------------------------------------
def test_a_year_to_date_window_is_trimmed_so_both_sides_match():
    """Jan-Sep against a warehouse starting 1 May 2025 compares nine months
    with five and reports the missing four as growth."""
    period, prior, notes = asc.like_for_like(
        date(2026, 1, 1), date(2026, 12, 31),
        data_first_day=date(2025, 5, 1), data_last_day=date(2026, 9, 19))
    assert period[1] == date(2026, 9, 19)
    assert prior[0] == date(2025, 5, 1)
    assert (period[1] - period[0]).days == (prior[1] - prior[0]).days
    assert any("same span" in n for n in notes)


def test_a_window_already_inside_the_data_is_left_alone():
    period, prior, notes = asc.like_for_like(
        date(2026, 6, 1), date(2026, 6, 30),
        data_first_day=date(2025, 5, 1), data_last_day=date(2026, 9, 19))
    assert period == (date(2026, 6, 1), date(2026, 6, 30))
    assert prior == (date(2025, 6, 1), date(2025, 6, 30))
    assert notes == []


# --------------------------------------------------------------------------
# saying it in words
# --------------------------------------------------------------------------
def test_the_headline_names_the_denominator_and_the_rank():
    said = _card().headline()
    assert "BE FRESH LIMITED" in said
    assert "agency sales with BS" in said
    assert "ranked 2 of 3" in said


def test_the_span_is_printed_so_the_card_can_be_forwarded():
    assert _card().span_label() == \
        "1 May 2026 to 19 Sep 2026, against 1 May 2025 to 19 Sep 2025"


def test_a_conflicting_iata_code_is_carried_into_the_warnings():
    now = [_acct("1", "Sky Travels", "BSP", 100, "11111111"),
           _acct("2", "Sky Travels", "BSP", 200, "22222222")]
    c = _card(now=now, before=now, term="Sky")
    assert any("two different IATA codes" in w for w in c.warnings)
