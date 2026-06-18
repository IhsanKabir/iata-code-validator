#!/usr/bin/env python
"""Step 0 — Zenith PNR event-history endpoint discovery probe (READ-ONLY, one-off).

Run this ONCE against a few known PNRs (ideally ones with real reissue/refund/void
activity) to reverse-engineer the dossier event-history tabs before any scraper is
written. It:

  1. logs in (reusing the app's ZenithSession),
  2. resolves each PNR -> internal dossier_id via lookup_pnr,
  3. fetches the Dossier page HTML and SCRAPES the event-history button targets
     (search_event.asp links / window.open / onclick) — the reliable way to learn
     each tab's exact URL+params (the tabs open in new windows),
  4. ALSO enumerates contexte=recap_dossier & CategorieEvent=1..6 as a fallback,
  5. for each tab: prints HTTP status, size, the table header + first rows (via the
     app's _TableReader), and any pagination markers ("Number of results", page links),
  6. saves every raw HTML response under tests/fixtures/pnr_history/ as test fixtures.

It is deliberately gentle: serial, 1.5s between requests, aborts on 401/403. NOTHING
is written to Zenith. This is a throwaway dev tool, not shipped app code.

USAGE (from the repo root, with the app venv):
    set ZENITH_USER=...        (PowerShell: $env:ZENITH_USER="...")
    set ZENITH_PASS=...
    set ZENITH_COMPANY=usba    (optional, default 'usba')
    .venv\\Scripts\\python.exe tools\\probe_pnr_history.py 09AHEA [PNR2 ...]
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from src.zenith_client import BASE_URL, ZenithSession, _backoff_with_jitter  # noqa: E402
from src.zenith_pnr_client import QUICK_SEARCH_URL, lookup_pnr  # noqa: E402
from src.zenith_history_downloader import HISTORY_VIEW_URL  # noqa: E402

try:
    from src.zenith_history_parser import _TableReader  # noqa: E402
except Exception:  # pragma: no cover - the parser import is best-effort for previews
    _TableReader = None

FIXTURES = _REPO / "tests" / "fixtures" / "pnr_history"
# Ticket history is a DIFFERENT endpoint from the changes/events log (confirmed from
# the real browser URLs): recettesco/HistoBillet.asp?haction=SEARCH&id_Dossier=<id>.
HISTOBILLET_URL = f"{BASE_URL}/newui/aerien/recettesco/HistoBillet.asp"
DELAY_S = 1.5
# Patterns that reveal the event-history endpoints inside the Dossier page HTML/JS.
_EVENT_URL_RE = re.compile(r"""search_event\.asp[^"'\s<>)]*""", re.IGNORECASE)
_PARAM_RE = re.compile(r"""(contexte|CategorieEvent|id_dossier|id_Dossier)\s*=\s*['"]?([\w-]+)""",
                       re.IGNORECASE)
_RESULTS_RE = re.compile(r"(Number of results|Nombre de r\w+sultats)\s*[:=]?\s*(\d+)", re.IGNORECASE)


def _save(name: str, text: str) -> Path:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]", "_", name)
    p = FIXTURES / f"{safe}.html"
    p.write_text(text, encoding="utf-8", errors="replace")
    return p


def _preview_table(html: str) -> tuple[list[str], list[list[str]]]:
    """(header_row, first_2_data_rows) via the app's _TableReader, best-effort."""
    if _TableReader is None:
        return [], []
    try:
        r = _TableReader()
        r.feed(html)
        rows = [row for row in r.rows if any(c.strip() for c in row)]
        return (rows[0] if rows else []), rows[1:3]
    except Exception as exc:  # noqa: BLE001
        return [f"<table preview failed: {exc}>"], []


