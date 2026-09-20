"""The Sales Movement workbook.

Sheets:
  Summary          what was compared, in words, and what the numbers rest on
  Declined         biggest money lost first -- never biggest percentage first
  Stopped buying   had a baseline, bought nothing this period
  Refunded more..  traded, but the net for the period ran backwards
  Grew             biggest money gained first
  New              no baseline at all, so no percentage is printed
  By zone          where the money moved, rolled up
  By sales person  the same roll-up against whoever owns the account

The Summary carries every caveat rather than leaving it in a docstring: the
windows actually compared, whether the period ran past the end of the data,
whether the baseline reached back before it, and how many rows were held back
by the floor. A sheet that cannot be forwarded without a covering note is not
finished.
"""
from __future__ import annotations

from collections import defaultdict

from openpyxl import Workbook

from . import sales_movement as sm
from .counter_master import (BAD, GOOD, GREY, LAST, MONEY, NAVY, PAPER, PCT,
                             WARN, _band, _cell, _headers, _kpi_strip)

#: This workbook's own column widths. The 21-column grid is kept so the
#: shared band and KPI helpers line up, but sales rows need wider money
#: columns than the counter sheets do.
WIDTHS = [30, 13, 18, 12, 8, 15, 15, 15, 9, 9, 9, 11, 12, 10, 10,
          7, 7, 7, 7, 7, 22]


def headers(settings: sm.Settings) -> list:
    """Column titles, in the unit actually being measured.

    These used to be hardcoded to BDT. Choosing "Tickets issued" then
    presented a count of tickets under a heading that said BDT, all the way
    through the workbook.
    """
    unit = settings.unit
    return ["Agency / customer", "IATA code", "Sales person", "Zone",
            "Station", f"Baseline average ({unit})", f"This period ({unit})",
            f"Change ({unit})", "Change %", "Tickets avg", "Tickets now",
            "Months traded", "Last bought", "Agent type", "Channel",
            "", "", "", "", "", "Verdict"]


def _fmt(settings: sm.Settings) -> str:
    """A ticket count is a count, not an amount of money."""
    return "#,##0" if settings.measure == sm.MEASURE_TICKETS else MONEY


def _prep(ws) -> None:
    for i, width in enumerate(WIDTHS, start=1):
        ws.column_dimensions[chr(64 + i)].width = width
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A5"


def _title(ws, text, note) -> None:
    ws.merge_cells(f"A1:{LAST}1")
    _cell(ws, 1, 1, f"  {text}", bold=True, size=16, color="FFFFFF", fill=NAVY,
          align="left")
    ws.row_dimensions[1].height = 34
    ws.merge_cells(f"A2:{LAST}2")
    _cell(ws, 2, 1, f"  {note}", size=9, color=GREY, fill=PAPER, align="left",
          wrap=True)
    ws.row_dimensions[2].height = 30


def _tone(m) -> str | None:
    if m.bucket in (sm.DECLINED, sm.LAPSED, sm.REFUNDED):
        return BAD
    if m.bucket in (sm.GREW, sm.NEW):
        return GOOD
    return None


def write_movers(ws, res: sm.Result, movements, title: str, note: str) -> None:
    """One bucket, ranked by money. The percentage is a filter, not a sort."""
    _prep(ws)
    _title(ws, title, note)
    money = _fmt(res.settings)
    r = 4
    r = _headers(ws, r, headers(res.settings))
    if not movements:
        _cell(ws, r, 1, "Nobody fell into this group.", size=10, color=GREY)
        return
    for m in movements:
        _cell(ws, r, 1, m.customer, bold=True, size=10, border=True)
        _cell(ws, r, 2, m.iata or None, size=9, border=True, align="center")
        _cell(ws, r, 3, m.sales_person or None, size=9, border=True)
        _cell(ws, r, 4, m.zone or None, size=9, border=True, align="center")
        _cell(ws, r, 5, m.station or None, size=9, border=True, align="center")
        _cell(ws, r, 6, round(m.baseline) or None, fmt=money, size=9,
              border=True, align="right")
        # a refunder's period is a real negative, so 0 is not "or None"d
        # away here the way an empty baseline is
        _cell(ws, r, 7, round(m.current) if m.current else None, fmt=money,
              size=9, border=True, align="right")
        _cell(ws, r, 8, round(m.change), fmt=money, size=10, bold=True,
              border=True, align="right")
        # a new customer has no denominator, so the cell stays empty rather
        # than carrying a zero that reads as "no change"
        _cell(ws, r, 9, m.change_pct, fmt=PCT, size=9, border=True,
              align="center", fill=_tone(m))
        _cell(ws, r, 10, round(m.tickets_baseline) or None, size=9,
              border=True, align="center")
        _cell(ws, r, 11, m.tickets_current or None, size=9, border=True,
              align="center")
        # said out loud: an average over 3 windows they traded 1 of means
        # something different from a steady buyer's average
        _cell(ws, r, 12, f"{m.windows_traded} of {m.trailing}"
              if m.baseline > 0 else None, size=9, border=True, align="center",
              fill=WARN if m.thin_baseline else None)
        _cell(ws, r, 13, m.last_bought, fmt="dd mmm yy", size=9, border=True,
              align="center")
        _cell(ws, r, 14, m.agent_type or None, size=9, border=True,
              align="center")
        _cell(ws, r, 15, m.channel or None, size=9, border=True,
              align="center")
        for j in range(16, 21):
            _cell(ws, r, j, None, border=True)
        _cell(ws, r, 21, m.verdict, bold=True, size=9, fill=_tone(m),
              border=True, align="center")
        r += 1


