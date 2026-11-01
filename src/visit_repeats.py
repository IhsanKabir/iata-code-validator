"""Text the reps repeated: one remark pasted onto many agencies, one remark
written by two people, one phone number given to two agencies.

The duplicate check in visit_master only drops a row read twice. It never
compares what was written, and that is where a visit report gets filled in
without a visit behind it: ten days of September held a rep who gave 53
agencies the same sentence, and one phone number filed for three different
agencies. None of it is proof on its own -- a rep may well say the same thing
to every agency -- so everything here is listed to be checked, not deducted.

Run it on visits whose rep names have already been merged (summarise does
that), or one person's two spellings look like two people agreeing.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from .visit_master import DATE_RE, ISO_DATE_RE, Visit, identity, person_key

#: A remark on this many different agencies is a stock remark.
STOCK_MIN_AGENCIES = 5
#: Two remarks this alike (rapidfuzz ratio, 0-100) are one remark: it takes
#: "pressured him to sell" and "pressured them to sell" together.
SIMILAR = 85
#: Shorter than this, two reps agreeing is just two people saying "new agency".
SHARED_MIN_CHARS = 25
SHARED_MIN_WORDS = 4
#: Two agency names this alike under one phone number are one agency spelled
#: twice ("Darvish Travels" / "DARWISH TRAVEL"), not two agencies.
AGENCY_SAME = 85
#: How many example rows a finding shows.
EXAMPLES = 12


def norm_text(text) -> str:
    """Lower case, punctuation as spaces: what two copies of one remark share."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


@dataclass(frozen=True)
class Entry:
    rep: str
    day: date | None
    agency: str
    text: str


@dataclass
class StockRemark:
    """One rep, one remark, many agencies."""
    rep: str
    text: str                 # the wording they used most
    wordings: int             # near-identical versions folded into it
    agencies: int
    rows: int
    rep_rows: int             # every visit the rep logged

    @property
    def share(self) -> float:
        return self.rows / self.rep_rows if self.rep_rows else 0.0


@dataclass
class SharedRemark:
    """One remark, written by more than one rep."""
    text: str
    reps: tuple
    entries: list
    joint: bool               # one agency, one day: they went together


@dataclass
class SharedPhone:
    """One phone number, filed for more than one agency."""
    phone: str
    agencies: tuple
    entries: list             # Entry.text is the contact person
    same_contact: bool        # one person at every agency: one owner, two firms?


@dataclass
class Overlap:
    """One agency, visited by more than one rep."""
    agency: str
    reps: tuple
    visits: int
    days: tuple
    where: tuple = ()          # "rep (location)": a common name in two
                               # stations is usually two businesses


@dataclass
class RepeatResult:
    stock: list = field(default_factory=list)
    shared: list = field(default_factory=list)
    phones: list = field(default_factory=list)
    overlaps: list = field(default_factory=list)

    @property
    def stock_reps(self) -> int:
        return len({s.rep for s in self.stock})

    @property
    def phones_different(self) -> int:
        """Numbers with a different contact person at each agency."""
        return sum(1 for p in self.phones if not p.same_contact)

    @property
    def copied(self) -> int:
        """Shared remarks that are not explained by a joint visit."""
        return sum(1 for s in self.shared if not s.joint)


# --------------------------------------------------------------------------
# grouping near-identical text
# --------------------------------------------------------------------------
def cluster(texts) -> dict:
    """Map each text to the first, most common text it is SIMILAR to.

    Leader clustering, not chaining: every member is compared with its
    leader, so a run of small edits cannot walk one remark into another.
    The caller passes texts most common first, so the leader is the wording
    most people actually used.
    """
    from rapidfuzz import fuzz, process

    leaders: list[str] = []
    out: dict[str, str] = {}
    for t in texts:
        hit = (process.extractOne(t, leaders, scorer=fuzz.ratio,
                                  score_cutoff=SIMILAR) if leaders else None)
        if hit is None:
            leaders.append(t)
            out[t] = t
        else:
            out[t] = hit[0]
    return out


def _leaders(visits) -> dict:
    counts = Counter(norm_text(v.remarks) for v in visits)
    counts.pop("", None)
    ordered = sorted(counts, key=lambda t: (-counts[t], t))
    return cluster(ordered)


