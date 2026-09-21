"""Where one agency stands with BS: share, growth and rank, this year and last.

Six numbers, and five of them have a way of being quietly wrong. Each is
measured against a stated denominator over a stated window, because every one
of those choices moves the answer:

* The denominator. Be Fresh holds 2.292% of agency sales but 2.158% of all
  sales including web and counter. Both are true; only one is "market share",
  so the sheet says which.
* Gross against net. On gross Be Fresh ranks 7th, on net 8th. A rank that
  changes with an unstated choice is not a fact.
* The window. The warehouse starts on 1 May 2025, so a year-to-date figure
  compares nine months against five and reports the missing four as growth.
  Both sides therefore use the SAME span, and the span is printed.
* The accounts. 206 agencies trade under more than one account number, and
  grouping them moves ranks by as much as 491 places. `agency_identity` does
  that grouping; nothing here ever sees a bare account.
* The tail. At rank 500 the gap to the next agency is 1,874 BDT on 5.9M, and
  at rank 2,000 it is 323 BDT -- one ticket. A rank down there is noise, and
  is reported with a band and a flag rather than as a position.

The sixth, BSP, needs no inference: `Agent Type` already classifies every
account as BSP, Non-IATA, Non BSP or blank, and it never changes for an
account. It is a property of the ACCOUNT rather than the business, which is
the whole reason grouping and this metric belong together -- Be Fresh trades
through a Non-IATA account carrying 2,199.0M and two BSP accounts carrying
289.9M between them, and "growth in BSP" means the second of those.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import agency_identity as ai

#: The accreditations `Agent Type` records. BSP is the one the BSP metric
#: counts; the rest are carried so the sheet can show the whole business.
TYPE_BSP = "BSP"
TYPE_NON_IATA = "Non-IATA"
TYPE_NON_BSP = "Non BSP"

#: Below this rank, one ticket moves a position, so it is shown as a band.
NOISE_RANK = 200


def _growth(now: float, before: float):
    """Growth, or None where there is nothing to grow from."""
    return ((now - before) / before) if before > 0 else None


@dataclass
class Side:
    """One agency over one window."""

    sales: float = 0.0
    #: Split by the accreditation of the account the sale went through.
    by_type: dict = field(default_factory=dict)
    market: float = 0.0
    rank: int | None = None
    agencies: int = 0
    accounts: int = 0

    @property
    def traded(self) -> bool:
        return self.sales > 0

    @property
    def bsp(self) -> float:
        return self.by_type.get(TYPE_BSP, 0.0)

    @property
    def non_iata(self) -> float:
        return self.by_type.get(TYPE_NON_IATA, 0.0)

    @property
    def share(self):
        """None where the agency did not trade in this window at all.

        Returning 0.0 there contradicted the card's own warning, which
        promises the figure is left blank rather than shown as zero -- and
        a 0% share reads as "they lost the market" rather than "they were
        not in it".
        """
        if self.market <= 0 or not self.traded:
            return None
        return self.sales / self.market

    @property
    def bsp_share(self):
        """How much of this agency's own business runs through BSP."""
        return (self.bsp / self.sales) if self.sales > 0 else None

    @property
    def rank_is_noise(self) -> bool:
        return bool(self.rank and self.rank > NOISE_RANK)

    @property
    def rank_band(self) -> str:
        if not self.rank or not self.agencies:
            return ""
        top = self.rank / self.agencies
        for edge, label in ((0.01, "top 1%"), (0.05, "top 5%"),
                            (0.10, "top 10%"), (0.25, "top quarter"),
                            (0.50, "top half")):
            if top <= edge:
                return label
        return "bottom half"


