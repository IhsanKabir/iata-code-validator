"""The pricing lookup: everything Zenith holds for some fare bases / routes.

No comparison -- a read-out, one row per season, for the seasons ticked:

* Fare basis  the fare basis's own settings and rules (GetFareBasisList)
* Fares       every Fare Editor row: amounts, currency, baggage, who last
              changed it and when
* Penalties   change / refund fees per window and passenger type
* Taxes       every tax on the route, and who is exempt from it -- the
              looked-up fare bases, passenger types, points of sale...
* Quick Sale  how each fare sells: with and without tax

Read-only through pricing_client, like the season check.
"""
from __future__ import annotations

import fnmatch
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import pricing_check as pc
from . import pricing_parse as pp

#: A lookup is for some fare bases or routes; more is the check's job. Two
#: busy routes both ways come to a few hundred.
MAX_FARE_BASES = 400

#: Fare-basis settings shown, in this order (names as Zenith gives them).
#: Codes such as Refundable / Combinability are shown as Zenith stores them.
FARE_BASIS_FIELDS = (
    "Code", "Text", "IsEnabled", "BookingClassCode", "Level", "MinStay",
    "MaxStay", "AllowOneWay", "Refundable", "Modifiable", "Combinability",
    "AdvancePurchaseBO", "AdvancePurchaseFO", "AdvancePurchaseTA", "IsGDS",
    "IsOpen", "IsVisibleBO", "IsVisibleFO", "IsInterline",
    "IsUpgradeFareBasis", "IsSundayRule", "IdBaseFareBasis", "IdFareBasis")


@dataclass
class LookupSettings:
    fare_bases_text: str = ""        # "YBDCCUO, BBD*" -- wildcards allowed
    rbd_text: str = ""               # booking classes: "E, S, V, K"
    routes_text: str = ""            # "DAC-CCU, DAC-DXB", "DAC-*"
    include_return: bool = True
    #: route types kept (DOM = a Bangladesh airport); all four = no filter
    kinds: tuple = ("INT-INT", "INT-DOM", "DOM-INT", "DOM-DOM")
    season_ids: list = field(default_factory=list)
    fares: bool = True
    penalties: bool = True
    taxes: bool = True
    tax_exemptions: bool = True      # opens each tax: ~3 s and 4 MB each
    quick_sale: bool = True
    #: Quick Sale amounts in: "" = each fare's own currency (as filed in
    #: the Fare Editor), or a code such as "USD" for all of them
    qs_currency: str = ""


@dataclass
class LookupResult:
    seasons: list = field(default_factory=list)       # names
    routes: list = field(default_factory=list)
    fare_bases: list = field(default_factory=list)    # dict rows
    fares: list = field(default_factory=list)
    penalties: list = field(default_factory=list)
    taxes: list = field(default_factory=list)
    quick_sale: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    started: str = ""
    finished: str = ""


# ---- what to read ------------------------------------------------------------

def match_fare_bases(text: str, rows: list) -> tuple:
    """Codes or wildcards ('YBD*', 'Y?DCCUO') -> (matching rows, unmatched)."""
    picked, unmatched = {}, []
    for token in re.split(r"[,;\s]+", text or ""):
        token = token.strip().upper()
        if not token:
            continue
        hits = [r for r in rows
                if fnmatch.fnmatchcase(str(r.get("Code") or "").upper(),
                                       token)]
        if not hits:
            unmatched.append(token)
        for r in hits:
            picked[int(r["IdFareBasis"])] = r
    return list(picked.values()), unmatched


def booking_classes(text: str) -> set:
    """'E, S, V, K' / 'E S V K' / 'ESVK' -> {'E', 'S', 'V', 'K'}."""
    return {ch for ch in re.sub(r"[^A-Za-z]", "", text or "").upper()}


def by_class(rows: list, classes: set) -> list:
    return [r for r in rows
            if str(r.get("BookingClassCode") or "").upper() in classes]


def lookup_date(season, today: date) -> date:
    """A day inside the season: for one still to come, the check's rule (a
    future day); for one already over, a week after it began."""
    end = pc._d(season.end)
    if end <= today + timedelta(days=1):
        start = pc._d(season.start)
        return min(start + timedelta(days=7), end - timedelta(days=1))
    return pc.check_date(season, today)


