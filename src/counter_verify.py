"""Check the PNRs a counter wrote that the sales report has no match for.

The comparison already finds them -- 110 rows worth 2.3m BDT in August -- but
it can only say "the system has no such sale", which is the least useful true
thing to say about them. A counter reading that has no idea whether it typed
the code wrong, claimed a colleague's sale, or was right and the export is
short.

Most of the answer is already on hand. 85 of those 110 PNRs ARE in the month's
sales, just under another counter or another day, and matching them costs
nothing. Only what is genuinely nowhere needs asking Zenith about, which is
what makes the live check affordable: 25 lookups instead of 110.

Nothing here changes a figure. It explains one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# what the check concluded about one written PNR
SOLD_ELSEWHERE = "ANOTHER COUNTER'S SALE"
OTHER_DAY = "SYSTEM HAS ANOTHER DAY"
WROTE_IT_TWICE = "WRITTEN MORE TIMES THAN IT EXISTS"
NO_SUCH_PNR = "NO SUCH PNR"
VOIDED = "VOIDED OR CANCELLED"
MISSING_FROM_SALES = "REAL — MISSING FROM THE SALES REPORT"
UNCHECKED = "NOT CHECKED"
LOOKUP_FAILED = "COULD NOT BE CHECKED"

# a Zenith status that means the booking did not stand
_DEAD = ("cancel", "void", "refund", "deleted")

ORDER = (SOLD_ELSEWHERE, MISSING_FROM_SALES, NO_SUCH_PNR, VOIDED,
         WROTE_IT_TWICE, OTHER_DAY, LOOKUP_FAILED, UNCHECKED)


@dataclass
class PNRCheck:
    """One written PNR and what became of it."""
    counter: str
    locator: str
    block: str
    day: date | None
    reported_amount: float
    verdict: str
    detail: str = ""
    pos: str = ""                 # where the system actually has it
    sys_counter: str = ""         # and which counter that is, if we know
    sys_day: date | None = None
    sys_amount: float = 0.0
    zenith_status: str = ""
    zenith_amount: str = ""
    customer: str = ""

    @property
    def resolved(self) -> bool:
        """Did the check reach an answer, rather than run out of road?"""
        return self.verdict not in (UNCHECKED, LOOKUP_FAILED)


@dataclass
class VerifyResult:
    checks: list = field(default_factory=list)
    looked_up: int = 0            # how many live calls were actually made
    lookups_capped: bool = False

    def of(self, verdict) -> list:
        return [c for c in self.checks if c.verdict == verdict]

    @property
    def resolved(self) -> int:
        return sum(1 for c in self.checks if c.resolved)

    def amount(self, verdict) -> float:
        return sum(c.reported_amount for c in self.of(verdict))


def _index_by_locator(sales) -> dict:
    out: dict[str, list] = {}
    for ln in sales.lines:
        code = (ln.locator or "").strip().upper()
        if code:
            out.setdefault(code, []).append(ln)
    return out


def _from_the_sales_report(f, hits, mapped_pos, pos_to_counter,
                           spare) -> PNRCheck:
    """The PNR is in the month's sales -- under whose name, and on what day?"""
    same = [h for h in hits if h.pos == mapped_pos]
    if same:
        days = sorted({h.day for h in same})
        shown = ", ".join(f"{d:%d %b}" for d in days[:3])
        # A PNR the system has once and the sheet has twice is not a counter
        # that mis-dated a sale -- it is the same sale written down again. The
        # two need separating: one is a typo, the other inflates the day.
        if not spare:
            return PNRCheck(
                counter=f.counter, locator=f.locator, block=f.block, day=f.day,
                reported_amount=f.reported_amount, verdict=WROTE_IT_TWICE,
                pos=mapped_pos, sys_counter=f.counter, sys_day=days[0],
                sys_amount=same[0].amount,
                detail=(f"the system has this PNR {len(hits)} time(s) and every "
                        f"one is already accounted for by another row"))
        return PNRCheck(
            counter=f.counter, locator=f.locator, block=f.block, day=f.day,
            reported_amount=f.reported_amount, verdict=OTHER_DAY,
            pos=mapped_pos, sys_counter=f.counter, sys_day=days[0],
            sys_amount=same[0].amount,
            detail=f"same counter, system dates it {shown}")
    other = hits[0]
    who = pos_to_counter.get(other.pos, "")
    return PNRCheck(
        counter=f.counter, locator=f.locator, block=f.block, day=f.day,
        reported_amount=f.reported_amount, verdict=SOLD_ELSEWHERE,
        pos=other.pos, sys_counter=who, sys_day=other.day,
        sys_amount=other.amount,
        detail=(f"the system has it at {other.pos}"
                + (f" ({who})" if who and who != f.counter else "")
                + f" on {other.day:%d %b}"))


