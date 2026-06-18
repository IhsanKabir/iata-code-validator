"""Per-PNR dossier CHANGES-history scraper (Phase 2).

For each PNR: resolve its dossier_id (`lookup_pnr`), then fetch the CHANGES history via
the Step-0-verified endpoint

    search_event.asp?contexte=recap_dossier&CategorieEvent=3&id_dossier_vol=<id>&excel=1

(the `excel=1` view is a clean table; `id_dossier_vol` is the right param — NOT `id_dossier`).
Raw HTML is stored in `ZenithPNRHistoryCache` (parse-on-read, so parser fixes never re-scrape),
parsed by `zenith_pnr_history_parser`, and the events are returned for the payment/contact audit.

Guardrails: a hard request budget (resume-safe stop on hit), polite delay, 504-resilient
retries (Zenith's CloudFront 504-storms), a cooperative stop flag, and progress callbacks.
Only the CHANGES tab is fetched — it already carries payment / contact / reissue (I->E); the
HistoBillet ticket tab (a different layout) is left to a later pass.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from . import config
from .zenith_client import BASE_URL, USER_AGENT, ZenithSession, _backoff_with_jitter
from .zenith_pnr_client import lookup_pnr
from .zenith_pnr_history_cache import ZenithPNRHistoryCache
from .zenith_pnr_history_parser import CHANGES_TAB, DossierEvent, parse_dossier_changes

log = logging.getLogger(__name__)

CHANGES_URL = f"{BASE_URL}/newui/aerien/commun/search_event.asp"


@dataclass(frozen=True)
class ScrapeStats:
    requested: int = 0          # PNRs attempted
    resolved: int = 0           # PNRs that resolved to a dossier_id
    from_cache: int = 0         # served from the fresh cache (no live fetch)
    scraped: int = 0            # fetched live
    failed: int = 0             # lookup/fetch failures
    requests_used: int = 0      # logical requests consumed from the budget
    aborted: bool = False       # stopped early (budget hit / stop flag / session lost)


class _SessionLost(Exception):
    """Raised on a 401/403 / login redirect — the run must stop and re-login."""


def _resilient_get(sess, url: str, params: dict, *, tries: int = 4, timeout: int = 90):
    """GET that rides through Zenith's intermittent 504s. Returns Response or None.

    Raises _SessionLost on a real auth loss so the whole run aborts cleanly.
    """
    last = "?"
    for attempt in range(1, tries + 1):
        try:
            resp = sess.get(url, params=params, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last = f"network {exc}"
            time.sleep(_backoff_with_jitter(attempt, base_s=2.0, cap_s=12.0))
            continue
        if resp.status_code in (401, 403) or "/otds/" in resp.url:
            raise _SessionLost(f"HTTP {resp.status_code}")
        if resp.status_code >= 500 and attempt < tries:
            last = f"HTTP {resp.status_code}"
            time.sleep(_backoff_with_jitter(attempt, base_s=2.0, cap_s=12.0))
            continue
        return resp
    log.warning("give up GET %s after %d tries (%s)", url, tries, last)
    return None


def scrape_dossier_events(
    session: ZenithSession,
    pnr_codes: Iterable[str],
    *,
    cache: ZenithPNRHistoryCache | None = None,
    max_requests: int = config.PNR_HISTORY_MAX_REQUESTS,
    delay_s: float = config.PNR_HISTORY_DELAY_S,
    stale_after_days: float = config.PNR_HISTORY_STALE_AFTER_DAYS,
    stop_flag: Callable[[], bool] | None = None,
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> tuple[list[DossierEvent], ScrapeStats]:
    """Scrape + parse the CHANGES history for each PNR. Returns (events, stats)."""
    sess = session.session
    sess.headers.setdefault("User-Agent", USER_AGENT)
    codes = list(dict.fromkeys(c.strip().upper() for c in pnr_codes if c and c.strip()))

    events: list[DossierEvent] = []
    used = requested = resolved = from_cache = scraped = failed = 0
    aborted = False

    for i, pnr in enumerate(codes, 1):
        if stop_flag and stop_flag():
            aborted = True
            break
        if progress_cb:
            progress_cb(i, len(codes), pnr)
        requested += 1
        try:
            if used >= max_requests:
                aborted = True
                break
            used += 1
            dossier_id = (lookup_pnr(session, pnr).dossier_id or "").strip()
            if not dossier_id:
                failed += 1
                continue
            resolved += 1

            html = ""
            if cache is not None and cache.is_fresh(dossier_id, stale_after_days=stale_after_days):
                bundle = cache.get_bundle(dossier_id) or {}
                if CHANGES_TAB in bundle:
                    html = bundle[CHANGES_TAB].html
                    from_cache += 1
            if not html:
                if used >= max_requests:
                    aborted = True
                    break
                used += 1
                resp = _resilient_get(sess, CHANGES_URL, {
                    "contexte": "recap_dossier", "CategorieEvent": "3",
                    "id_dossier_vol": dossier_id, "excel": "1"})
                if resp is None or resp.status_code != 200:
                    failed += 1
                    if delay_s:
                        time.sleep(delay_s)
                    continue
                html = resp.text
                if cache is not None:
                    cache.put_bundle(dossier_id, {CHANGES_TAB: (html, resp.status_code)})
                scraped += 1
                if delay_s:
                    time.sleep(delay_s)

            events.extend(parse_dossier_changes(html, dossier_id))
        except _SessionLost as exc:
            log.warning("session lost at %s (%s) — aborting", pnr, exc)
            aborted = True
            break
        except Exception as exc:  # noqa: BLE001 — one bad PNR must not kill the run
            log.warning("scrape failed for %s: %s", pnr, exc)
            failed += 1

    return events, ScrapeStats(
        requested=requested, resolved=resolved, from_cache=from_cache,
        scraped=scraped, failed=failed, requests_used=used, aborted=aborted)