def _when(stamp) -> str:
    """'/Date(1781085720000)/' -> '2026-06-10 15:02'."""
    m = re.search(r"/Date\((-?\d+)", str(stamp or ""))
    if not m:
        return ""
    return datetime.fromtimestamp(int(m.group(1)) / 1000).strftime(
        "%Y-%m-%d %H:%M")


# ---- the parts ---------------------------------------------------------------

def _read_fares(client, lookups, chosen, seasons, legs_filter, res, stop,
                progress, keep_rows, currency_of=None):
    """Fare rows per fare basis and season. Returns {fb id: set of legs};
    fills `currency_of[(code, leg, season)]` with the filed currency (the
    adult row's, else the first)."""
    pax = lookups.passenger_types
    legs_of: dict = {}
    currency_of = {} if currency_of is None else currency_of
    for i, fb in enumerate(chosen, 1):
        for s in seasons:
            if stop.is_set():
                raise InterruptedError("Stopped.")
            for row in client.load_fares(int(fb["IdFareBasis"]), s.id):
                leg = lookups.legs.get(int(row.get("IdLeg", -1)), "")
                if not leg or (legs_filter and leg not in legs_filter):
                    continue
                legs_of.setdefault(int(fb["IdFareBasis"]), set()).add(leg)
                key = (fb["Code"], leg, s.name)
                cur = str(row.get("Currency") or "")
                if cur and (key not in currency_of
                            or int(row.get("IdPassengerType") or 0) == 1):
                    currency_of[key] = cur
                if keep_rows:
                    res.fares.append({
                        "Season": s.name, "Route": leg, "Fare basis":
                        fb["Code"], "Passenger": pax.get(
                            int(row.get("IdPassengerType") or 0),
                            row.get("IdPassengerType")),
                        **{f: row.get(f) for f in pp.FARE_FIELDS},
                        "Changed by": row.get("Modifier") or "",
                        "Changed on": _when(row.get("Modified"))})
        progress(f"Fares: {i:,} of {len(chosen):,} fare bases")
    return legs_of


def _search_leg(client, options, leg_id, classes, seasons, every):
    """(rows, failed class ids, problems) for one route."""
    check, failed = pc.CheckResult(), []
    rows = pc._penalty_rows(
        client, classes, [s.id for s in seasons], leg_id,
        sorted(options.passenger_types), check, str(leg_id),
        chunk_size=pc.PENALTY_SWEEP_CHUNK if every else pc.PENALTY_CHUNK,
        failed=failed)
    return rows, failed, check.problems


def sweep_penalties(client, legs, seasons, enabled_ids, stop, progress):
    """Every enabled class on each route, asked once: (options,
    {leg: (rows, failed, problems)}) -- for finding fare bases and then
    for the Penalties sheet without asking twice."""
    options = pp.parse_penalty_page(client.penalty_page())
    leg_id = {v.replace(" ", ""): k for k, v in options.legs.items()}
    every = sorted(c for c in options.classes if c in set(enabled_ids))
    out = {}
    for i, leg in enumerate(sorted(legs), 1):
        if stop.is_set():
            raise InterruptedError("Stopped.")
        if leg in leg_id and every:
            out[leg] = _search_leg(client, options, leg_id[leg], every,
                                   seasons, True)
        progress(f"Penalties: {i:,} of {len(legs):,} routes")
    return options, out