def verify_unmatched(res, sales, *, session=None, lookup=None,
                     max_lookups: int = 200, stop_flag=None,
                     progress_cb=None) -> VerifyResult:
    """Explain every PNR a counter wrote that the comparison could not match.

    `session`/`lookup` are optional: without them the sales report alone still
    explains most of the rows, and the rest are reported as unchecked rather
    than guessed at.
    """
    from .counter_reconcile import NOT_IN_SYSTEM

    out = VerifyResult()
    findings = res.of(NOT_IN_SYSTEM)
    if not findings:
        return out

    index = _index_by_locator(sales)
    pos_to_counter = {p: c for c, p in (res.mapping or {}).items()}
    # How many system lines exist for each PNR, and how many the comparison has
    # already paired off. What is left over is what a further row could be.
    from .counter_reconcile import BLOCK_MISMATCH, DATE_SHIFTED, MATCHED
    sys_n: dict = {}
    for ln in sales.lines:
        key = (ln.pos, (ln.locator or "").upper())
        sys_n[key] = sys_n.get(key, 0) + 1
    claimed: dict = {}
    for f in res.findings:
        if f.kind in (MATCHED, DATE_SHIFTED, BLOCK_MISMATCH):
            key = (f.pos, (f.locator or "").upper())
            claimed[key] = claimed.get(key, 0) + 1

    pending: dict[str, list] = {}      # locator -> checks awaiting a lookup
    for f in findings:
        code = (f.locator or "").strip().upper()
        mapped = res.mapping.get(f.counter, f.pos or "")
        hits = index.get(code) if code else None
        if hits:
            key = (mapped, code)
            spare = sys_n.get(key, 0) - claimed.get(key, 0)
            out.checks.append(_from_the_sales_report(f, hits, mapped,
                                                     pos_to_counter, spare))
            claimed[key] = claimed.get(key, 0) + 1     # this row takes one
            continue
        check = PNRCheck(
            counter=f.counter, locator=f.locator, block=f.block, day=f.day,
            reported_amount=f.reported_amount, verdict=UNCHECKED,
            detail="not in this month's sales at all")
        out.checks.append(check)
        if code:
            pending.setdefault(code, []).append(check)

    if not pending or session is None or lookup is None:
        return out

    codes = sorted(pending)
    out.lookups_capped = len(codes) > max_lookups
    codes = codes[:max_lookups]
    for n, code in enumerate(codes, start=1):
        if stop_flag is not None and stop_flag():
            break
        if progress_cb is not None:
            progress_cb(n, len(codes), code)
        out.looked_up += 1
        try:
            d = lookup(session, code)
        except Exception as exc:                      # noqa: BLE001
            # A PNR that cannot be read is reported as unread, never as absent:
            # calling a failed lookup "no such PNR" would accuse a counter of
            # inventing a sale because the network blinked.
            name = type(exc).__name__
            if "NotFound" in name:
                for c in pending[code]:
                    c.verdict = NO_SUCH_PNR
                    c.detail = "Zenith has no booking with this code"
                continue
            for c in pending[code]:
                c.verdict = LOOKUP_FAILED
                c.detail = f"lookup failed: {name}"
            continue
        if d is None:
            for c in pending[code]:
                c.verdict = NO_SUCH_PNR
                c.detail = "Zenith has no booking with this code"
            continue
        status = str(getattr(d, "pnr_status", "") or "")
        amount = str(getattr(d, "total_amount", "") or "")
        customer = str(getattr(d, "customer_name", "") or "")
        # "the sales report is the one missing it" is the strongest claim here:
        # it says the counter was right and the export is short. It must rest
        # on a booking we actually read, not on a reply we could not recognise.
        if not any((status, amount, customer,
                    str(getattr(d, "pnr_code", "") or ""),
                    str(getattr(d, "dossier_id", "") or ""))):
            for c in pending[code]:
                c.verdict = LOOKUP_FAILED
                c.detail = "the lookup returned nothing recognisable"
            continue
        dead = any(w in status.lower() for w in _DEAD)
        for c in pending[code]:
            c.zenith_status = status
            c.zenith_amount = amount
            c.customer = customer
            if dead:
                c.verdict = VOIDED
                c.detail = (f"Zenith says {status} — the counter counted a "
                            f"sale that did not stand")
            else:
                c.verdict = MISSING_FROM_SALES
                c.detail = (f"Zenith has it as {status or 'a live booking'}"
                            + (f" for {amount}" if amount else "")
                            + " — the sales report is the one missing it")
    return out


