"""Every FFP member, out of a search that lists 50 at a time.

Zenith's FFP search shows at most 50 rows but always says how many matched
(see zenith_ffp). So the whole membership is reached by narrowing: search
FFP numbers beginning 0, 1, ... 9; any beginning that matches more than 50
is split into ten longer ones (10, 11, ... 19), and so on, until every
search lists all it matched. Every member is found this way whether the
field matches from the start of the number or anywhere in it: a member's
own number contains each of its own beginnings, so the narrowing follows
that path down to a search small enough to list it.

Checks come first, because the whole method rests on them:

* each level is searched on its own, so the counts Zenith holds per level
  are known, and the finished list can be checked against them;
* narrowing needs a field that matches PART of a value. On the live system
  the FFP number field matches whole numbers only -- a real number less its
  last digit found nothing. So each way is tried on a real member, and the
  first that brings that member back is used: part of the FFP number as
  typed, then with a '%' or '*' wildcard; then part of the last name within
  one level, the same three ways, noting whether case matters. If none
  works, the run stops saying so rather than returning a partial list.

Narrowing by last name works like narrowing by number, a level at a time:
names beginning A-Z, then each beginning that matches more than 50 grows
by one more character.

Progress is kept in a small local database as it goes -- each search that
finished, each member found -- so Stop, a crash or a signed-out session
loses nothing, and the next run carries on from where this one stopped.
It holds customers' personal details: it stays on this machine.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import zenith_ffp as zf

DIGITS = "0123456789"
#: Deeper than any customer number; a beginning this long that still
#: matches more than 50 is recorded as a gap rather than split forever.
MAX_DIGITS = 12

PENDING, DONE, SPLIT, CAPPED = "pending", "done", "split", "capped"

_MEMBER_COLS = ("ffp_number", "customer_id", "last_name", "first_name",
                "birth_date", "email", "phone", "id_number", "level",
                "miles")


class PartialMatchUnsupported(Exception):
    """The FFP number field only matches whole numbers."""


class FFPStore:
    """The run's progress and every member found, on this machine."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.con = sqlite3.connect(str(self.path), check_same_thread=False)
        cols = ", ".join(f"{c} TEXT NOT NULL DEFAULT ''"
                         for c in _MEMBER_COLS[1:])
        self.con.executescript(f"""
            CREATE TABLE IF NOT EXISTS members (
                ffp_number TEXT PRIMARY KEY, {cols},
                found_at TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS frontier (
                prefix TEXT PRIMARY KEY, state TEXT NOT NULL,
                total INTEGER NOT NULL DEFAULT -1);
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    # ---- meta ------------------------------------------------------------
    def get(self, key: str, default: str = "") -> str:
        row = self.con.execute("SELECT value FROM meta WHERE key=?",
                               (key,)).fetchone()
        return row[0] if row else default

    def put(self, key: str, value) -> None:
        with self._lock:
            self.con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                             (key, str(value)))
            self.con.commit()

    def level_totals(self) -> dict:
        return {lv: int(self.get(f"level_total:{lv}", "-1"))
                for lv in zf.LEVELS}

    # ---- members ---------------------------------------------------------
    def save_members(self, members) -> int:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        rows = [tuple(asdict(m)[c] for c in _MEMBER_COLS) + (now,)
                for m in members if m.ffp_number]
        with self._lock:
            before = self.member_count()
            self.con.executemany(
                f"INSERT OR REPLACE INTO members "
                f"({', '.join(_MEMBER_COLS)}, found_at) "
                f"VALUES ({', '.join('?' * (len(_MEMBER_COLS) + 1))})", rows)
            self.con.commit()
            return self.member_count() - before

    def member_count(self) -> int:
        return self.con.execute("SELECT count(*) FROM members").fetchone()[0]

    def counts_by_level(self) -> dict:
        return dict(self.con.execute(
            "SELECT level, count(*) FROM members GROUP BY level"))

    def members(self):
        cur = self.con.execute(
            f"SELECT {', '.join(_MEMBER_COLS)} FROM members "
            f"ORDER BY level, ffp_number")
        for row in cur:
            yield dict(zip(_MEMBER_COLS, row))

    # ---- frontier --------------------------------------------------------
    def seed(self, prefixes) -> None:
        with self._lock:
            self.con.executemany(
                "INSERT OR IGNORE INTO frontier (prefix, state) "
                "VALUES (?, ?)", [(p, PENDING) for p in prefixes])
            self.con.commit()

    def mark(self, prefix: str, state: str, total: int) -> None:
        with self._lock:
            self.con.execute(
                "UPDATE frontier SET state=?, total=? WHERE prefix=?",
                (state, total, prefix))
            self.con.commit()

    def next_pending(self):
        row = self.con.execute(
            "SELECT prefix FROM frontier WHERE state=? "
            "ORDER BY length(prefix), prefix LIMIT 1", (PENDING,)).fetchone()
        return row[0] if row else None

    def frontier_counts(self) -> dict:
        return dict(self.con.execute(
            "SELECT state, count(*) FROM frontier GROUP BY state"))

    def capped(self) -> list:
        return [r[0] for r in self.con.execute(
            "SELECT prefix FROM frontier WHERE state=?", (CAPPED,))]

    def reset(self) -> None:
        with self._lock:
            self.con.executescript(
                "DELETE FROM members; DELETE FROM frontier; DELETE FROM meta;")
            self.con.commit()


@dataclass
class Progress:
    members: int = 0
    searches: int = 0
    pending: int = 0
    done: int = 0
    last: str = ""
    level_totals: dict = field(default_factory=dict)
    level_found: dict = field(default_factory=dict)

    @property
    def expected(self) -> int:
        return sum(v for v in self.level_totals.values() if v > 0)


def progress_of(store: FFPStore, searches: int = 0, last: str = "") -> Progress:
    fc = store.frontier_counts()
    return Progress(members=store.member_count(), searches=searches,
                    pending=fc.get(PENDING, 0),
                    done=fc.get(DONE, 0) + fc.get(SPLIT, 0) + fc.get(CAPPED, 0),
                    last=last, level_totals=store.level_totals(),
                    level_found=store.counts_by_level())


#: Ways a search field might accept "the start of" a value: as typed, or
#: with a SQL or shell wildcard after it. Tried in this order.
WILDCARDS = ("", "%", "*")

#: How a last name may continue after its first letter.
_NAME_TAIL = " -.'"
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWER = _UPPER.lower()
MAX_NAME = 30

NUMBER, NAME = "number", "name"


def _found(page, member) -> bool:
    """The probe brought the sample member back."""
    return any(m.ffp_number == member.ffp_number for m in page.members)


def _check_levels(searcher, store: FFPStore, log) -> dict:
    """Count each level in Zenith; keep the members each search listed."""
    samples = {}
    for level in zf.LEVELS:
        page = searcher.search(level=level)
        store.put(f"level_total:{level}", page.total)
        store.save_members(page.members)
        log(f"{level}: {page.total:,} member(s) in Zenith.")
        if page.members:
            samples[level] = page.members
    if not samples:
        raise PartialMatchUnsupported(
            "No level returned any member, so there is nothing to collect "
            "-- or the level names have changed.")
    return samples


def _try_number(searcher, sample, log):
    """Does the start of an FFP number find its member, with any wildcard?"""
    for wild in WILDCARDS:
        probe = searcher.search(ffp_number=sample.ffp_number[:-1] + wild)
        if _found(probe, sample):
            return wild
    log("Part of an FFP number finds nothing, with or without a wildcard.")
    return None


def _try_name(searcher, samples: dict, log):
    """Does the start of a last name, within a level, find its member?

    Returns (wildcard, case-sensitive) or None. The probe drops the last
    letter of a real member's last name, so a field that matches whole
    names only comes back without that member.
    """
    for level, members in samples.items():
        sample = next((m for m in members if len(m.last_name) >= 3), None)
        if sample is None:
            continue
        part = sample.last_name[:-1]
        for wild in WILDCARDS:
            probe = searcher.search(level=level, last_name=part + wild)
            if not _found(probe, sample):
                continue
            swapped = part.swapcase()
            if swapped == part:
                return wild, False
            other = searcher.search(level=level, last_name=swapped + wild)
            return wild, not _found(other, sample)
        break
    log("Part of a last name finds nothing, with or without a wildcard.")
    return None


def _choose_method(searcher, store: FFPStore, log) -> None:
    samples = _check_levels(searcher, store, log)
    first = next(iter(samples.values()))[0]
    wild = _try_number(searcher, first, log)
    if wild is not None:
        store.put("mode", NUMBER)
        store.put("wild", wild)
        store.seed(DIGITS)
        log("Narrowing by FFP number" + (f" (with '{wild}')" if wild else "")
            + ".")
        return
    got = _try_name(searcher, samples, log)
    if got is None:
        raise PartialMatchUnsupported(
            "Zenith's FFP search matches only whole FFP numbers and whole "
            "last names -- with or without a wildcard -- so the list cannot "
            "be narrowed to fit its 50-row limit. The level counts are "
            "exact, and the members found so far can be exported; the full "
            "list is not collectable through this search.")
    wild, case_sensitive = got
    store.put("mode", NAME)
    store.put("wild", wild)
    store.put("case", "1" if case_sensitive else "0")
    # Where case matters, a name typed in lower case ("afrin") begins with a
    # lower-case letter and is only reached from one.
    first = _UPPER + (_LOWER if case_sensitive else "")
    totals = store.level_totals()
    for level in zf.LEVELS:
        if totals.get(level, 0) > zf.PAGE_CAP:
            store.seed(f"{level}|{c}" for c in first)
    log("Narrowing by last name within each level"
        + (f" (with '{wild}')" if wild else "")
        + (", case-sensitive" if case_sensitive else "") + ".")


def _children(store: FFPStore, prefix: str):
    if store.get("mode") == NUMBER:
        return [prefix + d for d in DIGITS]
    tail = _LOWER + _NAME_TAIL
    if store.get("case") == "1":
        tail += _UPPER              # "Al Amin": capitals after a space
    return [prefix + c for c in tail]


def _search(searcher, store: FFPStore, prefix: str):
    wild = store.get("wild")
    if store.get("mode") == NUMBER:
        return searcher.search(ffp_number=prefix + wild)
    level, part = prefix.split("|", 1)
    return searcher.search(level=level, last_name=part + wild)


def _too_deep(store: FFPStore, prefix: str) -> bool:
    if store.get("mode") == NUMBER:
        return len(prefix) >= MAX_DIGITS
    return len(prefix.split("|", 1)[1]) >= MAX_NAME


def collect(searcher, store: FFPStore, *, stop: threading.Event,
            log=lambda _m: None, on_progress=lambda _p: None,
            delay_s: float = 1.0) -> Progress:
    """Collect every member into `store`, carrying on from any earlier run."""
    if store.get("mode") not in (NUMBER, NAME):
        _choose_method(searcher, store, log)
    while not stop.is_set():
        prefix = store.next_pending()
        if prefix is None:
            break
        page = _search(searcher, store, prefix)
        if page.total == 0:
            store.mark(prefix, DONE, 0)
        elif page.complete:
            store.save_members(page.members)
            store.mark(prefix, DONE, page.total)
        elif _too_deep(store, prefix):
            store.save_members(page.members)
            store.mark(prefix, CAPPED, page.total)
            log(f"'{prefix}' still matches {page.total:,}; the first 50 "
                f"are kept.")
        else:
            store.save_members(page.members)
            store.seed(_children(store, prefix))
            store.mark(prefix, SPLIT, page.total)
        on_progress(progress_of(store, searcher.searches,
                                f"{prefix}: {page.total:,}"))
        if delay_s > 0:
            stop.wait(delay_s)
    store.put("finished", "1" if store.next_pending() is None else "0")
    return progress_of(store, searcher.searches)


# ---- the workbook ---------------------------------------------------------

HEADERS = ("FFP number", "Customer ID", "Last name", "First name",
           "Date of birth", "Email", "Phone", "ID number", "Level",
           "Total miles")


def export_workbook(store: FFPStore, out_path) -> int:
    """Write every member found, and a summary against Zenith's own counts.

    Written row by row (write-only mode): the membership runs past 100,000.
    """
    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    summary = wb.create_sheet("Summary")
    found = store.counts_by_level()
    totals = store.level_totals()
    summary.append(["FFP MEMBERS — CONFIDENTIAL: holds customers' personal "
                    "details. Do not forward outside the company."])
    summary.append([])
    summary.append(["Level", "In Zenith", "Collected", "Missing"])
    for level in zf.LEVELS:
        z, f = totals.get(level, -1), found.get(level, 0)
        summary.append([level, z if z >= 0 else None, f,
                        (z - f) if z >= 0 else None])
    others = {k: v for k, v in found.items() if k not in zf.LEVELS}
    for level, n in sorted(others.items()):
        summary.append([level or "(no level)", None, n, None])
    summary.append([])
    finished = store.get("finished") == "1"
    summary.append(["Collection", "complete" if finished else
                    "NOT FINISHED — press Collect to carry on"])
    gaps = store.capped()
    if gaps:
        summary.append(["Could not narrow", ", ".join(gaps)])

    sheet = wb.create_sheet("Members")
    sheet.append(list(HEADERS))
    n = 0
    for m in store.members():
        sheet.append([m[c] for c in _MEMBER_COLS])
        n += 1
    wb.save(str(out_path))
    return n


def default_filename() -> str:
    return f"FFP_Members_{time.strftime('%Y-%m-%d')}.xlsx"
