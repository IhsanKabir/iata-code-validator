"""Retry resilience for single-record customer lookups.

Reproduces the production failure where bulk Customer Lookup runs returned
`ZenithError: Zenith returned 504 for ID ...` and dropped IDs on the first
gateway-timeout. fetch_customer now retries transient 5xx / network blips
with capped backoff + jitter, and only fails an ID once retries are spent.
Synthetic — a fake session serves queued responses, no network.
"""

from __future__ import annotations

import pytest

from src import zenith_client
from src.zenith_client import CustomerRecord, ZenithError, ZenithSession

# Minimal HTML that parse_customer_html accepts (carries presence markers).
_OK_HTML = (
    '<input name="m$txtFirstName" value="John">'
    '<input name="m$txtLastName" value="Doe">'
    '<input name="m$txtEmail" value="j@x.com">'
)


class _Resp:
    def __init__(self, status: int, text: str = "",
                 url: str = "https://usba.ttinteractive.com/x") -> None:
        self.status_code = status
        self.text = text
        self.url = url


class _SeqSession:
    """Serves queued responses in order; records how many calls were made."""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.headers: dict = {}
        self.calls = 0

    def get(self, url, params=None, allow_redirects=True, timeout=None):
        self.calls += 1
        if not self._responses:
            raise AssertionError("more requests than queued responses")
        return self._responses.pop(0)


def _session(responses) -> ZenithSession:
    return ZenithSession(session=_SeqSession(responses))


def test_504_then_success_recovers(monkeypatch):
    """Two 504s then a 200 must succeed via retry — not drop the ID."""
    monkeypatch.setattr(zenith_client.time, "sleep", lambda *_a, **_k: None)
    sess = _session([_Resp(504), _Resp(504), _Resp(200, _OK_HTML)])
    rec = sess.fetch_customer("12604657")
    assert isinstance(rec, CustomerRecord)
    assert rec.first_name == "John" and rec.last_name == "Doe"
    assert sess.session.calls == 3  # two 504s retried, third succeeded


def test_persistent_504_fails_after_retries(monkeypatch):
    """A genuinely stuck 504 still fails — but only after exhausting retries."""
    monkeypatch.setattr(zenith_client.time, "sleep", lambda *_a, **_k: None)
    sess = _session([_Resp(504)] * 4)
    with pytest.raises(ZenithError):
        sess.fetch_customer("12604657", max_attempts=4)
    assert sess.session.calls == 4  # all attempts used, then raised


def test_session_loss_not_retried(monkeypatch):
    """401/403 means the session is gone — fail fast, don't waste retries."""
    monkeypatch.setattr(zenith_client.time, "sleep", lambda *_a, **_k: None)
    sess = _session([_Resp(403)])
    with pytest.raises(zenith_client.SessionExpiredError):
        sess.fetch_customer("12604657")
    assert sess.session.calls == 1


# A 200 whose body has none of the form markers — what overload serves (an error page
# rendered as 200, a partial response). A genuinely missing ID returns an EMPTY FORM
# (markers present), so "no markers" is a degraded page, not a real not-found.
_NO_FORM_HTML = "<html><body>504 Gateway Timeout — origin overloaded</body></html>"


def test_no_form_page_retried_then_recovers(monkeypatch):
    """A valid customer that came back as a degraded no-form page must be RETRIED, not
    dropped as NOT_FOUND (the 12499484 'BANGLADESH BANK' case during a 504 storm)."""
    monkeypatch.setattr(zenith_client.time, "sleep", lambda *_a, **_k: None)
    sess = _session([_Resp(200, _NO_FORM_HTML), _Resp(200, _OK_HTML)])
    rec = sess.fetch_customer("12499484")
    assert isinstance(rec, CustomerRecord) and rec.first_name == "John"
    assert sess.session.calls == 2          # no-form retried, second got the real form


def test_persistent_no_form_raises_not_found(monkeypatch):
    """If the form never appears across every attempt, it's a real not-found."""
    monkeypatch.setattr(zenith_client.time, "sleep", lambda *_a, **_k: None)
    sess = _session([_Resp(200, _NO_FORM_HTML)] * 4)
    with pytest.raises(zenith_client.CustomerNotFoundError):
        sess.fetch_customer("99999999", max_attempts=4)
    assert sess.session.calls == 4          # retried each time, then declared not-found


