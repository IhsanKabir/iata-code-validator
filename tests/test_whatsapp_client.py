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
