"""Feature health checks — "is each feature actually working right now?"

These apps depend on external websites that change and break (IATA's CheckACode
page, regtravelagency.gov.bd, the Zenith GDS, oep.gov.bd) plus local bits
(the bundled browser, Outlook, the report engine). A silent break in any of
them looks like "the app is broken" to a user. This module runs a fast battery
of LOCAL + REACHABILITY checks and reports OK / WARN / FAIL per feature with a
plain-language fix hint, so breakage is caught proactively.

Depth is deliberately shallow-and-safe: environment probes + can-we-reach the
external host. It does NOT run full feature flows (no real IATA lookup, no
Zenith login) — those are slower, hit sites harder, and need a session.

Pure and injectable (`probe` is swappable) so it is fully unit-testable offline.
"""

from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

ENVIRONMENT = "Environment"
CONNECTIVITY = "Connectivity"
INTEGRATION = "Integration"


@dataclass(frozen=True)
class HealthResult:
    key: str
    feature: str
    category: str
    status: str
    detail: str
    remedy: str = ""


def classify_http(code: int) -> str:
    """Classify a reachability response for a SPECIFIC feature URL.

    2xx/3xx = fine. 401/403 = present but gated (fine — the host answered).
    404/410 = the feature's page/endpoint is GONE or moved (a real break, not
    just 'host up') -> WARN so it doesn't read as a false green. 5xx = up but
    erroring -> WARN."""
    if code in (404, 410):
        return WARN
    if code >= 500:
        return WARN
    return OK


def _same_site(want: str, got: str) -> bool:
    """True if `got` host is the requested host or a sub/parent of it — so a
    normal www<->apex or CDN-subdomain redirect isn't treated as off-site."""
    if not got or not want:
        return True
    want, got = want.lower(), got.lower()
    return want == got or got.endswith("." + want) or want.endswith("." + got)


def default_probe(url: str, timeout: float = 8.0) -> tuple[str, str]:
    """Reachability probe. Returns (status, detail).

    Follows redirects but INSPECTS the final host: a bounce to a different
    site (captive portal / login gate) reads as WARN, not a green OK — the
    false-green the review flagged. requests is imported lazily."""
    import requests
    from urllib.parse import urlparse
    want_host = urlparse(url).hostname or ""
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True)
        code = r.status_code
        if code in (403, 405, 501):                 # HEAD often refused; try GET
            r = requests.get(url, timeout=timeout, stream=True, allow_redirects=True)
            code = r.status_code
            final = r.url
            r.close()
        else:
            final = r.url
        final_host = urlparse(final).hostname or ""
        if not _same_site(want_host, final_host):
            return WARN, f"redirected off-site to {final_host} (captive portal / login?)"
        return classify_http(code), f"reachable (HTTP {code})"
    except requests.exceptions.SSLError as exc:
        return WARN, f"TLS warning: {str(exc)[:80]}"
    except requests.exceptions.Timeout:
        return FAIL, f"timed out after {timeout:.0f}s"
    except requests.exceptions.RequestException as exc:
        return FAIL, f"unreachable: {type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001 — never let a probe crash the run
        return FAIL, f"error: {type(exc).__name__}"


# --- environment / integration checks (local, no network) -------------------

def _check_app_dir() -> tuple[str, str, str]:
    from . import config
    d = config.APP_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".health_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return OK, f"writable: {d}", ""
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"cannot write {d}: {exc}", "Check folder permissions / disk space."


def _check_caches() -> tuple[str, str, str]:
    from . import config
    dbs = [getattr(config, n, None) for n in
           ("CACHE_DB", "BD_CACHE_DB", "ZENITH_CACHE_DB", "MAILER_LOG_DB")]
    dbs = [p for p in dbs if p]
    broken = []
    for p in dbs:
        try:
            con = sqlite3.connect(str(p))
            con.execute("SELECT 1")
            con.close()
        except Exception as exc:  # noqa: BLE001
            broken.append(f"{Path(p).name}: {type(exc).__name__}")
    if broken:
        return WARN, "; ".join(broken), "Delete the named cache file; it rebuilds on next run."
    return OK, f"{len(dbs)} local cache(s) open cleanly", ""