# --------------------------------------------------------------------------
# the four checks
# --------------------------------------------------------------------------
def stock_remarks(visits, leader_of: dict,
                  min_agencies: int = STOCK_MIN_AGENCIES) -> list:
    rep_rows = Counter(v.rep for v in visits)
    groups: dict = defaultdict(list)
    for v in visits:
        t = norm_text(v.remarks)
        if t:
            groups[(v.rep, leader_of[t])].append(v)
    out = []
    for (rep, _leader), vs in groups.items():
        agencies = {identity(v.agency) for v in vs}
        if len(agencies) < min_agencies:
            continue
        wordings = Counter(_n(v.remarks) for v in vs)
        out.append(StockRemark(
            rep=rep, text=wordings.most_common(1)[0][0],
            wordings=len({norm_text(w) for w in wordings}),
            agencies=len(agencies), rows=len(vs), rep_rows=rep_rows[rep]))
    return sorted(out, key=lambda s: (-s.agencies, s.rep))


def shared_remarks(visits, leader_of: dict) -> list:
    groups: dict = defaultdict(list)
    for v in visits:
        t = norm_text(v.remarks)
        if t:
            groups[leader_of[t]].append(v)
    out = []
    for leader, vs in groups.items():
        reps = sorted({v.rep for v in vs})
        if (len(reps) < 2 or len(leader) < SHARED_MIN_CHARS
                or len(leader.split()) < SHARED_MIN_WORDS):
            continue
        joint = len({(identity(v.agency), v.day) for v in vs}) == 1
        out.append(SharedRemark(
            text=Counter(_n(v.remarks) for v in vs).most_common(1)[0][0],
            reps=tuple(reps), joint=joint,
            entries=[Entry(v.rep, v.day, v.agency, _n(v.remarks))
                     for v in sorted(vs, key=_order)]))
    return sorted(out, key=lambda s: (s.joint, -len(s.reps), -len(s.entries)))


def phone_keys(raw) -> list:
    """The phone numbers in one cell, as their last ten digits.

    A cell can hold two numbers, and in some files the column holds dates;
    a date read as digits would make every visit that week share a number.
    """
    text = _n(raw)
    if not text or ISO_DATE_RE.match(text) or DATE_RE.fullmatch(text):
        return []
    keys = []
    for part in re.split(r"[,;/&]|\bor\b|\s{2,}", text):
        digits = re.sub(r"\D", "", part)
        if 7 <= len(digits) <= 15 and len(set(digits)) > 2:
            keys.append(digits[-10:])
    return keys


def _one_agency(a: str, b: str) -> bool:
    """Two identity keys for one business: alike, or one the other with words
    added ('ispahani' / 'ispahanigroup', 'rajdhanitravel' /
    'rajdhanitravelandtyping')."""
    from rapidfuzz import fuzz

    short, long_ = sorted((a, b), key=len)
    return (fuzz.ratio(a, b) >= AGENCY_SAME
            or (len(short) >= 5 and long_.startswith(short)))


def _agency_groups(names) -> list:
    """Agency names under one number, with spellings of one agency folded."""
    groups: list[list[str]] = []
    for name in sorted(names, key=lambda n: (-len(n), n)):
        key = identity(name)
        for g in groups:
            if _one_agency(key, identity(g[0])):
                g.append(name)
                break
        else:
            groups.append([name])
    return groups


def _one_contact(entries) -> bool:
    """Every row names the same contact person (and at least one names one)."""
    from rapidfuzz import fuzz

    keys = [person_key(e.text) for e in entries if person_key(e.text)]
    return bool(keys) and len(keys) == len(entries) and all(
        fuzz.ratio(keys[0], k) >= AGENCY_SAME for k in keys[1:])


def shared_phones(visits) -> list:
    by_phone: dict = defaultdict(list)
    for v in visits:
        for key in dict.fromkeys(phone_keys(v.phone)):
            by_phone[key].append(v)
    out = []
    for phone, vs in by_phone.items():
        groups = _agency_groups({_n(v.agency) for v in vs})
        if len(groups) < 2:
            continue
        entries = [Entry(v.rep, v.day, v.agency, _n(v.contact))
                   for v in sorted(vs, key=_order)]
        out.append(SharedPhone(
            phone=phone, agencies=tuple(g[0] for g in groups),
            entries=entries, same_contact=_one_contact(entries)))
    # a different person at each agency is the one to check first
    return sorted(out, key=lambda p: (p.same_contact, -len(p.agencies),
                                      p.phone))


