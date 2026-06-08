"""Microsoft Graph email backend — send as your M365 address without Outlook/SMTP.

Why this exists: the user's M365 tenant blocks SMTP basic-auth, and
desktop Outlook isn't an option. Graph is Microsoft's sanctioned way to
send mail programmatically. We use the **device-code flow** with the
well-known Microsoft public client ID, so NO Azure app registration or
admin secret is required — the user signs in once in a browser (with
MFA) and we cache the refresh token locally.

Scope: `Mail.Send` (+ `Mail.ReadWrite` so draft-mode can create drafts).
Both are delegated permissions a user can usually consent to themselves;
if the tenant demands admin consent, sign-in returns a clear error and
the caller falls back to another transport.

Token cache lives at `%LOCALAPPDATA%/IATAChecker/graph_token.bin` so the
browser sign-in is a one-time step.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .mailer_client import MailerError, OutgoingEmail, SendOutcome, re_split_addresses

log = logging.getLogger(__name__)


# Well-known public client: "Microsoft Graph Command Line Tools" — the
# same first-party app id Connect-MgGraph / the Graph CLI use. Unlike the
# Azure CLI client (04b07795…, which is only preauthorized for ARM and
# returns AADSTS65002 for Graph), this one IS preauthorized for delegated
# Microsoft Graph scopes including Mail.Send / Mail.ReadWrite — so the
# device-code flow works with no app registration.
_PUBLIC_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
_AUTHORITY = "https://login.microsoftonline.com/common"
_SCOPES = ["Mail.Send", "Mail.ReadWrite"]
_GRAPH = "https://graph.microsoft.com/v1.0"


class GraphAuthError(MailerError):
    """Sign-in failed or was declined (incl. admin-consent required)."""


class GraphSendError(MailerError):
    """Graph rejected a sendMail / createMessage call."""


def _token_cache_path() -> Path:
    from . import config
    return config.APP_DIR / "graph_token.bin"


def _load_cache():
    import msal
    cache = msal.SerializableTokenCache()
    p = _token_cache_path()
    if p.exists():
        try:
            cache.deserialize(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — corrupt cache → start fresh
            log.warning("graph token cache unreadable; ignoring")
    return cache


def _save_cache(cache) -> None:
    if cache.has_state_changed:
        p = _token_cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(cache.serialize(), encoding="utf-8")


@dataclass
class GraphSession:
    """An authenticated Graph mail session.

    Construct via `sign_in` (interactive device code) or
    `from_cache` (silent, if a token is cached). `account` is the
    signed-in address — the From identity for everything sent.
    """

    _app: object
    access_token: str
    account: str

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @classmethod
    def _build_app(cls):
        import msal
        cache = _load_cache()
        app = msal.PublicClientApplication(
            _PUBLIC_CLIENT_ID, authority=_AUTHORITY, token_cache=cache,
        )
        return app, cache

    @classmethod
    def try_silent(cls) -> "GraphSession | None":
        """Return a session from cached tokens, or None if none/expired."""
        app, cache = cls._build_app()
        accounts = app.get_accounts()
        if not accounts:
            return None
        result = app.acquire_token_silent(_SCOPES, account=accounts[0])
        _save_cache(cache)
        if result and "access_token" in result:
            return cls(
                _app=app, access_token=result["access_token"],
                account=accounts[0].get("username", ""),
            )
        return None

    @classmethod
    def sign_in(
        cls, *, prompt_cb: Callable[[str, str], None], timeout_s: int = 300,
    ) -> "GraphSession":
        """Interactive device-code sign-in.

        `prompt_cb(message, user_code)` receives the human instructions
        ("go to microsoft.com/devicelogin and enter CODE") plus the bare
        code so the GUI can keep it on screen. Blocks until the user
        completes sign-in or the code expires (~15 min, server-set).
        """
        app, cache = cls._build_app()
        flow = app.initiate_device_flow(scopes=_SCOPES)
        if "user_code" not in flow:
            raise GraphAuthError(
                "Couldn't start device-code sign-in: "
                f"{flow.get('error_description', 'unknown error')}"
            )
        prompt_cb(flow["message"], flow["user_code"])
        result = app.acquire_token_by_device_flow(flow)  # blocks until done/expired
        _save_cache(cache)
        if "access_token" not in result:
            err = result.get("error_description") or result.get("error") or "sign-in failed"
            raise GraphAuthError(
                f"Microsoft sign-in failed: {err}\n\n"
                "If it says admin approval is required, your tenant blocks "
                "self-service consent — you'd need IT to approve the app."
            )
        accounts = app.get_accounts()
        account = accounts[0].get("username", "") if accounts else ""
        return cls(_app=app, access_token=result["access_token"], account=account)

    @classmethod
    def sign_out(cls) -> None:
        """Forget cached tokens (next run requires a fresh sign-in)."""
        p = _token_cache_path()
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _message_json(self, email: OutgoingEmail) -> dict:
        """Build the Graph message resource (with base64 attachments)."""
        def recips(blob: str) -> list[dict]:
            return [{"emailAddress": {"address": a}} for a in re_split_addresses(blob)]

        attachments = []
        for path in email.attachments:
            p = Path(path)
            data = p.read_bytes()
            attachments.append({
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": p.name,
                "contentBytes": base64.b64encode(data).decode("ascii"),
            })
        msg: dict = {
            "subject": email.subject,
            "body": {"contentType": "Text", "content": email.body},
            "toRecipients": recips(email.to),
        }
        if email.cc:
            msg["ccRecipients"] = recips(email.cc)
        if email.bcc:
            msg["bccRecipients"] = recips(email.bcc)
        if attachments:
            msg["attachments"] = attachments
        return msg

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def send(self, email: OutgoingEmail) -> SendOutcome:
        """POST /me/sendMail — dispatches immediately."""
        import requests
        try:
            body = {"message": self._message_json(email), "saveToSentItems": True}
            resp = requests.post(
                f"{_GRAPH}/me/sendMail", json=body, headers=self._headers(), timeout=120,
            )
            if resp.status_code in (202, 200):
                return SendOutcome(to=email.to, status="SENT")
            return SendOutcome(
                to=email.to, status="FAILED",
                error=f"Graph {resp.status_code}: {resp.text[:200]}",
            )
        except FileNotFoundError as exc:
            return SendOutcome(to=email.to, status="FAILED", error=f"Attachment: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("Graph send failed for %s", email.to)
            return SendOutcome(to=email.to, status="FAILED", error=f"{type(exc).__name__}: {exc}")

    def draft(self, email: OutgoingEmail) -> SendOutcome:
        """POST /me/messages — creates a draft in the signed-in mailbox."""
        import requests
        try:
            resp = requests.post(
                f"{_GRAPH}/me/messages", json=self._message_json(email),
                headers=self._headers(), timeout=120,
            )
            if resp.status_code in (201, 200):
                return SendOutcome(
                    to=email.to, status="DRAFTED",
                    entry_id=str(resp.json().get("id", "")),
                )
            return SendOutcome(
                to=email.to, status="FAILED",
                error=f"Graph {resp.status_code}: {resp.text[:200]}",
            )
        except FileNotFoundError as exc:
            return SendOutcome(to=email.to, status="FAILED", error=f"Attachment: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("Graph draft failed for %s", email.to)
            return SendOutcome(to=email.to, status="FAILED", error=f"{type(exc).__name__}: {exc}")
