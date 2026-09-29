"""Every FFP member, out of a search that lists 50 at a time.

Zenith's FFP search shows at most 50 rows but always says how many matched
(see zenith_ffp). So the whole membership is reached by narrowing: search
FFP numbers beginning 0, 1, ... 9; any beginning that matches more than 50
is split into ten longer ones (10, 11, ... 19), and so on, until every
search lists all it matched. Every member is found this way whether the
field matches from the start of the number or anywhere in it: a member's
own number contains each of its own beginnings, so the narrowing follows
that path down to a search small enough to list it.

Two checks come first, because the whole method rests on them:

* each level is searched on its own, so the counts Zenith holds per level
  are known, and the finished list can be checked against them;
* one real FFP number, less its last digit, is searched. If the member does
  not come back, the field matches whole numbers only, narrowing cannot
  work, and the run stops saying so rather than returning a partial list.

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


def _check_levels_and_partial(searcher, store: FFPStore, log) -> None:
    """Count each level, then prove a partial FFP number finds its member."""
    sample = None
    for level in zf.LEVELS:
        page = searcher.search(level=level)
        store.put(f"level_total:{level}", page.total)
        store.save_members(page.members)
        log(f"{level}: {page.total:,} member(s) in Zenith.")
        if sample is None and page.members:
            sample = page.members[0]
    if sample is None:
        raise PartialMatchUnsupported(
            "No level returned any member, so there is nothing to collect "
            "-- or the level names have changed.")
    probe_prefix = sample.ffp_number[:-1]
    probe = searcher.search(ffp_number=probe_prefix)
    if not any(m.ffp_number == sample.ffp_number for m in probe.members) \
            and probe.total <= 1:
        raise PartialMatchUnsupported(
            "Zenith's FFP number search only matches whole numbers, so the "
            "list cannot be narrowed to fit its 50-row limit. The level "
            "counts above are exact; the full member list is not "
            "collectable this way.")
    store.put("partial_ok", "1")
    log("Partial FFP numbers work -- collecting by number.")


def collect(searcher, store: FFPStore, *, stop: threading.Event,
            log=lambda _m: None, on_progress=lambda _p: None,
            delay_s: float = 1.0) -> Progress:
    """Collect every member into `store`, carrying on from any earlier run."""
    if store.get("partial_ok") != "1":
        _check_levels_and_partial(searcher, store, log)
    store.seed(DIGITS)
    while not stop.is_set():
        prefix = store.next_pending()
        if prefix is None:
            break
        page = searcher.search(ffp_number=prefix)
        if page.total == 0:
            store.mark(prefix, DONE, 0)
        elif page.complete:
            store.save_members(page.members)
            store.mark(prefix, DONE, page.total)
        elif len(prefix) >= MAX_DIGITS:
            store.save_members(page.members)
            store.mark(prefix, CAPPED, page.total)
            log(f"FFP numbers beginning {prefix} still match "
                f"{page.total:,}; the first 50 are kept.")
        else:
            store.seed(prefix + d for d in DIGITS)
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
