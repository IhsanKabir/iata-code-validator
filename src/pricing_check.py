"""The season check: is one season's pricing set up exactly like another's?

Read-only, through pricing_client. Three parts, each comparing a base season
(the current one) with one or more others (the next, or all):

* Fares -- every fare row (fare basis x route x passenger type) from the
  Fare Editor: currency, amounts, baggage and the rest of the row must match.
  Cross-checked against Quick Sale, which shows what sales actually sees.
* Taxes -- the tax list per route on a date in each season (codes, amounts,
  currencies, YQ/YR included), plus Quick Sale's total tax per fare.
* Penalties -- change/refund/no-show fees per fare class, route, passenger
  type and time window, all seasons in one search, kept small because large
  searches are slow and can come back incomplete.

Every difference is reported; nothing is excused (the user's rule).
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import pricing_parse as pp

#: Fare classes per penalty search. Big searches are slow and sometimes do
#: not finish loading; a page that stops part-way is searched again smaller.
PENALTY_CHUNK = 15

SAME, DIFFERENT, MISSING, NEW, DUPLICATE = (
    "same", "different", "missing", "new", "duplicate")


# ---- seasons and dates -------------------------------------------------------

def _d(text: str) -> date:
    return datetime.strptime(text, "%Y/%m/%d").date()


def current_season(seasons, today: date):
    """The season today falls in (start <= today < end)."""
    for s in seasons:
        if _d(s.start) <= today < _d(s.end):
            return s
    return None


#: Which later seasons are compared with the current one.
SEASON_MODES = {
    "next": "Current vs next season",
    "after_next": "Current vs the season after next",
    "next_two": "Current vs next two seasons",
    "all": "Current vs ALL later seasons",
}


def seasons_to_compare(seasons, today: date, mode: str = "next"):
    """(base season, [seasons compared with it]); mode from SEASON_MODES."""
    base = current_season(seasons, today)
    if base is None:
        raise ValueError("No season covers today's date.")
    later = sorted((s for s in seasons if _d(s.start) > _d(base.start)),
                   key=lambda s: _d(s.start))
    picked = {"next": later[:1], "after_next": later[1:2],
              "next_two": later[:2], "all": later}.get(mode)
    if picked is None:
        raise ValueError(f"Unknown season choice {mode!r}.")
    if not picked:
        raise ValueError("Zenith has no such season set up yet — "
                         f"seasons after {base.name}: "
                         + (", ".join(s.name for s in later) or "none") + ".")
    return base, picked


def check_date(season, today: date) -> date:
    """A day to look at a season on: after today, a week in, never its last
    day (seasons share their boundary day). Quick Sale moves a date that has
    passed to the next year, so the day must still be ahead."""
    start, end = _d(season.start), _d(season.end)
    day = max(start, today + timedelta(days=1)) + timedelta(days=7)
    return min(day, end - timedelta(days=1))


def fdi_date(day: date) -> str:
    return day.strftime("%d%b%y").upper()        # 20OCT26


# ---- routes ------------------------------------------------------------------

def route_pairs(text: str) -> tuple:
    """Every route typed, however it is separated: 'DAC-DOH, DAC-DXB',
    'DAC-DOH DAC-DXB', 'dac doh' or one per line; 'DAC-DOH-DXB' is a chain
    (DAC-DOH, DOH-DXB). Returns ([(from, to)], [what could not be read])."""
    import re
    pairs, unread = [], []
    for token in re.split(r"[,;\n]+", text or ""):
        single = []                        # airports typed one at a time
        for chunk in token.split():
            codes = [c.upper() for c in re.findall(r"[A-Za-z]{3}", chunk)]
            if re.sub(r"[A-Za-z]{3}|[-–/>]", "", chunk):
                unread.append(chunk)
                continue
            if len(codes) >= 2:
                pairs.extend(zip(codes, codes[1:]))
            elif len(codes) == 1:
                single.append(codes[0])
                if len(single) == 2:
                    pairs.append(tuple(single))
                    single = []
        if single:
            unread.append(single[0])
    return pairs, unread


def parse_routes(text: str, known_legs, include_return: bool = True):
    """'DAC-CCU, dac dxb' -> {'DAC-CCU', 'CCU-DAC', ...}; unknown ones listed."""
    known = set(known_legs)
    wanted, unknown = set(), []
    pairs, unread = route_pairs(text)
    unknown.extend(unread)
    for a, b in pairs:
        legs = [f"{a}-{b}"] + ([f"{b}-{a}"] if include_return else [])
        for leg in legs:
            if leg in known:
                wanted.add(leg)
            elif leg not in unknown:
                unknown.append(leg)
    return wanted, unknown


# ---- the route list (which fare bases fly which routes) ----------------------

class RouteIndex:
    """fare basis id -> the routes it has fares on, in any season still to
    come. Built by reading every enabled fare basis; kept until rebuilt."""

    def __init__(self, path):
        self.path = Path(path)
        self.built = ""
        self.legs: dict = {}            # fare basis id -> set of "DAC-CCU"

    def load(self) -> bool:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        self.built = raw.get("built", "")
        self.legs = {int(k): set(v) for k, v in raw.get("legs", {}).items()}
        return bool(self.legs)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "built": self.built,
            "legs": {str(k): sorted(v) for k, v in self.legs.items()}}),
            encoding="utf-8")

    def fare_bases_for(self, legs) -> list:
        legs = set(legs)
        return sorted(k for k, v in self.legs.items() if v & legs)

    def build(self, client, lookups, fare_bases, seasons, *, stop, progress):
        legs: dict = {}
        total = len(fare_bases) * len(seasons)
        n = 0
        for fb in fare_bases:
            for s in seasons:
                if stop.is_set():
                    raise InterruptedError("Stopped while building the "
                                           "route list.")
                for row in client.load_fares(fb.id, s.id):
                    leg = lookups.legs.get(int(row.get("IdLeg", -1)))
                    if leg:
                        legs.setdefault(fb.id, set()).add(leg)
                n += 1
                if n % 25 == 0:
                    progress(f"Route list: {n:,} of {total:,} reads")
        self.legs = legs
        self.built = time.strftime("%Y-%m-%d %H:%M")
        self.save()


# ---- results -----------------------------------------------------------------

@dataclass
class Finding:
    part: str           # Fares / Sales check / QS taxes / Taxes / Penalties
    kind: str           # different / missing / new / duplicate / ...
    route: str
    item: str           # fare basis + passenger, tax, penalty class...
    detail: str         # field or window
    season_a: str
    value_a: str
    season_b: str
    value_b: str


@dataclass
class CheckResult:
    base: str = ""
    others: list = field(default_factory=list)
    routes: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    compared: dict = field(default_factory=dict)   # part -> items compared
    problems: list = field(default_factory=list)   # couldn't check
    #: every value read: part -> {(route, item): {season: {field: value}}}
    values: dict = field(default_factory=dict)
    value_fields: dict = field(default_factory=dict)   # part -> fields
    started: str = ""
    finished: str = ""

    @property
    def seasons(self) -> list:
        return [self.base] + list(self.others)

    def keep(self, part, key, season, row, fields) -> None:
        self.value_fields[part] = tuple(fields)
        self.values.setdefault(part, {}).setdefault(key, {})[season] = {
            f: row.get(f) for f in fields}

    def count(self, part: str) -> dict:
        out: dict = {}
        for f in self.findings:
            if f.part == part:
                out[f.kind] = out.get(f.kind, 0) + 1
        return out


def _norm(field_name: str, value):
    if field_name == "Currency":
        return str(value or "")
    if value is None:
        return 0.0
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return str(value)


def _show(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    return str(value)


def compare_rows(part, route_of, item_of, rows_a, rows_b, fields, season_a,
                 season_b, result):
    """Rows keyed by (route, item); every field compared, both directions."""
    def index(rows):
        # an item set up twice in one season is kept as "#2", not lost
        out, dup, seen = {}, set(), {}
        for r in rows:
            k = (route_of(r), item_of(r))
            seen[k] = seen.get(k, 0) + 1
            if seen[k] > 1:
                dup.add(k)
                k = (k[0], f"{k[1]} #{seen[k]}")
            out[k] = r
        return out, dup
    a, dup_a = index(rows_a)
    b, dup_b = index(rows_b)
    for k, r in a.items():
        result.keep(part, k, season_a, r, fields)
    for k, r in b.items():
        result.keep(part, k, season_b, r, fields)
    for k in sorted(dup_a | dup_b):
        where = season_a if k in dup_a else season_b
        result.findings.append(Finding(part, DUPLICATE, k[0], k[1],
                                       "set up twice (see #2)", where, "", "",
                                       ""))
    for k in sorted(set(a) | set(b)):
        if k not in b:
            result.findings.append(Finding(part, MISSING, k[0], k[1], "",
                                           season_a, "present", season_b,
                                           "MISSING"))
            continue
        if k not in a:
            result.findings.append(Finding(part, NEW, k[0], k[1], "",
                                           season_a, "absent", season_b,
                                           "present"))
            continue
        for f in fields:
            va, vb = a[k].get(f), b[k].get(f)
            if _norm(f, va) != _norm(f, vb):
                result.findings.append(Finding(
                    part, DIFFERENT, k[0], k[1], f, season_a, _show(va),
                    season_b, _show(vb)))
    result.compared[part] = result.compared.get(part, 0) + len(set(a) | set(b))


# ---- the three parts ---------------------------------------------------------

def check_fares(client, lookups, fb_by_id, fare_base_ids, legs, base, others,
                result, *, stop, progress):
    """Returns {season id: {(code, leg): adult row}} for the sales check."""
    pax = lookups.passenger_types
    adults: dict = {}
    for i, fb_id in enumerate(fare_base_ids, 1):
        if stop.is_set():
            raise InterruptedError("Stopped.")
        fb = fb_by_id[fb_id]
        per_season = {}
        for s in [base] + others:
            rows = []
            for row in client.load_fares(fb_id, s.id):
                leg = lookups.legs.get(int(row.get("IdLeg", -1)), "")
                if leg in legs:
                    row = dict(row, _leg=leg, _code=fb.code)
                    rows.append(row)
                    if int(row.get("IdPassengerType", 0)) == 1:
                        adults.setdefault(s.id, {})[(fb.code, leg)] = row
            per_season[s.id] = rows
        for other in others:
            compare_rows(
                "Fares", lambda r: r["_leg"],
                lambda r: f"{r['_code']} · "
                          f"{pax.get(int(r['IdPassengerType']), r['IdPassengerType'])}",
                per_season[base.id], per_season[other.id], pp.FARE_FIELDS,
                base.name, other.name, result)
        progress(f"Fares: {i:,} of {len(fare_base_ids):,} fare bases")
    return adults


def _qs(client, command, result, route, season_name):
    try:
        shown = pp.parse_quick_sale(client.quick_sale(command))
    except pp.LayoutChanged as exc:
        result.problems.append(f"{route} {season_name}: Quick Sale layout "
                               f"changed ({exc}); not checked.")
        return None
    if shown.error:
        result.problems.append(f"{route} {season_name}: Quick Sale said "
                               f"'{shown.error[:80]}'.")
        return None
    return shown


#: Quick Sale columns kept and compared (taxes = with-tax minus without).
QS_FIELDS = ("Currency", "OW", "RT", "OW tax", "RT tax", "Class", "Level",
             "Refund", "Change", "Min stay", "Max stay")


def check_quick_sale(client, legs, base, others, today, adults, result, *,
                     stop, progress):
    """What sales sees, per route and season: every fare Quick Sale lists,
    without and with taxes, compared season against season. With the Fare
    Editor's adult fares (`adults`), also each one against Quick Sale in its
    own currency, so no exchange rate stands between them."""
    for i, leg in enumerate(sorted(legs), 1):
        if stop.is_set():
            raise InterruptedError("Stopped.")
        od = leg.replace("-", "")
        shown = {}
        for s in [base] + others:
            day = fdi_date(check_date(s, today))
            net = _qs(client, f"FDI{od}/D{day}/R,NP,WT", result, leg, s.name)
            gross = _qs(client, f"FDI{od}/D{day}/R,NP", result, leg, s.name)
            if net is None or gross is None:
                continue
            g = {f.fare_basis: f for f in gross.fares}
            shown[s.id] = [_qs_row(f, g.get(f.fare_basis), net.currency)
                           for f in net.fares]
            if adults:
                _sales_check(client, od, day, leg, s, adults, result)
        for other in others:
            if base.id in shown and other.id in shown:
                compare_rows("Quick Sale", lambda r: leg, lambda r: r["code"],
                             shown[base.id], shown[other.id], QS_FIELDS,
                             base.name, other.name, result)
        progress(f"Quick Sale: {i:,} of {len(legs):,} routes")


def _qs_row(net, gross, currency) -> dict:
    return {"code": net.fare_basis, "Currency": currency, "OW": net.ow,
            "RT": net.rt, "OW tax": _minus(gross, net, "ow"),
            "RT tax": _minus(gross, net, "rt"), "Class": net.booking_class,
            "Level": net.level, "Refund": net.refund, "Change": net.change,
            "Min stay": net.min_stay, "Max stay": net.max_stay}


def _minus(gross, net, side):
    a = getattr(gross, side) if gross else None
    b = getattr(net, side)
    return None if a is None or b is None else round(a - b, 2)


SALES_FIELDS = ("Currency", "Fare Editor OW", "Quick Sale OW",
                "Fare Editor RT", "Quick Sale RT", "Status")
ON_SALE = "on sale, same amount"


def _sales_status(row, f) -> tuple:
    """(status, is a problem). Quick Sale "-" on one side is how a fare
    sold return-only (or one-way only) shows -- the Fare Editor may still
    hold an amount there that is never sold; that is noted, not flagged."""
    if f is None:
        return "not in Quick Sale", True
    notes = []
    for side, fld in (("ow", "AmountOW"), ("rt", "AmountRT")):
        v, fe = getattr(f, side), _norm(fld, row.get(fld))
        if v is None:
            if fe:
                notes.append(f"{side.upper()} not sold")
        elif round(v, 2) != fe:
            return f"{side.upper()} amount differs", True
    return (", ".join(notes) or ON_SALE), False


def _sales_check(client, od, day, leg, season, adults, result):
    """Each adult fare in the Fare Editor against Quick Sale's display."""
    mine = {code: row for (code, lg), row in adults.get(season.id, {}).items()
            if lg == leg}
    by_currency: dict = {}
    for code, row in mine.items():
        by_currency.setdefault(str(row.get("Currency") or ""), {})[code] = row
    for currency, rows in by_currency.items():
        shown = _qs(client, f"FDI{od}/D{day}/R,NP,WT,FC-{currency}", result,
                    leg, season.name)
        if shown is None:
            continue
        qs = {f.fare_basis: f for f in shown.fares}
        for code, row in sorted(rows.items()):
            f = qs.get(code)
            status, bad = _sales_status(row, f)
            values = {"Currency": currency,
                      "Fare Editor OW": row.get("AmountOW"),
                      "Fare Editor RT": row.get("AmountRT"),
                      "Quick Sale OW": f.ow if f else None,
                      "Quick Sale RT": f.rt if f else None,
                      "Status": status}
            result.keep("Sales check", (leg, code), season.name, values,
                        SALES_FIELDS)
            if bad:
                result.findings.append(Finding(
                    "Sales check", "not on sale" if f is None
                    else "amount differs", leg, code, status, season.name,
                    f"Fare Editor OW {_show(row.get('AmountOW'))} / RT "
                    f"{_show(row.get('AmountRT'))} {currency}", season.name,
                    "not listed" if f is None else
                    f"Quick Sale OW {_show(f.ow)} / RT {_show(f.rt)}"))
        result.compared["Sales check"] = (result.compared.get("Sales check", 0)
                                          + len(rows))


