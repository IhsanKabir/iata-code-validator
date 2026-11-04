"""The tax setup check: is a tax set up on every route it should be, and
set up the same way?

Routes are picked by kind -- INT-INT, INT-DOM, DOM-INT, DOM-DOM (domestic =
a Bangladesh airport) -- and/or by name ("AUH-DAC", "AUH-*", "*-CGP"), from
the tax screen's own airport list. Each pair of airports is searched once
(a search lists both directions) on one travel date, and every tax found
is placed on the route it applies to: its departure airport -> its arrival
airport, or every route from its departure when it names no arrival.

Then, per route, which taxes are there -- and what looks wrong:

* Not set up        a tax code you asked for is not on the route, though
                    other routes from the same airport have it
* Missing           most routes from that airport (same kind) have it
* Different amount  the same tax from the same airport differs in amount
* Set up twice      the same tax twice on one route
* Only on some      a tax on a few routes from the airport, not the rest
* Not applicable    Zenith marks the entry not applicable
* Not ticked        the International / Domestic tick does not fit the route
* Set at 0          an applicable tax with a zero amount
* Not on any route from the airport   a code asked for that no checked
                    route from that airport has (expected for, say, AE
                    on routes leaving Bangkok)
* No route tax      a route with no route-specific tax at all
* Not fully read    a tax on the route's search could not be placed, so the
                    route is left out of the gap checks rather than judged

READ ONLY: every search goes through pricing_client's check_allowed.
"""
from __future__ import annotations

import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from . import pricing_parse as pp
from .flight_change_auth import BD_AIRPORTS

KINDS = ("INT-INT", "INT-DOM", "DOM-INT", "DOM-DOM")
#: Airport pairs whose answer cannot be read, with none read yet, before
#: the run stops (the screen changed, or the session is gone).
GIVE_UP_AFTER = 3
ANY = ""                         # a tax that names no airport on that side

NOT_SET_UP, MISSING, DIFFERENT, TWICE, SOME, NOT_APPLICABLE, NOT_TICKED, \
    ZERO, NOT_AT_AIRPORT, NO_ROUTE_TAX, NOT_READ = (
        "Not set up", "Missing", "Different amount", "Set up twice",
        "Only on some routes", "Not applicable", "Not ticked for the route",
        "Set at 0", "Not on any route from the airport", "No route tax",
        "Not fully read")
#: Findings sheet order: what most likely needs fixing first.
FINDING_ORDER = (NOT_READ, NOT_SET_UP, MISSING, DIFFERENT, TWICE, SOME, NOT_APPLICABLE,
                 NOT_TICKED, ZERO, NOT_AT_AIRPORT, NO_ROUTE_TAX)


def route_kind(dep: str, arr: str) -> str:
    side = ("DOM" if dep in BD_AIRPORTS else "INT",
            "DOM" if arr in BD_AIRPORTS else "INT")
    return "-".join(side)


def base_name(name: str) -> str:
    """'Passenger Service Charge | XDAC' -> 'Passenger Service Charge'."""
    return name.split("|", 1)[0].strip()


def is_zero(value: str) -> bool:
    m = re.match(r"\s*(-?\d+(?:[.,]\d+)?)", value or "")
    return bool(m) and float(m.group(1).replace(",", ".")) == 0


# ---- what to check ------------------------------------------------------------

@dataclass
class TaxSetupSettings:
    codes_text: str = ""             # "AE, ZR, YQ" -- empty = every tax
    routes_text: str = ""            # "AUH-DAC, DXB-*" -- empty = all
    include_return: bool = False
    kinds: tuple = KINDS
    date_ddmmyyyy: str = ""


def parse_codes(text: str) -> list:
    codes = []
    for token in re.split(r"[,;\s]+", (text or "").upper()):
        if not token:
            continue
        if not re.fullmatch(r"[A-Z0-9]{2}", token):
            raise ValueError(f"'{token}' is not a tax code — use two "
                             f"letters/digits such as AE, YQ, E5.")
        if token not in codes:
            codes.append(token)
    return codes