@dataclass
class Scorecard:
    """The six metrics for one agency, with what they rest on."""

    agency: object
    now: Side = field(default_factory=Side)
    before: Side = field(default_factory=Side)
    period: tuple = ()
    prior: tuple = ()
    measure_label: str = "gross ticket sales"
    denominator_label: str = "all agency sales with BS"
    warnings: list = field(default_factory=list)

    # --- the six -----------------------------------------------------------
    @property
    def share_now(self):
        return self.now.share

    @property
    def share_before(self):
        return self.before.share

    @property
    def growth(self):
        return _growth(self.now.sales, self.before.sales)

    @property
    def growth_bsp(self):
        return _growth(self.now.bsp, self.before.bsp)

    @property
    def growth_non_iata(self):
        return _growth(self.now.non_iata, self.before.non_iata)

    @property
    def rank_now(self):
        return self.now.rank

    @property
    def rank_before(self):
        return self.before.rank

    # --- reading them ------------------------------------------------------
    @property
    def comparable(self) -> bool:
        """Whether last year exists for this agency at all.

        1,558 of the 3,471 agencies trading this year did not trade in the
        same window last year. For those, a share, a growth and a rank are
        not small numbers -- they do not exist.
        """
        return self.before.traded

    @property
    def rank_move(self):
        """Positive means they climbed."""
        if self.rank_now is None or self.rank_before is None:
            return None
        return self.rank_before - self.rank_now

    @property
    def share_move(self):
        a, b = self.share_now, self.share_before
        return None if a is None or b is None else a - b

    @property
    def has_bsp(self) -> bool:
        return self.now.bsp > 0 or self.before.bsp > 0

    def span_label(self) -> str:
        def one(w):
            return f"{w[0].day} {w[0]:%b %Y} to {w[1].day} {w[1]:%b %Y}"
        return f"{one(self.period)}, against {one(self.prior)}"

    def headline(self) -> str:
        """One line a non-technical reader can act on."""
        name = getattr(self.agency, "name", "")
        if not self.now.traded:
            return f"{name} has no sales with BS in this window."
        bits = [f"{self.share_now:.3f}% of {self.denominator_label}"]
        if self.rank_now:
            bits.append(f"ranked {self.rank_now:,} of {self.now.agencies:,}")
        if self.comparable and self.growth is not None:
            bits.append(f"{self.growth:+.1%} year on year")
        else:
            bits.append("no sales in the same window last year")
        return f"{name}: " + ", ".join(bits) + "."


def like_for_like(period_from: date, period_to: date, *,
                  data_first_day: date | None = None,
                  data_last_day: date | None = None) -> tuple:
    """This window and the same one a year earlier, both inside the data.

    Returns (period, prior, notes). Where the year-earlier window starts
    before the warehouse does, BOTH ends move together -- comparing nine
    months against five reports the four missing ones as growth, which is
    how a year-to-date figure lies.
    """
    from .sales_movement import a_year_before

    notes = []
    first, last = period_from, period_to
    if data_last_day and last > data_last_day:
        notes.append(f"The window was shortened to {data_last_day.day} "
                     f"{data_last_day:%b %Y}, where the data ends.")
        last = data_last_day
    p_from, p_to = a_year_before(first), a_year_before(last)
    if data_first_day and p_from < data_first_day:
        shift = (data_first_day - p_from).days
        notes.append(
            f"Last year's window begins on {data_first_day.day} "
            f"{data_first_day:%b %Y}, where the warehouse starts, so both "
            f"sides were trimmed by {shift} day(s) to cover the same span. "
            f"Comparing a longer window with a shorter one reports the "
            f"missing days as growth.")
        p_from = date.fromordinal(p_from.toordinal() + shift)
        first = date.fromordinal(first.toordinal() + shift)
    return (first, last), (p_from, p_to), notes


def _side(groups: dict, key: str, per_group: dict, per_type: dict) -> Side:
    """One window's figures for one agency, and its place in the book."""
    ordered = sorted(per_group.items(), key=lambda kv: -kv[1])
    side = Side(
        sales=per_group.get(key, 0.0),
        by_type=dict(per_type.get(key, {})),
        market=sum(per_group.values()),
        agencies=sum(1 for _k, v in ordered if v > 0),
        accounts=len(groups[key].members) if key in groups else 0)
    if side.sales > 0:
        for i, (k, _v) in enumerate(ordered, start=1):
            if k == key:
                side.rank = i
                break
    return side