def check_taxes(client, airports, legs, base, others, today, result, *, stop,
                progress):
    for i, leg in enumerate(sorted(legs), 1):
        if stop.is_set():
            raise InterruptedError("Stopped.")
        dep, arr = leg.split("-")
        if dep not in airports or arr not in airports:
            result.problems.append(f"{leg}: airport not in the tax screen's "
                                   f"list; taxes not checked.")
            continue
        lists = {}
        for s in [base] + others:
            day = check_date(s, today).strftime("%d/%m/%Y")
            html = client.tax_search(pp.tax_search_form(
                airports[dep], airports[arr], day))
            lists[s.id] = [{"route": f"{t.departure} → {t.arrival}",
                            "item": f"{t.code} · {t.name}",
                            "Value": t.value,
                            "International": "yes" if t.international
                            else "no",
                            "Domestic": "yes" if t.domestic else "no"}
                           for t in pp.parse_tax_list(html)]
        for other in others:
            compare_rows("Taxes", lambda r: f"{leg} ({r['route']})",
                         lambda r: r["item"], lists[base.id], lists[other.id],
                         ("Value", "International", "Domestic"), base.name,
                         other.name, result)
        progress(f"Taxes: {i:,} of {len(legs):,} routes")


def check_penalties(client, options, index, lookups, legs, base, others,
                    result, *, stop, progress):
    leg_id = {v.replace(" ", ""): k for k, v in options.legs.items()}
    pax_ids = sorted(options.passenger_types)
    seasons = [base] + others
    for i, leg in enumerate(sorted(legs), 1):
        if stop.is_set():
            raise InterruptedError("Stopped.")
        lid = leg_id.get(leg)
        classes = [c for c in index.fare_bases_for([leg])
                   if c in options.classes]
        if lid is None or not classes:
            result.problems.append(f"{leg}: no fare classes on the penalty "
                                   f"screen for this route.")
            continue
        rows = _penalty_rows(client, classes, [s.id for s in seasons], lid,
                             pax_ids, result, leg)
        by_season = {s.name: [] for s in seasons}
        for r in rows:
            if r.season in by_season:
                by_season[r.season].append(
                    {"item": f"{r.fare_class} · {r.passenger}",
                     "window": r.window, "Change": r.change,
                     "Refund": r.refund})
        for other in others:
            compare_rows("Penalties",
                         lambda r: f"{leg} · {r['window']}",
                         lambda r: r["item"], by_season[base.name],
                         by_season[other.name], ("Change", "Refund"),
                         base.name, other.name, result)
        progress(f"Penalties: {i:,} of {len(legs):,} routes")


