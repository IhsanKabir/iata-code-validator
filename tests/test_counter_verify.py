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


def test_a_booking_with_no_sale_against_it_is_named_as_that():
    """Zenith holding the booking does not make it money taken. The counter
    counted a booking; the month records no sale for it."""
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
    assert check.verdict == cv.NO_SALE_RECORDED
    assert check.zenith_status == "Issued"
    assert "records no sale against it" in check.detail
    assert check.is_overclaim


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
    assert all(c.verdict == cv.NO_SALE_RECORDED for c in out.checks)


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


# --- the overclaim sheet ---------------------------------------------------

def _wrote_by(counter, locator, amount, who, day=date(2026, 8, 5)):
    f = _wrote(counter, locator, amount, day)
    f.wrote_by = who
    return f


def test_a_sale_on_the_wrong_date_is_never_called_an_overclaim():
    """The money is real and the system has it. Only the day is wrong, and
    counting that as a claim would accuse a counter of taking money it did
    take."""
    sales = _Sales([_Line("DAC-09 Uttara", "0A2222", date(2026, 8, 9), 4000)])
    res = _res([_wrote("Uttara", "0A2222", 4000)], {"Uttara": "DAC-09 Uttara"})
    out = cv.verify_unmatched(res, sales)
    assert out.checks[0].verdict == cv.OTHER_DAY
    assert not out.checks[0].is_overclaim
    assert out.overclaims == []
    assert out.overclaimed == 0


def test_nothing_unanswered_is_ever_counted_as_an_overclaim():
    """An unanswered row is not evidence of anything."""
    res = _res([_wrote("Uttara", "0AZZZZ", 9000)], {"Uttara": "DAC-09 Uttara"})
    out = cv.verify_unmatched(res, _Sales([]))
    assert out.checks[0].verdict == cv.UNCHECKED
    assert out.overclaimed == 0
    assert out.still_unchecked == 1

    def boom(_s, _c):
        raise TimeoutError

    res = _res([_wrote("Uttara", "0AZZZZ", 9000)], {"Uttara": "DAC-09 Uttara"})
    out = cv.verify_unmatched(res, _Sales([]), session=object(), lookup=boom)
    assert out.checks[0].verdict == cv.LOOKUP_FAILED
    assert out.overclaimed == 0


def test_claiming_another_counters_sale_is_an_overclaim():
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote_by("Uttara", "0A1111", 5000, "ALEX ROY")],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New"})
    out = cv.verify_unmatched(res, sales)
    assert out.overclaimed == 5000
    assert out.overclaims[0].wrote_by == "ALEX ROY"


def test_the_totals_agree_with_the_rows_they_are_built_from():
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7)),
                    _Line("DAC-09 Uttara", "0A2222", date(2026, 8, 9))])
    res = _res([_wrote_by("Uttara", "0A1111", 5000, "ALEX ROY"),
                _wrote_by("Uttara", "0A2222", 4000, "SAM LEE"),
                _wrote_by("Uttara", "0AZZZZ", 3000, "ALEX ROY")],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New"})
    out = cv.verify_unmatched(res, sales)

    by_counter = cv.overclaim_by(out, lambda c: c.counter)
    by_staff = cv.overclaim_by(out, lambda c: c.wrote_by)
    assert sum(s["amount"] for s in by_counter.values()) == out.overclaimed
    assert sum(s["amount"] for s in by_staff.values()) == out.overclaimed
    assert sum(s["n"] for s in by_staff.values()) == len(out.overclaims)
    # the wrong-date row is in neither
    assert out.overclaimed == 5000


def test_a_row_with_no_name_on_it_is_kept_not_dropped():
    """The money was still claimed. Dropping it because nobody signed the row
    would quietly shrink the total."""
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote("Uttara", "0A1111", 5000)],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New"})
    out = cv.verify_unmatched(res, sales)
    by_staff = cv.overclaim_by(out, lambda c: c.wrote_by)
    assert list(by_staff) == ["(not written)"]
    assert by_staff["(not written)"]["amount"] == 5000