def _read_penalties(client, chosen, legs_of, seasons, res, stop, progress,
                    sweep=None, route_legs=(), prefetched=None):
    """Penalties per route. `sweep` (enabled fare basis ids) asks the
    penalty screen for EVERY enabled class on the route -- a route lookup
    must not depend on the route list, which can miss classes with
    penalties there (EX-CAN classes on CAN-MLE). Without it, only the
    looked-up fare bases are asked for. A looked-up fare basis with no
    penalty on a route it flies gets a row saying so."""
    options, found = prefetched or (
        pp.parse_penalty_page(client.penalty_page()), {})
    leg_id = {v.replace(" ", ""): k for k, v in options.legs.items()}
    code = {int(fb["IdFareBasis"]): fb["Code"] for fb in chosen}
    names = [s.name for s in seasons]
    by_leg: dict = {leg: [] for leg in route_legs}
    for fb_id, legs in legs_of.items():
        for leg in legs:
            by_leg.setdefault(leg, []).append(fb_id)
    every = (sorted(c for c in options.classes if c in set(sweep))
             if sweep is not None else None)
    # results name a class by its description: the screen's own list, then
    # the fare bases' Text, lead back to the code
    by_text = {str(fb.get("Text") or ""): fb["Code"] for fb in chosen
               if fb.get("Text")}
    texts = [str(fb.get("Text")) for fb in chosen if fb.get("Text")]
    for text in {t for t in texts if texts.count(t) > 1}:
        by_text.pop(text, None)            # shared by two fare bases
    by_text.update(options.code_of_text())
    for i, (leg, fb_ids) in enumerate(sorted(by_leg.items()), 1):
        if stop.is_set():
            raise InterruptedError("Stopped.")
        mine = sorted(c for c in fb_ids if c in options.classes)
        missing = sorted(set(fb_ids) - set(mine))
        if missing:
            res.notes.append(f"{leg}: not on the penalty screen — "
                             + ", ".join(code[c] for c in missing))
        classes = every if every is not None else mine
        if leg not in leg_id or not classes:
            if leg not in leg_id:
                res.notes.append(f"{leg}: route not on the penalty screen; "
                                 f"penalties not read.")
            continue
        rows, failed, problems = found.get(leg) or _search_leg(
            client, options, leg_id[leg], classes, seasons, every is not None)
        res.notes.extend(p.replace(f"{leg_id[leg]}:", f"{leg}:", 1)
                         for p in problems)
        # only classes really searched can be said to have no penalty
        searched = set(classes) - set(failed)
        expected = {code[c] for c in fb_ids if c in searched}
        seen, extra, unnamed = set(), set(), 0
        for r in rows:
            if r.season not in names:
                continue
            fb_code = _code_for(r.fare_class, by_text)
            seen.add((fb_code, r.season))
            unnamed += not fb_code
            row = {"Season": r.season, "Route": leg, "Fare basis": fb_code,
                   "Fare class (as shown)": r.fare_class,
                   "Passenger": r.passenger, "Window": r.window,
                   "Change fee": r.change, "Refund fee": r.refund}
            if fb_code not in expected:
                extra.add(fb_code or r.fare_class)
                row["Status"] = ("penalty set, but no Fare Editor fare for "
                                 "it on this route")
            res.penalties.append(row)
        if extra:
            res.notes.append(
                f"{leg}: penalties set for {len(extra)} fare basis(es) with "
                f"no Fare Editor fare on this route (included, marked in "
                f"Status): " + ", ".join(sorted(extra)))
        if unnamed:
            res.notes.append(
                f"{leg}: {unnamed} penalty row(s) could not be matched to a "
                f"fare basis code (Fare basis left empty) — so no fare "
                f"basis on this route is reported as having no penalty.")
            expected = set()
        for fb_code in sorted(expected):
            for season in names:
                if (fb_code, season) not in seen:
                    res.penalties.append({
                        "Season": season, "Route": leg,
                        "Fare basis": fb_code,
                        "Status": "NO PENALTY SET in Zenith for this "
                                  "route and season"})
        progress(f"Penalties: {i:,} of {len(by_leg):,} routes")


def _code_for(shown: str, by_text: dict) -> str:
    """The penalty table names a class by its description; match it back."""
    if shown in by_text:
        return by_text[shown]
    # a near match only when exactly one description fits -- never a guess
    near = {code for text, code in by_text.items()
            if shown and text and (shown in text or text in shown)}
    return near.pop() if len(near) == 1 else ""