def summarise(vres: VerifyResult) -> list:
    """(verdict, rows, value) worst first, for a heading strip."""
    rows = []
    for v in ORDER:
        got = vres.of(v)
        if got:
            rows.append((v, len(got), sum(c.reported_amount for c in got)))
    return rows


# --------------------------------------------------------------------------
# the sheet
# --------------------------------------------------------------------------
_TONE = {
    SOLD_ELSEWHERE: "C00000",
    WROTE_IT_TWICE: "C00000",
    NO_SUCH_PNR: "C00000",
    MISSING_FROM_SALES: "1F6F3C",
    VOIDED: "BF8F00",
    OTHER_DAY: "BF8F00",
    LOOKUP_FAILED: "808080",
    UNCHECKED: "808080",
}

_MEANS = {
    SOLD_ELSEWHERE:
        "the sheet claims a sale the system records at another point of sale",
    WROTE_IT_TWICE:
        "the same PNR written down more times than the system has it — this "
        "inflates the counter's day",
    OTHER_DAY:
        "the right sale on the wrong date; the value is real, the day is not",
    NO_SUCH_PNR:
        "Zenith has no such booking — the code was written wrong, or the sale "
        "never happened",
    VOIDED:
        "the booking was cancelled, voided or refunded, so it should not be "
        "counted as a sale",
    MISSING_FROM_SALES:
        "the counter was right: Zenith has the booking and the sales report "
        "does not",
    LOOKUP_FAILED: "the lookup could not complete, so nothing is concluded",
    UNCHECKED: "not in this month's sales; tick the Zenith box to check it",
}


