"""Counter reports vs the airline sales report: what was never reported.

The counters hand-keep their daily activity workbooks; the sales report is what
the system actually recorded. This module joins the two and separates four very
different things that a naive total would blend into one wrong number:

  * UNREPORTED    - the system has the sale, the counter never wrote it down
  * DATE-SHIFTED  - the counter wrote it down, on another day
  * NOT IN SYSTEM - the counter wrote down something the system does not have
  * MISMATCHED    - both have it, for different money

The join is on Record Locator (``0A7I4Q``), not "PNR Zenith" -- that column holds
the numeric dossier id, while the counters write the locator.

Scope matters more than anything else here. A sales report covering 16-22 Aug
compared against a full-month workbook would report three weeks of imaginary
"unreported" sales, so every comparison is clipped to the date range the sales
report actually contains, and to the counters that could be mapped to a point of
sale.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path

from openpyxl import load_workbook

# --------------------------------------------------------------------------
# the sales report
# --------------------------------------------------------------------------
LOCATOR_RE = re.compile(r"^[0-9A-Z]{6}$")

# customer-facing transaction lines. Commission and its void are airline-internal
# bookkeeping, not money the counter handled, but they ride on the same PNR --
# they are kept because the counters' own totals include them (this is what made
# a day reconcile to the sheet exactly).
REFUND_LINES = {"refund", "adjustment"}
REISSUE_LINES = {"reissuance adjustment"}
VOID_LINES = {"ticket void", "void commission"}

REQUIRED_COLUMNS = ("Date", "Transaction", "Record Locator", "Point of sales",
                    "Balance (base currency)")


@dataclass
class SalesLine:
    day: date
    locator: str
    pos: str
    block: str            # ISSUE / REISSUE / REFUND
    amount: float
    agent: str = ""
    customer: str = ""


@dataclass
class SalesData:
    lines: list = field(default_factory=list)
    first_day: date | None = None
    last_day: date | None = None
    points_of_sale: Counter = field(default_factory=Counter)
    rows_read: int = 0
    voided: int = 0

    @property
    def days(self) -> set:
        return {ln.day for ln in self.lines}


def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _num(v) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def read_sales_report(path, *, sheet_name=None, progress_cb=None) -> SalesData:
    """Stream the sales report into per-PNR-per-day lines.

    Read-only and streamed: these exports run to 130k rows and 26 MB.
    """
    path = Path(path)
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames \
            else wb[wb.sheetnames[0]]
        it = ws.iter_rows(values_only=True)
        try:
            header = [str(v).strip() if v is not None else "" for v in next(it)]
        except StopIteration:
            raise ValueError("The sales report is empty.") from None
        idx = {h: j for j, h in enumerate(header)}
        missing = [c for c in REQUIRED_COLUMNS if c not in idx]
        if missing:
            raise ValueError(
                "This does not look like an airline sales report — missing "
                f"column(s): {', '.join(missing)}. Found: "
                f"{', '.join(h for h in header[:8] if h)}…")

        j_date, j_txn = idx["Date"], idx["Transaction"]
        j_loc, j_pos = idx["Record Locator"], idx["Point of sales"]
        j_amt = idx["Balance (base currency)"]
        j_head = idx.get("Heading")
        j_agent = idx.get("Sale agent")
        j_cust = idx.get("Customer")

        # group first, classify second: a Penalty line only means "reissue" or
        # "refund" because of the OTHER lines sitting on the same PNR that day
        groups: dict[tuple, list] = defaultdict(list)
        data = SalesData()
        for row in it:
            data.rows_read += 1
            if progress_cb is not None and data.rows_read % 20000 == 0:
                progress_cb(data.rows_read)
            loc = str(row[j_loc] or "").strip().upper()
            if not LOCATOR_RE.match(loc):
                continue
            day = _as_date(row[j_date])
            if day is None:
                continue
            pos = str(row[j_pos] or "").strip()
            data.points_of_sale[pos] += 1
            txn = str(row[j_txn] or "").strip().lower()
            head = str(row[j_head] or "").strip().lower() if j_head is not None else ""
            if txn in VOID_LINES:
                data.voided += 1
                continue
            groups[(pos, day, loc)].append((
                txn, head, _num(row[j_amt]),
                str(row[j_agent] or "").strip() if j_agent is not None else "",
                str(row[j_cust] or "").strip() if j_cust is not None else "",
            ))
    finally:
        wb.close()

    for (pos, day, loc), lines in groups.items():
        has_refund = any(t in REFUND_LINES or h in REFUND_LINES for t, h, *_ in lines)
        has_reissue = any(t in REISSUE_LINES or h in REISSUE_LINES
                          for t, h, *_ in lines)
        buckets: dict[str, float] = defaultdict(float)
        agent = customer = ""
        for txn, head, amt, ag, cust in lines:
            agent = agent or ag
            customer = customer or cust
            if txn in REFUND_LINES or head in REFUND_LINES:
                # signed throughout, absolute only at the end: a refund is
                # negative and a penalty on top of it is positive, so a -8,000
                # refund with a 1,000 penalty is 7,000 actually returned
                buckets["REFUND"] += amt
            elif txn in REISSUE_LINES or head in REISSUE_LINES:
                buckets["REISSUE"] += amt
            elif txn == "penalty":
                # a penalty belongs to whatever the PNR was doing that day
                buckets["REFUND" if has_refund else
                        ("REISSUE" if has_reissue else "ISSUE")] += amt
            else:
                buckets["ISSUE"] += amt
        for block, amount in buckets.items():
            if round(amount, 2) == 0:
                continue
            data.lines.append(SalesLine(day=day, locator=loc, pos=pos, block=block,
                                        amount=abs(amount), agent=agent,
                                        customer=customer))
    days = data.days
    data.first_day, data.last_day = (min(days), max(days)) if days else (None, None)
    return data


# --------------------------------------------------------------------------
# counter <-> point of sale
# --------------------------------------------------------------------------
# Points of sale that are not a reporting counter at all.
NON_COUNTER = ("extranet", "galileo", "web", "abacus", "sabre", "mobiapp",
               "travelsky", "amadeus", "worldspan", "bo-1", "bo-2", "revenue")

# Station codes the counters name their files by, against the city the sales
# report names the point of sale by.
CODE_CITY = {
    "auh": "abu dhabi", "dxb": "dubai", "shj": "sharjah", "doh": "doha",
    "kul": "kuala lumpur", "can": "guangzhou", "sin": "singapore",
    "maa": "chennai", "jsr": "jessore", "zyl": "sylhet", "rjh": "rajshahi",
    "spd": "saidpur", "cxb": "cox", "cgp": "chittagong", "dac": "dhaka",
    "bkk": "bangkok", "jed": "jeddah", "mct": "muscat", "ruh": "riyadh",
    "ccu": "kolkata", "mle": "maldives",
}
# Spellings the counters use for a place the sales report spells differently.
NAME_ALIASES = {
    "rongpur": "rangpur", "banashree": "banoshree", "dhamnondi": "dhanmondi",
    "motijhreel": "motijheel", "nasirabad": "nasirabad", "coxbazar": "cox",
    "banglamotor": "banglamotor",
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


def _pos_key(pos: str) -> str:
    """'DAC-07 Baridhara' -> 'baridhara'; keeps what identifies the place."""
    body = re.sub(r"^[A-Z]{3}[\s\-]*\d*\s*", "", str(pos).strip(), flags=re.I)
    return _norm(body)


def is_counter_pos(pos: str) -> bool:
    p = str(pos).strip().lower()
    return bool(p) and not any(n in p for n in NON_COUNTER)


def suggest_mapping(counter_names, points_of_sale):
    """counter -> point of sale, best effort, plus the ones left over.

    The names do not line up on their own: 'Rongpur' is 'SPD-3 Rangpur City',
    'RJH' is 'RJH-2  Rajshahi City' (two spaces), 'DOH' is 'INT Doha (Qatar)'.
    Several places also have both a city and an airport point of sale, so the
    city wins on a tie and the choice is reported rather than hidden.
    """
    candidates = [p for p in points_of_sale if is_counter_pos(p)]
    mapping: dict[str, str] = {}
    scores: dict[str, float] = {}
    ambiguous: dict[str, list] = {}
    for name in counter_names:
        key = _norm(name)
        key = NAME_ALIASES.get(key, key)
        if len(key) == 3 and key in CODE_CITY:
            key = _norm(CODE_CITY[key])
        best, best_score = "", 0.0
        for pos in candidates:
            pk = _pos_key(pos)
            score = SequenceMatcher(None, key, pk).ratio()
            if key and (key in pk or pk in key):
                score = max(score, 0.9)
            # a plain city counter should not bind to the airport desk
            if any(w in pos.lower() for w in ("airport", "office")):
                score -= 0.15
            if score > best_score:
                best, best_score = pos, score
        if best_score >= 0.6:
            mapping[name] = best
            scores[name] = best_score
            # 'Uttara' matches both DAC-06 Uttara and DAC-17 Uttara USBA-Office;
            # a near-tie is a real second desk, not noise, so it is reported
            rivals = [p for p in candidates
                      if p != best and _pos_key(p) and (
                          key in _pos_key(p) or _pos_key(p) in key)]
            if rivals:
                ambiguous[name] = rivals
    used = set(mapping.values())
    unmatched_counters = [n for n in counter_names if n not in mapping]
    unmatched_pos = [p for p in candidates if p not in used]
    return mapping, scores, unmatched_counters, unmatched_pos, ambiguous


# --------------------------------------------------------------------------
# the comparison
# --------------------------------------------------------------------------
MATCHED, UNREPORTED, DATE_SHIFTED, NOT_IN_SYSTEM, BLOCK_MISMATCH = (
    "MATCHED", "UNREPORTED", "DATE-SHIFTED", "NOT IN SYSTEM", "BLOCK MISMATCH")


@dataclass
class Finding:
    kind: str
    counter: str
    pos: str
    day: date | None
    locator: str
    block: str
    system_amount: float = 0.0
    reported_amount: float = 0.0
    agent: str = ""
    customer: str = ""
    note: str = ""
    day_filed: bool = True     # did the counter file a sheet for this day at all?

    @property
    def gap(self) -> float:
        return self.system_amount - self.reported_amount


@dataclass
class ReconResult:
    findings: list = field(default_factory=list)
    per_counter: dict = field(default_factory=dict)
    per_agent: dict = field(default_factory=dict)
    unmapped_pos: list = field(default_factory=list)
    non_comparable: list = field(default_factory=list)
    currency_suspect: dict = field(default_factory=dict)
    ambiguous: dict = field(default_factory=dict)
    clean_matches: int = 0
    mapping: dict = field(default_factory=dict)
    first_day: date | None = None
    last_day: date | None = None
    sales_rows: int = 0

    def of(self, kind):
        return [f for f in self.findings if f.kind == kind]

    @property
    def unreported_amount(self) -> float:
        """Only where the two sides are in the same currency."""
        skip = set(self.non_comparable)
        return sum(f.system_amount for f in self.of(UNREPORTED)
                   if f.counter not in skip)

    def comparable(self, counter: str) -> bool:
        return counter not in set(self.non_comparable)

    @property
    def omitted(self) -> list:
        """Missing even though the counter DID file a sheet that day."""
        return [f for f in self.of(UNREPORTED) if f.day_filed]

    @property
    def unfiled(self) -> list:
        """Missing because no sheet exists for that day at all."""
        return [f for f in self.of(UNREPORTED) if not f.day_filed]

    @property
    def omitted_amount(self) -> float:
        skip = set(self.non_comparable)
        return sum(f.system_amount for f in self.omitted if f.counter not in skip)


def reconcile(sales: SalesData, counter_rows, mapping, *,
              currency_by_counter=None, base_currency: str = "BDT",
              filed_days=None, ambiguous=None) -> ReconResult:
    """Compare system lines against what the counters wrote down.

    Only days the sales report actually covers are judged. Anything outside that
    window is invisible to this comparison rather than counted as unreported --
    a week-long export against a month-long workbook would otherwise invent three
    weeks of missing sales.
    """
    res = ReconResult(mapping=dict(mapping), first_day=sales.first_day,
                      last_day=sales.last_day, sales_rows=sales.rows_read)
    # An overseas counter writes its sheet in local currency while the sales
    # report is in base currency, so their AMOUNTS cannot be subtracted from one
    # another -- CNY 30,292 against BDT 556,266 is not a 526k shortfall. Counts
    # stay comparable, so those counters are judged on counts alone.
    cur = dict(currency_by_counter or {})
    res.non_comparable = sorted(c for c, v in cur.items()
                                if (v or base_currency) != base_currency)
    if sales.first_day is None:
        return res
    pos_to_counter = {v: k for k, v in mapping.items()}
    filed = {k: set(v) for k, v in (filed_days or {}).items()}
    res.ambiguous = dict(ambiguous or {})

    # AGGREGATE the reported side first. A three-passenger booking is three rows
    # on the counter sheet but one line in the system, so matching row-by-row
    # left two of them looking like sales the system never had.
    rep: dict[tuple, dict] = {}
    for r in counter_rows:
        pos = mapping.get(r.counter)
        if not pos or not r.pnr or r.day is None:
            continue
        key = (pos, r.day, r.pnr, r.block)
        slot = rep.get(key)
        if slot is None:
            slot = rep[key] = {"amount": 0.0, "n": 0, "counter": r.counter,
                               "day": r.day, "block": r.block}
        slot["amount"] += r.amount
        slot["n"] += 1
    by_day: dict[tuple, list] = defaultdict(list)
    by_pnr: dict[tuple, list] = defaultdict(list)
    for key in rep:
        pos, day, pnr, block = key
        by_day[(pos, pnr, block)].append(key)
        by_pnr[(pos, pnr)].append(key)

    matched: set = set()
    per_counter: dict = defaultdict(lambda: Counter())
    per_agent: dict = defaultdict(lambda: Counter())
    ratios: dict = defaultdict(list)

    for ln in sales.lines:
        counter = pos_to_counter.get(ln.pos)
        if counter is None:
            continue
        day = ln.day.day
        c = per_counter[counter]
        c["sys_n"] += 1
        c["sys_amt"] += ln.amount

        key = (ln.pos, day, ln.locator, ln.block)
        slot = rep.get(key)
        if slot is not None:
            matched.add(key)
            c["rep_n"] += 1
            c["rep_amt"] += slot["amount"]
            if slot["amount"] > 0:
                ratios[counter].append(ln.amount / slot["amount"])
            if abs(slot["amount"] - ln.amount) > max(1.0, ln.amount * 0.02):
                res.findings.append(Finding(
                    MATCHED, counter, ln.pos, ln.day, ln.locator, ln.block,
                    ln.amount, slot["amount"], ln.agent, ln.customer,
                    note=("amount differs" if slot["n"] == 1
                          else f"amount differs ({slot['n']} rows summed)")))
                c["mismatch_n"] += 1
                c["mismatch_gap"] += ln.amount - slot["amount"]
            else:
                c["clean_n"] += 1
            continue

        shifted = [k for k in by_day.get((ln.pos, ln.locator, ln.block), ())
                   if k not in matched]
        if shifted:
            k = shifted[0]
            matched.add(k)
            c["shift_n"] += 1
            res.findings.append(Finding(
                DATE_SHIFTED, counter, ln.pos, ln.day, ln.locator, ln.block,
                ln.amount, rep[k]["amount"], ln.agent, ln.customer,
                note=f"counter logged it on day {k[1]}"))
            continue

        other = [k for k in by_pnr.get((ln.pos, ln.locator), ())
                 if k not in matched]
        if other:
            k = other[0]
            matched.add(k)
            c["block_n"] += 1
            res.findings.append(Finding(
                BLOCK_MISMATCH, counter, ln.pos, ln.day, ln.locator, ln.block,
                ln.amount, rep[k]["amount"], ln.agent, ln.customer,
                note=f"counter logged it as {k[3]}"))
            continue

        # Nothing on the counter side. Whether a sheet exists for that day at all
        # separates a per-sale omission from a whole day never being filed --
        # very different failures, and blending them overstates the first.
        day_filed = day in filed.get(counter, set()) if filed else True
        c["unrep_n" if day_filed else "unfiled_n"] += 1
        c["unrep_amt" if day_filed else "unfiled_amt"] += ln.amount
        a = per_agent[ln.agent or "(no agent)"]
        a["n"] += 1
        a["amt"] += ln.amount
        res.findings.append(Finding(
            UNREPORTED, counter, ln.pos, ln.day, ln.locator, ln.block,
            ln.amount, 0.0, ln.agent, ln.customer, day_filed=day_filed))

    # the other direction: written down, but the system has no such sale
    lo, hi = sales.first_day.day, sales.last_day.day
    for key, slot in rep.items():
        if key in matched:
            continue
        if not (lo <= slot["day"] <= hi):
            continue                      # outside the window the export covers
        cnt = slot["counter"]
        per_counter[cnt]["notsys_n"] += 1
        per_counter[cnt]["notsys_amt"] += slot["amount"]
        res.findings.append(Finding(
            NOT_IN_SYSTEM, cnt, key[0], None, key[2], key[3],
            0.0, slot["amount"], note=f"counter day {slot['day']}"))

    # A counter whose matched sales are consistently a different SIZE is keeping
    # its sheet in another currency, whatever its column header claims. Trusting
    # the label reported a 58x "shortfall" for a desk that simply writes AED.
    for cnt, rs in ratios.items():
        if len(rs) < 3:
            continue
        rs = sorted(rs)
        median = rs[len(rs) // 2]
        if median > 2 or median < 0.5:
            res.currency_suspect[cnt] = median
            if cnt not in res.non_comparable:
                res.non_comparable.append(cnt)
    res.non_comparable = sorted(res.non_comparable)
    res.clean_matches = sum(v.get("clean_n", 0) for v in per_counter.values())

    res.per_counter = {k: dict(v) for k, v in per_counter.items()}
    res.per_agent = {k: dict(v) for k, v in per_agent.items()}
    res.unmapped_pos = [
        (p, n) for p, n in sales.points_of_sale.most_common()
        if is_counter_pos(p) and p not in pos_to_counter]
    return res


# --------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------
def write_reconciliation(ws, res: ReconResult, *, base_currency: str = "BDT",
                         detail_limit: int = 400, filed_days=None) -> None:
    """Render the comparison onto a worksheet, worst counter first."""
    from openpyxl.formatting.rule import ColorScaleRule, DataBarRule

    from .counter_master import (BAD, COLS, GOOD, GREY, LAST, MONEY, NAVY,
                                 PAPER, PCT, WARN, _band, _cell, _headers,
                                 _kpi_strip)

    for col, width in COLS:
        ws.column_dimensions[col].width = width
    ws.sheet_view.showGridLines = False

    window = (f"{res.first_day:%d %b} to {res.last_day:%d %b %Y}"
              if res.first_day else "no dated rows")
    ws.merge_cells(f"A1:{LAST}1")
    _cell(ws, 1, 1, "  UNREPORTED SALES  ·  counter reports vs the sales report",
          bold=True, size=16, color="FFFFFF", fill=NAVY, align="left")
    ws.row_dimensions[1].height = 34
    ws.merge_cells(f"A2:{LAST}2")
    _cell(ws, 2, 1,
          f"  The sales report covers {window} — ONLY those days are judged. "
          f"{res.sales_rows:,} rows read. A sale the counter logged on another "
          f"day counts as date-shifted, not as missing. OMITTED means the counter filed a sheet that day and the sale is not on it; NO SHEET means no sheet exists for that day at all."
          + (f"   ·   Amounts are NOT compared for "
             f"{', '.join(res.non_comparable)} — they report in local currency."
             if res.non_comparable else ""),
          size=9, color=GREY, fill=PAPER, align="left")
    ws.row_dimensions[2].height = 17

    tot = {k: sum(v.get(k, 0) for c, v in res.per_counter.items()
                  if res.comparable(c))
           for k in ("sys_n", "sys_amt", "rep_amt", "unrep_amt")}
    mismatched = [f for f in res.of(MATCHED) if f.note]
    r = 4
    r = _band(ws, r, "AT A GLANCE")
    r = _kpi_strip(ws, r, [
        ("Days judged", (res.last_day - res.first_day).days + 1
         if res.first_day else 0, "0", None),
        ("System sales", tot["sys_n"], "#,##0", None),
        (f"System value ({base_currency})", tot["sys_amt"], MONEY, None),
        ("Reported value", tot["rep_amt"], MONEY, None),
        ("OMITTED (sheet filed)", len(res.omitted), "#,##0", "C00000"),
        ("Omitted value", res.omitted_amount, MONEY, "C00000"),
        ("No sheet that day", len(res.unfiled), "#,##0", "C00000"),
    ])
    r = _kpi_strip(ws, r, [
        ("Date-shifted", len(res.of(DATE_SHIFTED)), "#,##0", None),
        ("Not in system", len(res.of(NOT_IN_SYSTEM)), "#,##0", None),
        ("Block mismatch", len(res.of(BLOCK_MISMATCH)), "#,##0", None),
        ("Amount mismatch", len(mismatched), "#,##0", None),
        ("Sites with no report", len(res.unmapped_pos), "0", "C00000"),
        ("Missing total value", res.unreported_amount, MONEY, "C00000"),
        ("Matched cleanly", res.clean_matches, "#,##0", "1F6F3C"),
    ])
    r += 1

    # ---- per counter ----
    r = _band(ws, r, "BY COUNTER", "worst shortfall first")
    r = _headers(ws, r, [
        "Counter", "Point of sale", "Cur", "Sys #", "System value", "Rep #",
        "Reported value", "OMITTED #", "OMITTED value", "No sheet #",
        "No sheet value", "Shortfall %", "Date-shifted", "Not in system",
        "Block mismatch", "Amount mismatch", "Days filed", "", "", "",
        "Verdict"])
    first = r
    for c, v in sorted(res.per_counter.items(),
                       key=lambda kv: -(kv[1].get("unrep_amt", 0)
                                        + kv[1].get("unfiled_amt", 0))):
        ok = res.comparable(c)
        sys_amt = v.get("sys_amt", 0)
        omitted, unfiled = v.get("unrep_amt", 0), v.get("unfiled_amt", 0)
        share = (omitted + unfiled) / sys_amt if (ok and sys_amt) else None
        filed_n = len(filed_days.get(c, ())) if filed_days else None
        if c in res.currency_suspect:
            verdict, tone = "CURRENCY MISMATCH", WARN
        elif not ok:
            verdict, tone = "COUNTS ONLY", WARN
        elif v.get("unfiled_n", 0) and not v.get("rep_n", 0):
            verdict, tone = "FILED NOTHING", BAD
        elif v.get("unrep_n", 0) == 0 and v.get("unfiled_n", 0):
            # nothing omitted from the sheets it DID file -- the gap is the days
            # it never filed at all, which is a different conversation
            verdict, tone = "DAYS MISSING", WARN
        elif v.get("unrep_n", 0) == 0:
            verdict, tone = "CLEAN", GOOD
        elif share and share > 0.5:
            verdict, tone = "MOSTLY MISSING", BAD
        else:
            verdict, tone = "GAPS", WARN
        _cell(ws, r, 1, c, bold=True, size=10, border=True)
        _cell(ws, r, 2, res.mapping.get(c, ""), size=8, color=GREY, border=True)
        _cell(ws, r, 3, "BDT" if ok else "local", size=8, border=True,
              align="center")
        _cell(ws, r, 4, v.get("sys_n", 0) or None, size=9, border=True,
              align="center")
        _cell(ws, r, 5, sys_amt or None, fmt=MONEY, size=9, border=True,
              align="right")
        _cell(ws, r, 6, v.get("rep_n", 0) or None, size=9, border=True,
              align="center")
        _cell(ws, r, 7, v.get("rep_amt", 0) or None, fmt=MONEY, size=9,
              border=True, align="right")
        _cell(ws, r, 8, v.get("unrep_n", 0) or None, bold=True, size=10,
              border=True, align="center")
        _cell(ws, r, 9, (omitted if ok else None) or None, fmt=MONEY, bold=True,
              size=10, border=True, align="right")
        _cell(ws, r, 10, v.get("unfiled_n", 0) or None, size=9, border=True,
              align="center")
        _cell(ws, r, 11, (unfiled if ok else None) or None, fmt=MONEY, size=9,
              border=True, align="right")
        _cell(ws, r, 12, share, fmt=PCT, size=9, border=True, align="center")
        _cell(ws, r, 13, v.get("shift_n", 0) or None, size=9, border=True,
              align="center")
        _cell(ws, r, 14, v.get("notsys_n", 0) or None, size=9, border=True,
              align="center")
        _cell(ws, r, 15, v.get("block_n", 0) or None, size=9, border=True,
              align="center")
        _cell(ws, r, 16, v.get("mismatch_n", 0) or None, size=9, border=True,
              align="center")
        _cell(ws, r, 17, filed_n, size=9, border=True, align="center")
        note = []
        if c in res.currency_suspect:
            note.append(f"sheet reads {res.currency_suspect[c]:.0f}x smaller")
        if c in res.ambiguous:
            note.append("also: " + ", ".join(res.ambiguous[c])[:40])
        _cell(ws, r, 18, "; ".join(note), size=8, color="C00000", border=True)
        for j in range(19, 21):
            _cell(ws, r, j, None, border=True)
        _cell(ws, r, 21, verdict, bold=True, size=9, fill=tone, border=True,
              align="center")
        r += 1
    if r - 1 >= first:
        ws.conditional_formatting.add(
            f"I{first}:I{r-1}",
            DataBarRule(start_type="num", start_value=0, end_type="max",
                        color="C00000", showValue=True))
        ws.conditional_formatting.add(
            f"L{first}:L{r-1}",
            ColorScaleRule(start_type="num", start_value=0, start_color=GOOD,
                           mid_type="num", mid_value=0.25, mid_color=WARN,
                           end_type="num", end_value=1, end_color=BAD))
    r += 1

    # ---- selling locations with no counter report at all ----
    if res.unmapped_pos:
        r = _band(ws, r, "SELLING LOCATIONS WITH NO COUNTER REPORT",
                  "these sold tickets in the window but filed no workbook")
        r = _headers(ws, r, ["Point of sale", "Transactions"] + [""] * 19)
        for pos, n in res.unmapped_pos:
            _cell(ws, r, 1, pos, bold=True, size=10, border=True, fill=BAD)
            _cell(ws, r, 2, n, size=9, border=True, align="center")
            for j in range(3, 22):
                _cell(ws, r, j, None, border=True)
            r += 1
        r += 1

    # ---- who made the unreported sales ----
    if res.per_agent:
        r = _band(ws, r, "UNREPORTED BY SALE AGENT",
                  "the agent the SYSTEM recorded against the sale")
        r = _headers(ws, r, ["Sale agent", "Unreported #", "Unreported value"]
                     + [""] * 18)
        for ag, v in sorted(res.per_agent.items(),
                            key=lambda kv: -kv[1]["amt"])[:25]:
            _cell(ws, r, 1, ag, bold=True, size=10, border=True)
            _cell(ws, r, 2, v["n"], size=9, border=True, align="center")
            _cell(ws, r, 3, v["amt"], fmt=MONEY, size=9, border=True,
                  align="right")
            for j in range(4, 22):
                _cell(ws, r, j, None, border=True)
            r += 1
        r += 1

    # ---- the actual missing sales ----
    r = _band(ws, r, "UNREPORTED SALES — DETAIL",
              f"largest first, top {detail_limit}")
    hdr = r
    r = _headers(ws, r, ["Date", "Counter", "PNR", "Type", "System value",
                         "Sale agent", "Customer", "Sheet filed?"]
                 + [""] * 13)
    detail = sorted(res.of(UNREPORTED),
                    key=lambda f: -f.system_amount)[:detail_limit]
    for f in detail:
        _cell(ws, r, 1, f"{f.day:%d %b}" if f.day else "", size=9, border=True,
              align="center")
        _cell(ws, r, 2, f.counter, size=9, border=True)
        _cell(ws, r, 3, f.locator, bold=True, size=10, border=True)
        _cell(ws, r, 4, f.block.title(), size=9, border=True, align="center")
        _cell(ws, r, 5, f.system_amount, fmt=MONEY, size=10, border=True,
              align="right")
        _cell(ws, r, 6, f.agent, size=8, border=True)
        _cell(ws, r, 7, f.customer[:40], size=8, color=GREY, border=True)
        _cell(ws, r, 8, "yes" if f.day_filed else "NO SHEET", size=8,
              border=True, align="center",
              fill=None if f.day_filed else WARN)
        for j in range(9, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    if detail:
        ws.conditional_formatting.add(
            f"E{hdr+1}:E{r-1}",
            DataBarRule(start_type="num", start_value=0, end_type="max",
                        color="C00000", showValue=True))
        ws.auto_filter.ref = f"A{hdr}:H{r-1}"
    ws.freeze_panes = "A9"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"
