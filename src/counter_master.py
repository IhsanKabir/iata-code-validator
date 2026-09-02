"""Counter activity workbooks -> ONE master sheet.

The counters each keep a monthly workbook with a day-sheet per day, and each
day-sheet stacks seven blocks (issue / reissue / refund / query log / Sky Star /
mail / timesheet). This module reads any number of those workbooks and produces
a single sheet that can be read at a glance.

Why the parsing is defensive rather than header-driven: columns DRIFT between
sheets inside the same workbook. Merged cells and hand-inserted serial columns
push data off its own header, so reading "the value under Sales Amount" silently
picks up a customer mobile number instead -- that produced a fake 115,560,876
month for one agent. Every value is therefore identified by what it LOOKS like,
anchored on the employee ID, and payments are accepted only when they reconcile
against the sale they belong to.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from openpyxl import Workbook, load_workbook

from . import counter_blocks
from openpyxl.formatting.rule import ColorScaleRule, DataBarRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# --------------------------------------------------------------------------
# grammars
# --------------------------------------------------------------------------
EMP_ID = re.compile(r"^USBA[-\s]?\d{4,6}$", re.I)
PNR_RE = re.compile(r"^[0-9][0-9A-Z]{5}$")
MOBILE_RE = re.compile(r"^\+?0?\d{10,14}$")
MASKED_RE = re.compile(r"[*xX]{2,}")
# an amount, optionally wearing a currency label on either side
_AMOUNT_RE = re.compile(
    r"(?:(?P<cur1>[A-Za-z৳$€]{1,4})\s*)?"
    r"(?P<num>-?\d+(?:\.\d+)?)"
    r"(?:\s*(?P<cur2>[A-Za-z৳$€]{1,4}))?")
_CURRENCY_TOKEN = re.compile(
    r"bdt|tk|taka|inr|rs|rp|myr|rm|sgd|cny|rmb|usd|aed|qar|omr|sar|thb|npr"
    r"|৳|\$|€", re.I)
DAY_IN_TITLE = re.compile(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s*[/\- ]?\s*"
                          r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)", re.I)
DAY_IN_NAME = re.compile(r"(\d{1,2})")
DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(MINUTES?|MINS?|HOURS?|HRS?)\b", re.I)

# an amount outside this band is not a ticket price; it is a phone or card number
AMOUNT_MIN, AMOUNT_MAX = 50, 3_000_000
PAY_TOLERANCE = 0.02          # payments must reconcile to the sale within 2%
MAX_SHEET_ROWS = 400          # a day-sheet never runs longer than this

BLOCKS = {
    "ticket issue": "ISSUE", "ticket issued": "ISSUE", "ticket isuue": "ISSUE",
    "ticket issu": "ISSUE", "ticket purchase": "ISSUE", "ticket sale": "ISSUE",
    "ticket reissue": "REISSUE", "reissue": "REISSUE", "ticket change": "REISSUE",
    "ticket refund": "REFUND", "refund": "REFUND",
}

# header text -> canonical channel. Everything else lands in OTHER.
CHANNELS = {
    "cash": "Cash", "cash/cheque": "Cash", "cash/ cheque": "Cash",
    "bkash": "bKash", "bkash/nagad": "bKash", "nagad": "bKash",
    "card": "Card", "credit card": "Card",
    "cheque": "Cheque", "bank": "Bank",
    "online transaction to scb": "Bank", "online transaction on scb": "Bank",
    "invoice": "Invoice", "alipay": "Alipay", "wechat": "WeChat",
}
CHANNEL_ORDER = ["Cash", "bKash", "Card", "Cheque", "Bank", "Invoice",
                 "Alipay", "WeChat", "Not written", "Did not reconcile"]

# a blank the staff never filled -- never inferred, always reported as itself
NOT_WRITTEN = "not written"

# columns that hold a REFERENCE, not money -- "Bkash/ Card No", "Transaction no"
_REF_COLUMN = re.compile(r"\bno\b|\bnumber\b|\bref\b|\bapp?r\b")


def _channel_of(key: str) -> str | None:
    """Map a header to a payment channel, tolerating how it was typed.

    One counter spells a single channel seven ways -- "Online Transaction to scb",
    "Online Transacttion to Scb", "Online Transaction deposited to scb",
    "Online Transaction  on SCB" -- so exact matching loses most of its payments.
    """
    k = re.sub(r"\s+", " ", key.strip().lower())
    if not k or _REF_COLUMN.search(k):
        return None
    if k in CHANNELS:
        return CHANNELS[k]
    if "scb" in k or k.startswith("online transa"):
        return "Bank"
    for needle, ch in (("bkash", "bKash"), ("nagad", "bKash"), ("alipay", "Alipay"),
                       ("wechat", "WeChat"), ("cheque", "Cheque"), ("cash", "Cash"),
                       ("invoice", "Invoice"), ("bank", "Bank"), ("card", "Card")):
        if needle in k:
            return ch
    return None


SECTION_BREAKERS = ("ffp", "received mail", "shift name", "query method",
                    "sky star", "mail service", "others work")


def _n(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def _hkey(v) -> str:
    return _n(v).lower().rstrip("?:").strip()


def _money(v):
    """A real money value, or None. Rejects masked card refs and phone numbers."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if f else None
    s = _n(v)
    if not s or MASKED_RE.search(s):
        return None
    if MOBILE_RE.match(s.replace(" ", "").replace("-", "")):
        return None
    # "35,548 INR" is a real amount an overseas counter writes; stripping only
    # BDT and TK threw the whole value away. A currency token either side is
    # allowed, but nothing else -- "DH696NM88H" must not read as 696.
    m = _AMOUNT_RE.fullmatch(s.replace(",", "").strip())
    if not m:
        return None
    for token in (m.group("cur1"), m.group("cur2")):
        if token and not _CURRENCY_TOKEN.fullmatch(token):
            return None
    f = float(m.group("num"))
    return f if f else None


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------
@dataclass
class SaleRow:
    counter: str
    day: int | None
    block: str
    emp_id: str
    emp_name: str
    pnr: str
    amount: float
    currency: str
    payments: dict = field(default_factory=dict)
    allocated: bool = False
    pay_status: str = "ok"     # ok | not written | mismatch | no amount
    converted_rate: float | None = None   # set when restated in base currency


@dataclass
class Issue:
    counter: str
    kind: str
    detail: str


@dataclass
class MasterResult:
    """What the run produced — the caller reports this, it does not re-read."""
    path: Path
    counters: int = 0
    employees: int = 0
    rows: int = 0
    coverage: float = 0.0
    net: float = 0.0
    unallocated_pct: float = 0.0
    not_reporting: tuple = ()
    stopped: bool = False
    unreported_n: int = 0          # missing though a sheet WAS filed that day
    unreported_amount: float = 0.0
    unfiled_n: int = 0             # missing because no sheet exists for the day
    not_submitted_n: int = 0       # sales at a counter that sent no workbook
    not_submitted_amount: float = 0.0
    not_submitted_counters: tuple = ()
    gap_source: str = ""           # what the gap sheet was compared against
    no_report_sites: tuple = ()
    window: str = ""


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
_NAME_RE = re.compile(r"[A-Za-z][A-Za-z .\-]{3,}$")
_NOT_A_NAME = ("counter", "sales", "ticket", "walk", "total", "action",
               "employee", "remark", "query", "phone", "office")


def _staff_name(row, idx) -> str:
    """The employee name on a sale row, whether or not an ID sits beside it."""
    span = range(max(0, idx - 3), idx) if idx is not None else range(min(6, len(row)))
    for j in span:
        cand = row[j]
        if (cand and not EMP_ID.match(cand) and _NAME_RE.fullmatch(cand)
                and cand.lower() not in BLOCKS
                and not any(w in cand.lower() for w in _NOT_A_NAME)):
            return cand.upper()
    return ""


def _name_index(sheets) -> dict:
    """NAME -> employee id, from every row in the workbook that states both."""
    out: dict[str, str] = {}
    for _ordinal, _title, grid in sheets:
        for raw in grid:
            row = [_n(c) for c in raw]
            idx = next((j for j, c in enumerate(row) if EMP_ID.match(c)), None)
            if idx is None:
                continue
            name = _staff_name(row, idx)
            if name:
                out.setdefault(name, row[idx].upper().replace(" ", "-"))
    return out


