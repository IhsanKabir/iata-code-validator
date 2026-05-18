"""Tests for oep_client.

Network-free: exercises the HTML/JSON parsing + aggregation against captured
snippets that match the live response shape on oep.gov.bd as of 2026-05.
"""

import pytest

from src.oep_client import (
    CountryClearance,
    DivisionClearance,
    Option,
    aggregate_by_category,
    aggregate_by_country,
    aggregate_by_division,
    merge_gender_breakdowns,
    parse_division_table,
    parse_form_options,
    _validate_date,
)


# ---------------------------------------------------------------------------
# parse_division_table
# ---------------------------------------------------------------------------


DIVISION_TABLE_HTML = """
<table>
<tbody>
    <tr>
        <td>1</td>
        <td>Chattagram</td>
        <td>Comilla</td>
        <td>33,371</td>
    </tr>
    <tr>
        <td>2</td>
        <td>Dhaka</td>
        <td>Tangail</td>
        <td>16672</td>
    </tr>
    <tr>
        <td>3</td>
        <td>Sylhet</td>
        <td>Sylhet</td>
        <td>9,561</td>
    </tr>
</tbody>
</table>
"""


def test_parse_division_table_basic():
    rows = parse_division_table(DIVISION_TABLE_HTML)
    assert len(rows) == 3
    assert rows[0] == DivisionClearance("Chattagram", "Comilla", 33371)
    # Handles unquoted thousands separators
    assert rows[1].total_employee == 16672
    assert rows[2].district == "Sylhet"


def test_parse_division_table_handles_3col_layout():
    """Tolerate the no-SL-column variant."""
    html = (
        "<tbody>"
        "<tr><td>Dhaka</td><td>Dhaka</td><td>13858</td></tr>"
        "</tbody>"
    )
    rows = parse_division_table(html)
    assert rows == [DivisionClearance("Dhaka", "Dhaka", 13858)]


def test_parse_division_table_skips_unparseable_count():
    """A row whose count column isn't an integer is dropped, not raised."""
    html = (
        "<tbody>"
        "<tr><td>1</td><td>Dhaka</td><td>Dhaka</td><td>n/a</td></tr>"
        "<tr><td>2</td><td>Dhaka</td><td>Tangail</td><td>1500</td></tr>"
        "</tbody>"
    )
    rows = parse_division_table(html)
    assert len(rows) == 1
    assert rows[0].district == "Tangail"


def test_parse_division_table_empty_html():
    assert parse_division_table("<html></html>") == []


# ---------------------------------------------------------------------------
# parse_form_options
# ---------------------------------------------------------------------------


FORM_OPTIONS_HTML = """
<select id="country_name" name="country_name[]">
  <option value="2">Albania</option>
  <option value="9">Australia</option>
  <option value="154">Saudi Arabia</option>
</select>
<select id="division_name" name="division_name">
  <option value="">All</option>
  <option value="1">Chattagram</option>
  <option value="6">Dhaka</option>
</select>
<select name="gender_id" id="gender_id">
  <option value="">All</option>
  <option value="1">Male</option>
  <option value="2">Female</option>
</select>
"""


def test_parse_form_options_groups_by_select_id():
    out = parse_form_options(FORM_OPTIONS_HTML)
    assert Option("154", "Saudi Arabia") in out["country_name"]
    assert Option("6", "Dhaka") in out["division_name"]
    assert Option("2", "Female") in out["gender_id"]


def test_parse_form_options_drops_empty_value_placeholder():
    """The 'All' option (value='') should not appear in the parsed list."""
    out = parse_form_options(FORM_OPTIONS_HTML)
    assert all(o.value != "" for o in out["division_name"])
    assert all(o.label != "All" for o in out["gender_id"])


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


COUNTRY_ROWS = [
    CountryClearance(154, "Saudi Arabia", "Loading Unloading Labour", 68064),
    CountryClearance(154, "Saudi Arabia", "Warehouse Worker", 47174),
    CountryClearance(159, "Singapore", "A Builder", 11299),
    CountryClearance(144, "Qatar", "Driver", 5080),
    CountryClearance(154, "Saudi Arabia", "Driver", 1200),
]


def test_aggregate_by_country_sums_and_sorts():
    totals = aggregate_by_country(COUNTRY_ROWS)
    assert totals[0].country_name == "Saudi Arabia"
    assert totals[0].total_employee == 68064 + 47174 + 1200
    # Saudi has 3 categories, Singapore 1, Qatar 1
    assert totals[0].category_count == 3
    assert [t.country_name for t in totals] == ["Saudi Arabia", "Singapore", "Qatar"]


def test_aggregate_by_country_empty():
    assert aggregate_by_country([]) == []


def test_aggregate_by_category_skips_blank_categories():
    rows = [
        CountryClearance(1, "X", "", 100),
        CountryClearance(1, "X", "Driver", 50),
        CountryClearance(2, "Y", "Driver", 30),
    ]
    out = aggregate_by_category(rows)
    assert len(out) == 1
    assert out[0].category_name == "Driver"
    assert out[0].total_employee == 80
    assert out[0].country_count == 2


def test_aggregate_by_division_counts_distinct_districts():
    rows = [
        DivisionClearance("Dhaka", "Dhaka", 10),
        DivisionClearance("Dhaka", "Tangail", 20),
        DivisionClearance("Dhaka", "Dhaka", 5),   # dup district
        DivisionClearance("Sylhet", "Sylhet", 7),
    ]
    out = aggregate_by_division(rows)
    assert out[0].division == "Dhaka"
    assert out[0].total_employee == 35
    assert out[0].district_count == 2  # Dhaka + Tangail, deduped
    assert out[1].division == "Sylhet"


