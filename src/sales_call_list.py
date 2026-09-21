"""The decline list turned into something a rep can actually work from.

Sales Movement answers "who is down". It cannot answer "who do I ring", because
the warehouse holds no contact details -- it has agency names and money, and
nothing else. The visit reports hold exactly what is missing: a contact person,
a designation and a phone number for each agency a rep has been to.

So this joins the two. Agencies are matched on `visit_master.identity`, the
same key the visit master already uses to survive how a name was typed -- 'AB
Travel' against 'AB Travels', 'CARNIVAL AIR TICKETING LTD.' against the sales
system's 'Carnival Air Ticketing Ltd. (IATA)'.

Three rules, each because the alternative produces a list that wastes a rep's
morning or sends them to the wrong company:

* An agency with no contact on file is still on the sheet. Dropping it would
  hide the most important case there is -- an account losing real money that
  nobody has ever visited -- so it is listed with the contact columns empty
  and called out on the summary.
* A name match is not an identity. Movements are grouped by account number,
  but a contact can only be found by name, and 80 name keys in this warehouse
  are shared by two or more Customer IDs, covering 174 of 2,409 agencies.
  Those rows carry the name the contact was actually filed under, and are
  marked, rather than being handed somebody else's phone number in silence.
* The sheet is ranked by money lost, never by percentage, and it carries the
  columns a rep fills in by hand (called on, spoke to, outcome) so it comes
  back as a record rather than as a second spreadsheet.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from openpyxl import Workbook

from . import sales_movement as sm
from .counter_master import (BAD, GOOD, GREY, LAST, MONEY, NAVY, PAPER, WARN,
                             _band, _cell, _headers, _kpi_strip)

#: Wider than the movement sheet: this one carries phone numbers and names
#: rather than several money columns.
WIDTHS = [30, 12, 20, 16, 17, 12, 8, 16, 12, 9, 14, 14, 14,
          22, 12, 14, 12, 16, 22, 10, 10]

HEADERS = ["Agency", "IATA code", "Contact person", "Designation", "Phone",
           "Contact is filed under", "Station", "Sales person",
           "Last bought", "Days silent", "Baseline", "This period",
           "Change", "Why they are on this list", "Last visited",
           "Visited by", "Called on", "Spoke to", "Outcome", "", ""]

#: An agency silent for longer than this has stopped being a slow month and
#: started being a lost account.
LONG_SILENCE = 60


@dataclass
class Contact:
    """What the visit reports know about one agency."""

    agency: str = ""
    contact: str = ""
    designation: str = ""
    phone: str = ""
    rep: str = ""
    last_seen: date | None = None

    @property
    def reachable(self) -> bool:
        return bool(self.phone or self.contact)


def contacts_from_visits(data) -> dict:
    """Latest contact per agency, keyed the way the visit master keys them.

    A later visit wins, but only if it actually wrote something down -- reps
    leave the phone blank often enough that taking the newest row blindly
    would erase a number recorded the month before.
    """
    from .visit_master import identity

    out: dict = {}
    for v in getattr(data, "visits", ()) or ():
        key = identity(getattr(v, "agency", "") or "")
        if not key:
            continue
        got = out.get(key)
        if got is None:
            got = out[key] = Contact(agency=v.agency)
        day = getattr(v, "day", None)
        newer = (got.last_seen is None
                 or (isinstance(day, date) and day >= got.last_seen))
        if isinstance(day, date) and (got.last_seen is None
                                      or day > got.last_seen):
            got.last_seen = day
        for attr in ("contact", "designation", "phone", "rep"):
            value = str(getattr(v, attr, "") or "").strip()
            # fill a blank always; overwrite only from a newer visit
            if value and (newer or not getattr(got, attr)):
                setattr(got, attr, value)
    return out


@dataclass
class CallRow:
    movement: object
    contact: Contact
    days_silent: int | None = None
    #: True when this agency's name key is shared with another account on
    #: the list, so the contact may belong to the other one.
    ambiguous: bool = False

    @property
    def reachable(self) -> bool:
        return self.contact.reachable

    @property
    def why(self) -> str:
        m = self.movement
        if m.bucket == sm.LAPSED:
            return "Stopped buying altogether"
        if m.bucket == sm.REFUNDED:
            return "Refunded more than they bought"
        pct = m.change_pct
        return f"Down {abs(pct):.0%} against their baseline" if pct is not None \
            else "Down against their baseline"


def build(res: sm.Result, contacts: dict | None = None) -> list:
    """Every agency that went backwards, biggest money lost first.

    Movements are grouped by account number, but a contact can only be found
    by NAME, which re-opens the collision the account number closed: measured
    on August, 80 name keys are shared by two or more Customer IDs, covering
    174 of 2,409 agencies. Those rows are marked rather than quietly handed
    somebody else's phone number.
    """
    from .visit_master import identity

    contacts = contacts or {}
    end = res.settings.period_to
    movers = res.declined + res.lapsed + res.refunded
    # which name keys cover more than one account in this very list
    seen: dict = {}
    for m in movers:
        seen.setdefault(identity(m.customer), set()).add(
            m.customer_id or m.customer)
    out = []
    for m in movers:
        key = identity(m.customer)
        got = contacts.get(key) or Contact()
        silent = ((end - m.last_bought).days
                  if isinstance(m.last_bought, date) else None)
        out.append(CallRow(movement=m, contact=got, days_silent=silent,
                           ambiguous=len(seen.get(key, ())) > 1))
    out.sort(key=lambda r: r.movement.change)
    return out


def _prep(ws) -> None:
    for i, width in enumerate(WIDTHS, start=1):
        ws.column_dimensions[chr(64 + i)].width = width
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "A5"


def write_call_list(ws, res: sm.Result, rows) -> None:
    s = res.settings
    reachable = sum(1 for r in rows if r.reachable)
    ambiguous = sum(1 for r in rows if r.ambiguous)
    lost = sum(r.movement.change for r in rows)

    ws.merge_cells(f"A1:{LAST}1")
    _cell(ws, 1, 1, f"  CALL LIST  ·  {sm.period_label(s)}", bold=True,
          size=16, color="FFFFFF", fill=NAVY, align="left")
    ws.row_dimensions[1].height = 34
    ws.merge_cells(f"A2:{LAST}2")
    _cell(ws, 2, 1,
          f"  Every agency that went backwards in {sm.period_label(s)}, "
          f"biggest loss first.   Contact details come from the visit "
          f"reports, matched on the agency name — {reachable:,} of "
          f"{len(rows):,} have someone to ring.   The last three columns are "
          f"for the rep to fill in.", size=9, color=GREY, fill=PAPER,
          align="left", wrap=True)
    ws.row_dimensions[2].height = 30

    r = _kpi_strip(ws, 4, [
        ("Agencies to call", len(rows), "#,##0", "C00000"),
        ("Have a contact", reachable, "#,##0", "006100"),
        ("No contact on file", len(rows) - reachable, "#,##0",
         "C00000" if reachable < len(rows) else None),
        ("Check the contact", ambiguous, "#,##0",
         "C00000" if ambiguous else None),
        (f"At stake ({s.unit})", round(lost), MONEY, "C00000"),
    ])
    r += 1
    r = _headers(ws, r, HEADERS)

    for row in rows:
        _call_row(ws, r, row)
        r += 1

    if not rows:
        _cell(ws, r, 1, "Nobody went backwards this period.", size=10,
              color=GREY)
        return
    r += 1
    _band(ws, r, "HOW TO READ THIS",
          f"'Days silent' counts from the last ticket to {sm.period_label(s)}"
          f"'s end; amber contact columns mean the visit reports hold no "
          f"number, which is itself worth knowing")


def _call_row(ws, r: int, row) -> None:
    """One agency, with the three right-hand columns left for the rep."""
    m, c = row.movement, row.contact
    _cell(ws, r, 1, (f"{m.customer}  ({len(m.accounts)} accounts)"
                     if getattr(m, "is_group", False) else m.customer),
          bold=True, size=10, border=True)
    _cell(ws, r, 2, m.iata or None, size=9, border=True, align="center")
    # an agency nobody has a number for is the point of the list, not a
    # row to hide -- it is marked instead
    _cell(ws, r, 3, c.contact or None, size=9, border=True,
          fill=None if c.contact else WARN)
    _cell(ws, r, 4, c.designation or None, size=9, border=True)
    _cell(ws, r, 5, c.phone or None, size=9, border=True,
          fill=None if c.phone else WARN)
    # the name the contact was filed under in the visit report. When it is
    # not this agency's own name, a rep can see the match may be wrong.
    _cell(ws, r, 6, c.agency or None, size=9, border=True,
          fill=WARN if row.ambiguous else None)
    _cell(ws, r, 7, m.station or None, size=9, border=True, align="center")
    _cell(ws, r, 8, m.sales_person or None, size=9, border=True)
    _cell(ws, r, 9, m.last_bought, fmt="dd mmm yy", size=9, border=True,
          align="center")
    _cell(ws, r, 10, row.days_silent, size=9, border=True, align="center",
          fill=BAD if (row.days_silent or 0) >= LONG_SILENCE else None)
    _cell(ws, r, 11, round(m.baseline) or None, fmt=MONEY, size=9,
          border=True, align="right")
    _cell(ws, r, 12, round(m.current) if m.current else None, fmt=MONEY,
          size=9, border=True, align="right")
    _cell(ws, r, 13, round(m.change), fmt=MONEY, size=10, bold=True,
          border=True, align="right")
    _cell(ws, r, 14, row.why + (" · CHECK THE CONTACT: another account "
                                "trades under this name"
                                if row.ambiguous else ""),
          size=9, border=True, fill=WARN if row.ambiguous else None)
    _cell(ws, r, 15, c.last_seen, fmt="dd mmm yy", size=9, border=True,
          align="center")
    _cell(ws, r, 16, c.rep or None, size=9, border=True)
    # the three shaded columns come back filled in by hand
    for j in range(17, 22):
        _cell(ws, r, j, None, border=True, fill=PAPER if j <= 19 else None)


def build_workbook(res: sm.Result, out_path, contacts: dict | None = None):
    """One sheet, ready to print or send to the reps. Returns the rows."""
    rows = build(res, contacts)
    wb = Workbook()
    ws = wb.active
    ws.title = "Call list"
    _prep(ws)
    write_call_list(ws, res, rows)
    wb.save(str(out_path))
    return rows


def default_filename(settings: sm.Settings) -> str:
    return f"Call_List_{sm.period_label(settings).replace(' ', '')}.xlsx"