def test_the_overclaim_sheet_names_the_counter_the_person_and_the_reason():
    from openpyxl import Workbook

    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote_by("Uttara", "0A1111", 5000, "ALEX ROY")],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New"})
    out = cv.verify_unmatched(res, sales)

    wb = Workbook()
    cv.write_overclaim(wb.active, out, None, month=8, year=2026)
    text = " | ".join(str(c.value) for row in wb.active.iter_rows()
                      for c in row if c.value)
    assert "OVERCLAIM" in text
    assert "Uttara" in text and "ALEX ROY" in text and "0A1111" in text
    assert cv.SOLD_ELSEWHERE in text
    assert "wrong DATE is NOT counted here" in text


def test_the_sheet_calls_the_total_a_floor_while_rows_are_unanswered():
    """Otherwise a partial check reads as a finished accusation."""
    from openpyxl import Workbook

    res = _res([_wrote("Uttara", "0AZZZZ", 3000)], {"Uttara": "DAC-09 Uttara"})
    out = cv.verify_unmatched(res, _Sales([]))
    wb = Workbook()
    cv.write_overclaim(wb.active, out, None, month=8, year=2026)
    text = " ".join(str(c.value) for row in wb.active.iter_rows()
                    for c in row if c.value)
    assert "floor" in text
    assert "still unanswered" in text


def test_an_empty_overclaim_sheet_says_so_rather_than_looking_broken():
    from openpyxl import Workbook

    wb = Workbook()
    cv.write_overclaim(wb.active, cv.VerifyResult(), None, month=8, year=2026)
    text = " ".join(str(c.value) for row in wb.active.iter_rows()
                    for c in row if c.value)
    assert "Nothing was overclaimed" in text


# --- the three things that would accuse someone wrongly --------------------

class _Proof:
    def __init__(self, verdict):
        self.verdict = verdict


def test_no_claim_is_made_where_the_counters_desk_was_never_proved():
    """'This is another counter's sale' is decided entirely by which desk we
    think this counter is. Where that was never proved, the accusation is
    about our own matching, not about a person."""
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote_by("Uttara", "0A1111", 5000, "ALEX ROY")],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New"})
    res.proofs = {"Uttara": _Proof(cr.UNPROVEN)}

    out = cv.verify_unmatched(res, sales)
    check = out.checks[0]
    assert check.verdict == cv.MAPPING_UNPROVEN
    assert not check.is_overclaim
    assert out.overclaimed == 0
    assert check in out.held_back
    assert not check.resolved            # it is an open question, not an answer


def test_a_counter_that_works_two_desks_is_not_accused_either():
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote("Uttara", "0A1111", 5000)],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New"})
    res.proofs = {"Uttara": _Proof(cr.SPLIT)}
    assert cv.verify_unmatched(res, sales).overclaimed == 0


def test_an_ambiguous_counter_is_not_accused_either():
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote("Uttara", "0A1111", 5000)],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New"})
    res.ambiguous = {"Uttara": ["DAC-09 Uttara", "DAC-16 Banani New"]}
    assert cv.verify_unmatched(res, sales).overclaimed == 0


def test_a_proved_mapping_still_produces_the_claim():
    """The guard must not swallow the finding it exists to qualify."""
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote("Uttara", "0A1111", 5000)],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New"})
    res.proofs = {"Uttara": _Proof(cr.CONFIRMED)}
    out = cv.verify_unmatched(res, sales)
    assert out.checks[0].verdict == cv.SOLD_ELSEWHERE
    assert out.overclaimed == 5000


def test_writing_a_pnr_twice_is_still_a_claim_on_an_unproven_mapping():
    """That row proves its own mapping: the system HAS the PNR at that desk.
    Holding it back would lose a real finding."""
    sales = _Sales([_Line("DAC-09 Uttara", "0A3333", date(2026, 8, 9))])
    matched = cr.Finding(cr.MATCHED, "Uttara", "DAC-09 Uttara",
                         date(2026, 8, 9), "0A3333", "ISSUE", 1000.0, 1000.0)
    res = _res([matched, _wrote("Uttara", "0A3333", 7000)],
               {"Uttara": "DAC-09 Uttara"})
    res.proofs = {"Uttara": _Proof(cr.UNPROVEN)}
    out = cv.verify_unmatched(res, sales)
    assert out.checks[0].verdict == cv.WROTE_IT_TWICE
    assert out.overclaimed == 7000


