"""Blank forms with the columns the current ones are missing.

Every month both reports withhold a figure because nobody wrote it: the counter
sheets carry no payment channel at four counters, name the person behind an
enquiry on 15% of rows and time barely half their tasks; the visit reports carry
a billing figure on 43% of visits, a zone on two thirds and a date on 94%.

No amount of parsing recovers a blank. The only fix is a form that asks for the
field, so these are the same layouts the counters and reps already use, with the
missing columns present and marked, ready to hand out.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

NAVY = "1F3864"
BAND = "2E75B6"
REQUIRED = "FFF2CC"      # a field the report cannot do without
PAPER = "F2F5FA"
GREY = "808080"

_thin = Side(style="thin", color="BFBFBF")
BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)


def _cell(ws, r, c, v, *, bold=False, size=10, colour="000000", fill=None,
          wrap=False, border=False, align=None):
    cell = ws.cell(row=r, column=c, value=v)
    cell.font = Font(bold=bold, size=size, color=colour, name="Segoe UI")
    if fill:
        cell.fill = PatternFill("solid", fgColor=fill)
    if border:
        cell.border = BORDER
    if wrap or align:
        cell.alignment = Alignment(horizontal=align or "general",
                                   vertical="center", wrap_text=wrap)
    return cell


def _title(ws, r, text, note=""):
    last = get_column_letter(max(ws.max_column, 16))
    ws.merge_cells(f"A{r}:{last}{r}")
    _cell(ws, r, 1, f"  {text}" + (f"   ·   {note}" if note else ""),
          bold=True, size=11, colour="FFFFFF", fill=BAND)
    ws.row_dimensions[r].height = 20
    return r + 1


def _header(ws, r, columns):
    """columns: (label, width, required?)"""
    for j, (label, width, needed) in enumerate(columns, start=1):
        _cell(ws, r, j, label + (" *" if needed else ""), bold=True, size=9,
              colour="FFFFFF" if not needed else "7F6000",
              fill=NAVY if not needed else REQUIRED, wrap=True,
              align="center", border=True)
        ws.column_dimensions[get_column_letter(j)].width = width
    ws.row_dimensions[r].height = 30
    return r + 1


def _blank_rows(ws, r, columns, n):
    for _ in range(n):
        for j in range(1, len(columns) + 1):
            _cell(ws, r, j, None, border=True,
                  fill=REQUIRED if columns[j - 1][2] else None)
        r += 1
    return r


SALES_COLUMNS = [
    ("Counter Name", 18, False), ("Action", 14, False), ("Count PNR", 9, False),
    ("Employee Name", 20, True), ("Employee ID", 12, True), ("PNR", 10, True),
    ("Customer Mobile No", 16, False), ("Sales Amount (BDT)", 14, True),
    ("Cash", 11, True), ("bKash / Nagad", 12, True), ("Card", 11, True),
    ("Cheque", 10, True), ("Bank / Online transfer", 14, True),
    ("Card or wallet no", 14, False), ("Transaction no", 14, False),
    ("Remarks", 22, False),
]
QUERY_COLUMNS = [
    ("Query Method", 14, True), ("Query Type", 16, True),
    ("Segment", 14, False), ("Did query convert to sale?", 16, True),
    ("If not, why", 20, False), ("Received by", 18, True),
    ("Customer mobile", 16, False),
]
TIME_COLUMNS = [
    ("Employee ID", 12, True), ("Employee Name", 20, True),
    ("Shift name", 12, False), ("Task", 26, True),
    ("Agency / customer", 20, False),
    ("Time spent (minutes)", 14, True), ("Remarks", 20, False),
]


def build_counter_template(out_path, *, day=None) -> Path:
    """The counter daily sheet, with the fields the report needs marked."""
    day = day or date.today()
    wb = Workbook()
    ws = wb.active
    ws.title = "Day sheet"
    ws.sheet_view.showGridLines = False

    r = 1
    ws.merge_cells("A1:P1")
    _cell(ws, 1, 1, f"  Counter Daily Activities of {day.day} {day:%b %Y}",
          bold=True, size=14, colour="FFFFFF", fill=NAVY)
    ws.row_dimensions[1].height = 30
    r = 3
    ws.merge_cells(f"A{r}:P{r}")
    _cell(ws, r, 1,
          "  Fields shaded like this are the ones the monthly report cannot do "
          "without. A blank is never read as a zero — it is counted and "
          "reported as 'not written', which is why last month's report could "
          "not say how 14% of the money arrived.",
          size=9, colour=GREY, fill=PAPER, wrap=True)
    ws.row_dimensions[r].height = 28
    r += 2

    for action in ("Ticket Issue", "Ticket Reissue", "Ticket Refund"):
        r = _title(ws, r, action,
                   "put the agency in Remarks" if action == "Ticket Reissue"
                   else "")
        cols = list(SALES_COLUMNS)
        if action == "Ticket Reissue":
            cols = cols[:7] + [("PNR Booked from", 18, True)] + cols[7:]
        r = _header(ws, r, cols)
        r = _blank_rows(ws, r, cols, 8) + 1

    r = _title(ws, r, "Walk-in / phone / WhatsApp enquiries",
               "'Received by' is new — without it no enquiry can be credited "
               "to anyone")
    r = _header(ws, r, QUERY_COLUMNS)
    r = _blank_rows(ws, r, QUERY_COLUMNS, 10) + 1

    r = _title(ws, r, "Other work",
               "minutes are required — 'unable to count' cannot be added up")
    r = _header(ws, r, TIME_COLUMNS)
    r = _blank_rows(ws, r, TIME_COLUMNS, 10) + 1

    ws.merge_cells(f"A{r}:P{r}")
    _cell(ws, r, 1,
          "  Totals are not typed here any more: the report adds them from the "
          "rows, so a typed total that disagrees is one more thing to chase.",
          size=8, colour=GREY, fill=PAPER, wrap=True)
    ws.freeze_panes = "A5"
    out_path = Path(out_path)
    wb.save(out_path)
    return out_path


VISIT_COLUMNS = [
    ("Sl.", 6, False), ("Name of Agent", 28, True),
    ("Contact Person", 18, False), ("Designation", 16, False),
    ("Contact No", 15, True), ("Location", 16, False),
    ("Productivity / Monthly Sale", 20, True),
    ("Most Selling Route", 22, False), ("Remarks", 26, False),
]


def build_visit_template(out_path, *, day=None) -> Path:
    """The rep's daily visit block, with the fields the report needs marked."""
    day = day or date.today()
    wb = Workbook()
    ws = wb.active
    ws.title = "Rep sheet"
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:I1")
    _cell(ws, 1, 1, "  Daily Agency & Corporate Visit", bold=True, size=14,
          colour="FFFFFF", fill=NAVY)
    ws.row_dimensions[1].height = 30

    r = 3
    for label, hint in (("Date & Day *", f"{day:%d/%m/%Y}  (write the year)"),
                        ("Zone *", "e.g. Zone 7"),
                        ("Name *", "your full name, spelled the same each day")):
        _cell(ws, r, 1, label, bold=True, size=10, fill=REQUIRED, border=True,
              colour="7F6000")
        _cell(ws, r, 2, None, border=True, fill=REQUIRED)
        _cell(ws, r, 3, hint, size=8, colour=GREY)
        r += 1
    r += 1

    ws.merge_cells(f"A{r}:I{r}")
    _cell(ws, r, 1,
          "  Shaded fields are the ones the monthly report cannot do without. "
          "For the monthly figure write ONE number — 'BDT 300000' or '3 lakh'. "
          "A range like '3-5 lakh' is left out entirely rather than read as 3, "
          "so last month 57% of visits carried no usable figure.",
          size=9, colour=GREY, fill=PAPER, wrap=True)
    ws.row_dimensions[r].height = 28
    r += 2

    r = _title(ws, r, "Daily Agency Sales Visit")
    r = _header(ws, r, VISIT_COLUMNS)
    r = _blank_rows(ws, r, VISIT_COLUMNS, 12)

    r += 1
    ws.merge_cells(f"A{r}:I{r}")
    _cell(ws, r, 1,
          "  Copy this block for each day. Keep the date, zone and name on "
          "every copy — a block without a date cannot be placed in the month, "
          "and one carrying last month's date is counted as an error.",
          size=8, colour=GREY, fill=PAPER, wrap=True)
    ws.row_dimensions[r].height = 26
    ws.freeze_panes = "A9"
    out_path = Path(out_path)
    wb.save(out_path)
    return out_path
