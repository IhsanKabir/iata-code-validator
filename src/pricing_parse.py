"""Reading Zenith's pricing screens into plain records.

Every layout here is as captured in browser HARs of the live system. Where a
screen is text rather than data (Quick Sale, the penalty and tax pages), the
reader checks the header it expects and raises LayoutChanged otherwise --
a changed screen must stop the check, not be misread into wrong answers.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape

#: Fare-row fields compared between seasons ("all of them" -- the user).
FARE_FIELDS = (
    "Currency", "AmountOW", "AmountRT", "AmountYQ", "AmountYR",
    "GdsAmountOW", "GdsAmountRT", "GdsAmountYQ", "GdsAmountYR",
    "ID_BagConceptType", "BaggageWeightAllowance",
    "BaggageExcessWeightAmount", "BagPieceAllowance",
    "BagWeightAllowancePerPiece", "BagPieceExcedentAmount",
    "TravelerCheckTypeId", "TravelerCheckAmountOW", "TravelerCheckAmountRT",
    "MilesAmountOW", "MilesAmountRT")

QS_HEADER = ("FARE BASIS", "OW", "RT", "PRBD", "LVL", "REF", "MOD", "MIN",
             "MAX")


class LayoutChanged(Exception):
    """A screen no longer looks as recorded; nothing is read from it."""


_TAG = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", fragment))
                  .replace("\xa0", " ")).strip()


def _js_value(html: str, name: str):
    m = re.search(r"var\s+" + re.escape(name) + r"\s*=\s*", html)
    if not m:
        raise LayoutChanged(f"Fare Editor page has no '{name}' list.")
    return json.JSONDecoder().raw_decode(html[m.end():])[0]


# ---- Fare Editor -------------------------------------------------------------

@dataclass(frozen=True)
class Season:
    id: int
    name: str
    start: str          # yyyy/mm/dd, as the page gives it
    end: str


@dataclass
class Lookups:
    legs: dict = field(default_factory=dict)          # id -> "DAC-CCU"
    seasons: list = field(default_factory=list)       # [Season]
    airports: dict = field(default_factory=dict)      # id -> "AUH - ..."
    airport_code: dict = field(default_factory=dict)  # "AUH" -> id
    passenger_types: dict = field(default_factory=dict)  # id -> "AD - Adult(s)"


def parse_fare_editor_page(html: str) -> Lookups:
    legs = _js_value(html, "legs")
    periods = _js_value(html, "periods")
    airports = _js_value(html, "airports")
    pax = _js_value(html, "passengerTypes")
    out = Lookups()
    out.legs = {int(k): v["Text"] for k, v in legs.items()}
    out.seasons = [Season(int(p["id"]), p["group"], p["start"], p["end"])
                   for p in periods]
    for a in airports:
        out.airports[int(a["Id"])] = a["Text"]
        code = a["Text"].split(" - ", 1)[0].strip()
        if len(code) == 3:
            out.airport_code[code] = int(a["Id"])
    out.passenger_types = {int(k): f"{v.get('Code', '')} - {v['Text']}"
                           for k, v in pax.items()}
    return out


@dataclass(frozen=True)
class FareBasis:
    id: int
    code: str
    text: str
    enabled: bool
    booking_class: str


def parse_fare_basis_list(rows: list) -> list:
    return [FareBasis(int(r["IdFareBasis"]), str(r.get("Code") or ""),
                      str(r.get("Text") or ""), bool(r.get("IsEnabled")),
                      str(r.get("BookingClassCode") or ""))
            for r in rows if r.get("IdFareBasis") is not None]


# ---- Quick Sale --------------------------------------------------------------

@dataclass(frozen=True)
class QSFare:
    fare_basis: str
    ow: float | None
    rt: float | None
    booking_class: str
    level: str
    refund: str
    change: str
    min_stay: str
    max_stay: str


@dataclass
class QSDisplay:
    currency: str
    taxes_included: bool
    season: str
    fares: list
    error: str = ""


def _amount(cell: str):
    cell = cell.strip()
    if cell in ("", "-"):
        return None
    try:
        return float(cell.replace(",", ""))
    except ValueError:
        raise LayoutChanged(f"Quick Sale amount {cell!r} is not a number.")


def parse_quick_sale(html: str) -> QSDisplay:
    lines = [_text(l) for l in re.split(r"<br\s*/?>|\n", html or "")]
    lines = [l for l in lines if l]
    joined = " ".join(lines)
    if "not recognized" in joined or "are invalid" in joined:
        return QSDisplay("", False, "", [], error=joined[:200])
    head = next((l for l in lines if l.startswith("Currency:")), None)
    cols = next((l for l in lines if "FARE BASIS" in l), None)
    if head is None or cols is None:
        if not any(re.match(r"\d+ \|", l) for l in lines):
            return QSDisplay("", False, "", [], error=joined[:200] or
                             "empty display")
        raise LayoutChanged("Quick Sale display has no currency header.")
    got = tuple(c.strip() for c in cols.strip("| ").split("|"))
    if got != QS_HEADER:
        raise LayoutChanged(f"Quick Sale columns changed: {got}")
    currency = re.match(r"Currency:\s*([A-Z]{3})", head).group(1)
    season = next((l for l in lines if re.match(r"(WINTER|SUMMER)\b", l)), "")
    fares = []
    for l in lines:
        m = re.match(r"\d+\s*\|(.*)", l)
        if not m:
            continue
        c = [x.strip() for x in m.group(1).split("|")]
        if len(c) != 9:
            raise LayoutChanged(f"Quick Sale row has {len(c)} columns.")
        fares.append(QSFare(c[0], _amount(c[1]), _amount(c[2]), c[3], c[4],
                            c[5], c[6], c[7], c[8]))
    return QSDisplay(currency, "Taxes included" in head,
                     season.split(":")[0].strip(), fares)


# ---- penalties ---------------------------------------------------------------

@dataclass
class PenaltyOptions:
    classes: dict = field(default_factory=dict)    # id -> code
    legs: dict = field(default_factory=dict)       # id -> "DAC - DXB"
    seasons: dict = field(default_factory=dict)    # id -> name
    passenger_types: dict = field(default_factory=dict)


def parse_penalty_page(html: str) -> PenaltyOptions:
    out = PenaltyOptions()
    for kind, attr, target in (("IDClasse", "idClass", out.classes),
                               ("IDLeg", "idLeg", out.legs),
                               ("IDPeriode", "idPeriod", out.seasons),
                               ("IDTypePassager", "idPassengerType",
                                out.passenger_types)):
        for m in re.finditer(
                r'<input[^>]*name="(\d+)_' + kind + r'"[^>]*>\s*<label[^>]*>'
                r'\s*<span>(.*?)</span>', html, re.S):
            target.setdefault(int(m.group(1)), _text(m.group(2)))
    if not out.classes or not out.seasons:
        raise LayoutChanged("Penalty screen lists were not found.")
    return out


def penalty_search_form(class_ids, season_ids, leg_ids, pax_ids) -> dict:
    """The search exactly as the browser posts it -- every value empty."""
    form = {"Action": "Appliquer", "Made": "Seek", "BoolAffTaxes": "",
            "BoolAffTarifs": "", "BoolAffBagages": "",
            "pannelSelection": "on", "date_deb_saison": "",
            "date_fin_saison": "", "CodeDevise": "", "ApplicationType": "1",
            "chkShowAllLegsAllLevels": "on",
            "modification-section-display": "on", "DelaiMin": "",
            "DelaiMax": "", "tpsDep": "avant", "penMod": "",
            "penDevModif": "", "penRemb": "", "penDevRemb": "",
            "noshow-section-display": "on", "penNoShowMod": "",
            "penDevNoShowModif": "", "penNoShowRemb": "",
            "penDevNoShowRemb": ""}
    for kind, ids in (("IDClasse", class_ids), ("IDPeriode", season_ids),
                      ("IDLeg", leg_ids), ("IDTypePassager", pax_ids)):
        for i in ids:
            form[f"{int(i)}_{kind}"] = "on"
    return form


@dataclass(frozen=True)
class Penalty:
    fare_class: str
    leg: str
    passenger: str
    season: str
    window: str
    change: str          # "300 AED"
    refund: str


_PEN_COLS = ("classe", "leg", "typpass", "periode", "interval",
             "penalitemodif", "pendevremb")


class Incomplete(LayoutChanged):
    """The page stopped part-way -- what it holds is not the whole answer."""


_PEN_ROW = re.compile(r'<td[^>]*class="col-classe"')


def parse_penalty_results(html: str) -> list:
    """Every result row on the page. Zenith splits a big answer into tables
    of 100 rows, one after another -- all of them are the answer (reading
    only the first is what once made rows look missing)."""
    html = html or ""
    if "</html>" not in html[-2000:].lower():
        raise Incomplete("Penalty page did not load completely.")
    if "Modif. penalty" not in html:
        return []                      # no results table: nothing matched
    out = []
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html, re.S):
        if not _PEN_ROW.search(row):
            continue                   # headers and other tables
        cells = {}
        for col in _PEN_COLS:
            m = re.search(r'class="col-' + col + r'"[^>]*>(.*?)</td>', row,
                          re.S)
            if not m:
                raise LayoutChanged(f"Penalty row has no '{col}' column.")
            parts = re.findall(r'xs-right">(.*?)</div>', m.group(1), re.S)
            cells[col] = _text(parts[-1] if parts else m.group(1))
        out.append(Penalty(*(cells[c] for c in _PEN_COLS)))
    on_page = len(_PEN_ROW.findall(html))
    if len(out) != on_page:
        raise LayoutChanged(f"Penalty page shows {on_page} rows but "
                            f"{len(out)} could be read.")
    return out


# ---- taxes -------------------------------------------------------------------

def parse_tax_airports(html: str) -> dict:
    """'DAC' -> geo id, from the tax screen's airport list."""
    m = re.search(r'<select[^>]*name="RecCodeDestGeo"[^>]*>(.*?)</select>',
                  html, re.S)
    if not m:
        raise LayoutChanged("Tax screen airport list not found.")
    out = {}
    for value, label in re.findall(r'<option[^>]*value="(\d+)"[^>]*>([^<]*)',
                                   m.group(1)):
        code = label.split(" - ", 1)[0].strip()
        if len(code) == 3:
            out[code] = int(value)
    return out


