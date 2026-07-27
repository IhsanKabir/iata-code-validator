"""NATTA (Nepal Association of Tour & Travel Agents) member-directory extractor.

natta.org.np/members/ lists every member as
    <pre class="member-detail-link"><a href=".../member/slug/">Name</a>  [Member ID: NATTA-570/13]</pre>
and each member page carries a <dl><dt>Label:</dt><dd>Value</dd> block (Member ID,
Company Name, Telephone, Office Address, Email Address, Website) plus an owner
widget (photo + "Mr. Name <span>( Designation )</span>"). Both parsers are pure
functions (offline-tested); `fetch_all_members` does the network run — polite
concurrency, per-page retries, stop-event aware, fail-safe per member (one bad
page never sinks the run). Validated live against the full 644-member directory.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html import unescape
from typing import Callable

import requests

log = logging.getLogger(__name__)

MEMBERS_INDEX_URL = "https://natta.org.np/members/"
_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")}

# Core dt-labels mapped to fixed dataclass fields; anything else lands in extras.
_CORE_LABELS = {
    "natta member id": "member_id_page",
    "company name": "company_name",
    "telephone": "telephone",
    "office address": "office_address",
    "email address": "email",
    "website": "website",
}

_TAG_RE = re.compile(r"<[^>]+>")
_INDEX_ROW_RE = re.compile(
    r'<pre class="member-detail-link">\s*<a href="([^"]+)">(.*?)</a>\s*'
    r'(?:\[Member ID:\s*([^\]]*)\])?', re.IGNORECASE | re.DOTALL)
_DL_RE = re.compile(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", re.DOTALL | re.IGNORECASE)
_OWNER_RE = re.compile(
    r'member-image-widgets">.*?(?:<img[^>]*src="([^"]*)"[^>]*>)?\s*<h4>(.*?)</h4>',
    re.DOTALL | re.IGNORECASE)
_SPAN_RE = re.compile(r"<span>(.*?)</span>", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class NattaMember:
    member_id: str = ""            # from the index ("NATTA-570/13")
    name: str = ""                 # from the index anchor text
    url: str = ""
    owner_name: str = ""
    designation: str = ""
    telephone: str = ""
    office_address: str = ""
    email: str = ""
    website: str = ""
    company_name: str = ""         # as the detail page spells it
    member_id_page: str = ""       # as the detail page spells it
    photo_url: str = ""
    status: str = ""               # OK / NOT_FOUND / HTTP_x / ERROR: …
    extras: dict[str, str] = field(default_factory=dict)  # unrecognised dt labels


def _txt(fragment: str) -> str:
    return " ".join(unescape(_TAG_RE.sub(" ", fragment)).replace("\xa0", " ").split())


def parse_member_index(html: str) -> list[tuple[str, str, str]]:
    """(member_id, name, url) for every member on the A-Z index page."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for url, name, mid in _INDEX_ROW_RE.findall(html):
        url = url.strip()
        if url in seen:
            continue
        seen.add(url)
        out.append((_txt(mid), _txt(name), url))
    return out


def parse_member_html(html: str) -> dict[str, str | dict]:
    """Every field a member-detail page carries, keyed by dataclass field name.
    Unrecognised dt labels are kept under 'extras' so nothing is dropped."""
    fields: dict[str, str | dict] = {}
    extras: dict[str, str] = {}
    for dt, dd in _DL_RE.findall(html):
        label = _txt(dt).rstrip(":").strip()
        if not label:
            continue
        key = _CORE_LABELS.get(label.lower())
        if key:
            fields[key] = _txt(dd)
        else:
            extras[label] = _txt(dd)
    m = _OWNER_RE.search(html)
    if m:
        photo, h4 = m.group(1) or "", m.group(2)
        sp = _SPAN_RE.search(h4)
        if sp:
            fields["designation"] = _txt(sp.group(1)).strip("() ")
        owner = _txt(_SPAN_RE.sub(" ", h4))
        if owner:
            fields["owner_name"] = owner
        if photo and "no-img" not in photo:
            fields["photo_url"] = photo
    if extras:
        fields["extras"] = extras
    return fields


def fetch_index(session: requests.Session | None = None, *,
                timeout_s: float = 60.0) -> list[tuple[str, str, str]]:
    """Fetch + parse the members index. Raises on failure (nothing to work with)."""
    sess = session or requests
    r = sess.get(MEMBERS_INDEX_URL, headers=_UA, timeout=timeout_s)
    if r.status_code != 200:
        raise RuntimeError(f"NATTA members index returned HTTP {r.status_code}.")
    members = parse_member_index(r.text)
    if not members:
        raise RuntimeError("NATTA members index page had no member links — "
                           "the site layout may have changed.")
    return members


def _fetch_one(sess, member_id: str, name: str, url: str, timeout_s: float) -> NattaMember:
    last = "ERROR"
    for attempt in range(3):
        try:
            r = sess.get(url, headers=_UA, timeout=timeout_s)
        except requests.RequestException as exc:
            last = f"ERROR: {type(exc).__name__}"
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 404:
            return NattaMember(member_id=member_id, name=name, url=url,
                               status="NOT_FOUND")
        if r.status_code == 200 and "<dt>" in r.text:
            f = parse_member_html(r.text)
            extras = f.pop("extras", {})
            return NattaMember(member_id=member_id, name=name, url=url,
                               status="OK", extras=extras, **f)  # type: ignore[arg-type]
        last = "NO_DETAIL_BLOCK" if r.status_code == 200 else f"HTTP_{r.status_code}"
        if r.status_code == 200:
            break                      # a rendered page without the block won't heal
        time.sleep(1.5 * (attempt + 1))
    return NattaMember(member_id=member_id, name=name, url=url, status=last)


def fetch_all_members(
    *,
    progress_cb: Callable[[int, int, str], None] | None = None,
    stop_event: threading.Event | None = None,
    concurrency: int = 4,
    delay_s: float = 0.3,
    timeout_s: float = 40.0,
    session: requests.Session | None = None,
) -> list[NattaMember]:
    """Fetch the index then every member page. Returns members in index order;
    stopping early returns what finished so a partial run still exports."""
    sess = session or requests.Session()
    index = fetch_index(sess, timeout_s=timeout_s)
    total = len(index)
    if progress_cb:
        progress_cb(0, total, f"Index: {total} members — fetching details…")

    done: dict[str, NattaMember] = {}
    lock = threading.Lock()
    n = 0

    def work(item: tuple[str, str, str]) -> None:
        nonlocal n
        mid, name, url = item
        if stop_event is not None and stop_event.is_set():
            return
        member = _fetch_one(sess, mid, name, url, timeout_s)
        if delay_s > 0:
            time.sleep(delay_s)
        with lock:
            done[url] = member
            n += 1
            if progress_cb and (n % 10 == 0 or n == total):
                ok = sum(1 for m in done.values() if m.status == "OK")
                progress_cb(n, total, f"{n:,}/{total:,} fetched · {ok:,} OK")

    with ThreadPoolExecutor(max_workers=max(1, min(int(concurrency), 8))) as pool:
        futures = [pool.submit(work, it) for it in index]
        for fut in as_completed(futures):
            fut.result()               # surface unexpected worker crashes
            if stop_event is not None and stop_event.is_set():
                break

    return [done[url] for _, _, url in index if url in done]
