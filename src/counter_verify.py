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
NO_SALE_RECORDED = "IN ZENITH, NO SALE RECORDED"
MAPPING_UNPROVEN = "COUNTER'S DESK NOT PROVEN"
ANOTHER_MONTH = "SOLD IN ANOTHER MONTH"
UNCHECKED = "NOT CHECKED"
LOOKUP_FAILED = "COULD NOT BE CHECKED"

# a Zenith status that means the booking did not stand
_DEAD = ("cancel", "void", "refund", "deleted")

ORDER = (SOLD_ELSEWHERE, WROTE_IT_TWICE, NO_SUCH_PNR, VOIDED,
         NO_SALE_RECORDED, OTHER_DAY, ANOTHER_MONTH, MAPPING_UNPROVEN,
         LOOKUP_FAILED, UNCHECKED)

# Claiming value the counter is not owed. A sale on the wrong DATE is not in
# here: the money is real and the system has it, only the day is wrong. Nor is
# anything unchecked -- an unanswered row is not evidence of anything.
OVERCLAIM = (SOLD_ELSEWHERE, WROTE_IT_TWICE, NO_SUCH_PNR, VOIDED,
             NO_SALE_RECORDED)


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
    wrote_by: str = ""            # the staff who wrote the row
    converted: bool = False       # the value was restated from another currency

    @property
    def is_overclaim(self) -> bool:
        """Value claimed that the counter is not owed."""
        return self.verdict in OVERCLAIM

    @property
    def resolved(self) -> bool:
        """Did the check reach an answer, rather than run out of road?"""
        return self.verdict not in (UNCHECKED, LOOKUP_FAILED,
                                    MAPPING_UNPROVEN)


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

    @property
    def overclaims(self) -> list:
        return [c for c in self.checks if c.is_overclaim]

    @property
    def overclaimed(self) -> float:
        return sum(c.reported_amount for c in self.overclaims)

    @property
    def converted_claims(self) -> list:
        """Claims whose value we restated from another currency ourselves."""
        return [c for c in self.overclaims if c.converted]

    @property
    def held_back(self) -> list:
        """Rows an accusation was deliberately NOT made about."""
        return [c for c in self.checks
                if c.verdict in (MAPPING_UNPROVEN, ANOTHER_MONTH)]

    @property
    def still_unchecked(self) -> int:
        """Rows no answer was reached for -- the overclaim total is a FLOOR
        until these are settled, and saying so is the difference between a
        number and an accusation."""
        return sum(1 for c in self.checks if not c.resolved)


def _index_by_locator(sales) -> dict:
    out: dict[str, list] = {}
    for ln in sales.lines:
        code = (ln.locator or "").strip().upper()
        if code:
            out.setdefault(code, []).append(ln)
    return out


def _from_the_sales_report(f, hits, mapped_pos, pos_to_counter,
                           spare, unproven=False) -> PNRCheck:
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
                wrote_by=getattr(f, 'wrote_by', ''),
                pos=mapped_pos, sys_counter=f.counter, sys_day=days[0],
                sys_amount=same[0].amount,
                detail=(f"the system has this PNR {len(hits)} time(s) and every "
                        f"one is already accounted for by another row"))
        return PNRCheck(
            counter=f.counter, locator=f.locator, block=f.block, day=f.day,
            reported_amount=f.reported_amount, verdict=OTHER_DAY,
            wrote_by=getattr(f, "wrote_by", ""), pos=mapped_pos, sys_counter=f.counter, sys_day=days[0],
            sys_amount=same[0].amount,
            detail=f"same counter, system dates it {shown}")
    other = hits[0]
    who = pos_to_counter.get(other.pos, "")
    if unproven:
        # "this is another counter's sale" is decided entirely by which desk we
        # think this counter is. Where that was never proved, the accusation is
        # really about our own matching, and it must not be charged to a person.
        return PNRCheck(
            counter=f.counter, locator=f.locator, block=f.block, day=f.day,
            reported_amount=f.reported_amount, verdict=MAPPING_UNPROVEN,
            wrote_by=getattr(f, "wrote_by", ""), pos=other.pos,
            sys_counter=who, sys_day=other.day, sys_amount=other.amount,
            detail=(f"the system has it at {other.pos}, but which desk "
                    f"{f.counter} is was never proved from its own PNRs — "
                    f"settle the mapping before reading this as a claim"))
    return PNRCheck(
        counter=f.counter, locator=f.locator, block=f.block, day=f.day,
        reported_amount=f.reported_amount, verdict=SOLD_ELSEWHERE,
        wrote_by=getattr(f, "wrote_by", ""), pos=other.pos, sys_counter=who, sys_day=other.day,
        sys_amount=other.amount,
        detail=(f"the system has it at {other.pos}"
                + (f" ({who})" if who and who != f.counter else "")
                + f" on {other.day:%d %b}"))


