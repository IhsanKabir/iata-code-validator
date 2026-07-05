"""WhatsApp Web automation client (free, un-official).

Drives web.whatsapp.com through a persistent-profile browser so the QR scan is
one-time. Sends a text message — optionally with one shared image — to a bare
international number.

THREAD MODEL (critical): Playwright's sync API pins its driver to the OS thread
that created it, so EVERY Playwright call must run on that one thread. This
class therefore owns a single long-lived "session thread": the browser is
created there and every command (login check, send, close) is marshalled onto
it through a queue. GUI worker threads only enqueue commands and block for the
result — they never touch the page directly. This is the fix for the
cross-thread "greenlet switch on wrong thread" failure that otherwise breaks
every send.

Works in BOTH apps: the combined console uses the bundled patchright Chromium;
the standalone mailer drives the user's installed Chrome/Edge via a channel.

DISCLAIMER: automating WhatsApp Web violates WhatsApp's Terms of Service and
can get the sending number BANNED. The GUI surfaces this at every step.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

# Send outcomes.
SENT = "SENT"
NOT_ON_WHATSAPP = "NOT_ON_WHATSAPP"
FAILED = "FAILED"
LOGGED_OUT = "LOGGED_OUT"
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


class WhatsAppBrowserError(RuntimeError):
    pass


def build_send_url(phone: str, text: str) -> str:
    """WhatsApp deep link that opens a 1:1 chat with the text pre-filled."""
    q = f"phone={quote(phone)}"
    if text:
        q += f"&text={quote(text)}"
    return f"{_WA_BASE}/send?{q}"


def _import_sync_playwright():
    """patchright (bundled in the console) first, then plain playwright."""
    try:
        from patchright.sync_api import sync_playwright  # type: ignore
        return sync_playwright
    except ImportError:
        from playwright.sync_api import sync_playwright   # type: ignore
        return sync_playwright


def resolve_launch_kwargs(browser: str, *, chrome_probe=None) -> dict:
    """Launch kwargs for the persistent context.

    browser: 'bundled' | 'system' | 'auto'. `chrome_probe(channel)->bool` lets
    tests inject availability."""
    def _have(channel: str) -> bool:
        if chrome_probe is not None:
            return chrome_probe(channel)
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

    import sys
    frozen = bool(getattr(sys, "frozen", False)) or \
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == "0"
    if browser == "bundled" or (browser == "auto" and frozen):
        return {}
    for channel in ("chrome", "msedge"):
        if _have(channel):
            return {"channel": channel}
    if browser == "system":
        raise WhatsAppBrowserError(
            "No Chrome or Edge found. Install Google Chrome, or use the "
            "combined Travel Ops Console app (it ships its own browser).")
    return {}


class WhatsAppSession:
    """One persistent WhatsApp Web session, confined to its own thread."""

    def __init__(
        self,
        profile_dir: str | Path,
        *,
        browser: str = "auto",
        on_log=None,
        nav_timeout_s: float = 45.0,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.browser = browser
        self._on_log = on_log or (lambda m: log.info(m))
        self._nav_timeout_s = nav_timeout_s
        self._nav_timeout_ms = int(nav_timeout_s * 1000)
        self._confirm_timeout_s = 20.0            # wait for a NEW outgoing bubble
        self._pw = None
        self._context = None
        self._page = None
        self._cmd_q: "queue.Queue" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._start_error: Exception | None = None
        self._stop = threading.Event()

    # -- session thread + command marshalling -------------------------------

    def start(self) -> None:
        """Spawn the session thread, which creates the browser ON that thread.
        Blocks until the browser is ready (or raises the startup error)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="wa-session", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=self._nav_timeout_s + 40):
            raise WhatsAppBrowserError("WhatsApp browser did not start in time.")
        if self._start_error is not None:
            err, self._start_error = self._start_error, None
            raise err

    def _create_browser(self) -> None:
        """Create the Playwright browser ON the session thread. Overridable in
        tests to inject a fake page (so the threading contract can be verified
        without a real browser)."""
        sync_playwright = _import_sync_playwright()
        self._pw = sync_playwright().start()
        kwargs = resolve_launch_kwargs(self.browser)
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=False,                           # QR scan + user guardrail
            viewport={"width": 1200, "height": 860},
            args=["--disable-blink-features=AutomationControlled"],
            **kwargs,
        )
        self._page = self._context.pages[0] if self._context.pages \
            else self._context.new_page()
        self._page.set_default_timeout(self._nav_timeout_ms)
        self._page.goto(_WA_BASE, wait_until="domcontentloaded")
        self._on_log("WhatsApp Web opened.")

    def _loop(self) -> None:
        # EVERYTHING Playwright happens on this thread.
        try:
            self._create_browser()
        except Exception as exc:  # noqa: BLE001
            self._start_error = exc
            self._started.set()
            return
        self._started.set()
        while True:
            item = self._cmd_q.get()
            if item is None:                          # close sentinel
                break
            fn, holder = item
            try:
                holder["result"] = fn()
            except Exception as exc:  # noqa: BLE001
                holder["error"] = exc
            finally:
                holder["event"].set()
        for teardown in (lambda: self._context.close(), lambda: self._pw.stop()):
            try:
                teardown()
            except Exception as exc:  # noqa: BLE001
                log.debug("wa teardown: %s", exc)

    def _call(self, fn, *, timeout: float):
        if self._thread is None or not self._thread.is_alive():
            raise WhatsAppBrowserError("WhatsApp session is not running.")
        holder: dict = {"event": threading.Event()}
        self._cmd_q.put((fn, holder))
        if not holder["event"].wait(timeout=timeout):
            raise TimeoutError("WhatsApp session command timed out.")
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")

    def stop(self) -> None:
        """Soft stop: the browser stays alive; the current/next send bails out."""
        self._stop.set()

    def clear_stop(self) -> None:
        """Re-arm the session for a fresh run after a soft stop."""
        self._stop.clear()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._cmd_q.put(None)
            self._thread.join(timeout=15)
        self._thread = None

    # -- public API (marshalled) --------------------------------------------

    def login_status(self) -> str:
        try:
            return self._call(self._do_login_status, timeout=20)
        except (WhatsAppBrowserError, TimeoutError):
            return LOADING

    def wait_until_logged_in(self, timeout_s: float = 180.0) -> bool:
        """Poll (on the CALLING thread) until the chat list appears. Each poll
        is a quick marshalled call, so the session thread stays free and Stop
        is honoured."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return False
            if self.login_status() == LOGGED_IN:
                return True
            time.sleep(1.0)
        return False

    def send(self, phone: str, text: str,
             image_path: str | None = None) -> WhatsAppSendResult:
        try:
            return self._call(lambda: self._do_send(phone, text, image_path),
                              timeout=self._nav_timeout_s + 60)
        except Exception as exc:  # noqa: BLE001
            return WhatsAppSendResult(phone, FAILED, str(exc)[:200])

    # -- page ops (run ON the session thread) -------------------------------

    def _do_login_status(self) -> str:
        if self._page is None:
            return LOADING
        try:
            if self._page.locator("#pane-side").count() > 0:
                return LOGGED_IN
            if self._page.locator(
                    'canvas[aria-label], div[data-ref], [data-testid="qrcode"]').count() > 0:
                return NEEDS_QR
        except Exception as exc:  # noqa: BLE001
            log.debug("login_status probe: %s", exc)
        return LOADING

    def _do_send(self, phone: str, text: str,
                 image_path: str | None) -> WhatsAppSendResult:
        if self._stop.is_set():
            return WhatsAppSendResult(phone, SKIPPED, "stopped")
        self._page.goto(build_send_url(phone, "" if image_path else text),
                        wait_until="domcontentloaded")
        state = self._await_chat_or_block()
        if state != "chat":
            return WhatsAppSendResult(phone, state)
        # Baseline the outgoing-message count BEFORE sending so confirmation is
        # scoped to a NEW message, not a pre-existing tick from earlier in the
        # same chat (the false-SENT bug).
        baseline = self._outgoing_count()
        try:
            if image_path:
                self._attach_and_caption(text, image_path)
            else:
                box = self._compose_box()
                box.wait_for(state="visible", timeout=15000)
                box.click()
                self._page.keyboard.press("Enter")
        except Exception as exc:  # noqa: BLE001
            return WhatsAppSendResult(phone, FAILED, str(exc)[:200])
        if self._confirm_new_message(baseline):
            return WhatsAppSendResult(phone, SENT)
        return WhatsAppSendResult(phone, FAILED, "no new message appeared")

    def _await_chat_or_block(self) -> str:
        deadline = time.monotonic() + self._nav_timeout_s
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return SKIPPED
            if self._page.locator(
                    '[role="dialog"], div[data-animate-modal-body="true"]').count() > 0 \
                    and self._page.get_by_text("invalid", exact=False).count() > 0:
                self._dismiss_modal()
                return NOT_ON_WHATSAPP
            if self._do_login_status() == NEEDS_QR:
                return LOGGED_OUT
            if self._compose_box().count() > 0:
                return "chat"
            time.sleep(0.4)
        return FAILED

    def _compose_box(self):
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

    def _attach_and_caption(self, caption: str, image_path: str) -> None:
        # Set the file on WhatsApp's hidden media <input>, bypassing the OS
        # dialog Playwright can't drive.
        file_input = self._page.locator(
            'input[type="file"][accept*="image"], input[type="file"]').first
        file_input.set_input_files(image_path, timeout=15000)
        # Wait for the media PREVIEW to be ready (its send button appears)
        # BEFORE typing the caption — otherwise the caption is lost / mistyped.
        send_btn = self._page.locator(
            'span[data-icon="send"], div[role="button"][aria-label="Send"], '
            '[data-testid="send"]').first
        send_btn.wait_for(state="visible", timeout=20000)
        if caption:
            cap = self._page.locator(
                'div[contenteditable="true"][role="textbox"]').last
            cap.wait_for(state="visible", timeout=10000)
            cap.click()
            self._page.keyboard.type(caption, delay=8)
        send_btn.click(timeout=8000)

    def _outgoing_count(self) -> int:
        try:
            return self._page.locator("div.message-out").count()
        except Exception:  # noqa: BLE001
            return 0

    def _confirm_new_message(self, baseline: int) -> bool:
        """Confirm a NEW outgoing message appeared (count grew past baseline).
        A new row means our text left the composer; we additionally wait for a
        status tick on it. 'Only a pre-existing tick, no new row' => not sent."""
        deadline = time.monotonic() + self._confirm_timeout_s
        grew = False
        while time.monotonic() < deadline:
            try:
                if self._page.locator("div.message-out").count() > baseline:
                    grew = True
                    tick = self._page.locator("div.message-out").last.locator(
                        'span[data-icon^="msg-"]')
                    if tick.count() > 0:
                        return True
            except Exception as exc:  # noqa: BLE001
                log.debug("confirm: %s", exc)
            time.sleep(0.4)
        return grew                                   # new row but no tick yet = sent-pending
