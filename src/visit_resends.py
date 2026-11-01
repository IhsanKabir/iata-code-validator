"""Reports sent again: a day's visit report, or part of it, filed under
another date or another rep's name.

The duplicate check in visit_master drops a row read twice for the SAME day.
Change the date and send it again, and every copied row reads as a revisit:
in ten days of September one rep's 3 Sep report was his 1 Sep report -- the
same nine agencies, in the same order, with the same contacts and the same
remarks -- plus two new rows, and it counted as eleven visits.

A row matches when the agency and what was discussed both match. A revisit
is the same agency; it is the same conversation, word for word, that a
revisit does not explain. Like visit_repeats, this lists rows to check, it
does not deduct them.

Run it on visits whose rep names are already merged (summarise does that).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from .visit_master import identity
from .visit_repeats import (SHARED_MIN_CHARS, SHARED_MIN_WORDS, Entry,
                            RepeatResult, _day, _entries, _leaders, _n, _wide,
                            norm_text)

#: This share of a day's rows, or more, matching one earlier report: the
#: whole report was sent again.
RESENT_SHARE = 0.8
#: Fewer matching rows than this is two days that happen to agree.
RESENT_MIN_ROWS = 3


@dataclass(frozen=True)
class Resend:
    """Rows of one rep-day that repeat an earlier rep-day."""
    rep: str
    day: date
    source_rep: str
    source_day: date
    rows: int                 # every row in the later report
    copied: tuple             # Entry per matching row of the later report
    # the matching visits themselves: two rows can read alike as Entries
    ids: frozenset = field(default=frozenset(), compare=False, repr=False)
    # the (agency, remark) keys repeated: one block sent on three later dates
    # is one finding with three dates, not three copies of the same evidence
    keys: frozenset = field(default=frozenset(), compare=False, repr=False)

    @property
    def share(self) -> float:
        return len(self.copied) / self.rows if self.rows else 0.0

    @property
    def same_rep(self) -> bool:
        return self.rep == self.source_rep

    @property
    def same_day(self) -> bool:
        """Two reps, one day: a joint visit, or one filed the other's report."""
        return self.day == self.source_day

    @property
    def whole(self) -> bool:
        return self.share >= RESENT_SHARE


def _informative(leader: str) -> bool:
    # "regular visit" at the same agency every week is a route, not a copy
    return (len(leader) >= SHARED_MIN_CHARS
            and len(leader.split()) >= SHARED_MIN_WORDS)


def _row_keys(visits, repeated: RepeatResult | None) -> dict:
    """Visit -> (agency, remark) key, for the rows that can be compared.

    A rep's stock remark is left out: it matches every visit they made, so a
    revisit would look like a copy. It is reported in REPEATED ENTRIES.
    """
    leader_of = _leaders(visits)
    stock = defaultdict(set)
    for s in (repeated.stock if repeated else ()):
        stock[s.rep].add(leader_of.get(norm_text(s.text), norm_text(s.text)))
    out = {}
    for v in visits:
        text, agency = norm_text(v.remarks), identity(v.agency)
        if not (v.day and text and agency):
            continue
        leader = leader_of[text]
        if _informative(leader) and leader not in stock[v.rep]:
            out[id(v)] = (agency, leader)
    return out


def find_resends(visits, repeated: RepeatResult | None = None) -> list:
    """Every rep-day whose rows repeat another rep-day's, best source first."""
    visits = list(visits)
    keys = _row_keys(visits, repeated)
    rows: dict = defaultdict(list)            # (rep, day) -> its visits
    keyset: dict = defaultdict(set)           # (rep, day) -> its row keys
    holders: dict = defaultdict(set)          # row key -> rep-days holding it
    for v in visits:
        if v.day:
            rows[(v.rep, v.day)].append(v)
        k = keys.get(id(v))
        if k:
            keyset[(v.rep, v.day)].add(k)
            holders[k].add((v.rep, v.day))

    out = []
    for target, mine in keyset.items():
        rep, day = target
        shared: Counter = Counter()
        for k in mine:
            for src in holders[k]:
                # earlier days only, and another rep on the same day once
                if src[1] < day or (src[1] == day and src[0] < rep):
                    shared[src] += 1
        # best source first: most rows, then the rep's own, then the earliest
        ranked = sorted((s for s, n in shared.items() if n >= RESENT_MIN_ROWS),
                        key=lambda s: (-shared[s], s[0] != rep, s[1], s[0]))
        covered: set = set()
        for src in ranked:
            common = mine & keyset[src]
            # 1, 3 and 5 Sep all alike: 5 Sep is reported against 1 Sep, not
            # again against 3 Sep, which adds nothing new
            if common <= covered:
                continue
            covered |= common
            # in the order the rep wrote them: the same rows in the same
            # order is the plainest sign of a copy
            hit = [v for v in rows[target] if keys.get(id(v)) in common]
            copied = tuple(Entry(v.rep, v.day, _n(v.agency), _n(v.remarks))
                           for v in hit)
            out.append(Resend(rep=rep, day=day, source_rep=src[0],
                              source_day=src[1], rows=len(rows[target]),
                              copied=copied,
                              ids=frozenset(id(v) for v in hit),
                              keys=frozenset(common)))
    return sorted(out, key=lambda x: (x.same_day, not x.whole, -x.share,
                                      x.rep, x.day))


