"""Seats per aircraft, and how sure we are of the number.

The aircraft strings are the ones the schedule feed actually returns:
'ATR 72 - 600', 'Boeing 737-800 Winglets', 'DH8', 'Airbus-A330-300'.
"""
from src import aircraft_seats as seats


class _Leg:
    def __init__(self, aircraft, capacity):
        self.aircraft, self.capacity = aircraft, capacity


# --------------------------------------------------------------------------
# reading the messy strings the feed returns
# --------------------------------------------------------------------------
def test_the_feeds_spellings_all_normalise():
    """One aircraft is written 'ATR-72-600', 'ATR 72 - 600' and 'ATR72'."""
    for text in ("ATR 72 - 600", "ATR-72-600", "ATR72", "atr 72"):
        assert "atr72" in seats.normalise(text)
    assert "737800" in seats.normalise("Boeing 737-800 Winglets")
    assert "a330300" in seats.normalise("Airbus-A330-300")
    assert "dh8" in seats.normalise("DH8")


def test_nothing_recognisable_is_not_a_guess():
    """A made-up number inside a share calculation is worse than a gap."""
    got = seats.seats_for("Flying Saucer 9000")
    assert got.known is False
    assert got.confidence == seats.UNKNOWN
    assert str(got) == "unknown"


def test_an_empty_aircraft_is_unknown():
    assert seats.seats_for("").known is False
    assert seats.seats_for(None).known is False


# --------------------------------------------------------------------------
# the three bases, weakest last
# --------------------------------------------------------------------------
def test_the_operators_own_capacity_wins_over_everything():
    """US-Bangla's A330-300 carries 436 in their own records. A typical
    A330-300 is 277, and using that would understate them by a third."""
    own = {"a330300": 436, "atr72": 72}
    got = seats.seats_for("Airbus A330-300", "BS", own)
    assert got.seats == 436
    assert got.confidence == seats.OPERATOR


def test_a_carrier_config_beats_the_generic_type():
    """Biman's Q400 is 74; a typical Q400 is 78."""
    got = seats.seats_for("DH8", "BG")
    assert got.seats == 74
    assert got.confidence == seats.AIRLINE


def test_the_generic_type_is_the_last_resort():
    got = seats.seats_for("Airbus A320", "ZZ")
    assert got.seats == 180
    assert got.confidence == seats.TYPE


def test_the_same_type_differs_by_carrier():
    """Counting flights hides this; counting seats does not."""
    assert seats.seats_for("Boeing 737-800", "BG").seats == 162
    assert seats.seats_for("Boeing 737-800", "MH").seats == 160
    assert seats.seats_for("Boeing 737-800", "FZ").seats == 174


# --------------------------------------------------------------------------
# the matches that are easy to get wrong
# --------------------------------------------------------------------------
def test_a_max_8_is_not_read_as_a_plain_737():
    got = seats.seats_for("Boeing 737 MAX 8", "MH")
    assert got.seats == 162            # MH MAX 8, not the 160 of its 737-800


def test_a_neo_is_not_read_as_a_ceo():
    assert seats.seats_for("Airbus A320neo", "6E").seats == 186
    assert seats.seats_for("Airbus A320", "6E").seats == 180


def test_the_indigo_a320_against_our_atr_is_the_whole_point():
    """180 against 72 is why seat share and flight share disagree."""
    theirs = seats.seats_for("Airbus A320", "6E").seats
    ours = seats.seats_for("ATR 72 - 600", "BS", {"atr72": 72}).seats
    assert theirs == 180 and ours == 72
    assert round(ours / (ours + theirs) * 100) == 29


# --------------------------------------------------------------------------
# building the operator table from real legs
# --------------------------------------------------------------------------
def test_the_operator_table_takes_the_modal_capacity():
    """The capacity column moves a little with saleable seats, so the mode
    picks the real layout where a mean would land between two of them."""
    legs = [_Leg("ATR-72-600", 72)] * 9 + [_Leg("ATR-72-600", 66),
                                           _Leg("ATR-72-600", 78)]
    assert seats.operator_table(legs) == {"atr72600": 72}


def test_the_operator_table_keeps_types_apart():
    legs = ([_Leg("ATR-72-600", 72)] * 3
            + [_Leg("Boeing 737-800", 189)] * 3
            + [_Leg("Airbus A330-300", 436)] * 3)
    table = seats.operator_table(legs)
    assert set(table.values()) == {72, 189, 436}


def test_legs_with_no_aircraft_are_skipped():
    legs = [_Leg("", 72), _Leg(None, 189), _Leg("ATR-72-600", 72)]
    assert seats.operator_table(legs) == {"atr72600": 72}


def test_the_estimate_prints_its_confidence():
    got = seats.seats_for("DH8", "BG")
    assert str(got) == "74 (airline)"


def test_a_bare_family_is_used_only_when_no_variant_is_given():
    """The feed writes 'Boeing-787' with no variant for some carriers.
    Dropping those lost 32 flights from the share; a bare family is a
    weaker answer than '787-9' but far better than a gap."""
    assert seats.seats_for("Boeing-787").seats == 290
    assert seats.seats_for("Boeing 787-9").seats == 296
    assert seats.seats_for("Boeing 787-9", "BG").seats == 298


def test_a_variant_always_beats_the_bare_family():
    assert seats.seats_for("Boeing 737-800").seats == 189   # not the bare 180
    assert seats.seats_for("Airbus A330-300").seats == 277  # not the bare 280
