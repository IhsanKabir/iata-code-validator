"""Tests for the feature health-check engine.

Network is stubbed via an injected `probe`, so these run offline and fast.
The point of the engine is to NEVER report a green when a feature is broken —
so the tests focus on the OK/WARN/FAIL classification and that a probe error
can't crash the whole run.
"""

from __future__ import annotations

from src.health_checks import (
    FAIL,
    OK,
    WARN,
    HealthResult,
    classify_http,
    run_health_checks,
)


def test_classify_http_status():
    assert classify_http(200) == OK
    assert classify_http(301) == OK
    assert classify_http(403) == OK           # present but gated — host answered
    assert classify_http(404) == WARN         # feature page GONE/moved — not a false green
    assert classify_http(410) == WARN
    assert classify_http(500) == WARN         # up but erroring
    assert classify_http(503) == WARN


def test_run_returns_a_result_per_registered_check():
    results = run_health_checks("console", probe=lambda url, timeout: (OK, "ok"))
    assert results
    assert all(isinstance(r, HealthResult) for r in results)
    keys = {r.key for r in results}
    # core console features are represented
    for expected in ("iata", "bd_agency", "zenith", "whatsapp", "app_dir"):
        assert expected in keys, f"missing check {expected}"


def test_mailer_app_has_its_own_smaller_registry():
    console = {r.key for r in run_health_checks("console", probe=lambda u, t: (OK, "ok"))}
    mailer = {r.key for r in run_health_checks("mailer", probe=lambda u, t: (OK, "ok"))}
    assert "whatsapp" in mailer and "outlook" in mailer
    assert "zenith" not in mailer               # mailer has no Zenith feature
    assert len(console) > len(mailer)


def test_failing_probe_marks_connectivity_fail_not_crash():
    def bad_probe(url, timeout):
        raise RuntimeError("boom")
    results = run_health_checks("console", probe=bad_probe)
    net = [r for r in results if r.category == "Connectivity"]
    assert net and all(r.status == FAIL for r in net)   # every net check FAILs, run survives


def test_fail_probe_produces_fail_status_and_remedy():
    results = run_health_checks(
        "console", probe=lambda url, timeout: (FAIL, "unreachable: ConnectionError"))
    iata = next(r for r in results if r.key == "iata")
    assert iata.status == FAIL
    assert iata.remedy                          # a fix hint is always present


def test_on_result_called_live_for_each():
    seen = []
    run_health_checks("console", probe=lambda u, t: (OK, "ok"),
                      on_result=lambda r: seen.append(r.key))
    assert len(seen) == len(run_health_checks("console", probe=lambda u, t: (OK, "ok")))


def test_same_site_redirect_logic():
    from src.health_checks import _same_site
    assert _same_site("regtravelagency.gov.bd", "regtravelagency.gov.bd")
    assert _same_site("oep.gov.bd", "www.oep.gov.bd")        # subdomain ok
    assert _same_site("www.oep.gov.bd", "oep.gov.bd")        # apex ok
    assert not _same_site("store.iata.org", "login.captive.net")   # off-site bounce
    assert not _same_site("regtravelagency.gov.bd", "portal.isp.com")


def test_new_coverage_checks_present():
    keys = {r.key for r in run_health_checks("console", probe=lambda u, t: (OK, "ok"))}
    assert "graph" in keys and "traffic" in keys            # M365 + traffic now covered
    mailer = {r.key for r in run_health_checks("mailer", probe=lambda u, t: (OK, "ok"))}
    assert "graph" in mailer                                 # M365 sign-in for the mailer


def test_verify_browser_param_accepted():
    # light path (verify_browser=False, the default) must not launch anything
    res = run_health_checks("mailer", verify_browser=False, probe=lambda u, t: (OK, "ok"))
    browser = next(r for r in res if r.key == "browser")
    assert browser.status in (OK, WARN, FAIL)              # runs without raising


def test_overall_summary_counts():
    from src.health_checks import summarize
    results = run_health_checks("console", probe=lambda u, t: (OK, "ok"))
    s = summarize(results)
    assert s["total"] == len(results)
    assert s["ok"] + s["warn"] + s["fail"] == s["total"]
    assert s["ok"] >= 1                          # connectivity checks are OK here
