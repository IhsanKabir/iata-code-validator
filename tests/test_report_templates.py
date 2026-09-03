"""The blank forms that make the missing columns exist."""
from datetime import date

import pytest
from openpyxl import load_workbook

from src import report_templates as rt


def _labels(ws):
    return [str(c.value or "") for row in ws.iter_rows() for c in row if c.value]


def test_the_counter_form_asks_for_what_the_report_needs(tmp_path):
    """Four counters record no payment channel, 85% of enquiries name nobody,
    and half the tasks are untimed. The form now asks for all three."""
    out = rt.build_counter_template(tmp_path / "form.xlsx", day=date(2026, 9, 1))
    ws = load_workbook(out).active
    text = " | ".join(_labels(ws))
    for needed in ("Cash *", "bKash / Nagad *", "Card *", "Cheque *",
                   "Bank / Online transfer *", "Received by *",
                   "Time spent (minutes) *", "Sales Amount (BDT) *"):
        assert needed in text, needed
    # all three sales blocks, and the reissue one asks where it came from
    for block in ("Ticket Issue", "Ticket Reissue", "Ticket Refund"):
        assert block in text
    assert "PNR Booked from *" in text


def test_the_visit_form_asks_for_the_three_fields_it_keeps_losing(tmp_path):
    out = rt.build_visit_template(tmp_path / "form.xlsx", day=date(2026, 9, 1))
    ws = load_workbook(out).active
    text = " | ".join(_labels(ws))
    for needed in ("Date & Day *", "Zone *", "Name *",
                   "Productivity / Monthly Sale *", "Name of Agent *"):
        assert needed in text, needed
    # and it says how to write the figure, because a range is discarded
    assert "3-5 lakh" in text


def test_a_required_field_is_visibly_marked(tmp_path):
    out = rt.build_counter_template(tmp_path / "form.xlsx")
    ws = load_workbook(out).active
    required = [c for row in ws.iter_rows() for c in row
                if str(c.value or "").endswith(" *")]
    assert required
    for cell in required[:5]:
        assert cell.fill.fgColor.rgb.endswith(rt.REQUIRED)


def test_the_form_the_counters_get_is_the_layout_they_already_use(tmp_path):
    """A form nobody recognises is a form nobody fills in."""
    out = rt.build_counter_template(tmp_path / "form.xlsx", day=date(2026, 9, 4))
    ws = load_workbook(out).active
    text = " | ".join(_labels(ws))
    assert "Counter Daily Activities of 4 Sep 2026" in text
    for familiar in ("Counter Name", "Action", "Count PNR", "Employee Name *",
                     "Employee ID *", "PNR *", "Customer Mobile No"):
        assert familiar in text, familiar


@pytest.mark.parametrize("build", [rt.build_counter_template,
                                   rt.build_visit_template])
def test_a_form_is_blank_and_ready_to_type_into(tmp_path, build):
    out = build(tmp_path / "form.xlsx")
    ws = load_workbook(out).active
    numbers = [c.value for row in ws.iter_rows() for c in row
               if isinstance(c.value, (int, float))]
    assert not numbers        # nothing pre-filled that someone might trust
    assert ws.freeze_panes    # the header stays put while they scroll