def _is_sales_header(keys: list[str]) -> bool:
    return "action" in keys and any(k.startswith("employee id") for k in keys)


def _resolve_day(sheet_title: str, grid, ordinal: int, month: int) -> tuple[int | None, str]:
    """Day-of-month plus how it was obtained.

    Three sources disagree in the wild: the sheet name may carry no day at all
    (Sheet2..Sheet33), and the in-sheet title may carry a STALE month left over
    from the previous month's template ("2 Jul 2026" inside an August file). The
    day number is reliable in both; the month is not, so the caller's month wins.
    """
    title = " ".join(_n(v) for row in grid[:2] for v in row if v is not None)
    m = DAY_IN_TITLE.search(title)
    if m:
        d = int(m.group(1))
        if 1 <= d <= 31:
            stale = m.group(2)[:3].upper() != date(2000, month, 1).strftime("%b").upper()
            return d, "title(stale-month)" if stale else "title"
    t = _n(sheet_title).upper()
    if not t.startswith("SHEET"):
        m = DAY_IN_NAME.search(t)
        if m and 1 <= int(m.group(1)) <= 31:
            return int(m.group(1)), "sheetname"
    return (ordinal if 1 <= ordinal <= 31 else None), "ordinal"


def _split_payments(row, cmap, amount, issues, counter, amount_col=None,
                    header_col=None):
    """Channel -> value, accepted only when it reconciles against the sale.

    Some counters put the CARD NUMBER in the Card column and the amount in the
    next one over, so an unchecked read invents millions. Payments are taken only
    when they add up to the sale; otherwise the sale is marked unallocated and
    counted in the data-quality section rather than being silently wrong.

    `amount_col` is the column the sale itself was read from and `header_col` is
    where its header sits. The gap between them IS the row's drift, so the same
    shift is applied to the payment headers. Without that the channels can still
    sum to the sale while every label is off by one -- cash reported as bKash.
    """
    def read(offset: int) -> dict[str, float]:
        got: dict[str, float] = {}
        for key, j in cmap.items():
            ch = _channel_of(key)
            j += offset
            if not ch or not (0 <= j < len(row)) or j == amount_col:
                continue
            v = _money(row[j])
            if v is not None and AMOUNT_MIN <= abs(v) <= AMOUNT_MAX:
                got[ch] = got.get(ch, 0.0) + v
        return got

    if not amount:
        return {}, False, "no amount"
    # nothing in any channel column: the staff did not write it. That is a
    # different fact from "the channels do not add up", and blending the two
    # made a reporting gap look like a reconciliation failure.
    if not any(read(o) for o in (0, 1, -1)):
        return {}, False, NOT_WRITTEN
    # A whole data row can sit one column off its own header (a merged cell or a
    # hand-inserted serial column above it). Try the row as written first, then
    # shifted by one either way, and keep the reading that actually adds up to
    # the sale -- that is the evidence the alignment was right.
    first = None
    if amount_col is None:
        # without knowing where the sale was read from, sliding the headers could
        # land a payment on the amount itself and reconcile a sale against itself
        offsets = (0,)
    else:
        drift = amount_col - header_col if header_col is not None else 0
        offsets = tuple(dict.fromkeys((drift, 0, 1, -1)))   # the measured shift first
    for offset in offsets:
        found = read(offset)
        if not found:
            continue
        if first is None:
            first = found
        total = sum(found.values())
        if abs(total - amount) <= max(1.0, abs(amount) * PAY_TOLERANCE):
            return found, True, "ok"
        exact = [c for c, v in found.items() if abs(v - amount) <= 1]
        if exact:
            return {exact[0]: amount}, True, "ok"
    # Nothing split cleanly. The sale may simply have been READ from a payment
    # column, because some counters leave Sales Amount blank and fill only the
    # channel they were paid through. Then the sale IS that channel, in full.
    if amount_col is not None:
        own = next((k for k, j in cmap.items() if j == amount_col), "")
        ch = _channel_of(own)
        if ch:
            return {ch: amount}, True, "ok"
    if first is None:
        return {}, False, NOT_WRITTEN
    issues.append(Issue(counter, "payment_mismatch",
                        f"channels={sum(first.values()):,.0f} vs sale={amount:,.0f}"))
    return first, False, "mismatch"


def parse_workbook(path: Path, month: int) -> tuple[list[SaleRow], list[Issue], dict]:
    """Read one counter workbook. Returns (sales, issues, meta).

    meta["activity"] carries the enquiry log and the timesheet, which are
    reported with their blanks counted rather than left out.
    """
    counter = _clean_counter_name(path.stem)
    # read_only: only values are needed, and loading the styles of ~30 heavily
    # formatted day-sheets per file dominated the runtime (14s a workbook, so
    # six minutes for the estate). Nothing here touches styles or merged ranges.
    wb = load_workbook(path, read_only=True, data_only=True)
    sales: list[SaleRow] = []
    issues: list[Issue] = []
    currencies = Counter()
    activity: list = []
    native: Counter = Counter()      # the channel labels this counter really uses
    day_sources = Counter()
    days_seen: set[int] = set()
    populated = 0
    total_sheets = 0

    # Read the whole workbook first. Staff often write a colleague's NAME and
    # leave the Employee ID blank, and the ID for that name may only appear on a
    # later sheet -- so the name/ID pairs have to be known before parsing.
    sheets = []
    for ordinal, ws in enumerate(wb.worksheets, start=0):
        # max_row is unreliable in read_only mode, so the cap is applied while
        # iterating rather than trusted up front
        grid = []
        for row in ws.iter_rows(values_only=True):
            grid.append(list(row))
            if len(grid) >= MAX_SHEET_ROWS:
                break
        sheets.append((ordinal, ws.title, grid))
    wb.close()
    name_to_id = _name_index(sheets)

    for ordinal, title, grid in sheets:
        total_sheets += 1
        if not any(any(_n(v) for v in r if v is not None) for r in grid):
            continue
        populated += 1
        day, src = _resolve_day(title, grid, ordinal, month)
        activity.append(counter_blocks.parse_activity(grid, counter, day, EMP_ID))
        day_sources[src] += 1
        if day:
            days_seen.add(day)
        if src == "title(stale-month)":
            issues.append(Issue(counter, "stale_month",
                                f"{title}: title names another month"))

        i = 0
        while i < len(grid):
            keys = [_hkey(c) for c in grid[i]]
            if not _is_sales_header(keys):
                i += 1
                continue
            cmap = {k: j for j, k in enumerate(keys) if k}
            hdr_i = i
            amt_j = next((j for j, k in enumerate(keys)
                          if k.startswith(("sales amount", "refund amount"))), None)
            currency = "BDT"
            if amt_j is not None:
                m = re.search(r"\((\w{3})\)", keys[amt_j])
                if m:
                    currency = m.group(1).upper()
            currencies[currency] += 1

            block = None
            i += 1
            while i < len(grid):
                raw = grid[i]
                row = [_n(c) for c in raw]
                rkeys = [_hkey(c) for c in raw]
                if _is_sales_header(rkeys) or any(k.startswith(SECTION_BREAKERS)
                                                  for k in rkeys):
                    break
                for c in row:
                    lc = c.lower()
                    if lc in BLOCKS:
                        block = BLOCKS[lc]
                idx = next((j for j, c in enumerate(row) if EMP_ID.match(c)), None)
                if block:
                    eid = row[idx].upper().replace(" ", "-") if idx is not None else ""
                    name = _staff_name(row, idx)
                    if not eid and name:
                        # the ID was left blank; the same name carries one
                        # elsewhere in this workbook often enough to recover it
                        eid = name_to_id.get(name, "")
                    pnr = next((c.upper() for c in row[idx or 0:]
                                if PNR_RE.match(c.upper()) and not c.isdigit()), "")
                    amount, amount_col = None, None
                    if amt_j is not None:
                        span = list(range(amt_j, min(amt_j + 3, len(raw)))) + \
                               list(range(max(0, amt_j - 2), amt_j))
                        for j in span:
                            v = _money(raw[j]) if j < len(raw) else None
                            if v is not None and AMOUNT_MIN <= abs(v) <= AMOUNT_MAX:
                                amount, amount_col = v, j
                                break
                    if amount is None:
                        # Drift can exceed the search span -- one refund row sat
                        # three columns right of its header. When the row holds
                        # exactly ONE money-shaped value there is nothing it
                        # could be confused with, so take it.
                        cands = [(j, _money(c)) for j, c in enumerate(raw)]
                        cands = [(j, v) for j, v in cands
                                 if v is not None and AMOUNT_MIN <= abs(v) <= AMOUNT_MAX]
                        if len(cands) == 1:
                            amount_col, amount = cands[0][0], cands[0][1]
                    amount = abs(amount) if amount else 0.0
                    # A row with no employee ID is still a sale that happened.
                    # Requiring the ID silently dropped 115,314 from one day of
                    # one counter, against the total that sheet printed itself.
                    if not eid and not (pnr and amount):
                        i += 1
                        continue
                    if not eid:
                        issues.append(Issue(counter, "no_employee_id",
                                            f"day {day} {pnr}"))
                    pays, ok, why = _split_payments(
                        raw, cmap, amount, issues, counter,
                        amount_col=amount_col, header_col=amt_j)
                    for key, j in cmap.items():
                        if _channel_of(key) and j < len(raw) and _money(raw[j]):
                            native[_n(grid[hdr_i][j]) or key.title()] += 1
                    if not amount:
                        issues.append(Issue(counter, "no_amount",
                                            f"day {day} {eid} {pnr}"))
                    if not pnr:
                        issues.append(Issue(counter, "no_pnr", f"day {day} {eid}"))
                    sales.append(SaleRow(counter, day, block, eid, name, pnr,
                                         amount, currency, pays, ok, why))
                i += 1

    if len(currencies) > 1:
        issues.append(Issue(counter, "currency_drift",
                            "sheets label " + "/".join(sorted(currencies))))
    meta = {
        "counter": counter,
        "currency": currencies.most_common(1)[0][0] if currencies else "BDT",
        "sheets": total_sheets,
        "populated": populated,
        "days": days_seen,
        "day_sources": dict(day_sources),
        "file": path.name,
        "native_channels": native,
        "activity": counter_blocks.merge(activity),
    }
    return sales, issues, meta