def _rollup(ws, r, res: sm.Result, key, heading: str, note: str) -> int:
    """Net movement grouped by one dimension, worst first."""
    r = _band(ws, r, heading, note)
    unit = res.settings.unit
    r = _headers(ws, r, [heading.title(), "Agencies down", "Agencies up",
                         "Stopped buying", "New", f"Lost ({unit})",
                         f"Gained ({unit})", f"Net ({unit})", "", "", "",
                         "", "", "", "", "", "", "", "", "", ""])
    groups: dict = defaultdict(lambda: [0, 0, 0, 0, 0.0, 0.0])
    for m in res.movements:
        g = groups[key(m) or "(not recorded)"]
        if m.bucket == sm.DECLINED:
            g[0] += 1
        elif m.bucket == sm.GREW:
            g[1] += 1
        elif m.bucket == sm.LAPSED:
            g[2] += 1
        elif m.bucket == sm.NEW:
            g[3] += 1
        elif m.bucket == sm.REFUNDED:
            g[0] += 1
        if m.change < 0:
            g[4] += m.change
        else:
            g[5] += m.change
    for name, g in sorted(groups.items(), key=lambda kv: kv[1][4] + kv[1][5]):
        net = g[4] + g[5]
        _cell(ws, r, 1, name, bold=True, size=10, border=True)
        for j, v in enumerate(g[:4], start=2):
            _cell(ws, r, j, v or None, size=9, border=True, align="center")
        _cell(ws, r, 6, round(g[4]) or None, fmt=_fmt(res.settings), size=9,
              border=True, align="right")
        _cell(ws, r, 7, round(g[5]) or None, fmt=_fmt(res.settings), size=9,
              border=True, align="right")
        _cell(ws, r, 8, round(net), fmt=_fmt(res.settings), size=10,
              bold=True, border=True, align="right",
              fill=BAD if net < 0 else GOOD)
        for j in range(9, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    return r + 1


def write_summary(ws, res: sm.Result) -> None:
    _prep(ws)
    ws.freeze_panes = "A4"
    s = res.settings
    c = res.counts
    _title(ws, f"SALES MOVEMENT  ·  {sm.period_label(s)}", res.describe())

    r = 4
    r = _band(ws, r, "AT A GLANCE",
              "ranked throughout by money, not by percentage — a fall of 3.2M "
              "at -31% matters more than one of 40k at -95%")
    r = _kpi_strip(ws, r, [
        ("Down", c[sm.DECLINED], "#,##0", "C00000"),
        ("Stopped buying", c[sm.LAPSED], "#,##0", "C00000"),
        ("Refunded > sold", c[sm.REFUNDED], "#,##0", "C00000"),
        ("Up", c[sm.GREW], "#,##0", "006100"),
        ("New", c[sm.NEW], "#,##0", "006100"),
        ("Steady", c[sm.STABLE], "#,##0", None),
        (f"Lost ({s.unit})", round(res.money_lost), _fmt(s), "C00000"),
        (f"Gained ({s.unit})", round(res.money_gained), _fmt(s), "006100"),
    ])
    r += 1

    if res.warnings:
        r = _band(ws, r, "READ THIS FIRST")
        for w in res.warnings:
            ws.merge_cells(f"A{r}:{LAST}{r}")
            _cell(ws, r, 1, f"  {w}", size=10, fill=WARN, align="left",
                  wrap=True)
            ws.row_dimensions[r].height = 30
            r += 1
        r += 1

    r = _band(ws, r, "WHAT WAS COMPARED")
    wins = sm.windows(s)
    r = _headers(ws, r, ["Window", "From", "To", "Days", "Role", "", "", "",
                         "", "", "", "", "", "", "", "", "", "", "", "", ""])
    for i, (a, b) in enumerate(wins):
        _cell(ws, r, 1, "This period" if i == 0 else f"Baseline {i}",
              bold=(i == 0), size=10, border=True)
        _cell(ws, r, 2, a, fmt="dd mmm yyyy", size=9, border=True,
              align="center")
        _cell(ws, r, 3, b, fmt="dd mmm yyyy", size=9, border=True,
              align="center")
        _cell(ws, r, 4, (b - a).days + 1, size=9, border=True, align="center")
        _cell(ws, r, 5, "measured" if i == 0 else "averaged into the baseline",
              size=9, color=GREY, border=True)
        for j in range(6, 22):
            _cell(ws, r, j, None, border=True)
        r += 1
    r += 1

    r = _band(ws, r, "WHAT THE GROUPS MEAN",
              "a percentage is only printed where a denominator exists")
    for label, text in (
        ("Down / Up",
         f"Traded in this period and in the baseline, and moved by "
         f"{s.threshold:.0%} or more against their own average."),
        ("Stopped buying",
         "Had a baseline and bought nothing at all this period. Shown "
         "separately rather than as '-100%', because the fact that matters "
         "is that they are gone, not the size of the drop."),
        ("Refunded more than sold",
         "Traded this period, but the net ran backwards — they took more "
         "money back than they spent. Not the same as lapsing: they were "
         "active, and the loss is LARGER than the baseline, so no "
         "percentage is printed."),
        ("New",
         "No baseline to divide by, so no percentage exists. Ranked by "
         "money only."),
        ("Months traded",
         "How many baseline windows the customer actually bought in. The "
         "average always divides by the number asked for, so an irregular "
         "buyer is not flattered — amber means the average covers more "
         "windows than they traded."),
        ("Held back by the floor",
         f"{res.below_floor:,} customer(s) fell below the "
         f"{s.floor:,.0f} BDT floor and are not in this workbook."),
    ):
        _cell(ws, r, 1, label, bold=True, size=10, align="left")
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=21)
        _cell(ws, r, 2, text, size=9, color=GREY, align="left", wrap=True)
        ws.row_dimensions[r].height = 28
        r += 1
    r += 1

    r = _rollup(ws, r, res, lambda m: m.zone, "BY ZONE",
                "net movement where the accounts sit")
    _rollup(ws, r, res, lambda m: m.sales_person, "BY SALES PERSON",
            "the same money against whoever owns the account")


