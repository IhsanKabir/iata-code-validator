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