def build(agency, groups: dict, now_totals: tuple, before_totals: tuple, *,
          period: tuple, prior: tuple, measure_label: str = "gross ticket sales",
          denominator_label: str = "all agency sales with BS",
          warnings=None) -> Scorecard:
    """Assemble the card from two windows of per-agency totals.

    `now_totals` and `before_totals` are each (per_group, per_type): a map of
    group key to money, and a map of group key to {agent type: money}.
    """
    card = Scorecard(agency=agency, period=period, prior=prior,
                     measure_label=measure_label,
                     denominator_label=denominator_label,
                     warnings=list(warnings or ()))
    card.now = _side(groups, agency.key, *now_totals)
    card.before = _side(groups, agency.key, *before_totals)
    if not card.comparable and card.now.traded:
        card.warnings.append(
            "This agency had no sales in the same window last year, so the "
            "last-year share, growth and ranking do not exist. They are left "
            "blank rather than shown as zero.")
    if card.now.rank_is_noise:
        card.warnings.append(
            f"A rank of {card.now.rank:,} is inside the long tail, where the "
            f"gap between neighbours is a few hundred taka — one ticket moves "
            f"it. Read the band, not the position.")
    if getattr(agency, "code_conflict", False):
        card.warnings.append(
            "The accounts grouped under this name carry two different IATA "
            "codes, so they may be two businesses. Check the accounts listed "
            "before relying on the total.")
    return card


# --------------------------------------------------------------------------
# reading the warehouse
# --------------------------------------------------------------------------
def fetch_window(source, first: date, last: date, *, measure_lines,
                 channels=("AGENCY",)) -> list:
    """Per-account totals for one window, split by accreditation."""
    from . import counter_reconcile as cr
    from .sales_movement import _quoted

    duckdb = cr._duckdb()
    if duckdb is None:
        raise ValueError("duckdb is not available in this build, so the "
                         "agency scorecard cannot be built.")
    target = (source.path.as_posix() if source.kind == "gold"
              else source.path.as_posix() + "/**/*.parquet").replace("'", "''")
    where_channel = (f'  and "Channel" in ({_quoted(channels)})\n'
                     if channels else "")
    sql = f"""
        select "Customer ID" as customer_id,
               arg_max("Customer", "Pure Date") as customer,
               arg_max("IATA Agency Code", "Pure Date") as iata,
               arg_max("Agent Type", "Pure Date") as agent_type,
               sum("Balance (base currency)") as value
        from read_parquet('{target}')
        where "Customer ID" is not null
          and "Pure Date" between DATE '{first}' and DATE '{last}'
          and "Transaction" in ({_quoted(measure_lines)})
{where_channel}        group by 1
    """
    con = duckdb.connect()
    con.execute("SET enable_progress_bar=false")
    cols = ("customer_id", "customer", "iata", "agent_type", "value")
    return [dict(zip(cols, row)) for row in con.execute(sql).fetchall()]


def _totals(rows, groups: dict) -> tuple:
    """Fold account rows into per-agency money and per-agency type splits."""
    per_group: dict = {}
    per_type: dict = {}
    by_id = {}
    for key, g in groups.items():
        for cid, _name, _v in g.members:
            by_id[cid] = key
    for r in rows:
        key = by_id.get(str(r.get("customer_id") or "").strip())
        if key is None:
            continue
        value = float(r.get("value") or 0.0)
        per_group[key] = per_group.get(key, 0.0) + value
        atype = str(r.get("agent_type") or "").strip() or "(blank)"
        per_type.setdefault(key, {})
        per_type[key][atype] = per_type[key].get(atype, 0.0) + value
    return per_group, per_type


def run(source, term: str, first: date, last: date, *,
        measure_lines=("Ticket payment",),
        measure_label: str = "gross ticket sales",
        channels=("AGENCY",)) -> tuple:
    """Look an agency up and score it. Returns (cards, candidates, notes).

    More than one candidate means the term was ambiguous -- 'TRAVELS' matches
    1,825 agency names -- so the caller is handed the list rather than a
    guess. `cards` is filled only when exactly one agency matched.
    """
    period, prior, notes = like_for_like(
        first, last,
        data_first_day=getattr(source, "first_day", None),
        data_last_day=getattr(source, "last_day", None))
    now_rows = fetch_window(source, *period, measure_lines=measure_lines,
                            channels=channels)
    before_rows = fetch_window(source, *prior, measure_lines=measure_lines,
                               channels=channels)
    # one identity map over BOTH windows, so an agency that changed account
    # between them is still one business
    groups, _skipped = ai.resolve(now_rows + before_rows)
    candidates = ai.find(groups, term)
    if len(candidates) != 1:
        return [], candidates, notes
    card = build(candidates[0], groups, _totals(now_rows, groups),
                 _totals(before_rows, groups), period=period, prior=prior,
                 measure_label=measure_label, warnings=notes)
    return [card], candidates, notes
