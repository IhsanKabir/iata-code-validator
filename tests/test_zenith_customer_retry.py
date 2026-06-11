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