def overlaps(visits) -> list:
    by_agency: dict = defaultdict(list)
    for v in visits:
        key = identity(v.agency)
        if key:
            by_agency[key].append(v)
    out = []
    for vs in by_agency.values():
        reps = sorted({v.rep for v in vs})
        if len(reps) < 2:
            continue
        out.append(Overlap(
            agency=max((_n(v.agency) for v in vs), key=len), reps=tuple(reps),
            visits=len(vs), days=tuple(sorted({v.day for v in vs if v.day})),
            where=tuple(dict.fromkeys(
                f"{v.rep} ({_n(v.location)})" if _n(v.location) else v.rep
                for v in sorted(vs, key=_order)))))
    return sorted(out, key=lambda o: (-len(o.reps), -o.visits, o.agency))


def find_repeats(visits) -> RepeatResult:
    """All four checks over visits whose rep names are already merged."""
    visits = list(visits)
    leader_of = _leaders(visits)
    return RepeatResult(stock=stock_remarks(visits, leader_of),
                        shared=shared_remarks(visits, leader_of),
                        phones=shared_phones(visits),
                        overlaps=overlaps(visits))


def _n(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def _order(v: Visit):
    return (v.rep, v.day or date.min, v.agency)


def stock_share(res: RepeatResult) -> dict:
    """Per rep: the share of their visits carrying one of their stock remarks."""
    rows: Counter = Counter()
    total: dict = {}
    for s in res.stock:
        rows[s.rep] += s.rows
        total[s.rep] = s.rep_rows
    return {rep: rows[rep] / total[rep] for rep in rows if total[rep]}


# --------------------------------------------------------------------------
# the section on the Visits sheet
# --------------------------------------------------------------------------
#: The overlap list is for information; past this it pushes the rest of the
#: sheet out of sight for no gain.
OVERLAPS_SHOWN = 40


def _day(d) -> str:
    return f"{d:%d %b}" if d else "no date"


def write_repeats(ws, r: int, res: RepeatResult) -> int:
    """The repeated-entries section, from row r; returns the next free row.

    On the Visits sheet, beside the Days out reconciliation, not on a sheet of
    its own: both say how far a rep's rows can be taken at their word.
    """
    from .counter_master import (BAD, GREY, LAST, PAPER, PCT, WARN, _band,
                                 _cell, _headers)

    blank = [""] * 21
    r = _band(ws, r, "REPEATED ENTRIES — TEXT WRITTEN MORE THAN ONCE",
              "a list to check against the visit, not a verdict")
    ws.merge_cells(f"A{r}:{LAST}{r}")
    _cell(ws, r, 1,
          f"  {res.stock_reps} rep(s) gave one remark to {STOCK_MIN_AGENCIES}+ "
          f"agencies · {res.copied} remark(s) written by more than one rep "
          f"outside a joint visit · {len(res.phones)} phone number(s) filed for "
          f"more than one agency, {res.phones_different} of them with a "
          f"different contact person at each. Remarks count as the same when "
          f"they are "
          f"{SIMILAR}% alike or more, so a changed word does not hide a copy. "
          "A rep may well say the same thing to every agency; what this shows "
          "is that the report does not record what was actually discussed.",
          size=9, color=GREY, fill=PAPER, align="left", wrap=True)
    ws.row_dimensions[r].height = 28
    r += 1

    # ---- one remark, many agencies -----------------------------------------
    r = _headers(ws, r, ["Sales rep", "Agencies", "Rows", "Of their visits",
                         f"One remark on {STOCK_MIN_AGENCIES}+ agencies",
                         *blank[5:]])
    if not res.stock:
        _cell(ws, r, 1, "No rep used one remark on that many agencies.",
              size=9, color=GREY)
        r += 1
    for s in res.stock:
        _cell(ws, r, 1, s.rep, bold=True, size=10, border=True)
        _cell(ws, r, 2, s.agencies, bold=True, size=10, border=True,
              align="center")
        _cell(ws, r, 3, s.rows, size=9, border=True, align="center")
        _cell(ws, r, 4, s.share, fmt=PCT, size=9, border=True, align="center",
              fill=BAD if s.share >= 0.5 else WARN if s.share >= 0.25 else None)
        ws.merge_cells(start_row=r, start_column=5, end_row=r, end_column=21)
        _cell(ws, r, 5, s.text[:300]
              + (f"   (+{s.wordings - 1} near-identical wording(s))"
                 if s.wordings > 1 else ""),
              size=9, border=True, wrap=True)
        ws.row_dimensions[r].height = 26 if len(s.text) > 110 else 15
        r += 1
    r += 1

    # ---- one remark, more than one rep -------------------------------------
    r = _band(ws, r, "ONE REMARK, MORE THAN ONE REP",
              "a joint visit explains it; a different agency does not")
    if not res.shared:
        _cell(ws, r, 1, f"No remark of {SHARED_MIN_WORDS} words or more was "
                        "written by two different reps.", size=9, color=GREY)
        r += 1
    for s in res.shared:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=21)
        _cell(ws, r, 1,
              f"  {'JOINT VISIT — ' if s.joint else ''}{len(s.reps)} reps, "
              f"{len(s.entries)} row(s):  “{s.text[:250]}”",
              bold=True, size=9, fill=PAPER if s.joint else WARN, wrap=True)
        ws.row_dimensions[r].height = 26 if len(s.text) > 110 else 15
        r = _entries(ws, r + 1, s.entries, "Remark as written")
    r += 1

    # ---- one phone, more than one agency -----------------------------------
    r = _band(ws, r, "ONE PHONE NUMBER, MORE THAN ONE AGENCY",
              "a different person at each agency first; one person at several "
              "may simply own several firms")
    r = _headers(ws, r, ["Number (last 10)", "Agencies", "Contact person",
                         "Dates", "Agency — contact (rep)", *blank[5:]])
    if not res.phones:
        _cell(ws, r, 1, "No phone number was filed for two different agencies.",
              size=9, color=GREY)
        r += 1
    for p in res.phones:
        _cell(ws, r, 1, p.phone, bold=True, size=9, border=True)
        _cell(ws, r, 2, len(p.agencies), size=9, border=True, align="center")
        _cell(ws, r, 3, "same person" if p.same_contact else "different",
              size=9, border=True, align="center",
              fill=None if p.same_contact else WARN)
        _cell(ws, r, 4, ", ".join(dict.fromkeys(_day(e.day)
                                                for e in p.entries))[:40],
              size=8, color=GREY, border=True)
        ws.merge_cells(start_row=r, start_column=5, end_row=r, end_column=21)
        text = "; ".join(dict.fromkeys(
            f"{e.agency} — {e.text or '?'} ({e.rep})" for e in p.entries))
        _cell(ws, r, 5, text[:400], size=9, border=True, wrap=True)
        ws.row_dimensions[r].height = 26 if len(text) > 120 else 15
        r += 1
    r += 1

    # ---- one agency, more than one rep -------------------------------------
    r = _band(ws, r, "ONE AGENCY, MORE THAN ONE REP",
              "for information: a territory question, not a copying one")
    r = _headers(ws, r, ["Agency", "Reps", "Visits", "Dates",
                         "Which reps (location written)", *blank[5:]])
    if not res.overlaps:
        _cell(ws, r, 1, "Every agency was visited by one rep only.",
              size=9, color=GREY)
        r += 1
    for o in res.overlaps[:OVERLAPS_SHOWN]:
        _cell(ws, r, 1, o.agency, bold=True, size=9, border=True)
        _cell(ws, r, 2, len(o.reps), size=9, border=True, align="center")
        _cell(ws, r, 3, o.visits, size=9, border=True, align="center")
        _cell(ws, r, 4, ", ".join(_day(d) for d in o.days)[:60] or "no date",
              size=8, color=GREY, border=True)
        ws.merge_cells(start_row=r, start_column=5, end_row=r, end_column=21)
        _cell(ws, r, 5, "; ".join(o.where or o.reps)[:300], size=9,
              border=True)
        r += 1
    if len(res.overlaps) > OVERLAPS_SHOWN:
        _cell(ws, r, 1, f"… and {len(res.overlaps) - OVERLAPS_SHOWN} more "
                        "agencies, fewest reps last", size=8, color=GREY)
        r += 1
    return r + 1


def _entries(ws, r: int, entries, what: str) -> int:
    from .counter_master import GREY, _cell, _headers

    r = _headers(ws, r, ["Sales rep", "Date", "Agency", what, *[""] * 17])
    for e in entries[:EXAMPLES]:
        _cell(ws, r, 1, e.rep, size=9, border=True)
        _cell(ws, r, 2, _day(e.day), size=9, border=True, align="center")
        _cell(ws, r, 3, e.agency, size=9, border=True)
        ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=21)
        _cell(ws, r, 4, e.text[:300], size=9, color=GREY, border=True)
        r += 1
    if len(entries) > EXAMPLES:
        _cell(ws, r, 1, f"… and {len(entries) - EXAMPLES} more row(s)",
              size=8, color=GREY)
        r += 1
    return r