def parse_routes(text: str, airports, include_return: bool) -> tuple:
    """'AUH-DAC, DXB-*, *-CGP, AUHBKK' -> ({(dep, arr)}, [not understood])."""
    known = set(airports)
    pairs, bad = set(), []
    for token in re.split(r"[,;\s]+", (text or "").upper()):
        if not token:
            continue
        m = (re.fullmatch(r"([A-Z]{3}|\*|ALL)-([A-Z]{3}|\*|ALL)", token)
             or re.fullmatch(r"([A-Z]{3})([A-Z]{3})", token))
        if not m:
            bad.append(token)
            continue
        ends = []
        for side in m.groups():
            if side in ("*", "ALL"):
                ends.append(sorted(known))
            elif side in known:
                ends.append([side])
            else:
                ends.append([])
        if not ends[0] or not ends[1]:
            bad.append(token)
            continue
        for d in ends[0]:
            for a in ends[1]:
                if d != a:
                    pairs.add((d, a))
                    if include_return:
                        pairs.add((a, d))
    return pairs, bad


def pick_routes(airports, settings: TaxSetupSettings) -> tuple:
    """(sorted [(dep, arr)], notes)."""
    kinds = [k for k in settings.kinds if k in KINDS]
    if not kinds:
        raise ValueError("Tick at least one route type.")
    notes = []
    if settings.routes_text.strip():
        pairs, bad = parse_routes(settings.routes_text, airports,
                                  settings.include_return)
        if bad:
            notes.append("Not understood or not on the tax screen, skipped: "
                         + ", ".join(bad))
    else:
        pairs = {(d, a) for d in airports for a in airports if d != a}
    routes = sorted(p for p in pairs if route_kind(*p) in kinds)
    if not routes:
        raise ValueError("No route of the ticked type(s) among those "
                         "routes.")
    return routes, notes


# ---- what Zenith holds ----------------------------------------------------------

@dataclass(frozen=True)
class SetupRow:
    """One tax entry as the tax screen lists it, airports as codes."""
    tax_id: int
    code: str
    name: str
    dep: str                 # code, or ANY
    arr: str                 # code, or ANY = every route from dep
    value: str
    international: bool
    domestic: bool
    applicable: bool
    crossed: bool = False    # International shown as a red cross: unknown

    @property
    def key(self) -> tuple:
        return (self.code, base_name(self.name))

    @property
    def every_route(self) -> bool:
        return self.dep == ANY or self.arr == ANY

    def applies_to(self, dep: str, arr: str) -> bool:
        """A tax named for the route applies to it (its ticks are checked
        as findings); one set for every route only where its International
        / Domestic tick takes in the kind of route."""
        if self.dep not in (dep, ANY) or self.arr not in (arr, ANY):
            return False
        if not self.every_route or self.crossed:
            return True     # a crossed entry is shown (as not applicable)
        return (self.domestic if route_kind(dep, arr) == "DOM-DOM"
                else self.international)


def setup_rows(taxes, code_of_name: dict, pair: tuple, notes: list) -> list:
    """Tax rows -> SetupRows; a row whose airport cannot be named is noted
    and left out rather than placed on a guessed route."""
    out = []
    for t in taxes:
        ends = []
        for name in (t.departure, t.arrival):
            name = re.sub(r"\s+", " ", name or "").strip()
            ends.append(ANY if not name else code_of_name.get(name))
        if None in ends or any(e not in (ANY, *pair) for e in ends):
            notes.append(f"{pair[0]}-{pair[1]}: tax {t.code} '{t.name}' "
                         f"({t.departure} → {t.arrival or 'any'}) could not "
                         f"be placed on a route; left out.")
            continue
        out.append(SetupRow(t.tax_id, t.code, t.name, ends[0], ends[1],
                            t.value, t.international, t.domestic,
                            t.applicable, t.crossed))
    return out


# ---- the result -----------------------------------------------------------------