def _read_taxes(client, chosen, legs, seasons, today, exemptions, res, stop,
                progress):
    airports = pp.parse_tax_airports(client.tax_page())
    fb_code = {int(fb["IdFareBasis"]): fb["Code"] for fb in chosen}
    opened: dict = {}                      # (tax id, day) -> TaxDetail
    for i, leg in enumerate(sorted(legs), 1):
        dep, arr = leg.split("-")
        if dep not in airports or arr not in airports:
            res.notes.append(f"{leg}: airport not on the tax screen; taxes "
                             f"not read.")
            continue
        for s in seasons:
            if stop.is_set():
                raise InterruptedError("Stopped.")
            day = lookup_date(s, today).strftime("%d/%m/%Y")
            try:
                taxes = pp.parse_tax_list(client.tax_search(
                    pp.tax_search_form(airports[dep], airports[arr], day)))
            except pp.LayoutChanged as exc:
                res.notes.append(f"{leg} {s.name}: the tax screen's answer "
                                 f"could not be read ({exc}); taxes not "
                                 f"read.")
                continue
            for t in taxes:
                row = {"Season": s.name, "Route": leg, "On date": day,
                       "Tax code": t.code, "Tax": t.name,
                       "Departure": t.departure, "Arrival": t.arrival,
                       "Value": t.value,
                       "International": "yes" if t.international else "no",
                       "Domestic": "yes" if t.domestic else "no",
                       "Applicable": "yes" if t.applicable else
                       "NO — Zenith marks it not applicable"}
                if exemptions and t.tax_id:
                    key = (t.tax_id, day)
                    if key not in opened:
                        opened[key] = pp.parse_tax_detail(client.tax_search(
                            pp.tax_detail_form(t.tax_id, airports[dep],
                                               airports[arr], day)))
                    row.update(_exemption_columns(opened[key], fb_code))
                res.taxes.append(row)
        progress(f"Taxes: {i:,} of {len(legs):,} routes")


def _exemption_columns(detail, fb_code: dict) -> dict:
    exempt = detail.exempt_fare_bases()
    mine = sorted(c for i, c in fb_code.items() if i in exempt)
    out = {"Looked-up fare bases exempt": ", ".join(mine) or "none"}
    for name, label in pp.EXEMPT_LISTS.items():
        values = detail.exempt.get(name, {})
        out[f"Exempt: {label}"] = "; ".join(
            values[k] or k for k in sorted(values)) or ""
    return out


def _read_quick_sale(client, chosen, legs, seasons, today, res, stop,
                     progress, currency_of, forced=""):
    """How each fare sells, in its own filed currency (Quick Sale's ,FC-XXX)
    -- or in `forced` for all. Quick Sale shows the selling currency (BDT)
    unless asked, which is why amounts came out in BDT before."""
    codes = {fb["Code"] for fb in chosen}
    later = [s for s in seasons if pc._d(s.end) > today + timedelta(days=1)]
    for s in seasons:
        if s not in later:
            res.notes.append(f"Quick Sale: {s.name} is over — Quick Sale "
                             f"cannot show past dates (it moves them a year "
                             f"on).")
    for i, leg in enumerate(sorted(legs), 1):
        od = leg.replace("-", "")
        for s in later:
            if stop.is_set():
                raise InterruptedError("Stopped.")
            day = pc.fdi_date(pc.check_date(s, today))
            groups: dict = {}
            for code in sorted(codes):
                cur = forced or currency_of.get((code, leg, s.name), "")
                groups.setdefault(cur, set()).add(code)
            for cur, mine in sorted(groups.items()):
                _quick_sale_in(client, od, leg, s, day, cur, mine, res)
        progress(f"Quick Sale: {i:,} of {len(legs):,} routes")


def _quick_sale_in(client, od, leg, season, day, cur, codes, res) -> None:
    """One currency's display (without and with tax) for these fares."""
    convert = f",FC-{cur}" if cur else ""
    check = pc.CheckResult()
    net = pc._qs(client, f"FDI{od}/D{day}/R,NP,WT{convert}", check, leg,
                 season.name)
    gross = pc._qs(client, f"FDI{od}/D{day}/R,NP{convert}", check, leg,
                   season.name)
    res.notes.extend(check.problems)
    if net is None or gross is None:
        return
    g = {f.fare_basis: f for f in gross.fares}
    listed = set()
    for f in net.fares:
        if f.fare_basis not in codes:
            continue
        listed.add(f.fare_basis)
        row = pc._qs_row(f, g.get(f.fare_basis), net.currency)
        res.quick_sale.append({"Season": season.name, "Route": leg,
                               "Travel date": day,
                               "Fare basis": row.pop("code"), **row})
    for code in sorted(codes - listed):
        res.quick_sale.append({"Season": season.name, "Route": leg,
                               "Travel date": day, "Fare basis": code,
                               "Currency": "not listed in Quick Sale"})


# ---- the whole lookup --------------------------------------------------------