def _check_browser_engine(verify: bool = False) -> tuple[str, str, str]:
    """The IATA validator + WhatsApp Blast need a driveable browser.

    A green here must mean a browser actually EXISTS, not just that the
    Playwright package imports (the review's false-green). A system channel is
    only returned when its exe was found on disk. For the bundled path, `verify`
    resolves the real executable and confirms the file exists (spawns only the
    node driver, not a browser window) — the GUI sets verify=True."""
    try:
        from .whatsapp_client import _import_sync_playwright, resolve_launch_kwargs
        sp = _import_sync_playwright()
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"Playwright/patchright missing: {type(exc).__name__}", \
            "Reinstall the app; the browser engine didn't bundle."
    try:
        kwargs = resolve_launch_kwargs("auto")
    except Exception as exc:  # noqa: BLE001
        return WARN, str(exc)[:100], "Install Google Chrome for the WhatsApp Blast."
    if kwargs.get("channel"):
        return OK, f"system {kwargs['channel']} browser found", ""
    if not verify:
        return OK, "browser engine present (not launch-verified)", ""
    # Bundled path — actually resolve + stat the Chromium binary.
    try:
        pw = sp().start()
        try:
            path = pw.chromium.executable_path
        finally:
            pw.stop()
        if path and Path(path).is_file():
            return OK, "bundled Chromium present", ""
        return FAIL, "browser binary not found on disk", \
            "Reinstall the app, or install Google Chrome."
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"browser won't initialise: {type(exc).__name__}", \
            "Reinstall the app, or install Google Chrome."


def _check_report_engine() -> tuple[str, str, str]:
    """Instant Reports need duckdb + pyarrow + openpyxl at runtime."""
    missing = []
    for mod in ("duckdb", "pyarrow", "openpyxl"):
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            missing.append(mod)
    if missing:
        return FAIL, "missing: " + ", ".join(missing), \
            "Reinstall the app; a report dependency didn't bundle."
    return OK, "duckdb + pyarrow + openpyxl present", ""


def _check_outlook() -> tuple[str, str, str]:
    """Bulk Mailer's Outlook-desktop transport (optional — SMTP/Graph also work)."""
    try:
        import win32com.client  # noqa: F401
    except Exception:  # noqa: BLE001
        return WARN, "pywin32 not available", "Use SMTP or Microsoft 365 sign-in instead."
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            win32com.client.Dispatch("Outlook.Application")
            return OK, "Outlook desktop reachable", ""
        finally:
            pythoncom.CoUninitialize()
    except Exception:  # noqa: BLE001
        return WARN, "Outlook not installed / not running", \
            "Optional — use SMTP or Microsoft 365 sign-in instead."


# --- registries -------------------------------------------------------------

def _net(key, feature, url, remedy):
    return (key, feature, CONNECTIVITY, ("net", url, remedy))


def _local(key, feature, fn):
    return (key, feature, ENVIRONMENT, ("local", fn, ""))


def _integration(key, feature, fn):
    return (key, feature, INTEGRATION, ("int", fn, ""))


_GRAPH = "https://login.microsoftonline.com"    # Microsoft 365 sign-in host
_TRAFFIC_SAMPLE = "https://data.gov.sg"          # representative traffic source host


