"""Settling the PNRs a counter wrote that the system has no match for."""
from datetime import date

import pytest

from src import counter_reconcile as cr
from src import counter_verify as cv


class _Line:
    def __init__(self, pos, locator, day, amount=1000.0, block="ISSUE"):
        self.pos, self.locator, self.day = pos, locator, day
        self.amount, self.block = amount, block
        self.agent = self.customer = ""


class _Sales:
    def __init__(self, lines):
        self.lines = lines


def _res(findings, mapping):
    res = cr.ReconResult(mapping=dict(mapping))
    res.findings = list(findings)
    return res


def _wrote(counter, locator, amount=1000.0, day=date(2026, 8, 5),
           block="ISSUE"):
    """A row the counter wrote that the comparison could not match."""
    return cr.Finding(cr.NOT_IN_SYSTEM, counter, "", day, locator, block,
                      0.0, amount)


def test_a_pnr_the_system_has_at_another_counter_is_named(tmp_path):
    """The counter claims a sale the system records somewhere else. Saying
    'no such sale' hides that; saying whose it is does not."""
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote("Uttara", "0A1111", 5000)],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New"})

    out = cv.verify_unmatched(res, sales)
    (check,) = out.checks
    assert check.verdict == cv.SOLD_ELSEWHERE
    assert check.pos == "DAC-16 Banani New"
    assert check.sys_counter == "Banani"        # named, not just the POS code
    assert "07 Aug" in check.detail
    assert out.resolved == 1
    assert out.looked_up == 0                   # answered for free


def test_the_same_counter_on_another_day_is_a_date_not_a_phantom():
    sales = _Sales([_Line("DAC-09 Uttara", "0A2222", date(2026, 8, 9))])
    res = _res([_wrote("Uttara", "0A2222")], {"Uttara": "DAC-09 Uttara"})

    (check,) = cv.verify_unmatched(res, sales).checks
    assert check.verdict == cv.OTHER_DAY
    assert check.sys_day == date(2026, 8, 9)


def test_a_pnr_written_more_times_than_it_exists_is_called_that():
    """One system line, two written rows. The second is not a mis-dated sale,
    it is the same sale counted again -- which inflates the counter's day."""
    sales = _Sales([_Line("DAC-09 Uttara", "0A3333", date(2026, 8, 9))])
    matched = cr.Finding(cr.MATCHED, "Uttara", "DAC-09 Uttara",
                         date(2026, 8, 9), "0A3333", "ISSUE", 1000.0, 1000.0)
    res = _res([matched, _wrote("Uttara", "0A3333")],
               {"Uttara": "DAC-09 Uttara"})

    checks = cv.verify_unmatched(res, sales).checks
    assert [c.verdict for c in checks] == [cv.WROTE_IT_TWICE]


def test_two_extra_rows_do_not_both_claim_the_one_spare_line():
    """A second unmatched row must not be told the same free line is waiting
    for it -- the first one already took it."""
    sales = _Sales([_Line("DAC-09 Uttara", "0A4444", date(2026, 8, 9)),
                    _Line("DAC-09 Uttara", "0A4444", date(2026, 8, 10))])
    res = _res([_wrote("Uttara", "0A4444"), _wrote("Uttara", "0A4444"),
                _wrote("Uttara", "0A4444")],
               {"Uttara": "DAC-09 Uttara"})

    verdicts = [c.verdict for c in cv.verify_unmatched(res, sales).checks]
    assert verdicts == [cv.OTHER_DAY, cv.OTHER_DAY, cv.WROTE_IT_TWICE]


def test_without_a_session_the_rest_are_unchecked_not_guessed():
    """A PNR that is nowhere in the month is not thereby a phantom. Without a
    lookup the honest answer is that it was not checked."""
    res = _res([_wrote("Uttara", "0A5555")], {"Uttara": "DAC-09 Uttara"})
    out = cv.verify_unmatched(res, _Sales([]))
    (check,) = out.checks
    assert check.verdict == cv.UNCHECKED
    assert not check.resolved
    assert out.resolved == 0


def test_zenith_settles_what_the_sales_report_cannot():
    class _Details:
        pnr_status = "Issued"
        total_amount = "12,000 BDT"
        customer_name = "A CUSTOMER"

    calls = []

    def lookup(_session, code):
        calls.append(code)
        return _Details()

    res = _res([_wrote("Uttara", "0A6666", 12000)],
               {"Uttara": "DAC-09 Uttara"})
    out = cv.verify_unmatched(res, _Sales([]), session=object(), lookup=lookup)
    (check,) = out.checks
    assert calls == ["0A6666"]
    assert check.verdict == cv.MISSING_FROM_SALES
    assert check.zenith_status == "Issued"
    assert "sales report is the one missing it" in check.detail


@pytest.mark.parametrize("status", ["Cancelled", "Voided", "Refunded"])
def test_a_booking_that_did_not_stand_is_not_a_sale(status):
    class _Details:
        pnr_status = status
        total_amount = ""
        customer_name = ""

    res = _res([_wrote("Uttara", "0A7777")], {"Uttara": "DAC-09 Uttara"})
    out = cv.verify_unmatched(res, _Sales([]), session=object(),
                              lookup=lambda *_a: _Details())
    assert out.checks[0].verdict == cv.VOIDED