def write_pnr_check(ws, vres: VerifyResult, *, month: int, year: int,
                    base_currency: str = "BDT", limit: int = 600) -> None:
    """Render what became of every PNR the comparison could not match."""
    from .counter_master import (BAD, COLS, GOOD, GREY, LAST, MONEY, NAVY,
                                 PAPER, WARN, _band, _cell, _headers,
                                 _kpi_strip)

    for col, width in COLS:
        ws.column_dimensions[col].width = width
    ws.sheet_view.showGridLines = False

    ws.merge_cells(f"A1:{LAST}1")
    _cell(ws, 1, 1,
          "  PNR CHECK  ·  what became of the sales the counters wrote that "
          "the system has no match for",
          bold=True, size=16, color="FFFFFF", fill=NAVY, align="left")
    ws.row_dimensions[1].height = 34
    ws.merge_cells(f"A2:{LAST}2")
    _cell(ws, 2, 1,
          f"  {len(vres.checks):,} written PNR(s) had no matching sale. "
          f"{vres.resolved:,} are explained from the sales report itself, at no "
          f"cost. "
          + (f"{vres.looked_up:,} were then looked up live in Zenith."
             if vres.looked_up else
             "The rest need a live Zenith lookup — tick 'Look up unmatched "
             "PNRs' and run again.")
          + ("  Only the first batch was looked up; run again for the rest."
             if vres.lookups_capped else "")
          + "  Nothing here changes a figure elsewhere in this workbook; it "
            "explains one.",
          size=9, color=GREY, fill=PAPER, align="left")
    ws.row_dimensions[2].height = 17

    rows = summarise(vres)
    r = 4
    r = _band(ws, r, "AT A GLANCE", "worst first")
    r = _kpi_strip(ws, r, [(v, n, "#,##0", _TONE.get(v))
                           for v, n, _a in rows[:7]] or
                   [("Nothing to check", 0, "0", None)])
    r = _kpi_strip(ws, r, [(f"{v} ({base_currency})", a, MONEY, _TONE.get(v))
                           for v, _n, a in rows[:7]] or
                   [("", 0, MONEY, None)])
    r += 1

    r = _band(ws, r, "WHAT EACH VERDICT MEANS")
    for v, n, amt in rows:
        _cell(ws, r, 1, v, bold=True, size=9, border=True,
              color=_TONE.get(v))
        _cell(ws, r, 2, n, size=9, border=True, align="center")
        _cell(ws, r, 3, amt or None, fmt=MONEY, size=9, border=True,
              align="right")
        ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=21)
        _cell(ws, r, 4, _MEANS.get(v, ""), size=8, color=GREY, border=True)
        r += 1
    r += 1

    r = _band(ws, r, "EVERY UNMATCHED PNR", f"largest first, top {limit}")
    hdr = r
    r = _headers(ws, r, [
        "Counter", "Written on", "PNR", "Type", "Written value", "Verdict",
        "What the check found", "System point of sale", "System counter",
        "System date", "System value", "Zenith status", "Zenith value",
        "Customer"] + [""] * 7)
    for c in sorted(vres.checks, key=lambda c: -c.reported_amount)[:limit]:
        _cell(ws, r, 1, c.counter, bold=True, size=9, border=True)
        _cell(ws, r, 2, f"{c.day:%d %b}" if c.day else "", size=9, border=True,
              align="center")
        _cell(ws, r, 3, c.locator, bold=True, size=10, border=True)
        _cell(ws, r, 4, c.block.title(), size=9, border=True, align="center")
        _cell(ws, r, 5, c.reported_amount or None, fmt=MONEY, size=10,
              border=True, align="right")
        _cell(ws, r, 6, c.verdict, bold=True, size=9, border=True,
              align="center", color="FFFFFF",
              fill=(BAD if _TONE.get(c.verdict) == "C00000"
                    else GOOD if _TONE.get(c.verdict) == "1F6F3C"
                    else WARN if _TONE.get(c.verdict) == "BF8F00" else None))
        _cell(ws, r, 7, c.detail, size=8, color=GREY, border=True)
        _cell(ws, r, 8, c.pos or None, size=8, border=True)
        _cell(ws, r, 9, c.sys_counter or None, size=8, border=True)
        _cell(ws, r, 10, f"{c.sys_day:%d %b}" if c.sys_day else None, size=8,
              border=True, align="center")
        _cell(ws, r, 11, c.sys_amount or None, fmt=MONEY, size=9, border=True,
              align="right")
        _cell(ws, r, 12, c.zenith_status or None, size=8, border=True)
        _cell(ws, r, 13, c.zenith_amount or None, size=8, border=True)
        _cell(ws, r, 14, (c.customer or "")[:40] or None, size=8, color=GREY,
              border=True)
        for j in range(15, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    if vres.checks:
        ws.auto_filter.ref = f"A{hdr}:N{r - 1}"
    ws.freeze_panes = f"A{hdr + 1}"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"
