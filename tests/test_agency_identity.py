"""Which Customer IDs are the same agency.

Fixtures mirror what the warehouse actually holds -- above all Be Fresh
Limited, which trades under four accounts, two of them sharing an IATA code
and the others carrying the '0' placeholder.
"""
from src import agency_identity as ai


def _row(cid, name, iata="0", value=0.0):
    return {"customer_id": cid, "customer": name, "iata": iata,
            "value": value}


BE_FRESH = [
    _row("11662412", "Be Fresh Limited (IATA)", "42306773", 289_796_112),
    _row("10000277", "BE FRESH LIMITED", "0", 2_192_526_079),
    _row("10469441", "Be Fresh Limited (IATA)", "42306773", 54_804),
]


# --------------------------------------------------------------------------
# the placeholder that would have fused thousands of agencies into one
# --------------------------------------------------------------------------
def test_the_zero_code_is_not_a_code():
    for placeholder in ("0", "", "  ", "00", "none", "N/A", "-"):
        assert ai.clean_iata(placeholder) == ""


def test_a_real_code_survives():
    assert ai.clean_iata(" 42306773 ") == "42306773"


def test_grouping_is_not_done_on_the_iata_code():
    """2,545 of 4,688 accounts carry '0'. Keying on it would put them all in
    one agency."""
    rows = [_row("1", "Alpha Travels", "0", 100),
            _row("2", "Beta Tours", "0", 200)]
    groups, _ = ai.resolve(rows)
    assert len(groups) == 2


# --------------------------------------------------------------------------
# a business, not an account
# --------------------------------------------------------------------------
def test_be_fresh_comes_back_as_one_agency():
    groups, _ = ai.resolve(BE_FRESH)
    assert len(groups) == 1
    g = next(iter(groups.values()))
    assert g.is_group is True
    assert set(g.ids) == {"11662412", "10000277", "10469441"}


def test_the_group_total_is_the_sum_of_its_accounts():
    groups, _ = ai.resolve(BE_FRESH)
    g = next(iter(groups.values()))
    assert g.value == 289_796_112 + 2_192_526_079 + 54_804


def test_the_group_takes_the_name_of_its_biggest_account():
    groups, _ = ai.resolve(BE_FRESH)
    assert next(iter(groups.values())).name == "BE FRESH LIMITED"


def test_members_are_listed_biggest_first_so_the_plus_expands_usefully():
    groups, _ = ai.resolve(BE_FRESH)
    g = next(iter(groups.values()))
    assert [m[0] for m in g.members] == ["10000277", "11662412", "10469441"]


def test_a_single_account_agency_is_not_marked_as_a_group():
    groups, _ = ai.resolve([_row("9", "Solo Tours", "0", 5)])
    assert next(iter(groups.values())).is_group is False


def test_the_label_says_how_many_accounts_are_folded_in():
    groups, _ = ai.resolve(BE_FRESH)
    assert next(iter(groups.values())).label() == \
        "BE FRESH LIMITED  (3 accounts)"


# --------------------------------------------------------------------------
# a counter is not an agency
# --------------------------------------------------------------------------
def test_a_counter_account_is_skipped_not_folded_into_the_agency():
    """'DAC-07 Baridhara' carries the agency's NAME but is a US-Bangla
    counter. Grouping on the name alone would add it to their total."""
    rows = BE_FRESH + [_row("DAC-07 Baridhara", "BE FRESH LIMITED", "0", 4749)]
    groups, skipped = ai.resolve(rows)
    g = next(iter(groups.values()))
    assert "DAC-07 Baridhara" not in g.ids
    assert skipped == [("DAC-07 Baridhara", "BE FRESH LIMITED")]


def test_account_numbers_are_numeric_and_counters_are_not():
    assert ai.is_agency_account("10000277") is True
    assert ai.is_agency_account("DAC-07 Baridhara") is False
    assert ai.is_agency_account("") is False
    assert ai.is_agency_account(None) is False


# --------------------------------------------------------------------------
# a wrong merge is shown, never silent
# --------------------------------------------------------------------------
def test_two_real_codes_under_one_name_are_flagged():
    rows = [_row("1", "Sky Travels", "11111111", 100),
            _row("2", "Sky Travels", "22222222", 200)]
    g = next(iter(ai.resolve(rows)[0].values()))
    assert g.is_group is True
    assert g.code_conflict is True


def test_one_code_and_a_placeholder_is_not_a_conflict():
    g = next(iter(ai.resolve(BE_FRESH)[0].values()))
    assert g.code_conflict is False
    assert g.iata == "42306773"


# --------------------------------------------------------------------------
# ordering and lookup
# --------------------------------------------------------------------------
def test_groups_come_back_biggest_first():
    rows = [_row("1", "Small Co", "0", 10), _row("2", "Big Co", "0", 900)]
    assert [g.name for g in ai.resolve(rows)[0].values()] == ["Big Co", "Small Co"]


def test_an_account_number_finds_its_agency_exactly():
    groups, _ = ai.resolve(BE_FRESH)
    got = ai.find(groups, "10000277")
    assert len(got) == 1 and got[0].name == "BE FRESH LIMITED"


def test_a_name_returns_candidates_rather_than_guessing():
    """'TRAVELS' matches 1,825 agency names in the real data, so a lookup
    that picked one would be wrong most of the time."""
    rows = [_row("1", "Alpha Travels", "0", 300),
            _row("2", "Beta Travels", "0", 200),
            _row("3", "Gamma Tours", "0", 100)]
    groups, _ = ai.resolve(rows)
    got = ai.find(groups, "travels")
    assert [g.name for g in got] == ["Alpha Travels", "Beta Travels"]


def test_candidates_are_ordered_by_size():
    rows = [_row("1", "Tiny Travels", "0", 1),
            _row("2", "Huge Travels", "0", 999)]
    groups, _ = ai.resolve(rows)
    assert ai.find(groups, "travels")[0].name == "Huge Travels"


def test_a_differently_spelled_name_still_finds_the_agency():
    groups, _ = ai.resolve(BE_FRESH)
    assert ai.find(groups, "be fresh ltd")[0].name == "BE FRESH LIMITED"


def test_searching_a_member_account_name_finds_the_group():
    groups, _ = ai.resolve(BE_FRESH)
    assert ai.find(groups, "Be Fresh Limited (IATA)")[0].name == \
        "BE FRESH LIMITED"


def test_an_empty_search_returns_nothing_rather_than_everything():
    groups, _ = ai.resolve(BE_FRESH)
    assert ai.find(groups, "  ") == []


# --------------------------------------------------------------------------
# the same account fed in twice is still one account
# --------------------------------------------------------------------------
def test_an_account_seen_in_two_windows_is_listed_once():
    """Callers resolve over both windows at once so an agency that changed
    account between them stays one business. Appending blindly listed every
    account twice and doubled the group total."""
    rows = BE_FRESH + BE_FRESH
    g = next(iter(ai.resolve(rows)[0].values()))
    assert sorted(g.ids) == ["10000277", "10469441", "11662412"]
    assert len(g.members) == 3


def test_the_two_windows_are_summed_not_duplicated():
    this_year = [_row("1", "Solo Tours", "0", 100)]
    last_year = [_row("1", "Solo Tours", "0", 40)]
    g = next(iter(ai.resolve(this_year + last_year)[0].values()))
    assert g.value == 140
    assert len(g.members) == 1