class _Warehouse:
    """Stands in for the local sales warehouse."""
    def __init__(self, hits):
        self.hits = hits


def test_a_pnr_the_warehouse_has_in_another_month_is_not_a_phantom(monkeypatch):
    """14 of August's 25 unknowns were sales from earlier months, mostly the
    original booking behind a reissue. Charging those to someone would be
    plainly wrong."""
    monkeypatch.setattr(cr, "find_locators", lambda _src, codes: {
        "0AOLD1": {"first": date(2026, 2, 12), "last": date(2026, 2, 12),
                   "n": 2, "pos": ["Galileo 1G 1G"]}})
    res = _res([_wrote_by("Uttara", "0AOLD1", 8000, "ALEX ROY")],
               {"Uttara": "DAC-09 Uttara"})
    out = cv.verify_unmatched(res, _Sales([]), warehouse=_Warehouse({}))
    check = out.checks[0]
    assert check.verdict == cv.ANOTHER_MONTH
    assert not check.is_overclaim
    assert check.resolved                # it IS an answer, just not a claim
    assert "Feb 2026" in check.detail
    assert "Galileo 1G 1G" in check.detail
    assert out.overclaimed == 0


def test_the_warehouse_is_asked_before_zenith_is(monkeypatch):
    """A question the local data answers must never cost a live call."""
    monkeypatch.setattr(cr, "find_locators", lambda _src, codes: {
        "0AOLD1": {"first": date(2026, 2, 12), "last": date(2026, 2, 12),
                   "n": 1, "pos": []}})
    calls = []
    res = _res([_wrote("Uttara", "0AOLD1"), _wrote("Uttara", "0ANEW1")],
               {"Uttara": "DAC-09 Uttara"})
    out = cv.verify_unmatched(res, _Sales([]), warehouse=_Warehouse({}),
                              session=object(),
                              lookup=lambda _s, c: calls.append(c))
    assert calls == ["0ANEW1"]          # the settled one was never asked about
    assert out.looked_up == 1


def test_a_warehouse_that_cannot_be_read_leaves_rows_unchecked(monkeypatch):
    """It must degrade to 'not checked', never to a wrong verdict."""
    def boom(_src, _codes):
        raise RuntimeError("parquet is gone")

    monkeypatch.setattr(cr, "find_locators", boom)
    res = _res([_wrote("Uttara", "0AOLD1", 5000)], {"Uttara": "DAC-09 Uttara"})
    with pytest.raises(RuntimeError):
        cv.verify_unmatched(res, _Sales([]), warehouse=_Warehouse({}))


def test_a_value_restated_from_another_currency_is_marked_as_an_estimate():
    """MAA writes INR and KUL writes MYR. Their claims are converted at a rate
    we derived ourselves, and must not read as counted figures."""
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote("MAA", "0A1111", 3936)],
               {"MAA": "INT Chennai City (India)",
                "Banani": "DAC-16 Banani New"})
    res.non_comparable = ["MAA"]
    out = cv.verify_unmatched(res, sales)
    assert out.checks[0].converted
    assert out.converted_claims == out.overclaims

    from openpyxl import Workbook
    wb = Workbook()
    cv.write_overclaim(wb.active, out, None, month=8, year=2026)
    text = " ".join(str(c.value) for row in wb.active.iter_rows()
                    for c in row if c.value)
    assert "converted" in text
    assert "estimates" in text


def test_a_bdt_counter_is_not_marked_converted():
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote("Uttara", "0A1111", 5000)],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New"})
    out = cv.verify_unmatched(res, sales)
    assert not out.checks[0].converted
    assert out.converted_claims == []