def test_merge_gender_breakdowns_basic():
    all_rows = [
        CountryClearance(1, "Saudi Arabia", "X", 100),
        CountryClearance(1, "Saudi Arabia", "Y", 50),
        CountryClearance(2, "UAE", "X", 80),
    ]
    male_rows = [CountryClearance(1, "Saudi Arabia", "X", 130)]
    female_rows = [
        CountryClearance(1, "Saudi Arabia", "Y", 15),
        CountryClearance(2, "UAE", "X", 30),
    ]
    out = merge_gender_breakdowns(all_rows, male_rows, female_rows)
    saudi = next(g for g in out if g.country_name == "Saudi Arabia")
    assert saudi.total == 150
    assert saudi.male == 130
    assert saudi.female == 15
    # Other = total - male - female, never negative
    assert saudi.other == 5
    uae = next(g for g in out if g.country_name == "UAE")
    assert uae.male == 0
    assert uae.female == 30
    assert uae.other == 50
    # Sorted by total desc
    assert out[0].country_name == "Saudi Arabia"


def test_merge_gender_breakdowns_clamps_negative_other_to_zero():
    """If male+female > total (rounding/race in source data), other = 0."""
    all_rows = [CountryClearance(1, "X", "a", 100)]
    male_rows = [CountryClearance(1, "X", "a", 70)]
    female_rows = [CountryClearance(1, "X", "a", 40)]  # 70+40 > 100
    out = merge_gender_breakdowns(all_rows, male_rows, female_rows)
    assert out[0].other == 0


# ---------------------------------------------------------------------------
# Date validation
# ---------------------------------------------------------------------------


def test_validate_date_accepts_iso():
    _validate_date("2026-05-18", "x")  # should not raise


@pytest.mark.parametrize("bad", ["", "2026/05/18", "26-05-18", "2026-13-01", "nope"])
def test_validate_date_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        _validate_date(bad, "x")


# ---------------------------------------------------------------------------
# Time-series helpers
# ---------------------------------------------------------------------------


def test_iter_year_months_inclusive():
    from src.oep_client import iter_year_months
    assert iter_year_months("2024-11-01", "2025-02-15") == [
        "2024-11", "2024-12", "2025-01", "2025-02",
    ]


def test_iter_year_months_single_month():
    from src.oep_client import iter_year_months
    assert iter_year_months("2025-03-01", "2025-03-31") == ["2025-03"]


def test_iter_year_months_handles_year_rollover():
    from src.oep_client import iter_year_months
    out = iter_year_months("2023-11-01", "2024-02-01")
    assert out == ["2023-11", "2023-12", "2024-01", "2024-02"]


def test_iter_year_months_empty_when_reversed():
    from src.oep_client import iter_year_months
    assert iter_year_months("2025-06-01", "2025-01-01") == []


def test_pivot_timeseries_fills_missing_cells_with_zero():
    from src.oep_client import MonthlyTotal, pivot_timeseries
    rows = [
        MonthlyTotal("2025-01", "Saudi Arabia", 100),
        MonthlyTotal("2025-02", "Saudi Arabia", 150),
        MonthlyTotal("2025-02", "Qatar", 20),  # Qatar missing 2025-01
    ]
    months, series = pivot_timeseries(rows)
    assert months == ["2025-01", "2025-02"]
    assert series["Saudi Arabia"] == [100, 150]
    assert series["Qatar"] == [0, 20]


# ---------------------------------------------------------------------------
# Country × Division pivot helpers
# ---------------------------------------------------------------------------


def test_pivot_country_division_preserves_country_order():
    from src.oep_client import CountryDivisionCell, pivot_country_division
    cells = [
        CountryDivisionCell("Saudi Arabia", "Dhaka", 1000),
        CountryDivisionCell("Saudi Arabia", "Chattagram", 800),
        CountryDivisionCell("Qatar", "Dhaka", 300),
        CountryDivisionCell("Qatar", "Sylhet", 50),
    ]
    divisions, countries, table = pivot_country_division(cells)
    # Divisions sorted alphabetically
    assert divisions == ["Chattagram", "Dhaka", "Sylhet"]
    # Countries in first-seen order (matches user's listbox order)
    assert countries == ["Saudi Arabia", "Qatar"]
    assert table[("Dhaka", "Saudi Arabia")] == 1000
    assert table[("Sylhet", "Qatar")] == 50
    # Missing cells are simply absent (renderer fills as "—")
    assert ("Sylhet", "Saudi Arabia") not in table


# ---------------------------------------------------------------------------
# Category drilldown
# ---------------------------------------------------------------------------


def test_categories_for_country_filters_and_sorts():
    from src.oep_client import categories_for_country
    rows = [
        CountryClearance(154, "Saudi Arabia", "Driver", 100),
        CountryClearance(154, "Saudi Arabia", "Driver", 50),     # same category — sums
        CountryClearance(154, "Saudi Arabia", "Cook", 200),
        CountryClearance(144, "Qatar", "Driver", 5000),          # other country — excluded
        CountryClearance(154, "Saudi Arabia", "", 999),          # blank category — excluded
    ]
    out = categories_for_country(rows, "Saudi Arabia")
    assert [c.category_name for c in out] == ["Cook", "Driver"]
    assert out[0].total_employee == 200
    assert out[1].total_employee == 150
