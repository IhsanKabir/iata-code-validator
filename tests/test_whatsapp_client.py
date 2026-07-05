"""Unit tests for the WhatsApp client's pure logic (no browser).

The live browser flow can't run in CI; these cover the deterministic parts:
deep-link building and browser-launch resolution (bundled vs system channel).
"""

from __future__ import annotations

import pytest

from src.whatsapp_client import (
    WhatsAppBrowserError,
    build_send_url,
    resolve_launch_kwargs,
)


def test_build_send_url_encodes_phone_and_text():
    url = build_send_url("8801812377362", "Hi, 50% off & more!")
    assert url.startswith("https://web.whatsapp.com/send?phone=8801812377362")
    assert "text=Hi%2C%2050%25%20off%20%26%20more%21" in url


def test_build_send_url_preserves_newlines_as_encoded():
    url = build_send_url("8801812377362", "Line1\nLine2")
    assert "%0A" in url                       # newline -> line break, not a send


def test_build_send_url_no_text():
    assert build_send_url("880181", "") == "https://web.whatsapp.com/send?phone=880181"


def test_resolve_bundled_returns_empty():
    assert resolve_launch_kwargs("bundled", chrome_probe=lambda c: False) == {}


def test_resolve_system_prefers_chrome_then_edge():
    only_edge = resolve_launch_kwargs("system", chrome_probe=lambda c: c == "msedge")
    assert only_edge == {"channel": "msedge"}
    has_chrome = resolve_launch_kwargs("system", chrome_probe=lambda c: True)
    assert has_chrome == {"channel": "chrome"}


def test_resolve_system_without_browser_raises():
    with pytest.raises(WhatsAppBrowserError, match="Chrome"):
        resolve_launch_kwargs("system", chrome_probe=lambda c: False)


def test_resolve_auto_uses_channel_when_not_frozen(monkeypatch):
    # not frozen + chrome present -> system channel
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    kwargs = resolve_launch_kwargs("auto", chrome_probe=lambda c: c == "chrome")
    assert kwargs == {"channel": "chrome"}


# --- threading contract: EVERY page op runs on the one session thread -------
# This directly guards the critical bug the review caught (Playwright sync
# objects created on one thread, driven from another -> every send fails).

import threading  # noqa: E402

from src.whatsapp_client import SENT, WhatsAppSession  # noqa: E402


class _FakeLocator:
    def __init__(self, page, kind):
        self.page, self.kind = page, kind

    def count(self):
        self.page.record()
        if self.kind == "compose":
            return 1
        if self.kind == "pane":
            return 1
        if self.kind == "outgoing":
            return self.page.outgoing
        if self.kind == "tick":
            return 1 if self.page.outgoing > self.page.baseline else 0
        return 0

    @property
    def last(self):
        return self

    @property
    def first(self):
        return self

    def locator(self, sel):
        return _FakeLocator(self.page, "tick")

    def wait_for(self, **_k):
        self.page.record()

    def click(self, **_k):
        self.page.record()

    def get_by_text(self, *_a, **_k):
        return _FakeLocator(self.page, "none")


class _FakePage:
    def __init__(self, owner_holder, *, send_works=True, prior_msgs=0):
        self.owner_holder = owner_holder     # records which thread touches us
        self.outgoing = prior_msgs           # pre-existing outgoing messages
        self.baseline = 0
        self.send_works = send_works
        self.keyboard = self

    def record(self):
        self.owner_holder.append(threading.get_ident())

    def set_default_timeout(self, _ms):
        self.record()

    def goto(self, *_a, **_k):
        self.record()

    def locator(self, sel):
        self.record()
        if "#pane-side" in sel:
            return _FakeLocator(self, "pane")
        if "contenteditable" in sel and "footer" in sel:
            return _FakeLocator(self, "compose")
        if "message-out" in sel:
            return _FakeLocator(self, "outgoing")
        return _FakeLocator(self, "none")

    def get_by_text(self, *_a, **_k):
        return _FakeLocator(self, "none")

    def press(self, *_a, **_k):
        self.record()
        if self.send_works:
            self.outgoing += 1           # "Enter" adds an outgoing message

    def type(self, *_a, **_k):
        self.record()


class _FakeSession(WhatsAppSession):
    _page_kwargs: dict = {}

    def _create_browser(self):
        self._page = _FakePage(self.touch_threads, **self._page_kwargs)
        self._page.set_default_timeout(self._nav_timeout_ms)


def test_all_page_ops_run_on_the_session_thread():
    s = _FakeSession("x", browser="bundled")
    s.touch_threads = []
    s.start()
    caller = threading.get_ident()
    assert s.login_status() == "logged_in"
    res = s.send("8801812377362", "hi")
    assert res.status == SENT                        # baseline->grew->tick = SENT
    s.close()
    # every recorded page touch happened on ONE thread, and NOT the caller's.
    assert s.touch_threads, "no page ops recorded"
    assert len(set(s.touch_threads)) == 1
    assert caller not in s.touch_threads


def test_no_new_message_is_FAILED_not_false_SENT():
    # The false-SENT bug: a chat with pre-existing outgoing ticks where our
    # send does NOT actually post a new message must be FAILED, not SENT.
    from src.whatsapp_client import FAILED
    s = _FakeSession("x", browser="bundled")
    s.touch_threads = []
    s._page_kwargs = {"send_works": False, "prior_msgs": 3}  # ticks already present
    s._confirm_timeout_s = 0.5                               # don't burn 20s
    s.start()
    res = s.send("8801812377362", "hi", None)
    s.close()
    assert res.status == FAILED
    assert "no new message" in res.error