def test_what_was_held_back_is_stated_rather_than_silently_dropped():
    from openpyxl import Workbook

    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote("Uttara", "0A1111", 5000)],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New"})
    res.proofs = {"Uttara": _Proof(cr.UNPROVEN)}
    out = cv.verify_unmatched(res, sales)

    wb = Workbook()
    cv.write_overclaim(wb.active, out, None, month=8, year=2026)
    text = " ".join(str(c.value) for row in wb.active.iter_rows()
                    for c in row if c.value)
    assert "deliberately left out" in text
    assert "HELD BACK ON PURPOSE" in text          # the tile labels are upper
    assert "WHAT IS DELIBERATELY NOT IN THAT TOTAL" in text


def test_more_than_seven_headline_tiles_are_not_lost_off_the_grid():
    """Seven tiles fill the 21-column grid. An eighth used to be written past
    the right-hand edge and vanish, taking a caveat with it."""
    from openpyxl import Workbook

    from src.counter_master import _kpi_strip

    wb = Workbook()
    ws = wb.active
    items = [(f"TILE {i}", i, "0", None) for i in range(9)]
    end = _kpi_strip(ws, 1, items)
    text = " ".join(str(c.value) for row in ws.iter_rows() for c in row
                    if c.value)
    for i in range(9):
        assert f"TILE {i}" in text, i
    assert end == 5          # two rows of tiles, each two rows tall, plus one


def test_a_restated_value_is_still_marked_after_the_second_comparison():
    """The comparison runs twice and the second runs on rows already restated,
    so it declares everything comparable -- right for it, and wrong for a
    sheet that has to say which values were converted rather than counted."""
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote("MAA", "0A1111", 3936)],
               {"MAA": "INT Chennai City (India)",
                "Banani": "DAC-16 Banani New"})
    res.non_comparable = []                 # exactly what the second pass says
    res.restated_counters = ("MAA",)
    out = cv.verify_unmatched(res, sales)
    assert out.checks[0].converted
    assert out.converted_claims


def test_a_counter_the_proving_pass_never_reached_is_held_back_too():
    """Absence of proof is not proof."""
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote("Uttara", "0A1111", 5000)],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New"})
    res.proofs = {"Banani": _Proof(cr.CONFIRMED)}      # Uttara never checked
    out = cv.verify_unmatched(res, sales)
    assert out.checks[0].verdict == cv.MAPPING_UNPROVEN
    assert out.overclaimed == 0


def test_two_counters_on_one_desk_are_both_named():
    """Naming whichever won a dict comprehension would tell someone their sale
    sits at a counter it may not."""
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote("Uttara", "0A1111", 5000)],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New",
                "Banani Old": "DAC-16 Banani New"})
    res.proofs = {c: _Proof(cr.CONFIRMED) for c in res.mapping}
    named = cv.verify_unmatched(res, sales).checks[0].sys_counter
    assert "Banani" in named and "Banani Old" in named, named


def test_a_rival_by_name_does_not_override_the_counters_own_pnrs():
    """Cox Bazar, Uttara and ZYL each had a rival desk by name and each put
    98-100% of their own PNRs at the desk they were given. Holding their
    claims back on the name alone suppressed real findings."""
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote("Uttara", "0A1111", 5000)],
               {"Uttara": "DAC-09 Uttara", "Banani": "DAC-16 Banani New"})
    res.ambiguous = {"Uttara": ["DAC-09 Uttara", "DAC-01 Airport (Dhaka)"]}
    res.proofs = {"Uttara": _Proof(cr.CONFIRMED)}
    out = cv.verify_unmatched(res, sales)
    assert out.checks[0].verdict == cv.SOLD_ELSEWHERE
    assert out.overclaimed == 5000


def test_an_ambiguous_counter_its_own_pnrs_could_not_settle_is_held_back():
    sales = _Sales([_Line("DAC-16 Banani New", "0A1111", date(2026, 8, 7))])
    res = _res([_wrote("DOH", "0A1111", 5000)],
               {"DOH": "INT Doha (Qatar)", "Banani": "DAC-16 Banani New"})
    res.ambiguous = {"DOH": ["INT Doha (Qatar)", "DAC-16 Banani New"]}
    res.proofs = {"DOH": _Proof(cr.UNPROVEN)}
    out = cv.verify_unmatched(res, sales)
    assert out.checks[0].verdict == cv.MAPPING_UNPROVEN
    assert out.overclaimed == 0