def tax_search_form(dep_id: int, arr_id: int, date_ddmmyyyy: str) -> dict:
    return {"NumRecord": "", "hAction": "Seek", "DuplicateTaxe": "0",
            "IsDuplicatedTaxe": "", "IsInsertByUser": "",
            "Id_TaxeAeropackHidden": "", "NumeroTaxe": "",
            "RT_OrderHidden": "1", "RecCodeDestGeo": str(dep_id),
            "RecCodeDestGeoLinked": str(arr_id),
            "RecEffDate": date_ddmmyyyy, "searchTaxCode": ""}


@dataclass(frozen=True)
class Tax:
    name: str
    code: str
    departure: str
    arrival: str
    value: str          # "500 BDT", "10 USD", "30 %"
    international: bool
    domestic: bool


_TAX_HEADER = ("Tax Name", "Tax Code", "Departure Airport", "Arrival Airport",
               "Value", "International", "Domestic", "History")


def parse_tax_list(html: str) -> list:
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", html or "",
                  flags=re.S)
    out, header_seen = [], False
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", body, re.S):
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, re.S)
        if len(cells) != len(_TAX_HEADER):
            # one cell is a message line; anything else after the header
            # would be a tax row read wrong -- stop rather than drop it
            if header_seen and len(cells) > 1:
                raise LayoutChanged(f"Tax row has {len(cells)} columns.")
            continue
        texts = [_text(c) for c in cells]
        if tuple(texts) == _TAX_HEADER:
            header_seen = True
            continue
        if not header_seen:
            continue
        # each cell repeats its label first (mobile layout): strip it
        vals = [t[len(h):].strip() if t.startswith(h) else t
                for t, h in zip(texts, _TAX_HEADER)]
        # the tick is an icon-font character, written as an HTML entity
        ticked = ["" in unescape(c) for c in cells]
        out.append(Tax(vals[0], vals[1], vals[2], vals[3], vals[4],
                       ticked[5], ticked[6]))
    if not header_seen and "Tax Code" in body:
        raise LayoutChanged("Tax list columns changed.")
    return out