def _penalty_rows(client, classes, season_ids, lid, pax_ids, result, leg):
    """Search in small pieces. A page that did not load completely is split
    in half and searched again; one class that still fails is reported as
    not checked rather than trusted."""
    out = []
    queue = [classes[i:i + PENALTY_CHUNK]
             for i in range(0, len(classes), PENALTY_CHUNK)]
    while queue:
        chunk = queue.pop(0)
        html = client.penalty_search(pp.penalty_search_form(
            chunk, season_ids, [lid], pax_ids))
        try:
            rows = pp.parse_penalty_results(html)
        except pp.Incomplete:
            if len(chunk) > 1:
                half = len(chunk) // 2
                queue[:0] = [chunk[:half], chunk[half:]]
            else:
                result.problems.append(
                    f"{leg}: penalties for fare class {chunk[0]} did not load "
                    f"completely; not checked.")
            continue
        out.extend(rows)
    return out


# ---- the whole check ---------------------------------------------------------

@dataclass
class Settings:
    routes_text: str = ""            # empty = all routes
    include_return: bool = True
    mode: str = "next"               # a SEASON_MODES key
    fares: bool = True
    quick_sale: bool = True
    taxes: bool = True
    penalties: bool = True


def run_check(client, index: RouteIndex, settings: Settings, *,
              today: date | None = None, stop=None,
              progress=lambda _m: None) -> CheckResult:
    stop = stop or threading.Event()
    today = today or date.today()
    result = CheckResult(started=time.strftime("%Y-%m-%d %H:%M"))
    fbs = pp.parse_fare_basis_list(client.fare_basis_list())
    enabled = [f for f in fbs if f.enabled]
    if not enabled:
        raise ValueError("Zenith returned no enabled fare bases.")
    lookups = pp.parse_fare_editor_page(client.fare_editor_page(enabled[0].id))
    base, others = seasons_to_compare(lookups.seasons, today, settings.mode)
    result.base, result.others = base.name, [s.name for s in others]
    if settings.routes_text.strip():
        legs, unknown = parse_routes(settings.routes_text,
                                     lookups.legs.values(),
                                     settings.include_return)
        if unknown:
            result.problems.append("Not Zenith routes, skipped: "
                                   + ", ".join(unknown))
    else:
        legs = set(lookups.legs.values())
    if not legs:
        raise ValueError("No route to check.")
    result.routes = sorted(legs)
    if (settings.fares or settings.penalties) and not index.legs:
        raise ValueError("The route list has not been built yet — press "
                         "'Rebuild route list' first.")
    fb_by_id = {f.id: f for f in enabled}
    ids = [i for i in index.fare_bases_for(legs) if i in fb_by_id]
    adults = {}
    if settings.fares:
        adults = check_fares(client, lookups, fb_by_id, ids, legs, base,
                             others, result, stop=stop, progress=progress)
    if settings.quick_sale:
        check_quick_sale(client, legs, base, others, today, adults, result,
                         stop=stop, progress=progress)
    if settings.taxes:
        airports = pp.parse_tax_airports(client.tax_page())
        check_taxes(client, airports, legs, base, others, today, result,
                    stop=stop, progress=progress)
    if settings.penalties:
        options = pp.parse_penalty_page(client.penalty_page())
        check_penalties(client, options, index, lookups, legs, base, others,
                        result, stop=stop, progress=progress)
    result.finished = time.strftime("%Y-%m-%d %H:%M")
    return result


def rebuild_index(client, index: RouteIndex, *, today=None, stop=None,
                  progress=lambda _m: None) -> int:
    stop = stop or threading.Event()
    today = today or date.today()
    fbs = [f for f in pp.parse_fare_basis_list(client.fare_basis_list())
           if f.enabled]
    lookups = pp.parse_fare_editor_page(client.fare_editor_page(fbs[0].id))
    seasons = [s for s in lookups.seasons if _d(s.end) > today]
    index.build(client, lookups, fbs, seasons, stop=stop, progress=progress)
    return len(index.legs)