def test_a_pnr_zenith_does_not_have_is_named_as_such():
    class PNRNotFoundError(Exception):
        pass

    def lookup(_s, _c):
        raise PNRNotFoundError("nope")

    res = _res([_wrote("Uttara", "0A8888")], {"Uttara": "DAC-09 Uttara"})
    out = cv.verify_unmatched(res, _Sales([]), session=object(), lookup=lookup)
    assert out.checks[0].verdict == cv.NO_SUCH_PNR


def test_a_lookup_that_fails_is_never_reported_as_a_missing_pnr():
    """Calling a network failure 'no such PNR' accuses a counter of inventing
    a sale because the connection blinked."""
    def lookup(_s, _c):
        raise TimeoutError("network")

    res = _res([_wrote("Uttara", "0A9999")], {"Uttara": "DAC-09 Uttara"})
    out = cv.verify_unmatched(res, _Sales([]), session=object(), lookup=lookup)
    check = out.checks[0]
    assert check.verdict == cv.LOOKUP_FAILED
    assert not check.resolved
    assert check.verdict != cv.NO_SUCH_PNR


def test_one_lookup_per_pnr_however_many_rows_wrote_it():
    calls = []

    class _Details:
        pnr_status = "Issued"
        total_amount = ""
        customer_name = ""

    def lookup(_s, code):
        calls.append(code)
        return _Details()

    res = _res([_wrote("Uttara", "0AAAAA"), _wrote("Banani", "0AAAAA")],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New"})
    out = cv.verify_unmatched(res, _Sales([]), session=object(), lookup=lookup)
    assert calls == ["0AAAAA"]             # one call, both rows settled
    assert out.looked_up == 1
    assert all(c.verdict == cv.MISSING_FROM_SALES for c in out.checks)


def test_the_lookup_budget_is_respected_and_reported():
    calls = []

    res = _res([_wrote("Uttara", f"0B{i:04d}") for i in range(5)],
               {"Uttara": "DAC-09 Uttara"})
    out = cv.verify_unmatched(res, _Sales([]), session=object(),
                              lookup=lambda _s, c: calls.append(c),
                              max_lookups=2)
    assert len(calls) == 2
    assert out.lookups_capped


def test_stopping_mid_check_leaves_the_rest_unchecked_not_wrong():
    res = _res([_wrote("Uttara", f"0C{i:04d}") for i in range(4)],
               {"Uttara": "DAC-09 Uttara"})
    out = cv.verify_unmatched(res, _Sales([]), session=object(),
                              lookup=lambda _s, _c: None,
                              stop_flag=lambda: True)
    assert out.looked_up == 0
    assert all(c.verdict == cv.UNCHECKED for c in out.checks)


def test_nothing_to_check_is_not_an_error():
    out = cv.verify_unmatched(_res([], {}), _Sales([]))
    assert out.checks == []
    assert cv.summarise(out) == []


def test_the_check_never_moves_a_figure():
    """It explains a number; it must not restate one."""
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    finding = _wrote("Uttara", "0A1111", 5000)
    res = _res([finding], {"Uttara": "DAC-09 Uttara",
                           "Banani": "DAC-16 Banani New"})
    before = (finding.kind, finding.system_amount, finding.reported_amount,
              finding.note)
    cv.verify_unmatched(res, sales)
    assert (finding.kind, finding.system_amount, finding.reported_amount,
            finding.note) == before


# --- the sheet -------------------------------------------------------------

def test_the_check_gets_its_own_sheet_with_a_verdict_per_row(tmp_path):
    from openpyxl import Workbook

    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote("Uttara", "0A1111", 5000), _wrote("Uttara", "0AZZZZ")],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New"})
    out = cv.verify_unmatched(res, sales)

    wb = Workbook()
    cv.write_pnr_check(wb.active, out, month=8, year=2026)
    text = " | ".join(str(c.value) for row in wb.active.iter_rows()
                      for c in row if c.value)
    assert cv.SOLD_ELSEWHERE in text
    assert "0A1111" in text and "0AZZZZ" in text
    assert "Banani" in text                    # who the system says has it
    # every verdict on the sheet is explained in words somewhere on it
    for verdict, _n, _a in cv.summarise(out):
        assert cv._MEANS[verdict][:30] in text


def test_the_sheet_says_what_is_still_unchecked_rather_than_looking_complete():
    from openpyxl import Workbook

    res = _res([_wrote("Uttara", "0AZZZZ")], {"Uttara": "DAC-09 Uttara"})
    out = cv.verify_unmatched(res, _Sales([]))
    wb = Workbook()
    cv.write_pnr_check(wb.active, out, month=8, year=2026)
    text = " ".join(str(c.value) for row in wb.active.iter_rows()
                    for c in row if c.value)
    assert "need a live Zenith lookup" in text


def test_an_unrecognisable_reply_never_becomes_the_counter_was_right():
    """'The sales report is the one missing it' is the strongest claim the
    check makes. It must rest on a booking that was actually read."""
    for reply in (object(), "nope", 0):
        res = _res([_wrote("Uttara", "0AJUNK", 5000)],
                   {"Uttara": "DAC-09 Uttara"})
        out = cv.verify_unmatched(res, _Sales([]), session=object(),
                                  lookup=lambda _s, _c: reply)
        check = out.checks[0]
        assert check.verdict == cv.LOOKUP_FAILED, reply
        assert not check.resolved