def build_workbook(res: sm.Result, out_path) -> None:
    """The whole workbook. Empty buckets still get a sheet, saying so."""
    wb = Workbook()
    write_summary(wb.active, res)
    wb.active.title = "Summary"
    s = res.settings
    period, base = sm.period_label(s), sm.baseline_label(s)
    for name, rows, note in (
        ("Declined", res.declined,
         f"Sales in {period} at least {s.threshold:.0%} below the average "
         f"for {base}. Biggest money lost first."),
        ("Stopped buying", res.lapsed,
         f"Bought nothing at all in {period} after averaging real money "
         f"over {base}. No percentage is printed; the baseline is the loss."),
        ("Refunded more than sold", res.refunded,
         f"Traded in {period}, but took more back than they spent — the "
         f"net for the period is negative. Their loss is bigger than the "
         f"baseline, so no percentage is printed."),
        ("Grew", res.grew,
         f"Sales in {period} at least {s.threshold:.0%} above the average "
         f"for {base}. Biggest money gained first."),
        ("New", res.new,
         f"No sales at all across {base}, so there is no percentage to "
         f"print. Ranked by money."),
    ):
        write_movers(wb.create_sheet(name), res, rows, f"{name.upper()}  ·  "
                     f"{period}", note)
    wb.save(str(out_path))


def default_filename(settings: sm.Settings) -> str:
    label = sm.period_label(settings).replace(" ", "")
    return f"Sales_Movement_{label}.xlsx"
