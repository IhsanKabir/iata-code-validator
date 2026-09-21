"""Which agencies grew, which fell away, measured against their own average.

The question is "who is up and who is down, and by how much" -- and the
obvious way to answer it is wrong. Measured on this warehouse, comparing
August 2026 with July 2026 at a 20% threshold flags 1,521 of the 1,959
agencies that traded in both months. Seventy-eight per cent of the book
"moved". A single prior period is not a baseline; it is another sample of the
same noise.

So the baseline here is an AVERAGE of several preceding windows -- or, where
the season is the thing that moves (Hajj, Umrah, the winter peak), the SAME
PERIOD A YEAR EARLIER, which a trailing average cannot see.

Five things are refused outright, each because doing them produced a wrong
list:

* A percentage is never invented where there is no denominator. Of 35,130
  customers active across July and August, 15,489 had no July sales at all.
  They are NEW, with a money figure and no percentage -- not "+infinity", and
  not silently dropped.
* A customer who stopped buying is not "-100%". 14,577 of them exist in that
  same pair of months, and filing them among the fallers buries the only fact
  that matters, which is that they are gone. They are LAPSED, on their own,
  and `change_pct` returns None for them -- guarding only on the baseline let
  -1.0 through, and the sheet printed "-100%" on the very row captioned "no
  percentage is printed".
* A period that nets BELOW zero is not an empty one. Nine agencies in the
  August run took more money back than they spent, and testing `current <= 0`
  filed every one of them under "stopped buying" -- wrong twice, because they
  were trading and because the loss exceeds the baseline. They are REFUNDED.
* The average is never taken over "the windows they traded in". An agency
  that bought once in three months would then show the same baseline as one
  that bought every month. The divisor is the number of windows ASKED FOR,
  and a thin baseline is declared rather than smoothed away.
* A period running past the end of the data is never reported as a fall. The
  warehouse stops mid-month; comparing a half-month against whole months
  shows the entire book collapsing, which is a fact about the calendar.

Ranking is by money, not by percentage. An agency down 3.2M at -31% matters
more than one down 40k at -95%, and sorting on the percentage puts the
trivial one on top.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # imported for typing only
    from collections.abc import Iterable

    from .counter_reconcile import WarehouseSource

# --- what is being measured ------------------------------------------------
MEASURE_GROSS = "gross"      # ticket payments only
MEASURE_NET = "net"          # less refunds and voids
MEASURE_TICKETS = "tickets"  # volume rather than value

#: Everything, including the lines the other two leave out.
MEASURE_ALL = "all"

#: Transaction lines behind each measure. Refunds and voids already carry a
#: negative balance in the warehouse, so netting is a plain sum.
GROSS_LINES = ("Ticket payment",)
NET_LINES = ("Ticket payment", "Refund", "Ticket void")
#: Penalty and Reissuance Adjustment are real money that neither gross nor
#: net counts -- 1,438.8M and 861.1M across the agency book. In aggregate
#: that is 2.8%, but 39 of the 820 agencies above a 500k floor carry more
#: than a tenth of their net in these lines, and for those the verdict can
#: be wrong in kind rather than in size.
ALL_LINES = NET_LINES + ("Penalty", "Reissuance Adjustment")

#: What each measure does NOT count, said on the sheet rather than implied.
EXCLUDES = {
    MEASURE_GROSS: "refunds, voids, penalties and reissue adjustments",
    MEASURE_NET: "penalties and reissue adjustments",
    MEASURE_TICKETS: "value entirely — this counts tickets, not money",
    MEASURE_ALL: "commission, which is a cost to the airline, not a sale",
}

# --- what the period is measured against -----------------------------------
#: An average of the windows immediately before the period.
BASELINE_TRAILING = "trailing"
#: The same period one year earlier. Hajj, Umrah and the winter peak move the
#: whole book together, and a trailing average cannot see that -- against
#: July, every agency "collapses" in a month that is quiet every year.
BASELINE_LAST_YEAR = "last_year"
#: Both at once. An agency down against recent months AND against last
#: year is in real decline; down against only one is a blip or a season.
#: Measured on August 2026, the two lists agreed on just 122 of the 559
#: agencies either of them flagged.
BASELINE_BOTH = "both"

# --- which way the mover moved ---------------------------------------------
DROPPED = "dropped"
INCREASED = "increased"
EITHER = "either"

# --- the buckets -----------------------------------------------------------
DECLINED = "declined"
GREW = "grew"
LAPSED = "lapsed"
NEW = "new"
STABLE = "stable"
#: Took more money back than they spent. Measured on August 2026, 9 of the 59
#: customers that looked like they had "stopped buying" were really this: they
#: traded, and the net ran backwards. Calling that "bought nothing" is wrong
#: twice over -- they were active, and the loss is bigger than the baseline.
REFUNDED = "refunded"

#: Sales to trade accounts. The warehouse holds 142,960 distinct customers,
#: but 5,266 of them are agencies -- the rest are WEB, OFFICE and MOBILE
#: retail passengers, most of whom bought exactly once and for whom a
#: month-on-month change means nothing.
AGENCY_CHANNELS = ("AGENCY",)


def _dm(d: date) -> str:
    """'19 Sep 2026' without a platform-specific strftime flag."""
    return f"{d.day} {d:%b %Y}"


def _shift_month(year: int, month: int, delta: int) -> tuple:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def a_year_before(d: date) -> date:
    """The same day one year earlier, stepping 29 Feb back to the 28th."""
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


def is_whole_month(first: date, last: date) -> bool:
    return (first.day == 1
            and (first.year, first.month) == (last.year, last.month)
            and last.day == monthrange(last.year, last.month)[1])


@dataclass(frozen=True)
class Settings:
    """Everything the operator chose, carried with the answer.

    A sheet that does not say what it measured cannot be forwarded, so these
    travel with the result and are printed on it in words.
    """

    period_from: date
    period_to: date
    #: How many preceding windows the baseline averages. 1 makes it a plain
    #: previous-period comparison.
    trailing: int = 3
    #: BASELINE_TRAILING or BASELINE_LAST_YEAR. The last-year mode uses a
    #: single window and ignores `trailing` -- this warehouse starts in
    #: May 2025, so there is only one prior year to average over anyway.
    baseline: str = BASELINE_TRAILING
    #: 0.20 = twenty per cent.
    threshold: float = 0.20
    direction: str = EITHER
    #: Ignore anyone below this in the baseline. Without a floor the August
    #: list runs to 3,981 rows; at 500,000 BDT it runs to 631.
    floor: float = 0.0
    measure: str = MEASURE_NET
    channels: tuple = AGENCY_CHANNELS

    def __post_init__(self) -> None:
        # The dates are interpolated straight into SQL further down. The GUI
        # parses them with date.fromisoformat first, but a second caller --
        # a CLI, a scheduled run -- would not necessarily, and a plain string
        # would land in the query verbatim. Refuse it here, once, rather than
        # trust every future caller to remember.
        for field_name in ("period_from", "period_to"):
            if not isinstance(getattr(self, field_name), date):
                raise TypeError(
                    f"{field_name} must be a datetime.date, not "
                    f"{type(getattr(self, field_name)).__name__}")
        if self.period_to < self.period_from:
            raise ValueError("period_to falls before period_from")
        if self.baseline not in (BASELINE_TRAILING, BASELINE_LAST_YEAR,
                                 BASELINE_BOTH):
            raise ValueError(f"unknown baseline {self.baseline!r}")

    @property
    def unit(self) -> str:
        """What the figures are counted in -- money, or tickets."""
        return "tickets" if self.measure == MEASURE_TICKETS else "BDT"


def _year_window(s: Settings) -> tuple:
    return (a_year_before(s.period_from), a_year_before(s.period_to))


def _trailing_windows(s: Settings) -> list:
    """The windows immediately before the period, most recent first."""
    if is_whole_month(s.period_from, s.period_to):
        out = []
        for i in range(1, max(1, s.trailing) + 1):
            y, m = _shift_month(s.period_from.year, s.period_from.month, -i)
            out.append((date(y, m, 1), date(y, m, monthrange(y, m)[1])))
        return out
    span = (s.period_to - s.period_from).days + 1
    return [(s.period_from - timedelta(days=i * span),
             s.period_from - timedelta(days=i * span) + timedelta(days=span - 1))
            for i in range(1, max(1, s.trailing) + 1)]


def averaged_count(s: Settings) -> int:
    """How many windows go into the averaged baseline."""
    return 1 if s.baseline == BASELINE_LAST_YEAR else max(1, s.trailing)


def year_index(s: Settings) -> int | None:
    """Where the year-earlier window sits in `windows()`, if there is one.

    Under BOTH it is the last window, after the averaged ones, so the two
    comparisons never draw on the same index.
    """
    if s.baseline == BASELINE_LAST_YEAR:
        return 1
    if s.baseline == BASELINE_BOTH:
        return averaged_count(s) + 1
    return None


def windows(s: Settings) -> list:
    """The reporting window, then each baseline window, most recent first.

    A whole calendar month is compared with whole calendar months, because
    picking "August" and being shown a baseline starting on the 31st of May
    reads as a bug. Any other range is compared with windows of exactly the
    same length, which is the only way a 28-day February is not a ten per
    cent fall against a 31-day January.

    Under BOTH the averaged windows come first and the year-earlier one last,
    because the two answer different questions and an agency can easily be
    down against one and level against the other.
    """
    out = [(s.period_from, s.period_to)]
    if s.baseline == BASELINE_LAST_YEAR:
        # One window, not an average: the point is to hold the season
        # constant, and averaging several years would blur it back out.
        out.append(_year_window(s))
        return out
    out.extend(_trailing_windows(s))
    if s.baseline == BASELINE_BOTH:
        out.append(_year_window(s))
    return out


def baseline_span(s: Settings) -> tuple:
    """First and last day the AVERAGED baseline is drawn from.

    The year-earlier window is deliberately not included: under BOTH it
    sits a year away, and folding it into one span would print a range
    covering twelve months nobody asked about.
    """
    w = windows(s)[1:1 + averaged_count(s)]
    return (w[-1][0], w[0][1])


def period_label(s: Settings) -> str:
    if is_whole_month(s.period_from, s.period_to):
        return f"{s.period_from:%b %Y}"
    return f"{_dm(s.period_from)} to {_dm(s.period_to)}"


def baseline_label(s: Settings) -> str:
    lo, hi = baseline_span(s)
    if is_whole_month(s.period_from, s.period_to):
        one_month = (lo.year, lo.month) == (hi.year, hi.month)
        return (f"{lo:%b %Y}" if one_month
                else f"{lo:%b %Y} to {hi:%b %Y}")
    return f"{_dm(lo)} to {_dm(hi)}"


@dataclass
class Movement:
    """One customer, this period against their own trailing average."""

    customer: str
    customer_id: str = ""
    iata: str = ""
    zone: str = ""
    station: str = ""
    sales_person: str = ""
    agent_type: str = ""
    channel: str = ""
    current: float = 0.0
    baseline: float = 0.0
    tickets_current: int = 0
    tickets_baseline: float = 0.0
    #: How many of the baseline windows this customer actually bought in.
    windows_traded: int = 0
    trailing: int = 1
    #: The same period a year earlier, when that comparison was asked for.
    baseline_year: float = 0.0
    year_known: bool = False
    #: The accounts this agency trades under, when rows were grouped by
    #: business rather than by account: (id, name, current, baseline).
    accounts: list = field(default_factory=list)
    threshold: float = 0.20
    last_bought: date | None = None

    @property
    def change(self) -> float:
        return self.current - self.baseline

    @property
    def change_pct(self) -> float | None:
        """None wherever a percentage would be a lie.

        Guarding on the baseline alone was not enough. A customer who stopped
        buying HAS a baseline -- that is what makes them lapsed rather than
        new -- so the old guard let -1.0 through, and the sheet printed
        '-100%' on the very row captioned 'no percentage is printed'. Both
        ends have to be real for the ratio to mean anything.
        """
        if self.baseline <= 0 or self.current <= 0:
            return None
        return self.change / self.baseline

    @property
    def bucket(self) -> str:
        # Refunds first: a negative period is not an empty one, and testing
        # `current <= 0` for LAPSED swept these in silently.
        if self.current < 0:
            return REFUNDED
        if self.baseline <= 0:
            return NEW
        if self.current == 0:
            return LAPSED
        pct = self.change_pct
        if pct <= -self.threshold:
            return DECLINED
        if pct >= self.threshold:
            return GREW
        return STABLE

    @property
    def change_year(self) -> float | None:
        """Movement against the same period a year earlier."""
        if not self.year_known:
            return None
        return self.current - self.baseline_year

    @property
    def change_pct_year(self) -> float | None:
        """None wherever a percentage would be a lie, as for the average."""
        if not self.year_known or self.baseline_year <= 0 or self.current <= 0:
            return None
        return (self.current - self.baseline_year) / self.baseline_year

    @property
    def agreement(self) -> str:
        """Whether the two comparisons tell the same story.

        This is the reason for running both. An agency down against recent
        months AND against last year is in real decline. Down against only
        one of them is a blip, or a season -- and on August 2026 the two
        lists agreed on barely a fifth of the names either flagged.
        """
        if not self.year_known:
            return ""
        a, b = self.change_pct, self.change_pct_year
        if a is None or b is None:
            return "only one side comparable"
        t = self.threshold
        down = (a <= -t, b <= -t)
        up = (a >= t, b >= t)
        if all(down):
            return "down on both"
        if all(up):
            return "up on both"
        if down[0]:
            return "down vs recent only"
        if down[1]:
            return "down vs last year only"
        if up[0]:
            return "up vs recent only"
        if up[1]:
            return "up vs last year only"
        return "steady on both"

    @property
    def is_group(self) -> bool:
        """More than one account, so the sheet offers a '+' to expand."""
        return len(self.accounts) > 1

    @property
    def thin_baseline(self) -> bool:
        """The average was taken over more windows than they traded in.

        Not an error -- it is how an irregular buyer is meant to be measured --
        but it changes what the percentage means, so it is shown.
        """
        return bool(self.baseline > 0 and self.windows_traded < self.trailing)

    @property
    def verdict(self) -> str:
        return {DECLINED: "DOWN", GREW: "UP", LAPSED: "STOPPED BUYING",
                NEW: "NEW", STABLE: "steady",
                REFUNDED: "REFUNDED MORE THAN SOLD"}[self.bucket]


@dataclass
class Result:
    settings: Settings
    movements: list = field(default_factory=list)
    below_floor: int = 0
    warnings: list = field(default_factory=list)
    period_incomplete: bool = False
    baseline_truncated: bool = False
    #: Set when no answer could honestly be given; nothing else is filled in.
    refused: str = ""
    #: Set when the period was cut back to where the data ends.
    period_clamped: str = ""
    rows_read: int = 0

    @property
    def all(self) -> list:
        return self.movements

    def by_name(self, name: str) -> "Movement | None":
        for m in self.movements:
            if m.customer == name:
                return m
        return None

    def _bucket(self, kind: str, *, biggest_first: bool) -> list:
        got = [m for m in self.movements if m.bucket == kind]
        # by money, never by percentage
        got.sort(key=lambda m: -m.change if biggest_first else m.change)
        return got

    @property
    def declined(self) -> list:
        return self._bucket(DECLINED, biggest_first=False)

    @property
    def grew(self) -> list:
        return self._bucket(GREW, biggest_first=True)

    @property
    def lapsed(self) -> list:
        return self._bucket(LAPSED, biggest_first=False)

    @property
    def new(self) -> list:
        return self._bucket(NEW, biggest_first=True)

    @property
    def refunded(self) -> list:
        return self._bucket(REFUNDED, biggest_first=False)

    @property
    def stable(self) -> list:
        return [m for m in self.movements if m.bucket == STABLE]

    @property
    def counts(self) -> dict:
        out = {k: 0 for k in (DECLINED, GREW, LAPSED, NEW, STABLE, REFUNDED)}
        for m in self.movements:
            out[m.bucket] += 1
        return out

    @property
    def reported(self) -> list:
        """The movers the chosen direction asks for, stable never among them."""
        d = self.settings.direction
        down = self.declined + self.lapsed + self.refunded
        up = self.grew + self.new
        if d == DROPPED:
            return down
        if d == INCREASED:
            return up
        return down + up

    @property
    def money_lost(self) -> float:
        return sum(m.change
                   for m in self.declined + self.lapsed + self.refunded)

    @property
    def money_gained(self) -> float:
        return sum(m.change for m in self.grew + self.new)

    def describe(self) -> str:
        """The settings as a sentence, so the sheet can be forwarded alone."""
        s = self.settings
        moved = {DROPPED: "fell", INCREASED: "rose"}.get(s.direction, "moved")
        against = {DROPPED: "below", INCREASED: "above"}.get(s.direction, "from")
        what = {MEASURE_GROSS: "gross ticket sales",
                MEASURE_NET: "ticket sales net of refunds and voids",
                MEASURE_ALL: "ticket sales net of refunds, voids, penalties "
                             "and reissue adjustments",
                MEASURE_TICKETS: "tickets issued"}[s.measure]
        who = "Agencies" if s.channels == AGENCY_CHANNELS else "Customers"
        year = f"{_year_window(s)[0]:%b %Y}"
        if s.baseline == BASELINE_LAST_YEAR:
            basis = f"the same period a year earlier ({year})"
        elif s.baseline == BASELINE_BOTH:
            basis = (f"their own average for {baseline_label(s)}, with "
                     f"{year} shown beside it")
        else:
            basis = f"their own average for {baseline_label(s)}"
        said = (f"{who} whose {what} in {period_label(s)} {moved} "
                f"{s.threshold:.0%} or more {against} {basis}")
        if s.floor > 0:
            said += f", ignoring anyone below {s.floor:,.0f} {s.unit}"
        return said + "."


def _identity(row: dict, by_account: dict | None = None) -> tuple:
    """What makes two rows the same customer.

    The account number, not the trading name. 281 agency names in this
    warehouse map to more than one Customer ID, so grouping on the name
    silently merges distinct agencies into one row; and 652 IDs carry more
    than one name, which is the same account renamed and SHOULD merge. The
    name is kept only for display.
    """
    cid = str(row.get("customer_id") or "").strip()
    name = str(row.get("customer") or "").strip()
    if by_account:
        # One business, however many accounts it trades under. 147 agencies
        # hold 333 accounts between them, and reported per account they
        # understate the agency and misplace it in every ranking.
        group = by_account.get(cid)
        if group is not None:
            return (group.key, group.name)
    return (cid or f"name:{name.upper()}", name)


def _accumulate(rows: "Iterable", res: Result, n: int, threshold: float,
                yi: int | None = None, max_window: int | None = None,
                by_account: dict | None = None) -> dict:
    """Per-customer running totals, current window against the baseline ones."""
    acc: dict = {}
    sub: dict = {}
    for r in rows:
        res.rows_read += 1
        key, name = _identity(r, by_account)
        if not name and not key:
            continue
        try:
            w = int(r.get("window"))
        except (TypeError, ValueError):
            continue
        if w < 0 or w > (max_window if max_window is not None else n):
            continue
        m = acc.get(key)
        if m is None:
            m = acc[key] = Movement(customer=name, trailing=n,
                                    threshold=threshold)
        for attr in ("customer_id", "iata", "zone", "station", "sales_person",
                     "agent_type", "channel"):
            if not getattr(m, attr):
                setattr(m, attr, str(r.get(attr) or "").strip())
        last = r.get("last_bought")
        if isinstance(last, date) and (m.last_bought is None
                                       or last > m.last_bought):
            m.last_bought = last
            if name:
                m.customer = name        # the name they trade under now
        amount = float(r.get("amount") or 0.0)
        tickets = int(r.get("tickets") or 0)
        if by_account:
            cid = str(r.get("customer_id") or "").strip()
            acct = sub.setdefault(key, {}).setdefault(
                cid, [cid, str(r.get("customer") or "").strip(), 0.0, 0.0])
            if w == 0:
                acct[2] += amount
            elif 1 <= w <= n:
                acct[3] += amount
        if w == 0:
            m.current += amount
            m.tickets_current += tickets
            continue
        # Under BOTH the year-earlier window sits past the averaged ones, so
        # it feeds only its own total. Under LAST_YEAR it is index 1 and is
        # both -- the averaged baseline there IS the year.
        if yi is not None and w == yi:
            m.baseline_year += amount
            m.year_known = True
        if 1 <= w <= n:
            m.baseline += amount
            m.tickets_baseline += tickets
            if amount:
                m.windows_traded += 1
    for key, accounts in sub.items():
        m = acc.get(key)
        if m is None:
            continue
        # the divisor is applied to the group total later, so the member
        # baselines are averaged here to match
        m.accounts = sorted(
            ((cid, nm, cur, base / n) for cid, nm, cur, base
             in accounts.values()), key=lambda a: -(a[2] + a[3]))
    return acc


def usable_baseline_windows(s: Settings, data_first_day: date | None) -> int:
    """How many baseline windows the data actually covers.

    The windows run most-recent-first and are contiguous, so the ones the
    data cannot reach are always the oldest. Dropping those and averaging
    over what is left beats averaging over windows that are silently zero:
    asking for June 2025 against three preceding months, on a warehouse that
    starts that May, divided one month's sales by three and turned 318
    agencies into heroes.
    """
    base = windows(s)[1:1 + averaged_count(s)]
    if data_first_day is None:
        return len(base)
    return sum(1 for (a, _b) in base if a >= data_first_day)


def year_available(s: Settings, data_first_day: date | None) -> bool:
    """Whether the data reaches the year-earlier window at all."""
    if year_index(s) is None:
        return False
    if data_first_day is None:
        return True
    return _year_window(s)[0] >= data_first_day


def clamp_period(s: Settings, data_last_day: date | None) -> tuple:
    """Shorten a period that overruns the data, and say so.

    A part-month against whole months shows the whole book collapsing -- 723
    agencies down against 34 up, for a September on a warehouse that stops on
    the 19th. Comparing the same number of days on each side is the only
    honest answer available, so the period is cut to what exists and the
    baseline windows follow it.

    Returns (settings, note). The note is "none" when the period starts after
    the data ends, which the caller turns into a refusal.
    """
    if data_last_day is None or s.period_to <= data_last_day:
        return s, ""
    if data_last_day < s.period_from:
        return s, "none"
    from dataclasses import replace
    days = (data_last_day - s.period_from).days + 1
    return replace(s, period_to=data_last_day), (
        f"You asked for {_dm(s.period_from)} – {_dm(s.period_to)}, but the "
        f"data ends on {_dm(data_last_day)}. The period was shortened to "
        f"match, and is compared with windows of the same {days} days — a "
        f"part-period against whole ones shows every customer falling.")


def build(rows: "Iterable", settings: Settings, *,
          data_first_day: date | None = None,
          data_last_day: date | None = None,
          period_clamped: str = "",
          by_account: dict | None = None) -> Result:
    """Fold per-customer, per-window totals into buckets.

    `rows` are mappings with at least customer, window and amount, where
    window 0 is the reporting period and 1..N are the baseline windows. The
    aggregation happens in SQL -- this is the part that decides what the
    numbers MEAN, which is why it takes tallies and not eight million rows.
    """
    res = Result(settings=settings, period_clamped=period_clamped)
    # However many baseline windows the mode produced -- three for a
    # trailing average, one for last year. Reading `trailing` directly
    # would divide the last-year comparison by three.
    # Only the AVERAGED windows count towards the divisor. Reading the whole
    # window list would include the year-earlier one under BOTH and divide a
    # three-month average by four.
    asked = averaged_count(settings)
    n = usable_baseline_windows(settings, data_first_day) or asked

    # A baseline the data cannot reach at all is not a small problem to note
    # in the margin. Asking for February 2026 against February 2025, on a
    # warehouse that starts in May 2025, produced a workbook in which all 788
    # agencies were "new" -- confident, complete, and meaningless. Refuse it.
    if data_first_day is not None and usable_baseline_windows(
            settings, data_first_day) == 0:
        lo, hi = baseline_span(settings)
        res.refused = (
            f"There is no data for the baseline at all. It covers "
            f"{_dm(lo)} to {_dm(hi)}, and the warehouse starts on "
            f"{_dm(data_first_day)}. Every customer would be reported as "
            f"new, which says nothing about any of them. Pick a later "
            f"period, or a baseline that falls inside the data.")
        return res
    if n < asked and data_first_day is not None:
        res.warnings.append(
            f"The baseline was averaged over {n} window(s), not the {asked} "
            f"asked for: the warehouse starts on {_dm(data_first_day)} and "
            f"the older windows fall outside it. Averaging over windows that "
            f"are silently zero lowers the baseline and flatters everyone.")
    if period_clamped:
        res.warnings.append(period_clamped)

    if data_last_day and settings.period_to > data_last_day:
        res.period_incomplete = True
        res.warnings.append(
            f"The period runs to {_dm(settings.period_to)} but the data stops "
            f"on {_dm(data_last_day)}. A part-period compared with whole ones "
            f"shows every customer falling; shorten the period or wait for "
            f"the load.")
    lo, _ = baseline_span(settings)
    if data_first_day and lo < data_first_day:
        res.baseline_truncated = True
        res.warnings.append(
            f"The baseline reaches back to {_dm(lo)} but the data starts on "
            f"{_dm(data_first_day)}. The missing windows count as zero, which "
            f"lowers the average and flatters everyone.")

    yi = year_index(settings)
    acc = _accumulate(rows, res, n, settings.threshold, yi=yi,
                      max_window=len(windows(settings)) - 1,
                      by_account=by_account)

    for m in acc.values():
        # the divisor is the windows ASKED FOR, not the ones they traded in
        m.baseline /= n
        m.tickets_baseline /= n
        # a refund-heavy window can net below zero; that is not a baseline
        m.baseline = max(0.0, m.baseline)
        if m.current == 0 and m.baseline <= 0:
            continue
        # A customer with no baseline is judged on what they did this period,
        # or a baseline floor would silently delete every new one -- and the
        # size of a refund is its magnitude, not its sign.
        basis = m.baseline if m.baseline > 0 else abs(m.current)
        if settings.floor > 0 and basis < settings.floor:
            res.below_floor += 1
            continue
        res.movements.append(m)
    return res


# --------------------------------------------------------------------------
# reading the warehouse
# --------------------------------------------------------------------------
def _lines(measure: str) -> tuple:
    if measure in (MEASURE_GROSS, MEASURE_TICKETS):
        return GROSS_LINES
    return ALL_LINES if measure == MEASURE_ALL else NET_LINES


def _quoted(values: "Iterable") -> str:
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)


def _sql(settings: Settings, target: str) -> str:
    """The one query, shaped for the chosen windows, measure and channels.

    Everything spliced in is a `date` (guarded at `Settings`), an internal
    constant, or the escaped warehouse path. Nothing the operator types
    reaches this as text.
    """
    wins = windows(settings)
    case = "\n".join(
        f"          when \"Pure Date\" between DATE '{a}' and DATE '{b}' "
        f"then {i}" for i, (a, b) in enumerate(wins))
    lo, hi = wins[-1][0], wins[0][1]
    value = ("1" if settings.measure == MEASURE_TICKETS
             else '"Balance (base currency)"')
    where_channel = (f'  and "Channel" in ({_quoted(settings.channels)})\n'
                     if settings.channels else "")
    # Grouped on the account number, with the trading name carried only for
    # display: 281 agency names in this warehouse map to more than one
    # Customer ID. arg_max takes each field from the customer's most recent
    # row, so an agency that was renamed or moved zone shows its current
    # details rather than whichever row happened to be read first.
    sql = f"""
        select coalesce("Customer ID", "Customer") as customer_id,
               case
{case}
               end as win,
               arg_max("Customer", "Pure Date") as customer,
               sum({value}) as amount,
               count(*) as tickets,
               max("Pure Date") as last_bought,
               arg_max("IATA Agency Code", "Pure Date") as iata,
               arg_max("Zone", "Pure Date") as zone,
               arg_max("Station", "Pure Date") as station,
               arg_max("Sales Person", "Pure Date") as sales_person,
               arg_max("Agent Type", "Pure Date") as agent_type,
               arg_max("Channel", "Pure Date") as channel
        from read_parquet('{target}')
        where "Customer" is not null
          and "Pure Date" between DATE '{lo}' and DATE '{hi}'
          and "Transaction" in ({_quoted(_lines(settings.measure))})
{where_channel}        group by customer_id, win
    """
    return sql


def fetch(source: "WarehouseSource", settings: Settings, *,
          progress_cb=None) -> list:
    """Per-customer, per-window tallies straight out of the warehouse.

    Eight million rows are reduced to a few thousand by the query; nothing
    larger ever reaches Python.
    """
    from . import counter_reconcile as cr
    duckdb = cr._duckdb()
    if duckdb is None:
        raise ValueError("duckdb is not available in this build, so sales "
                         "movement cannot be measured.")
    target = (source.path.as_posix() if source.kind == "gold"
              else source.path.as_posix() + "/**/*.parquet").replace("'", "''")
    sql = _sql(settings, target)
    if progress_cb is not None:
        progress_cb(0)
    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    cols = ("customer_id", "window", "customer", "amount", "tickets",
            "last_bought", "iata", "zone", "station", "sales_person",
            "agent_type", "channel")
    out = [dict(zip(cols, row)) for row in con.execute(sql).fetchall()]
    if progress_cb is not None:
        progress_cb(len(out))
    return out


def run(source: "WarehouseSource", settings: Settings, *,
        progress_cb=None, group_accounts: bool = True) -> Result:
    """Fetch and fold, refusing rather than guessing where the data runs out.

    The period is cut back to what the warehouse actually holds BEFORE the
    query runs, so both sides of the comparison cover the same number of days.
    """
    first = getattr(source, "first_day", None)
    last = getattr(source, "last_day", None)
    effective, clamped = clamp_period(settings, last)
    if clamped == "none":
        res = Result(settings=settings)
        res.refused = (
            f"The period starts after the data ends. The warehouse runs to "
            f"{_dm(last)}, and nothing has been loaded for "
            f"{_dm(settings.period_from)} onwards.")
        return res
    rows = fetch(source, effective, progress_cb=progress_cb)
    # Group the accounts into businesses before anything is measured.
    # 147 agencies hold 333 accounts between them, and left apart they
    # understate the agency and misplace it by as much as 491 ranks.
    by_account = None
    if group_accounts:
        from . import agency_identity as ai
        groups, _skipped = ai.resolve(
            {"customer_id": r.get("customer_id"),
             "customer": r.get("customer"), "iata": r.get("iata"),
             "value": abs(float(r.get("amount") or 0.0))}
            for r in rows)
        by_account = {cid: g for g in groups.values() for cid in g.ids}
    return build(rows, effective, data_first_day=first, data_last_day=last,
                 period_clamped=clamped, by_account=by_account)