_MONTH_TAIL = re.compile(
    r"(?i)\s*(jan|feb|mar|apr|may|jun|jul|aug|auh|sept?|oct|nov|dec)\s*\d{2,4}\s*$")
_COUNTER_WORD = re.compile(r"(?i)\bcoun[a-z]{0,4}\b")   # counter/counte/counterr


def _clean_counter_name(stem: str) -> str:
    """Filenames are hand-typed: 'RJH Counte auh 26', 'DXB Counterr aug 26'.

    The trailing month token is stripped first -- 'auh' is a typo for 'aug' there,
    but AUH is also a real station, so only a TRAILING month-like token goes.
    """
    s = _MONTH_TAIL.sub("", stem)
    s = _COUNTER_WORD.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" -_")
    if not s:
        return stem
    return s.upper() if len(s) == 3 else s.title()


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------
@dataclass
class Agg:
    key: str = ""
    name: str = ""
    counter: str = ""
    currency: str = "BDT"          # what the figures are IN, after conversion
    source_currency: str = "BDT"   # what the counter actually wrote
    rate: float | None = None      # the rate applied, when one was
    days: set = field(default_factory=set)
    n: Counter = field(default_factory=Counter)
    val: Counter = field(default_factory=Counter)
    pay: Counter = field(default_factory=Counter)
    unallocated: float = 0.0

    @property
    def net(self) -> float:
        return self.val["ISSUE"] + self.val["REISSUE"] - self.val["REFUND"]

    @property
    def avg_ticket(self) -> float:
        return self.val["ISSUE"] / self.n["ISSUE"] if self.n["ISSUE"] else 0.0

    @property
    def cash_share(self) -> float:
        tot = sum(self.pay.values())
        return self.pay.get("Cash", 0) / tot if tot else 0.0


def _to_base(sales, recon, base_currency: str) -> dict:
    """Restate each row in base currency, in place. Returns the rates applied.

    A row carries the currency its own sheet declared, so the rate is looked up
    per (counter, currency) -- one counter writes some sheets in SGD and some in
    BDT, and each needs its own. A row with no rate is left exactly as written
    and its counter is reported as unconverted rather than quietly mixed in.
    """
    applied: dict = {}
    if recon is None:
        return applied
    for row in sales:
        info = recon.rate_for(row.counter, row.currency)
        if info is None or abs(info["rate"] - 1.0) < 1e-9:
            continue
        # A row whose amount the staff never wrote still belongs to the restated
        # counter. Skipping it left MAA with 62 rows marked INR and 29 BDT, and
        # the aggregate then took INR as the counter's currency and refused to
        # rank anyone there.
        if row.amount:
            row.amount *= info["rate"]
            row.payments = {k: v * info["rate"] for k, v in row.payments.items()}
            row.converted_rate = info["rate"]
        applied[(row.counter, info["currency"])] = info
        row.currency = base_currency
    return applied


def aggregate(sales, metas):
    """Roll sale rows up to employee and counter grain."""
    emp: dict[str, Agg] = {}
    ctr: dict[str, Agg] = {}
    names: dict[str, Counter] = defaultdict(Counter)
    emp_counters: dict[str, Counter] = defaultdict(Counter)
    cur_by_counter = {m["counter"]: m["currency"] for m in metas}

    for s in sales:
        cur = cur_by_counter.get(s.counter, s.currency)
        for bucket, key, nm in ((emp, s.emp_id, s.emp_name), (ctr, s.counter, s.counter)):
            a = bucket.get(key)
            if a is None:
                a = bucket[key] = Agg(key=key, name=nm or key, counter=s.counter,
                                      currency=cur)
            if s.day:
                a.days.add((s.counter, s.day) if bucket is ctr else s.day)
            a.n[s.block] += 1
            a.val[s.block] += s.amount
            if s.allocated:
                for ch, v in s.payments.items():
                    a.pay[ch] += v
            elif s.amount:
                bucket = ("Not written" if s.pay_status == NOT_WRITTEN
                          else "Did not reconcile")
                a.pay[bucket] += s.amount
                a.unallocated += s.amount
        if s.emp_name:
            names[s.emp_id][s.emp_name] += 1
        emp_counters[s.emp_id][s.counter] += 1

    for bucket in (emp, ctr):
        for key, a in bucket.items():
            rows_for = [s for s in sales
                        if (s.emp_id if bucket is emp else s.counter) == key]
            if rows_for:
                a.currency = Counter(s.currency for s in rows_for).most_common(
                    1)[0][0]
                rate = next((s.converted_rate for s in rows_for
                             if s.converted_rate), None)
                a.rate = rate
            a.source_currency = cur_by_counter.get(a.counter, a.currency)

    for eid, a in emp.items():
        if names[eid]:
            a.name = names[eid].most_common(1)[0][0].title()
        if not eid:
            # rows whose employee ID the staff left blank; the sales are real
            # and stay counted, but this is not a person and is not headcount
            a.name = "(employee not written)"
            a.key = ""

        a.counter = emp_counters[eid].most_common(1)[0][0]
        # NOT a.currency: that is what the figures are IN after conversion, and
        # the header's label is only what the counter wrote them in
        a.source_currency = cur_by_counter.get(a.counter, a.currency)
        a.multi = len(emp_counters[eid]) > 1
    return emp, ctr


# --------------------------------------------------------------------------
# presentation
# --------------------------------------------------------------------------
NAVY = "1F3864"
BAND = "2E75B6"
LIGHT = "D9E2F3"
PAPER = "F2F5FA"
GOOD, WARN, BAD = "C6EFCE", "FFEB9C", "FFC7CE"
GREY = "808080"

MONEY = '#,##0;[Red]-#,##0'
PCT = '0%'

_thin = Side(style="thin", color="BFBFBF")
BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)