def _resilient_get(sess, url: str, params: dict | None, *, tries: int = 5, timeout: int = 90):
    """GET that rides through Zenith's intermittent 504/5xx overload (the storm).

    Returns the Response, or None if every attempt was a 5xx / network failure.
    Raises SystemExit on a real session loss (401/403/login redirect).
    """
    last = "?"
    for attempt in range(1, tries + 1):
        try:
            resp = sess.session.get(url, params=params, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last = f"network {exc}"
            time.sleep(_backoff_with_jitter(attempt, base_s=2.0, cap_s=12.0))
            continue
        if resp.status_code in (401, 403) or "/otds/" in resp.url:
            raise SystemExit(f"session expired (HTTP {resp.status_code}) — re-login and retry.")
        if resp.status_code >= 500 and attempt < tries:
            last = f"HTTP {resp.status_code}"
            time.sleep(_backoff_with_jitter(attempt, base_s=2.0, cap_s=12.0))
            continue
        return resp
    print(f"        (gave up after {tries} tries — last: {last})")
    return None


def _get(sess, url: str, params: dict | None, label: str) -> None:
    """One polite, 504-resilient GET; print a summary and save the raw HTML."""
    resp = _resilient_get(sess, url, params)
    if resp is None:
        print(f"    {label}: FAILED (Zenith 5xx/overload after retries)")
        return
    body = resp.text
    header, sample = _preview_table(body)
    results = _RESULTS_RE.search(body)
    saved = _save(label, body)
    print(f"    {label}: HTTP {resp.status_code}  {len(body):>7,} bytes"
          + (f"  · {results.group(0)}" if results else "")
          + (f"  · pages? {'yes' if re.search(r'page=', resp.url + body[:4000], re.I) else 'n/a'}"))
    if header:
        print(f"        header: {header}")
    for row in sample:
        print(f"        row:    {row}")
    print(f"        saved:  {saved.relative_to(_REPO)}")
    time.sleep(DELAY_S)


def probe_pnr(sess, pnr: str) -> None:
    print(f"\n=== PNR {pnr} ===")
    details = lookup_pnr(sess, pnr)
    dossier_id = details.dossier_id
    print(f"  dossier_id = {dossier_id!r}  · status={details.pnr_status} · segs={len(details.segments)}")
    if not dossier_id:
        print("  !! no dossier_id parsed — cannot probe history. Check lookup_pnr output.")
        return

    # 1) Fetch the Dossier page HTML (504-resilient) and scrape the event-history targets.
    raw = _resilient_get(sess, QUICK_SEARCH_URL,
                         {"vaction": "VERIF", "Id": pnr.strip().upper(),
                          "id_langue": "2", "GDSCRSPartnerRCIRLoc": ""})
    dossier_html = raw.text if raw is not None else ""
    if dossier_html:
        _save(f"{pnr}_dossier_page", dossier_html)
    found = sorted(set(_EVENT_URL_RE.findall(dossier_html)))
    print(f"  search_event.asp references in Dossier page: {len(found)}")
    for u in found:
        print(f"    -> {u[:160]}")
    # The button may build its URL in JS — surface any event/history/recap references too.
    js_refs = sorted(set(re.findall(
        r"[^\n;{]{0,120}(?:search_event|CategorieEvent|recap_dossier|histor|event)[^\n;{]{0,80}",
        dossier_html, re.IGNORECASE)))[:12]
    for j in js_refs:
        snippet = " ".join(j.split())
        if snippet:
            print(f"    js> {snippet[:150]}")
    params_seen = sorted(set(f"{k}={v}" for k, v in _PARAM_RE.findall(dossier_html)))
    if params_seen:
        print(f"  params seen near event refs: {', '.join(params_seen)}")
    time.sleep(DELAY_S)

    # 2) CHANGES history — the events log carrying PAX CONTACT / BKASH payment comments.
    #    REAL param is id_dossier_vol (NOT id_dossier) — confirmed from the browser URL;
    #    that wrong name is what triggered the earlier 500. Sweep CategorieEvent to find
    #    every category (3 is the known-good "File Modification" one).
    print("  -- CHANGES history: search_event.asp recap_dossier CategorieEvent=1..8 --")
    for n in range(1, 9):
        _get(sess, HISTORY_VIEW_URL,
             {"contexte": "recap_dossier", "CategorieEvent": str(n), "id_dossier_vol": dossier_id},
             label=f"{pnr}_changes_cat{n}")
    print("  -- CHANGES history excel=1 on CategorieEvent=3 --")
    _get(sess, HISTORY_VIEW_URL,
         {"contexte": "recap_dossier", "CategorieEvent": "3", "id_dossier_vol": dossier_id, "excel": "1"},
         label=f"{pnr}_changes_cat3_excel")

    # 3) TICKET history — a DIFFERENT endpoint: recettesco/HistoBillet.asp, id_Dossier.
    print("  -- TICKET history: HistoBillet.asp haction=SEARCH --")
    _get(sess, HISTOBILLET_URL, {"haction": "SEARCH", "id_Dossier": dossier_id},
         label=f"{pnr}_tickets")
    _get(sess, HISTOBILLET_URL, {"haction": "SEARCH", "id_Dossier": dossier_id, "excel": "1"},
         label=f"{pnr}_tickets_excel")


def main(argv: list[str]) -> int:
    pnrs = [a.strip().upper() for a in argv if a.strip()]
    if not pnrs:
        print(__doc__)
        return 2
    user = os.environ.get("ZENITH_USER")
    pwd = os.environ.get("ZENITH_PASS")
    company = os.environ.get("ZENITH_COMPANY", "usba")
    if not user or not pwd:
        print("Set ZENITH_USER and ZENITH_PASS environment variables first.")
        return 2
    print(f"Logging in as {user} (company={company}) ...")
    sess = ZenithSession.from_credentials(user, pwd, company_code=company)
    print("Logged in. Probing", len(pnrs), "PNR(s). Fixtures ->", FIXTURES.relative_to(_REPO))
    for pnr in pnrs:
        try:
            probe_pnr(sess, pnr)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 — keep probing the rest
            print(f"  !! {pnr} failed: {type(exc).__name__}: {exc}")
    print("\nDone. Review the saved fixtures + the headers/params above, then we map "
          "PNR_EVENT_CATEGORIES and build the parser against real HTML.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
