"""WhatsApp Web automation client (free, un-official).

Drives web.whatsapp.com through a persistent-profile browser so the QR scan is
one-time. Sends a text message — optionally with one shared image — to a bare
international number. Mirrors the proven persistent-context pattern in
validator.py. Works in BOTH apps:

  * Combined console: uses the bundled patchright Chromium.
  * Standalone mailer: drives the user's installed Chrome/Edge (no bundled
    browser -> the exe stays tiny) via a channel.

DISCLAIMER: automating WhatsApp Web violates WhatsApp's Terms of Service and
can get the sending number BANNED, especially on fast/large blasts to people
who never messaged you first. The GUI surfaces this at every step; this module
just does the mechanics as safely as it can (serial, verified, resumable).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

# Send outcomes (mirror mailer SendOutcome vocabulary where it overlaps).
SENT = "SENT"
NOT_ON_WHATSAPP = "NOT_ON_WHATSAPP"      # number has no WhatsApp account
FAILED = "FAILED"
LOGGED_OUT = "LOGGED_OUT"                 # session expired mid-run
SKIPPED = "SKIPPED"

# Login states.
LOGGED_IN = "logged_in"
NEEDS_QR = "needs_qr"
LOADING = "loading"

_WA_BASE = "https://web.whatsapp.com"


@dataclass(frozen=True)
class WhatsAppSendResult:
    phone: str
    status: str
    error: str = ""


def build_send_url(phone: str, text: str) -> str:
    """WhatsApp deep link that opens a 1:1 chat with the text pre-filled.

    The text lands in the compose box as content (newlines preserved as line
    breaks, NOT as premature sends) — we press Enter once to actually send."""
    q = f"phone={quote(phone)}"
    if text:
        q += f"&text={quote(text)}"
    return f"{_WA_BASE}/send?{q}"


def _import_sync_playwright():
    """patchright (bundled in the console) first, then plain playwright
    (standalone). Same API, so the rest of the module is agnostic."""
    try:
        from patchright.sync_api import sync_playwright  # type: ignore
        return sync_playwright
    except ImportError:
        from playwright.sync_api import sync_playwright   # type: ignore
        return sync_playwright


def resolve_launch_kwargs(browser: str, *, chrome_probe=None) -> dict:
    """Launch kwargs for the persistent context.

    browser: 'bundled' (use the packaged Chromium), 'system' (installed
    Chrome/Edge via a channel), or 'auto' (bundled when frozen/available, else
    system). `chrome_probe(channel)->bool` lets tests inject availability."""
    def _have(channel: str) -> bool:
        if chrome_probe is not None:
            return chrome_probe(channel)
        # Heuristic browser presence check (Windows install locations).
        candidates = {
            "chrome": [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ],
            "msedge": [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ],
        }.get(channel, [])
        return any(Path(p).is_file() for p in candidates)

    frozen = bool(getattr(__import__("sys"), "frozen", False)) or \
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == "0"
    if browser == "bundled" or (browser == "auto" and frozen):
        return {}                            # default bundled chromium
    for channel in ("chrome", "msedge"):
        if _have(channel):
            return {"channel": channel}
    if browser == "system":
        raise WhatsAppBrowserError(
            "No Chrome or Edge found. Install Google Chrome, or use the "
            "combined Travel Ops Console app (it ships its own browser).")
    return {}                                # last resort: bundled


class WhatsAppBrowserError(RuntimeError):
    pass


class WhatsAppSession:
    """One persistent WhatsApp Web browser session. Serial send only."""

    def __init__(
        self,
        profile_dir: str | Path,
        *,
        browser: str = "auto",
        on_log=None,
        nav_timeout_s: float = 60.0,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.browser = browser
        self._on_log = on_log or (lambda m: log.info(m))
        self._nav_timeout_ms = int(nav_timeout_s * 1000)
        self._pw = None
        self._context = None
        self._page = None
        self._stop = threading.Event()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        sync_playwright = _import_sync_playwright()
        self._pw = sync_playwright().start()
        kwargs = resolve_launch_kwargs(self.browser)
        # WhatsApp needs a VISIBLE window (QR scan + it's a user guardrail).
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=False,
            viewport={"width": 1200, "height": 860},
            args=["--disable-blink-features=AutomationControlled"],
            **kwargs,
        )
        self._page = self._context.pages[0] if self._context.pages \
            else self._context.new_page()
        self._page.set_default_timeout(self._nav_timeout_ms)
        self._page.goto(_WA_BASE, wait_until="domcontentloaded")
        self._on_log("WhatsApp Web opened.")

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        for fn in (
            lambda: self._context and self._context.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                log.debug("whatsapp close: %s", exc)
        self._context = self._page = self._pw = None

    # -- login --------------------------------------------------------------

    def login_status(self) -> str:
        """logged_in (chat list present) | needs_qr | loading."""
        if self._page is None:
            return LOADING
        try:
            if self._page.locator("#pane-side").count() > 0:
                return LOGGED_IN
            qr = self._page.locator(
                'canvas[aria-label], div[data-ref], [data-testid="qrcode"]')
            if qr.count() > 0:
                return NEEDS_QR
        except Exception as exc:  # noqa: BLE001
            log.debug("login_status probe: %s", exc)
        return LOADING

    def wait_until_logged_in(self, timeout_s: float = 180.0) -> bool:
        """Poll until the chat list appears (user scanned the QR)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return False
            if self.login_status() == LOGGED_IN:
                return True
            time.sleep(1.0)
        return False

    # -- sending ------------------------------------------------------------

    def send(self, phone: str, text: str,
             image_path: str | None = None) -> WhatsAppSendResult:
        """Send one message. Never raises for a per-contact problem — returns a
        classified result so the batch continues."""
        if self._stop.is_set():
            return WhatsAppSendResult(phone, SKIPPED, "stopped")
        if self._page is None:
            return WhatsAppSendResult(phone, FAILED, "session not started")
        try:
            self._page.goto(build_send_url(phone, "" if image_path else text),
                            wait_until="domcontentloaded")
            state = self._await_chat_or_block(phone)
            if state != "chat":
                return WhatsAppSendResult(phone, state, "")
            if image_path:
                return self._send_with_image(phone, text, image_path)
            return self._send_text(phone)
        except Exception as exc:  # noqa: BLE001 — one contact must not kill the run
            log.warning("whatsapp send %s failed: %s", phone, exc)
            return WhatsAppSendResult(phone, FAILED, str(exc)[:200])

    def _await_chat_or_block(self, phone: str) -> str:
        """Wait for the compose box (chat open) OR an invalid-number modal OR a
        logout. Returns 'chat' | NOT_ON_WHATSAPP | LOGGED_OUT | FAILED."""
        deadline = time.monotonic() + self._nav_timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return SKIPPED
            # invalid-number popup ("Phone number shared via url is invalid")
            invalid = self._page.get_by_text("invalid", exact=False)
            if invalid.count() > 0 and self._page.locator(
                    'div[data-animate-modal-body="true"], [role="dialog"]').count() > 0:
                self._dismiss_modal()
                return NOT_ON_WHATSAPP
            if self.login_status() == NEEDS_QR:
                return LOGGED_OUT
            if self._compose_box().count() > 0:
                return "chat"
            time.sleep(0.4)
        return FAILED

    def _compose_box(self):
        # Layered, most-stable-first. The compose box lives in the footer.
        return self._page.locator(
            'footer div[contenteditable="true"][role="textbox"], '
            'footer div[contenteditable="true"], '
            'div[title="Type a message"]').first

    def _dismiss_modal(self) -> None:
        for sel in ('div[data-testid="popup-controls-ok"]',
                    '[role="dialog"] button', 'div[role="button"]'):
            try:
                btn = self._page.locator(sel).first
                if btn.count() > 0:
                    btn.click(timeout=3000)
                    return
            except Exception:  # noqa: BLE001
                continue

    def _send_text(self, phone: str) -> WhatsAppSendResult:
        box = self._compose_box()
        box.wait_for(state="visible", timeout=15000)
        box.click()
        self._page.keyboard.press("Enter")
        if self._confirm_sent():
            return WhatsAppSendResult(phone, SENT)
        return WhatsAppSendResult(phone, FAILED, "no send confirmation")

    def _send_with_image(self, phone: str, caption: str,
                         image_path: str) -> WhatsAppSendResult:
        # Set the file directly on WhatsApp's hidden media <input>, bypassing
        # the OS file dialog (which Playwright can't drive).
        file_input = self._page.locator(
            'input[type="file"][accept*="image"], input[type="file"]').first
        file_input.set_input_files(image_path, timeout=15000)
        # media preview: a caption box appears; type the text there.
        cap = self._page.locator(
            'div[contenteditable="true"][role="textbox"]').last
        cap.wait_for(state="visible", timeout=15000)
        if caption:
            cap.click()
            self._page.keyboard.type(caption, delay=8)
        # send button in the media preview
        for sel in ('span[data-icon="send"]', 'div[role="button"][aria-label="Send"]',
                    '[data-testid="send"]'):
            try:
                btn = self._page.locator(sel).first
                if btn.count() > 0:
                    btn.click(timeout=5000)
                    break
            except Exception:  # noqa: BLE001
                continue
        else:
            self._page.keyboard.press("Enter")
        if self._confirm_sent(image=True):
            return WhatsAppSendResult(phone, SENT)
        return WhatsAppSendResult(phone, FAILED, "image send not confirmed")

    def _confirm_sent(self, *, image: bool = False) -> bool:
        """Confirm the message left our device: an outgoing bubble shows a
        status tick (pending clock -> single/double check). We accept the
        pending clock too (it left the composer), but wait briefly for a check."""
        try:
            # a message status icon on the newest outgoing message
            self._page.locator(
                'span[data-icon="msg-check"], span[data-icon="msg-dblcheck"], '
                'span[data-icon="msg-time"]'
            ).last.wait_for(state="visible", timeout=15000)
            time.sleep(0.3)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("send confirm: %s", exc)
            return False