# the single column grid every section shares, so the eye tracks straight down
COLS = [
    ("A", 26), ("B", 12), ("C", 17), ("D", 6), ("E", 7), ("F", 7), ("G", 13),
    ("H", 11), ("I", 7), ("J", 12), ("K", 7), ("L", 11), ("M", 14),
    ("N", 12), ("O", 11), ("P", 11), ("Q", 10), ("R", 10), ("S", 8),
    ("T", 7), ("U", 22),
]
LAST = COLS[-1][0]


def _cell(ws, r, c, v, *, bold=False, size=10, color=None, fill=None,
          fmt=None, align=None, wrap=False, border=False):
    cell = ws.cell(row=r, column=c, value=v)
    cell.font = Font(bold=bold, size=size, color=color or "000000",
                     name="Segoe UI")
    if fill:
        cell.fill = PatternFill("solid", fgColor=fill)
    if fmt:
        cell.number_format = fmt
    if align or wrap:
        cell.alignment = Alignment(horizontal=align or "general",
                                   vertical="center", wrap_text=wrap)
    if border:
        cell.border = BORDER
    return cell


def _band(ws, r, text, note=""):
    """A full-width section rule -- the thing that makes the sheet scannable."""
    ws.merge_cells(f"A{r}:{LAST}{r}")
    _cell(ws, r, 1, f"  {text}" + (f"    ·    {note}" if note else ""),
          bold=True, size=11, color="FFFFFF", fill=BAND, align="left")
    ws.row_dimensions[r].height = 20
    return r + 1


def _headers(ws, r, labels):
    for j, lab in enumerate(labels, start=1):
        _cell(ws, r, j, lab, bold=True, size=9, color="FFFFFF", fill=NAVY,
              align="center", wrap=True, border=True)
    ws.row_dimensions[r].height = 30
    return r + 1


def _kpi_strip(ws, r, items):
    """Seven merged KPI tiles across the grid: the headline, readable in one look."""
    span = 3
    for i, (label, value, fmt, tone) in enumerate(items):
        c = 1 + i * span
        ws.merge_cells(start_row=r, start_column=c, end_row=r, end_column=c + span - 1)
        ws.merge_cells(start_row=r + 1, start_column=c, end_row=r + 1,
                       end_column=c + span - 1)
        _cell(ws, r, c, label.upper(), bold=True, size=8, color="FFFFFF",
              fill=NAVY, align="center")
        _cell(ws, r + 1, c, value, bold=True, size=14, color=tone or NAVY,
              fill=PAPER, fmt=fmt, align="center")
    ws.row_dimensions[r].height = 15
    ws.row_dimensions[r + 1].height = 26
    return r + 2


def _pay_cells(ws, r, agg, start_col=14):
    """Cash / bKash / Card / Cheque / Bank+other, then the cash share."""
    cash = agg.pay.get("Cash", 0.0)
    bkash = agg.pay.get("bKash", 0.0)
    card = agg.pay.get("Card", 0.0)
    cheque = agg.pay.get("Cheque", 0.0)
    other = sum(v for k, v in agg.pay.items()
                if k not in ("Cash", "bKash", "Card", "Cheque", "Unallocated"))
    for off, v in enumerate((cash, bkash, card, cheque, other)):
        _cell(ws, r, start_col + off, v or None, fmt=MONEY, size=9, border=True,
              align="right")
    _cell(ws, r, start_col + 5, agg.cash_share or None, fmt=PCT, size=9,
          border=True, align="center")
    return cash, bkash, card, cheque, other