# --- fetch_many automatic retry sweeps (storm resilience) --------------------
from collections import Counter  # noqa: E402


class _FakeSession:
    """Stands in for ZenithSession.fetch_customer — sequenced outcomes per id."""

    def __init__(self, plan: dict) -> None:
        self.plan = {k: list(v) for k, v in plan.items()}
        self.calls: Counter = Counter()

    def fetch_customer(self, cid, **_kw):
        self.calls[cid] += 1
        outcomes = self.plan.get(cid, ["ok"])
        outcome = outcomes.pop(0) if outcomes else "ok"
        if outcome == "504":
            raise ZenithError(f"Zenith returned 504 for ID {cid} after 4 attempts.")
        if outcome == "notfound":
            raise zenith_client.CustomerNotFoundError(f"not found {cid}")
        return zenith_client.parse_customer_html(_OK_HTML, cid)


def _final(results):
    by_id = {}
    for r in results:
        by_id[r.customer_id] = r        # last outcome per id wins
    return by_id


def test_fetch_many_retry_sweep_recovers(monkeypatch):
    """A 504 in the main pass is recovered by a retry sweep — not left as an error."""
    monkeypatch.setattr(zenith_client.time, "sleep", lambda *_a, **_k: None)
    sess = _FakeSession({"A": ["504", "ok"], "B": ["ok"]})
    results = zenith_client.fetch_many(
        sess, ["A", "B"], concurrency=1, delay_s=0, retry_passes=2, retry_cooldown_s=1)
    final = _final(results)
    assert final["A"].status == zenith_client.STATUS_OK   # recovered on sweep 1
    assert final["B"].status == zenith_client.STATUS_OK
    assert sess.calls["A"] == 2                            # main pass + one retry


def test_fetch_many_not_found_not_retried(monkeypatch):
    """NOT_FOUND is terminal — sweeps must not re-attempt it."""
    monkeypatch.setattr(zenith_client.time, "sleep", lambda *_a, **_k: None)
    sess = _FakeSession({"X": ["notfound", "ok"]})        # would 'recover' IF retried
    results = zenith_client.fetch_many(
        sess, ["X"], concurrency=1, delay_s=0, retry_passes=2, retry_cooldown_s=1)
    assert _final(results)["X"].status == zenith_client.STATUS_NOT_FOUND
    assert sess.calls["X"] == 1                            # not retried


def test_fetch_many_return_is_deduped_after_sweep(monkeypatch):
    """A recovered id is appended again across sweeps; the RETURN must be one-per-id."""
    monkeypatch.setattr(zenith_client.time, "sleep", lambda *_a, **_k: None)
    sess = _FakeSession({"A": ["504", "ok"], "B": ["ok"]})
    results = zenith_client.fetch_many(
        sess, ["A", "B"], concurrency=1, delay_s=0, retry_passes=2, retry_cooldown_s=1)
    ids = [r.customer_id for r in results]
    assert ids.count("A") == 1 and ids.count("B") == 1      # no duplicate rows
    assert {r.customer_id: r.status for r in results}["A"] == zenith_client.STATUS_OK


def test_fetch_many_retry_passes_zero_disables_sweeps(monkeypatch):
    monkeypatch.setattr(zenith_client.time, "sleep", lambda *_a, **_k: None)
    sess = _FakeSession({"A": ["504", "ok"]})
    results = zenith_client.fetch_many(sess, ["A"], concurrency=1, delay_s=0, retry_passes=0)
    assert _final(results)["A"].status == zenith_client.STATUS_ERROR   # no sweep
    assert sess.calls["A"] == 1


def test_fetch_many_stop_skips_cooldown_and_sweeps(monkeypatch):
    import threading
    monkeypatch.setattr(zenith_client.time, "sleep", lambda *_a, **_k: None)
    stop = threading.Event()
    sess = _FakeSession({"A": ["504", "ok"]})
    # stop set before the call -> main pass returns cancelled, no sweep attempted
    stop.set()
    results = zenith_client.fetch_many(
        sess, ["A"], concurrency=1, delay_s=0, stop_event=stop, retry_passes=2)
    # the id was cancelled (never really fetched); no successful recovery
    assert _final(results)["A"].status == zenith_client.STATUS_ERROR