@dataclass
class Finding:
    kind: str
    route: str               # "AUH-DAC", or several, or "AUH → every route"
    route_type: str
    code: str
    tax: str
    found: str
    expected: str = ""
    note: str = ""

    def row(self) -> dict:
        return {"Kind": self.kind, "Route": self.route,
                "Route type": self.route_type, "Tax code": self.code,
                "Tax": self.tax, "Found": self.found,
                "Expected / others": self.expected, "Note": self.note}


@dataclass
class TaxSetupResult:
    date: str = ""
    kinds: list = field(default_factory=list)
    codes_asked: list = field(default_factory=list)
    routes: list = field(default_factory=list)          # [(dep, arr)]
    per_route: dict = field(default_factory=dict)       # route -> [SetupRow]
    unread: set = field(default_factory=set)            # routes not fully read
    findings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    searches: int = 0
    started: str = ""
    finished: str = ""

    def count(self) -> Counter:
        return Counter(f.kind for f in self.findings)


def run_tax_setup_check(client, settings: TaxSetupSettings, *, stop=None,
                        progress=lambda _m: None) -> TaxSetupResult:
    stop = stop or threading.Event()
    try:
        day = datetime.strptime(settings.date_ddmmyyyy.strip(), "%d/%m/%Y")
    except ValueError:
        raise ValueError("Travel date must be dd/mm/yyyy, e.g. "
                         "03/11/2026.") from None
    codes = parse_codes(settings.codes_text)
    res = TaxSetupResult(date=day.strftime("%d/%m/%Y"),
                         kinds=[k for k in KINDS if k in settings.kinds],
                         codes_asked=codes,
                         started=time.strftime("%Y-%m-%d %H:%M"))
    listed = pp.parse_tax_airport_list(client.tax_page())
    geo = {code: g for code, g, _n in listed}
    code_of_name = _names(listed, res.notes)
    res.routes, notes = pick_routes(geo, settings)
    res.notes.extend(notes)
    pairs = sorted({tuple(sorted(r)) for r in res.routes})
    found: dict = {}
    failures = read_ok = 0
    for i, (a, b) in enumerate(pairs, 1):
        if stop.is_set():
            raise InterruptedError("Stopped.")
        try:
            taxes = pp.parse_tax_list(client.tax_search(
                pp.tax_search_form(geo[a], geo[b], res.date)))
        except pp.LayoutChanged as exc:
            # one odd answer costs that pair, not the run -- unless nothing
            # can be read at all (signed out, or the screen really changed)
            failures += 1
            if failures >= GIVE_UP_AFTER and not read_ok:
                raise pp.LayoutChanged(
                    f"the tax screen's answer could not be read for the "
                    f"first {failures} airport pairs (last {a}-{b}: {exc})"
                ) from exc
            res.notes.append(f"{a}-{b}: the tax screen's answer could not "
                             f"be read ({exc}); this pair was skipped.")
            res.unread |= {(a, b), (b, a)}
            found[(a, b)] = []
            res.searches += 1
            continue
        read_ok += 1
        before = len(res.notes)
        found[(a, b)] = setup_rows(taxes, code_of_name, (a, b), res.notes)
        if len(res.notes) > before:
            res.unread |= {(a, b), (b, a)}
        res.searches += 1
        progress(f"Taxes: {i:,} of {len(pairs):,} airport pairs")
    for dep, arr in res.routes:
        rows = found[tuple(sorted((dep, arr)))]
        res.per_route[(dep, arr)] = [r for r in rows
                                     if r.applies_to(dep, arr)]
    res.unread &= set(res.routes)
    res.findings = find_issues(res.per_route, codes, res.unread)
    res.finished = time.strftime("%Y-%m-%d %H:%M")
    return res


def _names(listed, notes: list) -> dict:
    """Airport name -> code; a name two airports share cannot be traced."""
    seen = Counter(name for _c, _g, name in listed)
    for name, n in seen.items():
        if n > 1:
            notes.append(f"Two airports are both called '{name}' on the tax "
                         f"screen; their taxes cannot be told apart.")
    return {name: code for code, _g, name in listed if seen[name] == 1}


# ---- what looks wrong -----------------------------------------------------------

