"""Fare bases on a route that the route list does not know.

The route list (pricing_check.RouteIndex) is built from Fare Editor rows
and goes stale: a fare basis added or moved to a new route after it was
built is missing from every route-based read until it is rebuilt. Two
screens answer per route, so they fill the gap on every run:

* Quick Sale    every fare basis on sale on the route on a day in each
                season still to come (FDI.../R,NP,WT)
* Penalties     every class with penalties on the route (the screen is
                asked for all enabled classes)

What they find is added to the run, and the route list learns each fare
basis's routes from the fare rows read for it (`learn`) -- it only ever
gains routes here; a full rebuild is what drops old ones. Read only.
"""
from __future__ import annotations

from datetime import timedelta

from . import pricing_check as pc

QUICK_SALE, PENALTY_SCREEN, ROUTE_LIST = (
    "Quick Sale", "penalty screen", "route list")


def enabled_ids_by_code(rows) -> dict:
    """Fare basis list rows -> {CODE: id}, enabled fare bases only."""
    return {str(r.get("Code") or "").strip().upper(): int(r["IdFareBasis"])
            for r in rows
            if r.get("IsEnabled") and r.get("IdFareBasis") is not None}


def on_sale(client, legs, seasons, today, by_code: dict, problems: list, *,
            stop, progress) -> dict:
    """{fare basis id: {leg}} for every enabled fare basis Quick Sale lists
    on the legs, in each season still to come."""
    later = [s for s in seasons if pc._d(s.end) > today + timedelta(days=1)]
    found: dict = {}
    for i, leg in enumerate(sorted(legs), 1):
        for s in later:
            if stop.is_set():
                raise InterruptedError("Stopped.")
            check = pc.CheckResult()
            day = pc.fdi_date(pc.check_date(s, today))
            shown = pc._qs(client, f"FDI{leg.replace('-', '')}/D{day}/R,NP,WT",
                           check, leg, s.name)
            problems.extend(check.problems)
            for f in (shown.fares if shown else []):
                fb_id = by_code.get(f.fare_basis.strip().upper())
                if fb_id is not None:
                    found.setdefault(fb_id, set()).add(leg)
        progress(f"Finding fare bases on sale: {i:,} of {len(legs):,} routes")
    return found


def merge(sources: dict, found: dict, label: str) -> None:
    """sources: {id: {label}}; found: {id: anything} -- add `label`."""
    for fb_id in found:
        sources.setdefault(fb_id, set()).add(label)


def outside_list(index, legs, sources: dict) -> list:
    """Ids found on the legs by a screen but not by the route list there."""
    listed = set(index.fare_bases_for(legs)) if index.legs else set()
    return sorted(i for i, how in sources.items()
                  if i not in listed and how - {ROUTE_LIST})


def learn(index, legs_of: dict) -> int:
    """Add the routes fare rows showed to the route list (never remove).
    Returns how many fare bases gained a route; saves when any did."""
    if not index.legs and not index.built:
        return 0                          # no list yet: rebuild makes one
    gained = 0
    for fb_id, legs in legs_of.items():
        have = index.legs.setdefault(int(fb_id), set())
        if not set(legs) <= have:
            have |= set(legs)
            gained += 1
    if gained:
        index.save()
    return gained


def note(codes: list, how: dict, gained: int) -> str:
    """One line for the notes / problems."""
    shown = ", ".join(f"{c} ({', '.join(sorted(how[c]))})"
                      for c in codes[:40])
    more = f" and {len(codes) - 40} more" if len(codes) > 40 else ""
    tail = (f" The route list now knows {gained} of them." if gained
            else "")
    return (f"{len(codes)} fare basis(es) on these routes were missing from "
            f"the route list and were added to this run: {shown}{more}."
            + tail)