def _discover(client, index, rows, by_id, chosen, legs, seasons, today,
              settings, res, stop, progress):
    """A route lookup: the route list's fare bases, plus every enabled one
    Quick Sale sells on the routes or the penalty screen has there -- so a
    stale route list cannot hide one. Returns (chosen, {id: found via},
    the penalty answers to reuse or None)."""
    from . import pricing_discover as pd
    by_code = pd.enabled_ids_by_code(rows)
    sources = {int(r["IdFareBasis"]): {pd.ROUTE_LIST} for r in chosen}
    pd.merge(sources, pd.on_sale(client, legs, seasons, today, by_code,
                                 res.notes, stop=stop, progress=progress),
             pd.QUICK_SALE)
    prefetched = None
    if settings.penalties:
        prefetched = sweep_penalties(client, legs, seasons,
                                     set(by_code.values()), stop, progress)
        options, per_leg = prefetched
        names = options.code_of_text()
        hits = {}
        for leg_rows, _failed, _problems in per_leg.values():
            for r in leg_rows:
                fb_id = by_code.get(names.get(r.fare_class, "").strip()
                                    .upper())
                if fb_id is not None:
                    hits[fb_id] = True
        pd.merge(sources, hits, pd.PENALTY_SCREEN)
    have = {int(r["IdFareBasis"]) for r in chosen}
    extra = [i for i in pd.outside_list(index, legs, sources)
             if i in by_id and i not in have]
    return chosen + [by_id[i] for i in extra], sources, prefetched