def resend_counts(found) -> dict:
    """Per rep: (days sent again whole, rows copied) -- joint days left out.

    A day counts as re-sent when RESENT_SHARE of its rows repeat earlier
    reports between them: four rows of 1 Sep and four of 2 Sep make all of a
    5 Sep report, though neither report alone is most of it.
    """
    per_day: dict = defaultdict(set)
    size: dict = {}
    for x in found:
        if not x.same_day:
            per_day[(x.rep, x.day)].update(x.ids)
            size[(x.rep, x.day)] = x.rows
    out: dict = {}
    for (rep, day), ids in per_day.items():
        days, rows = out.get(rep, (0, 0))
        out[rep] = (days + (len(ids) >= RESENT_SHARE * size[(rep, day)]),
                    rows + len(ids))
    return out


def write_resends(ws, r: int, found) -> int:
    """The re-sent-reports section, from row r; returns the next free row."""
    from .counter_master import (BAD, GREY, LAST, PAPER, WARN, _band, _cell,
                                 _headers)

    blocks = group_resends(found)
    later = [b for b in blocks if not b[0].same_day]
    whole = sum(1 for x in found if x.whole and not x.same_day)
    r = _band(ws, r, "RE-SENT REPORTS — A DAY'S REPORT FILED AGAIN",
              "same agency, same remark, on a later date or under another rep")
    ws.merge_cells(f"A{r}:{LAST}{r}")
    _cell(ws, r, 1,
          f"  {whole} day report(s) sent again whole ({RESENT_SHARE:.0%}+ of the "
          f"day's rows repeat an earlier report) · {len(later)} set(s) of rows "
          f"sent again in all, each with every date it came back on · "
          f"{len(blocks) - len(later)} pair(s) of reps filing the same rows on "
          f"the same day. A row repeats when the agency and the remark both do; "
          f"a remark under {SHARED_MIN_WORDS} words, or a rep's stock remark, is "
          f"not compared. A remark that describes the agency ('holiday maker') "
          f"can fairly repeat on a revisit — one that reports a conversation "
          f"should not.",
          size=9, color=GREY, fill=PAPER, align="left", wrap=True)
    ws.row_dimensions[r].height = 30
    r += 1
    r = _headers(ws, r, ["Sales rep", "First sent", "Rows",
                         "Sent again on (rows repeated of that day's report)",
                         *[""] * 17])
    if not found:
        _cell(ws, r, 1, "No day's report repeats another one.", size=9,
              color=GREY)
        return r + 2
    for block in blocks:
        x = block[0]
        is_whole = any(b.whole for b in block)
        kind = ("JOINT VISIT, OR ONE REP FILED THE OTHER'S REPORT" if x.same_day
                else ("WHOLE REPORT SENT AGAIN" if is_whole
                      else "PART OF A REPORT SENT AGAIN")
                + ("" if x.same_rep else f" UNDER {x.rep.upper()}'S NAME"))
        tone = PAPER if x.same_day else BAD if is_whole else WARN
        source = (_day(x.source_day) if x.same_rep
                  else f"{x.source_rep}, {_day(x.source_day)}")
        _cell(ws, r, 1, x.rep, bold=True, size=10, border=True, fill=tone)
        _cell(ws, r, 2, source, bold=True, size=9, border=True,
              align="center", fill=tone)
        _cell(ws, r, 3, len(x.copied), size=9, border=True, align="center",
              fill=tone)
        _wide(ws, r, 4, f"{kind}:  " + " · ".join(
            f"{_day(b.day)} ({len(b.copied)} of {b.rows}, {b.share:.0%})"
            for b in block), _cell, bold=True, fill=tone)
        r = _entries(ws, r + 1, x.copied, f"Remark — word for word as on {source}")
    return r + 1


def group_resends(found) -> list:
    """Findings that repeat the same rows of the same report, as one list
    each, in the order found: '9 Jul's rows on 16, 23 and 28 Jul'."""
    groups: dict = {}
    for x in found:
        groups.setdefault((x.rep, x.source_rep, x.source_day, x.keys,
                           x.same_day), []).append(x)
    # strongest block first, as found; its dates in date order
    return [sorted(g, key=lambda b: b.day) for g in groups.values()]