def _registry(app: str, verify_browser: bool = False) -> list:
    from . import config
    try:
        from .zenith_client import BASE_URL as ZENITH_URL
    except Exception:  # noqa: BLE001
        ZENITH_URL = "https://asia.ttinteractive.com"
    try:
        from . import auth
        backend = (getattr(auth, "API_BASE_URL", "")
                   or getattr(auth, "DEFAULT_API_BASE_URL", ""))
    except Exception:  # noqa: BLE001
        backend = ""

    def _browser():
        return _check_browser_engine(verify_browser)

    if app == "mailer":
        return [
            _local("app_dir", "App data folder", _check_app_dir),
            _local("browser", "Browser engine (WhatsApp)", _browser),
            _integration("outlook", "Bulk Mailer · Outlook desktop", _check_outlook),
            _net("graph", "Bulk Mailer · Microsoft 365 sign-in", _GRAPH, "Microsoft sign-in host unreachable — Outlook/SMTP still work."),
            _net("whatsapp", "WhatsApp Blast", "https://web.whatsapp.com", "WhatsApp Web may be down, or check your internet."),
            _net("smtp_dns", "Bulk Mailer · Gmail SMTP", "https://smtp.gmail.com", "For other providers this is informational."),
        ]
    # console (combined app)
    checks = [
        _local("app_dir", "App data folder", _check_app_dir),
        _local("browser", "Browser engine (IATA / WhatsApp)", _browser),
        _local("caches", "Local caches (SQLite)", _check_caches),
        _local("reports", "Instant Reports engine", _check_report_engine),
        _integration("outlook", "Bulk Mailer · Outlook desktop", _check_outlook),
        _net("iata", "IATA Code Validator", getattr(config, "IATA_URL", "https://store.iata.org/ieccacfree"), "IATA's CheckACode site may be down or changed — retry later."),
        _net("bd_agency", "BD Travel Agency Lookup", "https://regtravelagency.gov.bd", "The BD registry site may be down — retry later."),
        _net("oep", "BD Overseas Movement (OEP)", "https://www.oep.gov.bd", "oep.gov.bd may be down — retry later."),
        _net("traffic", "Traffic Movement (sample source)", _TRAFFIC_SAMPLE, "A traffic data source is unreachable; others may still work."),
        _net("zenith", "Zenith GDS", ZENITH_URL, "Try the other host in the Zenith Server picker (usba vs asia)."),
        _net("graph", "Bulk Mailer · Microsoft 365 sign-in", _GRAPH, "Microsoft sign-in host unreachable — Outlook/SMTP still work."),
        _net("whatsapp", "WhatsApp Blast", "https://web.whatsapp.com", "WhatsApp Web may be down, or check your internet."),
    ]
    if backend:
        # Probe a REAL endpoint, not the API root — a Cloud Run API has no route
        # at "/" and legitimately 404s there (that was a false WARN). The
        # updater's version endpoint answers (200, or 401 when gated) if the
        # backend is alive.
        backend_url = backend.rstrip("/") + "/api/v1/app/latest"
        checks.append(_net("backend", "Sign-in / auto-update backend", backend_url,
                           "The app's backend is unreachable — sign-in and updates may fail."))
    return checks


def run_health_checks(
    app: str = "console",
    *,
    timeout: float = 8.0,
    verify_browser: bool = False,
    probe: Callable[[str, float], tuple[str, str]] | None = None,
    on_result: Callable[[HealthResult], None] | None = None,
) -> list[HealthResult]:
    """Run every check for `app`. Local checks run inline; connectivity checks
    run concurrently. `on_result` fires as each result lands (for live UI).
    `verify_browser=True` (the GUI) makes the browser check prove a real binary
    exists rather than just that the package imports."""
    probe = probe or default_probe
    reg = _registry(app, verify_browser)
    results: dict[str, HealthResult] = {}

    def emit(r: HealthResult) -> None:
        results[r.key] = r
        if on_result is not None:
            try:
                on_result(r)
            except Exception:  # noqa: BLE001
                log.debug("on_result raised", exc_info=True)

    net_items = []
    for key, feature, category, (kind, target, remedy) in reg:
        if kind in ("local", "int"):
            try:
                status, detail, fix = target()
            except Exception as exc:  # noqa: BLE001
                status, detail, fix = FAIL, f"check error: {type(exc).__name__}", ""
            emit(HealthResult(key, feature, category, status, detail, fix or remedy))
        else:
            net_items.append((key, feature, category, target, remedy))

    def _do_net(item):
        key, feature, category, url, remedy = item
        try:
            status, detail = probe(url, timeout)
        except Exception as exc:  # noqa: BLE001
            status, detail = FAIL, f"probe error: {type(exc).__name__}"
        return HealthResult(key, feature, category, status, detail,
                            remedy if status != OK else "")

    if net_items:
        with ThreadPoolExecutor(max_workers=min(8, len(net_items))) as ex:
            for r in ex.map(_do_net, net_items):
                emit(r)

    # preserve registry order for a stable display
    order = {k[0]: i for i, k in enumerate(reg)}
    return sorted(results.values(), key=lambda r: order.get(r.key, 999))


def summarize(results: list[HealthResult]) -> dict:
    counts = {"total": len(results), "ok": 0, "warn": 0, "fail": 0}
    for r in results:
        if r.status == OK:
            counts["ok"] += 1
        elif r.status == WARN:
            counts["warn"] += 1
        elif r.status == FAIL:
            counts["fail"] += 1
    return counts