def find_issues(per_route: dict, codes_asked=(), unread=()) -> list:
    """per_route: {(dep, arr): [SetupRow applying to it]} -> [Finding].
    Routes in `unread` had a tax that could not be placed: they are named
    as such and never called missing anything."""
    out: list = [Finding(NOT_READ, f"{d}-{a}", route_kind(d, a), "", "",
                         "a tax could not be placed (see About → Notes)",
                         note="Left out of the gap checks")
                 for d, a in sorted(unread)]
    taxed = {r for r, rows in per_route.items() if r not in unread
             and any(x.applicable and not x.every_route for x in rows)}
    for (dep, arr), rows in sorted(per_route.items()):
        if (dep, arr) not in taxed and (dep, arr) not in unread:
            named = [x for x in rows if not x.every_route]
            out.append(Finding(NO_ROUTE_TAX, f"{dep}-{arr}",
                               route_kind(dep, arr), "", "",
                               "only not-applicable entries" if named
                               else "only taxes set for every route" if rows
                               else "no tax at all",
                               note="Not sold, or taxes not set up yet?"))
    out += _not_set_up(per_route, taxed, codes_asked)
    out += _by_departure(per_route, taxed, codes_asked)
    out += _amounts(per_route)
    out += _twice(per_route)
    out += _entry_marks(per_route)
    rank = {k: i for i, k in enumerate(FINDING_ORDER)}
    return sorted(out, key=lambda f: (rank[f.kind], f.route, f.code))


def _route_list(routes) -> str:
    return ", ".join(f"{d}-{a}" for d, a in sorted(routes))


def _usual(rows) -> str:
    vals = Counter(r.value for r in rows)
    return " / ".join(v for v, _n in vals.most_common(3))


def _not_set_up(per_route, taxed, codes_asked) -> list:
    """Each code asked for, per departure airport and route type: the
    routes that lack it while others like them have it -- or, when none
    has it, one line for them all (a departure tax such as AE is not
    expected on the way back)."""
    by_dep: dict = {}
    for route in taxed:
        by_dep.setdefault((route[0], route_kind(*route)), []).append(route)
    out = []
    for code in codes_asked:
        for (dep, kind), routes in sorted(by_dep.items()):
            has = {r: [x for x in per_route[r] if x.code == code]
                   for r in routes}
            live = [x for r in routes for x in has[r] if x.applicable]
            lacking = sorted(r for r in routes
                             if not any(x.applicable for x in has[r]))
            if not lacking:
                continue
            if not live:
                out.append(Finding(
                    NOT_AT_AIRPORT, f"every checked {kind} route from {dep}"
                    if len(lacking) > 1 else _route_list(lacking), kind, code,
                    "", "—", note=f"{len(lacking)} route(s): "
                                  f"{_route_list(lacking)}"))
                continue
            for route in lacking:
                out.append(Finding(
                    NOT_SET_UP, f"{route[0]}-{route[1]}", route_kind(*route),
                    code, "", "only a not-applicable entry" if has[route]
                    else "—", f"{_usual(live)} on {len(routes) - len(lacking)}"
                              f" other {kind} route(s) from {dep}"))
    return out


def _by_departure(per_route, taxed, codes_asked) -> list:
    """Routes from one airport, of one kind, should carry the same taxes."""
    groups: dict = {}
    for route in taxed:
        groups.setdefault((route[0], route_kind(*route)), []).append(route)
    out = []
    for (dep, kind), routes in sorted(groups.items()):
        if len(routes) < 2:
            continue
        named: dict = {}               # key -> {route: row named for it}
        present: dict = {}             # key -> routes carrying it at all
        for route in routes:
            for r in per_route[route]:
                if not r.applicable:
                    continue
                present.setdefault(r.key, set()).add(route)
                if not r.every_route:
                    named.setdefault(r.key, {})[route] = r
        for key, where in sorted(named.items()):
            code, name = key
            if code in codes_asked:
                continue                # Not set up covers the codes asked
            lacking = [r for r in routes if r not in present[key]]
            if not lacking:
                continue
            if 2 * len(present[key]) > len(routes):
                for route in sorted(lacking):
                    out.append(Finding(
                        MISSING, f"{route[0]}-{route[1]}", kind, code, name,
                        "—", f"{_usual(where.values())} on "
                             f"{len(present[key])} of {len(routes)} {dep} "
                             f"{kind} routes"))
            else:
                out.append(Finding(
                    SOME, _route_list(present[key]), kind, code, name,
                    _usual(where.values()),
                    f"not on {len(lacking)} other {dep} {kind} route(s): "
                    f"{_route_list(lacking)}"))
    return out