def verify_unmatched(res, sales, *, session=None, lookup=None,
                     warehouse=None, max_lookups: int = 200, stop_flag=None,
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
    # Two counters can be mapped to one desk. Naming only whichever happened to
    # win the dict comprehension would tell someone their sale is at a counter
    # it may not be, so name them all.
    pos_to_counter: dict = {}
    for counter, pos in (res.mapping or {}).items():
        pos_to_counter[pos] = (f"{pos_to_counter[pos]} / {counter}"
                               if pos in pos_to_counter else counter)
    # Counters whose desk was never proved from their own PNRs, or that the
    # matching could not tell apart. Anything decided BY the mapping is held
    # back for these.
    from .counter_reconcile import SPLIT, UNPROVEN
    unproven = {c for c, pr in (res.proofs or {}).items()
                if getattr(pr, "verdict", "") in (UNPROVEN, SPLIT)}
    unproven |= set(res.ambiguous or ())
    if res.proofs:
        # Absence of proof is not proof. A counter the proving pass never
        # reached is held back exactly like one it could not confirm.
        unproven |= set(res.mapping or ()) - set(res.proofs)
    # Counters whose sheet is kept in another currency: their value here was
    # restated by us at a rate we derived, so it is an estimate.
    restated = set(getattr(res, "restated_counters", ()) or ())         | set(res.non_comparable or ())
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
            out.checks.append(_from_the_sales_report(
                f, hits, mapped, pos_to_counter, spare,
                unproven=f.counter in unproven))
            claimed[key] = claimed.get(key, 0) + 1     # this row takes one
            continue
        check = PNRCheck(
            counter=f.counter, locator=f.locator, block=f.block, day=f.day,
            reported_amount=f.reported_amount, verdict=UNCHECKED,
            wrote_by=getattr(f, "wrote_by", ""),
            detail="not in this month's sales at all")
        out.checks.append(check)
        if code:
            pending.setdefault(code, []).append(check)

    for c in out.checks:
        c.converted = c.counter in restated

    # Before calling anything unknown, ask the warehouse whether it holds the
    # PNR in ANY month. A row the counter wrote in August against a booking
    # sold in February is a dating or reissue problem, not a phantom sale --
    # and it is not something to charge to anyone.
    if pending and warehouse is not None:
        from .counter_reconcile import find_locators
        elsewhere = find_locators(warehouse, pending)
        for code, info in elsewhere.items():
            for c in pending.get(code, ()):
                where = ", ".join(info["pos"][:2])
                when = (f"{info['first']:%b %Y}" if info["first"] else "")
                if info["first"] and info["last"] and (
                        info["first"].month != info["last"].month
                        or info["first"].year != info["last"].year):
                    when = f"{info['first']:%b %Y} to {info['last']:%b %Y}"
                c.verdict = ANOTHER_MONTH
                c.pos = info["pos"][0] if info["pos"] else ""
                c.sys_day = info["first"]
                c.detail = (f"the system sold this PNR in {when}"
                            + (f" at {where}" if where else "")
                            + " — outside the month being compared, so it is "
                              "a date or a reissue, not a sale it never had")
        for code in elsewhere:
            pending.pop(code, None)

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
        # Every verdict from here is an accusation -- that the counter counted
        # something it was not owed. It must rest on a booking actually read,
        # never on a reply we could not recognise.
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
                # Zenith holds the BOOKING; the month holds no SALE against it.
                # The counter counted a booking as money taken.
                c.verdict = NO_SALE_RECORDED
                c.detail = (f"Zenith has the booking as "
                            f"{status or 'live'}"
                            + (f" for {amount}" if amount else "")
                            + ", but the month records no sale against it")
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
    NO_SALE_RECORDED: "C00000",
    VOIDED: "BF8F00",
    OTHER_DAY: "BF8F00",
    ANOTHER_MONTH: "BF8F00",
    MAPPING_UNPROVEN: "808080",
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
    NO_SALE_RECORDED:
        "Zenith has the booking but no sale was ever recorded against it — a "
        "booking counted as money taken",
    ANOTHER_MONTH:
        "the system sold this PNR in a different month — a date or a reissue, "
        "not a sale it never had, so it is not charged to anyone",
    MAPPING_UNPROVEN:
        "which desk this counter is was never proved from its own PNRs, so "
        "what looks like another counter's sale may be our matching, not a "
        "claim — settle the mapping first",
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


# --------------------------------------------------------------------------
# the overclaim sheet
# --------------------------------------------------------------------------
def overclaim_by(vres: VerifyResult, key) -> dict:
    """Group the overclaims by whatever `key(check)` returns."""
    out: dict = {}
    for c in vres.overclaims:
        k = key(c) or "(not written)"
        slot = out.setdefault(k, {"n": 0, "amount": 0.0, "reasons": {}})
        slot["n"] += 1
        slot["amount"] += c.reported_amount
        slot["reasons"][c.verdict] = slot["reasons"].get(c.verdict, 0) + 1
    return out


def _reasons(slot) -> str:
    return " · ".join(f"{v.lower()} x{n}" for v, n in
                      sorted(slot["reasons"].items(), key=lambda kv: -kv[1]))


def write_overclaim(ws, vres: VerifyResult, recon=None, *, month: int,
                    year: int, base_currency: str = "BDT",
                    limit: int = 600) -> None:
    """Value the counters wrote down that the system does not owe them.

    Deliberately narrow. A sale on the wrong DATE is not here -- the money is
    real and the system has it, only the day is wrong. Nor is anything the
    check could not answer: an unanswered row is not evidence of anything, and
    the heading says how many are still outstanding so the total reads as the
    floor it is rather than as a finished accusation.
    """
    from .counter_master import (BAD, COLS, GREY, LAST, MONEY, NAVY, PAPER,
                                 PCT, _band, _cell, _headers, _kpi_strip)

    for col, width in COLS:
        ws.column_dimensions[col].width = width
    ws.sheet_view.showGridLines = False

    claims = vres.overclaims
    total = vres.overclaimed
    reported = 0.0
    if recon is not None:
        reported = sum(v.get("rep_amt", 0) for v in recon.per_counter.values())

    ws.merge_cells(f"A1:{LAST}1")
    _cell(ws, 1, 1,
          "  OVERCLAIM  ·  value written down that the system does not owe",
          bold=True, size=16, color="FFFFFF", fill=NAVY, align="left")
    ws.row_dimensions[1].height = 34
    ws.merge_cells(f"A2:{LAST}2")
    _cell(ws, 2, 1,
          f"  {len(claims):,} row(s) worth {total:,.0f} {base_currency}"
          + (f" — {total / reported:.2%} of everything the counters reported"
             if reported else "")
          + ".  A sale on the wrong DATE is NOT counted here: that money is "
            "real and the system has it, only the day is wrong."
          + (f"  {vres.still_unchecked:,} row(s) are still unanswered, so this "
             f"total is a floor rather than a finished figure — tick the "
             f"Zenith box and run again to settle them."
             if vres.still_unchecked else
             "  Every unmatched row was answered.")
          + (f"  {len(vres.held_back)} row(s) were deliberately left out: "
             f"the counter's desk was never proved, or the system sold the PNR "
             f"in another month."
             if vres.held_back else "")
          + (f"  {len(vres.converted_claims)} row(s) are restated from another "
             f"currency at a rate derived from that counter's own matched "
             f"sales, so those values are estimates."
             if vres.converted_claims else "")
          + "  The PNR Check sheet shows the working behind each row.",
          size=9, color=GREY, fill=PAPER, align="left")
    ws.row_dimensions[2].height = 17

    r = 4
    r = _band(ws, r, "AT A GLANCE")
    r = _kpi_strip(ws, r, [
        ("Overclaimed rows", len(claims), "#,##0", "C00000"),
        (f"Overclaimed value ({base_currency})", total, MONEY, "C00000"),
        ("Share of reported", (total / reported) if reported else None, PCT,
         "C00000"),
        ("Counters involved", len({c.counter for c in claims}), "0", None),
        ("Staff involved",
         len({c.wrote_by for c in claims if c.wrote_by}), "0", None),
        ("Of which converted", len(vres.converted_claims), "#,##0", "BF8F00"),
    ])
    r = _band(ws, r, "WHAT IS DELIBERATELY NOT IN THAT TOTAL")
    r = _kpi_strip(ws, r, [
        ("Wrong date — money is real", len(vres.of(OTHER_DAY)), "#,##0", None),
        ("Sold in another month", len(vres.of(ANOTHER_MONTH)), "#,##0", None),
        ("Desk never proved", len(vres.of(MAPPING_UNPROVEN)), "#,##0", None),
        ("Still unanswered", vres.still_unchecked, "#,##0", "BF8F00"),
        ("Held back on purpose", len(vres.held_back), "#,##0", None),
    ])
    r += 1

    r = _band(ws, r, "BY COUNTER", "largest claim first")
    r = _headers(ws, r, ["Counter", "Overclaimed #", "Overclaimed value",
                         "Reported value", "Share of what it reported",
                         "Why"] + [""] * 15)
    per_counter = overclaim_by(vres, lambda c: c.counter)
    for name, slot in sorted(per_counter.items(),
                             key=lambda kv: -kv[1]["amount"]):
        rep_amt = ((recon.per_counter.get(name, {}) or {}).get("rep_amt", 0)
                   if recon is not None else 0)
        _cell(ws, r, 1, name, bold=True, size=10, border=True)
        _cell(ws, r, 2, slot["n"], bold=True, size=10, border=True,
              align="center")
        _cell(ws, r, 3, slot["amount"], fmt=MONEY, bold=True, size=10,
              border=True, align="right", color="C00000")
        _cell(ws, r, 4, rep_amt or None, fmt=MONEY, size=9, border=True,
              align="right")
        _cell(ws, r, 5, (slot["amount"] / rep_amt) if rep_amt else None,
              fmt=PCT, size=9, border=True, align="center")
        ws.merge_cells(start_row=r, start_column=6, end_row=r, end_column=21)
        _cell(ws, r, 6, _reasons(slot), size=8, color=GREY, border=True)
        r += 1
    r += 1

    r = _band(ws, r, "BY THE PERSON WHO WROTE IT",
              "a claim belongs to a person, not to a building")
    r = _headers(ws, r, ["Staff", "Counter(s)", "Overclaimed #",
                         "Overclaimed value", "Why"] + [""] * 16)
    per_staff = overclaim_by(vres, lambda c: c.wrote_by)
    where: dict = {}
    for c in claims:
        where.setdefault(c.wrote_by or "(not written)", set()).add(c.counter)
    for name, slot in sorted(per_staff.items(), key=lambda kv: -kv[1]["amount"]):
        _cell(ws, r, 1, name, bold=True, size=10, border=True,
              color=GREY if name == "(not written)" else None)
        _cell(ws, r, 2, ", ".join(sorted(where.get(name, ())))[:40], size=8,
              border=True)
        _cell(ws, r, 3, slot["n"], bold=True, size=10, border=True,
              align="center")
        _cell(ws, r, 4, slot["amount"], fmt=MONEY, bold=True, size=10,
              border=True, align="right", color="C00000")
        ws.merge_cells(start_row=r, start_column=5, end_row=r, end_column=21)
        _cell(ws, r, 5, _reasons(slot), size=8, color=GREY, border=True)
        r += 1
    r += 1

    r = _band(ws, r, "EVERY OVERCLAIMED ROW", f"largest first, top {limit}")
    hdr = r
    r = _headers(ws, r, ["Counter", "Written by", "Written on", "PNR", "Type",
                         "Claimed value", "Value is", "Why it is not owed",
                         "What the check found", "System has it at",
                         "System counter", "System date"] + [""] * 9)
    for c in sorted(claims, key=lambda c: -c.reported_amount)[:limit]:
        _cell(ws, r, 1, c.counter, bold=True, size=9, border=True)
        _cell(ws, r, 2, c.wrote_by or "(not written)", size=9, border=True,
              color=GREY if not c.wrote_by else None)
        _cell(ws, r, 3, f"{c.day:%d %b}" if c.day else "", size=9, border=True,
              align="center")
        _cell(ws, r, 4, c.locator, bold=True, size=10, border=True)
        _cell(ws, r, 5, c.block.title(), size=9, border=True, align="center")
        _cell(ws, r, 6, c.reported_amount or None, fmt=MONEY, bold=True,
              size=10, border=True, align="right", color="C00000")
        # a claim restated from another currency is an estimate, at a rate we
        # derived ourselves -- it must not read as a counted figure
        _cell(ws, r, 7, "converted" if c.converted else "as written", size=8,
              border=True, align="center",
              color="BF8F00" if c.converted else GREY)
        _cell(ws, r, 8, c.verdict, bold=True, size=9, border=True,
              align="center", color="FFFFFF", fill=BAD)
        _cell(ws, r, 9, c.detail, size=8, color=GREY, border=True)
        _cell(ws, r, 10, c.pos or None, size=8, border=True)
        _cell(ws, r, 11, c.sys_counter or None, size=8, border=True)
        _cell(ws, r, 12, f"{c.sys_day:%d %b}" if c.sys_day else None, size=8,
              border=True, align="center")
        for j in range(13, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    if claims:
        ws.auto_filter.ref = f"A{hdr}:L{r - 1}"
    else:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=21)
        _cell(ws, r, 1,
              "  Nothing was overclaimed among the rows that could be checked.",
              size=10, color=GREY, fill=PAPER)
        r += 1
    ws.freeze_panes = f"A{hdr + 1}"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"