def build_master(paths, out_path: Path, *, month: int, year: int,
                 base_currency: str = "BDT", progress_cb=None,
                 stop_flag=None, sales_report=None,
                 use_warehouse: bool = False, zenith_session=None,
                 max_lookups: int = 300, mapping_file=None) -> MasterResult:
    """Read every counter workbook and write ONE master sheet.

    `progress_cb(done, total, name)` is called per workbook so a UI can show
    which counter is being read; `stop_flag()` returning True abandons the run
    after the current file.
    """
    paths = [Path(p) for p in paths]
    all_sales, all_issues, metas = [], [], []
    for n, p in enumerate(paths, start=1):
        if stop_flag is not None and stop_flag():
            break
        if progress_cb is not None:
            progress_cb(n, len(paths), p.stem)
        s, i, m = parse_workbook(p, month)
        all_sales.extend(s)
        all_issues.extend(i)
        metas.append(m)
    if not metas:
        raise ValueError("No counter workbooks were read.")

    meta_by = {m["counter"]: m for m in metas}
    issues_by = defaultdict(Counter)
    for it in all_issues:
        issues_by[it.counter][it.kind] += 1

    emp, ctr = aggregate(all_sales, metas)

    recon, gap_source = None, ""
    cr = sales = mapping = ambiguous = filed_days = None
    proofs: dict = {}
    if mapping_file is None:
        from . import config
        mapping_file = config.APP_DIR / "counter_points_of_sale.json"
    # Stop was only checked while reading workbooks, so pressing it during the
    # sales pass did nothing for the ~11s that takes. The master sheet is already
    # built at this point, so skipping the gap sheet still leaves a usable file.
    if (sales_report or use_warehouse) and not (stop_flag is not None and stop_flag()):
        # a second sheet in the SAME workbook: the unreported check only means
        # anything read next to what was reported
        from . import counter_reconcile as cr  # noqa: F811 - bound above
        note = ((lambda n: progress_cb(0, 0, f"sales data: {n:,} rows"))
                if progress_cb else None)
        if sales_report:
            sales = cr.read_sales_report(sales_report, progress_cb=note)
            gap_source = Path(sales_report).name
        else:
            # the whole month straight from the local warehouse -- no exporting,
            # merging or normalising by hand
            src = cr.find_sales_warehouse()
            if src is None:
                raise ValueError(
                    "No sales warehouse found on this machine, so the gap sheet "
                    "cannot be built. Untick the box, or point ANALYSIS_HOME at "
                    "the data root.")
            if not src.covers(month, year):
                raise ValueError(
                    f"The local sales data covers {src.first_day:%d %b %Y} to "
                    f"{src.last_day:%d %b %Y}, which does not include "
                    f"{date(year, month, 1):%B %Y}.")
            sales = cr.read_sales_from_warehouse(src, month=month, year=year,
                                                 progress_cb=note)
            gap_source = src.label
        mapping, _scores, _uc, _up, ambiguous = cr.suggest_mapping(
            sorted(ctr), sales.points_of_sale)
        # Prove the name match against where the system actually put each
        # counter's own PNRs, then let any human decision override both.
        mapping, proofs = cr.verify_mapping(mapping, all_sales, sales)
        overrides = cr.load_mapping_overrides(mapping_file)
        mapping.update({c: p for c, p in overrides.items() if c in mapping})
        cr.save_mapping(mapping_file, mapping, proofs)
        filed_days = {m["counter"]: m["days"] for m in metas}
        recon = cr.reconcile(
            sales, all_sales, mapping, base_currency=base_currency,
            currency_by_counter={m["counter"]: m["currency"] for m in metas},
            filed_days=filed_days, ambiguous=ambiguous)

    # With the rates in hand, restate every counter in base currency so one
    # table can hold them all. Nothing external is assumed: each rate came from
    # that counter's own sales appearing on both sides.
    duplicates = 0
    if recon is not None and recon.duplicate_rows:
        # a row the counter wrote twice is not a second sale
        drop = {id(x) for x in recon.duplicate_rows}
        duplicates = len(drop)
        all_sales = [r for r in all_sales if id(r) not in drop]

    converted = _to_base(all_sales, recon, base_currency)
    if converted or duplicates:
        emp, ctr = aggregate(all_sales, metas)   # restate the whole sheet
    if recon is not None and (converted or duplicates):
        # Run the comparison again on the restated rows, so the gap sheet is in
        # the same currency as everything else. Without this it reported KUL as
        # having declared 77,127 while the master sheet said 2,424,810.
        rates, suspect = recon.currency_rates, recon.currency_suspect
        dupes = {c: v.get("dupe_n", 0) for c, v in recon.per_counter.items()
                 if v.get("dupe_n")}
        recon = cr.reconcile(
            sales, all_sales, mapping, base_currency=base_currency,
            currency_by_counter={m["counter"]: base_currency for m in metas},
            filed_days=filed_days, ambiguous=ambiguous)
        recon.currency_rates, recon.currency_suspect = rates, suspect
        recon.proofs = proofs
        for c, n in dupes.items():          # the second pass no longer sees them
            recon.per_counter.setdefault(c, {})["dupe_n"] = n
        recon.duplicates_removed = duplicates

    if recon is not None and zenith_session is not None:
        # AFTER the final comparison, not before: enriching the first pass and
        # then replacing it discarded every lookup, so the run made 158 live
        # calls to Zenith and put none of the answers on the sheet.
        from .zenith_pnr_client import lookup_pnr
        counter_blocks.enrich_from_zenith(
            zenith_session, recon.omitted, lookup=lookup_pnr,
            max_lookups=max_lookups, stop_flag=stop_flag,
            progress_cb=(lambda d, n, code: progress_cb(
                d, n, f"Zenith lookup {d}/{n} · {code}"))
            if progress_cb else None)

    dom = [c for c in ctr.values() if c.currency == base_currency]
    foreign = [c for c in ctr.values() if c.currency != base_currency]
    days_in_month = 31 if month in (1, 3, 5, 7, 8, 10, 12) else (
        30 if month != 2 else 28)

    tot_issue = sum(c.val["ISSUE"] for c in dom)
    tot_reissue = sum(c.val["REISSUE"] for c in dom)
    tot_refund = sum(c.val["REFUND"] for c in dom)
    tot_n = sum(c.n["ISSUE"] for c in dom)
    pay_tot = Counter()
    for c in dom:
        pay_tot.update(c.pay)
    grand_pay = sum(pay_tot.values()) or 1
    reported = sum(len(m["days"]) for m in metas)
    expected = len(metas) * days_in_month


    wb = Workbook()
    ws = wb.active
    ws.title = "Master"
    ws.sheet_view.showGridLines = False
    for col, width in COLS:
        ws.column_dimensions[col].width = width

    # ---- title -----------------------------------------------------------
    ws.merge_cells(f"A1:{LAST}1")
    _cell(ws, 1, 1, f"  COUNTER ACTIVITY — MASTER SUMMARY   ·   "
                    f"{date(year, month, 1):%B %Y}",
          bold=True, size=16, color="FFFFFF", fill=NAVY, align="left")
    ws.row_dimensions[1].height = 34
    ws.merge_cells(f"A2:{LAST}2")
    _cell(ws, 2, 1,
          f"  {len(metas)} counters · {sum(1 for k in emp if k)} employees · "
          f"{len(all_sales):,} transactions parsed · "
          f"reporting coverage {reported/max(expected,1):.0%}"
          f"   —   money totals cover {base_currency} counters only; "
          f"{len(foreign)} overseas counters are listed separately and NOT summed",
          size=9, color=GREY, fill=PAPER, align="left")
    ws.row_dimensions[2].height = 17

    r = 4
    r = _band(ws, r, "AT A GLANCE")
    r = _kpi_strip(ws, r, [
        ("Counters reporting", len(metas), "0", None),
        ("Employees", sum(1 for k in emp if k), "0", None),
        ("Tickets issued", tot_n, "#,##0", None),
        (f"Issue value ({base_currency})", tot_issue, MONEY, None),
        ("Reissue collected", tot_reissue, MONEY, None),
        ("Refunds", -tot_refund, MONEY, "C00000"),
        ("Net collected", tot_issue + tot_reissue - tot_refund, MONEY, "1F6F3C"),
    ])
    r = _kpi_strip(ws, r, [
        ("Reporting coverage", reported / max(expected, 1), PCT, None),
        ("Avg ticket value", tot_issue / max(tot_n, 1), MONEY, None),
        ("Cash share", pay_tot.get("Cash", 0) / grand_pay, PCT, None),
        ("Digital (bKash+Card)", (pay_tot.get("bKash", 0) + pay_tot.get("Card", 0))
         / grand_pay, PCT, None),
        ("Payment not written", pay_tot.get("Not written", 0) / grand_pay, PCT,
         "C00000"),
        ("Overseas counters", len(foreign), "0", None),
        ("Days reported", reported, "0", None),
    ])
    r += 1

    # ---- payment mix -----------------------------------------------------
    r = _band(ws, r, "PAYMENT TYPES — how the money actually came in",
              "'Not written' means the staff left every channel column blank; it is "
              "reported, never guessed")
    pay_hdr = r
    r = _headers(ws, r, ["Payment type", "Amount", "Share", "% bar", "", "", "",
                         "", "", "", "", "", "", "", "", "", "", "", "", "", ""])
    ws.merge_cells(f"D{pay_hdr}:H{pay_hdr}")
    _cell(ws, pay_hdr, 4, "SHARE OF COLLECTIONS", bold=True, size=9,
          color="FFFFFF", fill=NAVY, align="center")
    pay_first = r
    for ch in CHANNEL_ORDER:
        v = pay_tot.get(ch, 0.0)
        if not v:
            continue
        tone = (BAD if ch in ("Not written", "Did not reconcile")
                else (WARN if ch == "Cash" else None))
        _cell(ws, r, 1, ch, bold=True, size=10, border=True, fill=tone)
        _cell(ws, r, 2, v, fmt=MONEY, size=10, border=True, align="right")
        _cell(ws, r, 3, v / grand_pay, fmt=PCT, size=10, border=True,
              align="center")
        ws.merge_cells(f"D{r}:H{r}")
        _cell(ws, r, 4, v, fmt='#,##0;;', size=9, border=True, align="left")
        r += 1
    pay_last = r - 1
    if pay_last >= pay_first:
        ws.conditional_formatting.add(
            f"D{pay_first}:D{pay_last}",
            DataBarRule(start_type="num", start_value=0, end_type="max",
                        color=BAND, showValue=True))
    r += 1

    # ---- counter league table -------------------------------------------
    r = _band(ws, r, "COUNTER LEAGUE TABLE",
              "ranked by net collected · overseas counters shown in local currency")
    lt_head = ["Counter", "Currency", "Days reported", "Cov%", "Staff",
               "Iss #", "Issue value", "Avg ticket", "Rei #", "Reissue value",
               "Ref #", "Refund value", "NET COLLECTED",
               "Cash", "bKash", "Card", "Cheque", "Bank/Other", "Cash%",
               "Unalloc", "Flags"]
    r = _headers(ws, r, lt_head)
    ctr_first = r
    for a in sorted(ctr.values(), key=lambda x: -x.net):
        m = meta_by.get(a.counter, {})
        nd = len(m.get("days", ()))
        cov = nd / days_in_month
        flags = []
        ik = issues_by.get(a.counter, Counter())
        if cov < 0.5:
            flags.append("LOW REPORTING")
        _coll = sum(a.pay.values())
        if _coll and a.unallocated / _coll > 0.9:
            # these counters fill the sale but leave the channel columns blank,
            # so their money cannot be attributed -- say so rather than guess
            flags.append("NO PAYMENT BREAKDOWN")
        if ik.get("currency_drift"):
            flags.append("currency drift")
        if ik.get("stale_month"):
            flags.append(f"stale dates x{ik['stale_month']}")
        if a.rate:
            flags.append(f"converted x{a.rate:,.2f}")
            if _mislabelled:
                # the column header claims base currency, the numbers do not
                flags.append(f"header says {base_currency} but is not")
        elif a.currency != base_currency:
            flags.append(f"{a.currency} — no rate, NOT converted")
        _cell(ws, r, 1, a.counter, bold=True, size=10, border=True)
        _mislabelled = bool(a.rate) and a.source_currency == base_currency
        _cell(ws, r, 2,
              (a.source_currency + " → BDT") if (a.rate and not _mislabelled)
              else ("? → BDT" if _mislabelled else a.source_currency),
              size=8, border=True, align="center")
        _cell(ws, r, 3, f"{nd} / {days_in_month}", size=9, border=True,
              align="center")
        _cell(ws, r, 4, cov, fmt=PCT, size=9, border=True, align="center")
        _cell(ws, r, 5, sum(1 for k, e in emp.items()
                            if k and e.counter == a.counter),
              size=9, border=True, align="center")
        _cell(ws, r, 6, a.n["ISSUE"] or None, size=9, border=True, align="center")
        _cell(ws, r, 7, a.val["ISSUE"] or None, fmt=MONEY, size=10, border=True,
              align="right")
        _cell(ws, r, 8, a.avg_ticket or None, fmt=MONEY, size=9, border=True,
              align="right")
        _cell(ws, r, 9, a.n["REISSUE"] or None, size=9, border=True, align="center")
        _cell(ws, r, 10, a.val["REISSUE"] or None, fmt=MONEY, size=9, border=True,
              align="right")
        _cell(ws, r, 11, a.n["REFUND"] or None, size=9, border=True, align="center")
        _cell(ws, r, 12, -a.val["REFUND"] or None, fmt=MONEY, size=9, border=True,
              align="right")
        _cell(ws, r, 13, a.net, fmt=MONEY, bold=True, size=10, border=True,
              align="right")
        _pay_cells(ws, r, a)
        _collected = sum(a.pay.values())
        _cell(ws, r, 20, (a.unallocated / _collected) if _collected else None,
              fmt=PCT, size=9, border=True, align="center")
        _cell(ws, r, 21, ", ".join(flags), size=8, color="C00000", border=True)
        r += 1
    ctr_last = r - 1
    if ctr_last >= ctr_first:
        ws.conditional_formatting.add(
            f"M{ctr_first}:M{ctr_last}",
            DataBarRule(start_type="num", start_value=0, end_type="max",
                        color="1F6F3C", showValue=True))
        ws.conditional_formatting.add(
            f"D{ctr_first}:D{ctr_last}",
            ColorScaleRule(start_type="num", start_value=0, start_color=BAD,
                           mid_type="num", mid_value=0.6, mid_color=WARN,
                           end_type="num", end_value=1, end_color=GOOD))
        ws.conditional_formatting.add(
            f"S{ctr_first}:S{ctr_last}",
            ColorScaleRule(start_type="num", start_value=0, start_color=GOOD,
                           mid_type="num", mid_value=0.6, mid_color=WARN,
                           end_type="num", end_value=1, end_color=BAD))
    r += 1

    # ---- employee scorecard ---------------------------------------------
    r = _band(ws, r, "EMPLOYEE SCORECARD",
              "ranked WITHIN each counter — footfall differs far too much between "
              "counters for an estate-wide ranking to mean anything")
    emp_hdr = r
    r = _headers(ws, r, [
        "Employee", "Employee ID", "Counter", "Cur", "Days", "Iss #",
        "Issue value", "Avg ticket", "Rei #", "Reissue value", "Ref #",
        "Refund value", "NET COLLECTED", "Cash", "bKash", "Card", "Cheque",
        "Bank/Other", "Cash%", "Rank", "Flags"])
    emp_first = r

    ranked: dict[str, int] = {}
    for cname in {e.counter for e in emp.values()}:
        peers = sorted((e for k, e in emp.items() if k and e.counter == cname),
                       key=lambda x: -x.net)
        for pos, e in enumerate(peers, start=1):
            ranked[e.key] = pos

    order = sorted(emp.values(), key=lambda e: (
        -ctr[e.counter].net if e.counter in ctr else 0, ranked.get(e.key, 99)))
    current = None
    for a in order:
        if a.counter != current:            # a thin rule between counters
            current = a.counter
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=21)
            _cell(ws, r, 1, f"   {a.counter}", bold=True, size=9, color=NAVY,
                  fill=LIGHT)
            r += 1
        flags = []
        if getattr(a, "multi", False):
            flags.append("works >1 counter")
        if a.unallocated:
            flags.append("unallocated pay")
        if not a.n["ISSUE"]:
            flags.append("no issues logged")
        top = ranked.get(a.key) == 1
        _cell(ws, r, 1, a.name, bold=top, size=10, border=True,
              fill=GOOD if top else None)
        _cell(ws, r, 2, a.key or "not written", size=9, border=True,
              color=GREY if a.key else "C00000")
        _cell(ws, r, 3, a.counter, size=9, border=True)
        _cell(ws, r, 4, a.currency, size=8, border=True, align="center")
        _cell(ws, r, 5, len(a.days) or None, size=9, border=True, align="center")
        _cell(ws, r, 6, a.n["ISSUE"] or None, size=9, border=True, align="center")
        _cell(ws, r, 7, a.val["ISSUE"] or None, fmt=MONEY, size=10, border=True,
              align="right")
        _cell(ws, r, 8, a.avg_ticket or None, fmt=MONEY, size=9, border=True,
              align="right")
        _cell(ws, r, 9, a.n["REISSUE"] or None, size=9, border=True, align="center")
        _cell(ws, r, 10, a.val["REISSUE"] or None, fmt=MONEY, size=9, border=True,
              align="right")
        _cell(ws, r, 11, a.n["REFUND"] or None, size=9, border=True, align="center")
        _cell(ws, r, 12, -a.val["REFUND"] or None, fmt=MONEY, size=9, border=True,
              align="right")
        _cell(ws, r, 13, a.net, fmt=MONEY, bold=True, size=10, border=True,
              align="right")
        _pay_cells(ws, r, a)
        _cell(ws, r, 20, ranked.get(a.key), size=9, border=True, align="center",
              bold=top)
        _cell(ws, r, 21, ", ".join(flags), size=8, color="C00000", border=True)
        r += 1
    emp_last = r - 1
    if emp_last >= emp_first:
        ws.conditional_formatting.add(
            f"G{emp_first}:G{emp_last}",
            DataBarRule(start_type="num", start_value=0, end_type="max",
                        color=BAND, showValue=True))
        ws.conditional_formatting.add(
            f"H{emp_first}:H{emp_last}",
            ColorScaleRule(start_type="min", start_color="FFFFFF",
                           end_type="max", end_color="9DC3E6"))
        ws.conditional_formatting.add(
            f"S{emp_first}:S{emp_last}",
            ColorScaleRule(start_type="num", start_value=0, start_color=GOOD,
                           mid_type="num", mid_value=0.6, mid_color=WARN,
                           end_type="num", end_value=1, end_color=BAD))
    ws.auto_filter.ref = f"A{emp_hdr}:U{emp_last}"
    r += 1

    # ---- who sold the most ------------------------------------------------
    r = _band(ws, r, "BEST SALES PEOPLE",
              "ranked across the estate by value; overseas sheets are converted "
              "at the rate their OWN matched sales imply, never an assumed one")

    def _in_base(a, block):
        """(value, the rate that was applied or None, is it rankable?).

        The rows were already restated in base currency before aggregating, so
        NOTHING is multiplied here -- doing it again turned one agent's
        1,986,546 into 60,634,814. This only reports which rate got them there,
        and refuses to rank a counter that could not be converted at all.
        """
        info = converted.get((a.counter, a.source_currency))
        return a.val[block], info, a.currency == base_currency

    def _leaderboard(row, block, title, unit, top=15):
        pool, other = [], []
        for k, a in emp.items():
            if not k or a.val[block] <= 0:
                continue
            value, info, ranked = _in_base(a, block)
            (pool if ranked else other).append((a, value, info))
        other = sorted({a.counter for a, _v, _i in other})
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=21)
        _cell(ws, row, 1, f"   {title}", bold=True, size=10, color=NAVY,
              fill=LIGHT)
        row += 1
        row = _headers(ws, row, [
            "#", "Employee", "Employee ID", "Counter", unit,
            f"Value ({base_currency})", "Avg each", "Days active",
            "Value per active day", "Share of top", "Rate applied",
            "", "", "", "", "", "", "", "", "", ""])
        best = max((v for _a, v, _i in pool), default=0)
        for pos, (a, value, info) in enumerate(
                sorted(pool, key=lambda x: -x[1])[:top], start=1):
            days = len(a.days) or 1
            _cell(ws, row, 1, pos, bold=pos <= 3, size=10, border=True,
                  align="center", fill=GOOD if pos == 1 else None)
            _cell(ws, row, 2, a.name, bold=pos <= 3, size=10, border=True)
            _cell(ws, row, 3, a.key, size=8, color=GREY, border=True)
            _cell(ws, row, 4, a.counter, size=9, border=True)
            _cell(ws, row, 5, a.n[block], size=9, border=True, align="center")
            _cell(ws, row, 6, value, fmt=MONEY, bold=True, size=10,
                  border=True, align="right")
            _cell(ws, row, 7, value / a.n[block] if a.n[block] else None,
                  fmt=MONEY, size=9, border=True, align="right")
            _cell(ws, row, 8, len(a.days) or None, size=9, border=True,
                  align="center")
            _cell(ws, row, 9, value / days, fmt=MONEY, size=9,
                  border=True, align="right")
            _cell(ws, row, 10, value / best if best else None, fmt=PCT,
                  size=9, border=True, align="center")
            _cell(ws, row, 11,
                  (f"x{info['rate']:.2f} from {info['pairs']} matched sales"
                   if info else ""), size=8, color=GREY, border=True)
            for j in range(12, 22):
                _cell(ws, row, j, None, border=True)
            row += 1
        if other:
            ws.merge_cells(start_row=row, start_column=1, end_row=row,
                           end_column=21)
            _cell(ws, row, 1,
                  "   not ranked — no rate could be derived for: "
                  + ", ".join(other)
                  + " (too few sales matched on both sides, or the sheet mixes "
                    "currencies)", size=8, color="C00000", fill=PAPER)
            row += 1
        if pool:
            first_data = row - len(pool[:top]) - (1 if other else 0)
            ws.conditional_formatting.add(
                f"F{first_data}:F{row - 1 - (1 if other else 0)}",
                DataBarRule(start_type="num", start_value=0, end_type="max",
                            color=BAND, showValue=True))
        return row + 1

    r = _leaderboard(r, "ISSUE", "BY VALUE ISSUED", "Tickets issued")
    r = _leaderboard(r, "REISSUE", "BY VALUE REISSUED", "Reissues")

    # ---- payment channels as the counters record them --------------------
    r = _band(ws, r, "PAYMENT CHANNELS AS RECORDED",
              "each counter's own labels — the overseas desks have no bKash, and "
              "some record no channel at all")
    r = _headers(ws, r, ["Counter", "Cur", "Sale rows",
                         "Value with no channel written",
                         "Channels the staff use"] + [""] * 16)
    for m in sorted(metas, key=lambda x: -len(x.get("native_channels") or {})):
        cname = m["counter"]
        a = ctr.get(cname)
        if a is None:
            continue
        nat = m.get("native_channels") or Counter()
        rows_amt = sum(a.n.values())
        blank = a.pay.get("Not written", 0)
        _cell(ws, r, 1, cname, bold=True, size=10, border=True)
        _cell(ws, r, 2, m.get("currency", ""), size=8, border=True, align="center")
        _cell(ws, r, 3, rows_amt or None, size=9, border=True, align="center")
        _cell(ws, r, 4, blank or None, fmt=MONEY, size=9, border=True,
              align="right", fill=BAD if blank and not nat else None)
        label = (", ".join(f"{k} ({v})" for k, v in nat.most_common(6))
                 or "none recorded — not written")
        ws.merge_cells(start_row=r, start_column=5, end_row=r, end_column=21)
        _cell(ws, r, 5, label, size=9, border=True,
              color="C00000" if not nat else "000000")
        r += 1
    r += 1

    # ---- enquiries -------------------------------------------------------
    act = counter_blocks.merge([m["activity"] for m in metas])
    r = _band(ws, r, "ENQUIRIES — walk-in, phone and WhatsApp",
              "the conversion rate is taken ONLY over rows whose outcome was "
              "written; the rest are counted, not assumed")
    r = _headers(ws, r, ["Counter", "Enquiries", "Converted", "Lost", "Pending",
                         "Outcome not written", "Outcome coverage",
                         "Conversion (of those written)", "Named a person",
                         "", "", "", "", "", "", "", "", "", "", "", ""])
    per = act.by_counter()
    for cname in sorted(per, key=lambda c: -per[c].get("queries", 0)):
        v = per[cname]
        q = v.get("queries", 0)
        if not q:
            continue
        conv, lost = v.get("converted", 0), v.get("lost", 0)
        blank = v.get(counter_blocks.NOT_WRITTEN, 0)
        _cell(ws, r, 1, cname, bold=True, size=10, border=True)
        _cell(ws, r, 2, q, size=9, border=True, align="center")
        _cell(ws, r, 3, conv or None, size=9, border=True, align="center")
        _cell(ws, r, 4, lost or None, size=9, border=True, align="center")
        _cell(ws, r, 5, v.get("pending", 0) or None, size=9, border=True,
              align="center")
        _cell(ws, r, 6, blank or None, size=9, border=True, align="center",
              fill=BAD if blank > q / 2 else None)
        cover = (conv + lost) / q if q else 0
        _cell(ws, r, 7, cover, fmt=PCT, size=9, border=True, align="center",
              fill=BAD if cover < 0.25 else (WARN if cover < 0.6 else None))
        # a rate over a handful of written rows is not a rate; say so instead
        _cell(ws, r, 8,
              (conv / (conv + lost)) if cover >= 0.25 else "too few written",
              fmt=PCT if cover >= 0.25 else None, size=10, bold=True,
              border=True, align="center")
        _cell(ws, r, 9, v.get("attributed", 0) or None, size=9, border=True,
              align="center")
        for j in range(10, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    r += 1

    # ---- time ------------------------------------------------------------
    r = _band(ws, r, "TIME LOGGED",
              "counted from the rows that carry a duration; rows without one are "
              "shown as not written and are NOT treated as zero")
    r = _headers(ws, r, ["Counter", "Task rows", "With a duration",
                         "Duration not written", "Whole-shift rows",
                         "Task hours logged", "Coverage of rows",
                         "", "", "", "", "", "", "", "", "", "", "", "", "", ""])
    for cname in sorted(per, key=lambda c: -per[c].get("minutes", 0)):
        v = per[cname]
        rows_t = v.get("time_rows", 0)
        if not rows_t:
            continue
        blank = v.get("time_blank", 0)
        _cell(ws, r, 1, cname, bold=True, size=10, border=True)
        _cell(ws, r, 2, rows_t, size=9, border=True, align="center")
        _cell(ws, r, 3, rows_t - blank, size=9, border=True, align="center")
        _cell(ws, r, 4, blank or None, size=9, border=True, align="center",
              fill=BAD if blank > rows_t / 2 else None)
        _cell(ws, r, 5, v.get("shift_rows", 0) or None, size=9, border=True,
              align="center")
        _cell(ws, r, 6, v.get("minutes", 0) / 60, fmt='#,##0.0', size=10,
              bold=True, border=True, align="right")
        _cell(ws, r, 7, (rows_t - blank) / rows_t, fmt=PCT, size=9, border=True,
              align="center")
        for j in range(8, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    r += 1

    # ---- data quality ----------------------------------------------------
    r = _band(ws, r, "DATA QUALITY — read every number above against this",
              "a low-coverage counter cannot be compared with a complete one")
    r = _headers(ws, r, [
        "Counter", "File", "Day sheets", "Populated", "Cov%", "Rows",
        "No amount", "No PNR", "Payment mismatch", "Stale dates",
        "Currency drift", "Day source", "Duplicated rows removed",
        "", "", "", "", "", "", "", "Verdict"])
    dq_first = r
    for m in sorted(metas, key=lambda x: len(x["days"])):
        c = m["counter"]
        ik = issues_by.get(c, Counter())
        rows = sum(1 for s in all_sales if s.counter == c)
        cov = len(m["days"]) / days_in_month
        verdict = ("USABLE" if cov >= 0.8 and not ik.get("currency_drift")
                   else "PARTIAL" if cov >= 0.4 else "NOT REPORTING")
        tone = {"USABLE": GOOD, "PARTIAL": WARN, "NOT REPORTING": BAD}[verdict]
        _cell(ws, r, 1, c, bold=True, size=10, border=True)
        _cell(ws, r, 2, m["file"], size=8, color=GREY, border=True)
        _cell(ws, r, 3, m["sheets"], size=9, border=True, align="center")
        _cell(ws, r, 4, m["populated"], size=9, border=True, align="center")
        _cell(ws, r, 5, cov, fmt=PCT, size=9, border=True, align="center")
        _cell(ws, r, 6, rows or None, size=9, border=True, align="center")
        _cell(ws, r, 7, ik.get("no_amount") or None, size=9, border=True,
              align="center")
        _cell(ws, r, 8, ik.get("no_pnr") or None, size=9, border=True,
              align="center")
        _cell(ws, r, 9, ik.get("payment_mismatch") or None, size=9, border=True,
              align="center")
        _cell(ws, r, 10, ik.get("stale_month") or None, size=9, border=True,
              align="center")
        _cell(ws, r, 11, "YES" if ik.get("currency_drift") else None, size=9,
              border=True, align="center", color="C00000")
        _cell(ws, r, 12, ", ".join(f"{k}:{v}" for k, v in
                                   sorted(m["day_sources"].items())), size=8,
              border=True)
        # the counter's own printed total still includes these; ours does not
        _dupes = ((recon.per_counter.get(c, {}) or {}).get("dupe_n", 0)
                  if recon is not None else 0)
        _cell(ws, r, 13, _dupes or None, size=9, border=True, align="center",
              fill=WARN if _dupes else None)
        for j in range(14, 21):
            _cell(ws, r, j, None, border=True)
        _cell(ws, r, 21, verdict, bold=True, size=9, fill=tone, border=True,
              align="center")
        r += 1
    if r - 1 >= dq_first:
        ws.conditional_formatting.add(
            f"E{dq_first}:E{r-1}",
            ColorScaleRule(start_type="num", start_value=0, start_color=BAD,
                           mid_type="num", mid_value=0.6, mid_color=WARN,
                           end_type="num", end_value=1, end_color=GOOD))

    r += 1
    ws.merge_cells(f"A{r}:{LAST}{r}")
    _cell(ws, r, 1,
          "  Method: values are identified by shape (PNR / mobile / money), not by "
          "header position, because columns drift between sheets. Payments are "
          "credited to a channel only when they reconcile to the sale; the rest is "
          "shown as Unallocated rather than guessed. Query logs and timesheets are "
          "deliberately excluded — see the research addendum for why they are not "
          "yet trustworthy.", size=8, color=GREY, fill=PAPER, wrap=True)
    ws.row_dimensions[r].height = 26

    ws.freeze_panes = "A9"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"


    if recon is not None:
        cr.write_reconciliation(wb.create_sheet("Unreported"), recon,
                                base_currency=base_currency,
                                filed_days=filed_days)
    out_path = Path(out_path)
    wb.save(out_path)
    return MasterResult(
        path=out_path, counters=len(metas),
        # the nameless bucket holds real money but is not a person; the sheet's
        # own headline already excludes it, and the two must agree
        employees=sum(1 for k in emp if k),
        rows=len(all_sales), coverage=reported / max(expected, 1),
        net=tot_issue + tot_reissue - tot_refund,
        unallocated_pct=pay_tot.get("Unallocated", 0) / grand_pay,
        not_reporting=tuple(
            m["counter"] for m in metas
            if len(m["days"]) / days_in_month < 0.4),
        stopped=bool(stop_flag is not None and stop_flag()),
        unreported_n=len(recon.omitted) if recon else 0,
        unreported_amount=recon.omitted_amount if recon else 0.0,
        unfiled_n=len(recon.unfiled) if recon else 0,
        not_submitted_n=len(recon.not_submitted) if recon else 0,
        not_submitted_amount=recon.not_submitted_amount if recon else 0.0,
        not_submitted_counters=tuple(sorted(
            {f.counter for f in recon.not_submitted})) if recon else (),
        gap_source=gap_source,
        no_report_sites=tuple(p for p, _ in recon.unmapped_pos) if recon else (),
        window=(f"{recon.first_day:%d %b} to {recon.last_day:%d %b %Y}"
                if recon and recon.first_day else ""),
    )


WORKBOOK_SUFFIXES = (".xlsx", ".xlsm")


def _is_counter_workbook(p: Path) -> bool:
    """A readable workbook, not one of Excel's ~$ lock files."""
    return (p.suffix.lower() in WORKBOOK_SUFFIXES
            and not p.name.startswith("~$")
            and p.is_file())


def find_counter_workbooks(folder) -> list[Path]:
    """Every counter workbook in a folder, ignoring Excel's lock files."""
    return sorted(p for p in Path(folder).iterdir() if _is_counter_workbook(p))


def resolve_counter_inputs(selection) -> list[Path]:
    """Workbooks to read, from a folder, a single file, or a mix of both.

    Counters do not always arrive as a tidy folder -- one may be re-sent on its
    own after a correction -- so the caller can hand over any combination and get
    back a de-duplicated, ordered list.
    """
    if selection is None:
        return []
    if isinstance(selection, (str, Path)):
        text = str(selection).strip().strip('"')
        if not text:
            return []
        p = Path(text)
        if p.is_dir():
            return find_counter_workbooks(p)
        return [p] if _is_counter_workbook(p) else []
    seen: dict[Path, Path] = {}
    for item in selection:
        for p in resolve_counter_inputs(item):
            seen.setdefault(p.resolve(), p)
    return sorted(seen.values(), key=lambda x: x.name.lower())


_MONTHS = {m.upper(): i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}
_PERIOD_RE = re.compile(
    r"(\d{1,2})\s*(?:st|nd|rd|th)?\s*[/\- ]?\s*"
    r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\s*,?\s*(\d{4})?",
    re.I)


def detect_period(paths, *, max_sheets_per_file: int = 40):
    """The month these workbooks are for, by majority vote of their day-sheets.

    Each sheet titles itself ("Counter Daily Activities of 01 Aug 2026"), so the
    period does not need to be asked for. A vote rather than a first-match read is
    what makes it safe: some sheets carry a title left over from the previous
    month's template, and in the August set 611 sheets said Aug against 4 stale
    Jul. Returns (month, year, votes, agreement) -- or (None, None, ...) when the
    files say nothing.
    """
    votes: Counter = Counter()
    for p in resolve_counter_inputs(paths):
        try:
            wb = load_workbook(p, read_only=True, data_only=True)
        except Exception:                       # noqa: BLE001 - unreadable file
            continue                            # counted nowhere; the caller sees it
        try:
            for ws in list(wb.worksheets)[:max_sheets_per_file]:
                text = ""
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    text += " ".join(str(v) for v in row if v is not None)
                    if i >= 1:
                        break
                m = _PERIOD_RE.search(text)
                if not m:
                    continue
                mon = _MONTHS.get(m.group(2)[:3].upper())
                yr = int(m.group(3)) if m.group(3) else None
                if mon:
                    votes[(mon, yr)] += 1
        finally:
            wb.close()
    if not votes:
        return None, None, votes, 0.0
    # a missing year should not split the vote away from the same month
    best, hits = votes.most_common(1)[0]
    month, year = best
    if year is None:
        year = next((y for (mm, y), _ in votes.most_common() if mm == month and y),
                    None)
    total = sum(votes.values())
    return month, year, votes, hits / total


def build_master_path(out_dir, month: int, year: int) -> Path:
    return Path(out_dir) / f"Counter_Master_{date(year, month, 1):%b%Y}.xlsx"


def build_from_inputs(selection, out_path, *, month: int, year: int,
                      progress_cb=None, stop_flag=None, sales_report=None,
                      use_warehouse: bool = False, zenith_session=None,
                      mapping_file=None) -> MasterResult:
    """Build from a folder, a file, or any mix of the two."""
    paths = resolve_counter_inputs(selection)
    if not paths:
        raise ValueError(f"No .xlsx counter workbooks found in {selection}")
    return build_master(paths, out_path, month=month, year=year,
                        progress_cb=progress_cb, stop_flag=stop_flag,
                        sales_report=sales_report, use_warehouse=use_warehouse,
                        zenith_session=zenith_session, mapping_file=mapping_file)


# kept as the folder-shaped name the first callers used
build_from_folder = build_from_inputs