def _amounts(per_route) -> list:
    """One tax from one airport should cost the same on every route of a
    type. A route with the tax twice is left to Set up twice."""
    by_tax: dict = {}
    for route, rows in per_route.items():
        live = [r for r in rows if r.applicable]
        keys = Counter(r.key for r in live)
        for r in live:
            if keys[r.key] == 1:
                by_tax.setdefault((r.dep, route_kind(*route), r.key),
                                  {}).setdefault(r.value, set()).add(route)
    out = []
    for (dep, kind, (code, name)), values in sorted(by_tax.items()):
        if len(values) < 2:
            continue
        ranked = sorted(values.items(), key=lambda kv: -len(kv[1]))
        usual, usual_routes = ranked[0]
        tie = len(ranked[1][1]) == len(usual_routes)
        for value, routes in ranked if tie else ranked[1:]:
            others = "; ".join(f"{v} on {len(rr)} route(s)"
                               for v, rr in ranked if v != value)
            out.append(Finding(
                DIFFERENT, _route_list(routes), kind, code, name, value,
                others, f"from {dep or 'any airport'}"))
    return out


def _twice(per_route) -> list:
    out = []
    for route, rows in sorted(per_route.items()):
        same: dict = {}
        for r in rows:
            if r.applicable:
                same.setdefault(r.key, {})[r.tax_id] = r
        for (code, name), entries in sorted(same.items()):
            if len(entries) < 2:
                continue
            shown = [f"{r.value} ({r.name}"
                     + (", every route" if r.every_route else "") + ")"
                     for r in entries.values()]
            out.append(Finding(TWICE, f"{route[0]}-{route[1]}",
                               route_kind(*route), code, name,
                               " + ".join(shown),
                               note="Charged twice, or is one meant for "
                                    "other journeys?"))
    return out


def _entry_marks(per_route) -> list:
    """Per tax entry: not applicable, ticks that do not fit, zero. A tax
    set for every route only reaches routes its ticks fit, so only a tax
    named for the route can have the wrong tick."""
    seen: dict = {}
    for route, rows in per_route.items():
        for r in rows:
            seen.setdefault(r, set()).add(route)
    out = []
    for r, routes in sorted(seen.items(), key=lambda kv: kv[0].tax_id):
        where = _route_list(routes)
        if r.dep == ANY and r.arr == ANY:
            where = "every route"
        elif r.dep == ANY:
            where = f"every route to {r.arr}"
        elif r.arr == ANY:
            where = f"every route from {r.dep}"
        kinds = sorted({route_kind(*x) for x in routes})
        common = dict(route_type=", ".join(kinds), code=r.code, tax=r.name,
                      note=f"tax id {r.tax_id}")
        if not r.applicable:
            out.append(Finding(kind=NOT_APPLICABLE, route=where,
                               found=r.value, **common))
            continue
        wants_int = any(k != "DOM-DOM" for k in kinds)
        wants_dom = "DOM-DOM" in kinds
        gaps = []
        if not r.every_route and wants_int and not r.international:
            gaps.append("International")
        if not r.every_route and wants_dom and not r.domestic:
            gaps.append("Domestic")
        if gaps:
            out.append(Finding(kind=NOT_TICKED, route=where, found=r.value,
                               expected=" and ".join(gaps) + " ticked",
                               **common))
        if is_zero(r.value):
            out.append(Finding(kind=ZERO, route=where, found=r.value,
                               **common))
    return out