def run_lookup(client, index, settings: LookupSettings, *, today=None,
               stop=None, progress=lambda _m: None) -> LookupResult:
    stop = stop or threading.Event()
    today = today or date.today()
    res = LookupResult(started=time.strftime("%Y-%m-%d %H:%M"))
    rows = client.fare_basis_list()
    every = pp.parse_fare_basis_list(rows)
    if not every:
        raise ValueError("Zenith returned no fare bases.")
    lookups = pp.parse_fare_editor_page(client.fare_editor_page(every[0].id))
    seasons = sorted((s for s in lookups.seasons
                      if s.id in set(settings.season_ids)),
                     key=lambda s: pc._d(s.start))
    if not seasons:
        raise ValueError("Tick at least one season.")
    res.seasons = [s.name for s in seasons]

    legs = set()
    if settings.routes_text.strip():
        legs, unknown = pc.parse_routes(settings.routes_text,
                                        lookups.legs.values(),
                                        settings.include_return)
        if unknown:
            res.notes.append("Not Zenith routes, skipped: "
                             + ", ".join(unknown))
        if not legs:
            raise ValueError("None of those routes is in Zenith.")
        legs = pc.filter_kinds(legs, settings.kinds)
        if not legs:
            raise ValueError("None of those routes is of the ticked route "
                             "type(s): " + ", ".join(settings.kinds) + ".")
    by_id = {int(r["IdFareBasis"]): r for r in rows
             if r.get("IdFareBasis") is not None}
    classes = booking_classes(settings.rbd_text)
    if settings.fare_bases_text.strip():
        chosen, unmatched = match_fare_bases(settings.fare_bases_text, rows)
        if unmatched:
            res.notes.append("No fare basis matches: " + ", ".join(unmatched))
    elif legs:
        if index.load():
            chosen = [by_id[i] for i in index.fare_bases_for(legs)
                      if i in by_id]
            res.notes.append(f"Fare bases on these routes: the route list "
                             f"built {index.built}, plus what Quick Sale and "
                             f"the penalty screen show there (enabled fare "
                             f"bases only).")
            if index.age_note(today):
                res.notes.append(index.age_note(today))
        else:
            chosen = []
            res.notes.append("No route list yet: fare bases on these routes "
                             "come from Quick Sale and the penalty screen "
                             "only — build the route list to add the rest.")
    elif classes:
        chosen = [r for r in rows if r.get("IsEnabled")
                  and r.get("IdFareBasis") is not None]
        res.notes.append("Booking class alone: every ENABLED fare basis in "
                         "those classes, on whatever routes it flies.")
    else:
        raise ValueError("Enter a fare basis, a booking class (RBD) or a "
                         "route.")
    found_via: dict = {}
    prefetched = None
    if legs and not settings.fare_bases_text.strip() and not classes:
        chosen, found_via, prefetched = _discover(
            client, index, rows, by_id, chosen, legs, seasons, today,
            settings, res, stop, progress)
    if classes:
        chosen = by_class(chosen, classes)
        res.notes.append("Booking classes: " + ", ".join(sorted(classes)))
    if not chosen:
        raise ValueError("No fare basis to look up" + (
            f" in booking class {', '.join(sorted(classes))}"
            if classes else "") + ".")
    if len(chosen) > MAX_FARE_BASES:
        raise ValueError(f"That is {len(chosen):,} fare bases — narrow it to "
                         f"{MAX_FARE_BASES} or fewer: fewer routes, a route "
                         f"type, a booking class or a code pattern.")
    chosen.sort(key=lambda r: str(r.get("Code")))
    res.fare_bases = [{**{f: r.get(f) for f in FARE_BASIS_FIELDS},
                       **({"Found via": ", ".join(sorted(found_via.get(
                           int(r["IdFareBasis"]), ())))} if found_via
                          else {})}
                      for r in chosen]

    # route types alone narrow what a fare basis / class search reads: the
    # fares are read on those routes only, and taxes / Quick Sale then go
    # where the fare bases actually fly -- not every route of the type
    legs_filter = legs
    if len(set(settings.kinds)) < 4 and not legs:
        legs_filter = pc.filter_kinds(lookups.legs.values(), settings.kinds)
        res.notes.append("Route types: " + ", ".join(settings.kinds))
        if not legs_filter:
            raise ValueError("Zenith has no route of the ticked type(s).")

    # which routes each fare basis flies -- from its fare rows
    currency_of: dict = {}
    legs_of = _read_fares(client, lookups, chosen, seasons, legs_filter, res,
                          stop, progress, keep_rows=settings.fares,
                          currency_of=currency_of)
    if found_via:
        from . import pricing_discover as pd
        gained = pd.learn(index, legs_of)
        extra = sorted(str(r["Code"]) for r in chosen
                       if pd.ROUTE_LIST not in found_via.get(
                           int(r["IdFareBasis"]), {pd.ROUTE_LIST}))
        if extra:
            how = {str(r["Code"]): found_via[int(r["IdFareBasis"])]
                   for r in chosen if str(r["Code"]) in extra}
            res.notes.append(pd.note(extra, how, gained))
    if legs:
        routes = set(legs)
    else:
        routes = set().union(*legs_of.values()) if legs_of else set()
    res.routes = sorted(routes)
    if not routes:
        res.notes.append("No fares found for these fare bases in the ticked "
                         "seasons, so no routes to read taxes, penalties or "
                         "Quick Sale for.")
    # a route lookup reads every enabled class on the route; a fare
    # basis / class lookup only the ones asked for
    by_route = bool(legs) and not (settings.fare_bases_text.strip()
                                   or classes)
    if settings.penalties and (legs_of or by_route):
        _read_penalties(client, chosen, legs_of, seasons, res, stop, progress,
                        sweep=[int(r["IdFareBasis"]) for r in rows
                               if r.get("IsEnabled")
                               and r.get("IdFareBasis") is not None]
                        if by_route else None,
                        route_legs=sorted(legs) if by_route else (),
                        prefetched=prefetched)
    if settings.taxes and routes:
        _read_taxes(client, chosen, routes, seasons, today,
                    settings.tax_exemptions, res, stop, progress)
    if settings.quick_sale and routes:
        forced = settings.qs_currency.strip().upper()
        _read_quick_sale(client, chosen, routes, seasons, today, res, stop,
                         progress, currency_of, forced)
        res.notes.append("Quick Sale amounts are in " + (
            forced if forced else "each fare's own currency, as filed in the "
            "Fare Editor") + " — see the Currency column.")
    res.notes = list(dict.fromkeys(res.notes))      # each said once
    res.finished = time.strftime("%Y-%m-%d %H:%M")
    return res


def seasons_of(client) -> list:
    """Every season the Fare Editor offers, for the tick boxes."""
    every = pp.parse_fare_basis_list(client.fare_basis_list())
    if not every:
        return []
    page = pp.parse_fare_editor_page(client.fare_editor_page(every[0].id))
    return sorted(page.seasons, key=lambda s: pc._d(s.start))
