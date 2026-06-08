"""Outlook desktop automation for the Bulk Mailer tab.

Drives the locally-installed, already-signed-in Outlook via COM. This
deliberately sidesteps SMTP/app-password auth — corporate M365 tenants
disable SMTP basic-auth, but the desktop Outlook session is already
authenticated (incl. MFA), so handing it composed messages "just works".

Two send modes:
  - DRAFT   — message saved to the Outlook Drafts folder; nothing leaves
              the outbox. The user reviews, then bulk-sends from Outlook.
  - SEND    — message dispatched immediately via Outlook.

Everything here is import-light: `win32com` is imported lazily inside
`OutlookSession` so the rest of the app (and non-Windows test runs)
don't pay for it or crash when Outlook is absent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# Outlook OlItemType / folder constants (avoid importing the COM enums).
_OL_MAIL_ITEM = 0          # olMailItem
_OL_FORMAT_PLAIN = 1       # olFormatPlain
_OL_FOLDER_DRAFTS = 16     # olFolderDrafts


class MailerError(Exception):
    """Top-level Bulk Mailer failure."""


class OutlookUnavailableError(MailerError):
    """Outlook desktop isn't installed / COM couldn't start it."""


@dataclass(frozen=True)
class OutgoingEmail:
    """One composed-but-not-yet-created message."""

    to: str
    subject: str
    body: str
    attachments: tuple[Path, ...] = ()
    cc: str = ""
    bcc: str = ""


@dataclass(frozen=True)
class SendOutcome:
    """Result of attempting one message."""

    to: str
    status: str            # "DRAFTED" | "SENT" | "FAILED" | "SKIPPED"
    error: str = ""
    entry_id: str = ""     # Outlook EntryID for the created item, if any


class OutlookSession:
    """Thin wrapper around the Outlook.Application COM object.

    Use as a context manager so COM is initialised/torn down on the
    worker thread:

        with OutlookSession() as ol:
            ol.create(email, send=False)
    """

    def __init__(self) -> None:
        self._app = None
        self._pythoncom = None

    def __enter__(self) -> "OutlookSession":
        try:
            import pythoncom
            import win32com.client as win32
        except ImportError as exc:  # pragma: no cover - Windows-only dep
            raise OutlookUnavailableError(
                "pywin32 is not installed — the Bulk Mailer needs it to "
                "talk to Outlook."
            ) from exc
        self._pythoncom = pythoncom
        # Each worker thread must init COM for itself.
        pythoncom.CoInitialize()
        try:
            # Dispatch attaches to a running Outlook or starts one; it
            # reuses the signed-in profile either way.
            self._app = win32.Dispatch("Outlook.Application")
        except Exception as exc:  # noqa: BLE001 — COM raises broad types
            pythoncom.CoUninitialize()
            raise OutlookUnavailableError(
                "Couldn't start Outlook. Make sure Microsoft Outlook is "
                "installed and you're signed in."
            ) from exc
        return self

    def __exit__(self, *_exc) -> None:
        self._app = None
        if self._pythoncom is not None:
            try:
                self._pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass

    def verify_account(self) -> str:
        """Return the default sending account's address (proves we're live)."""
        if self._app is None:
            raise OutlookUnavailableError("Outlook session not started.")
        try:
            session = self._app.GetNamespace("MAPI")
            accounts = session.Accounts
            if accounts.Count >= 1:
                return str(accounts.Item(1).SmtpAddress or accounts.Item(1).DisplayName)
        except Exception as exc:  # noqa: BLE001
            log.warning("verify_account failed: %s", exc)
        return ""

    def _account_by_address(self, address: str):
        """Return the Outlook Account object whose SMTP matches, or None."""
        if not address or self._app is None:
            return None
        try:
            accounts = self._app.GetNamespace("MAPI").Accounts
            for i in range(1, accounts.Count + 1):
                acc = accounts.Item(i)
                if str(acc.SmtpAddress or "").strip().lower() == address.strip().lower():
                    return acc
        except Exception:  # noqa: BLE001
            pass
        return None

    def _drafts_folder_for(self, address: str):
        """Return the Drafts folder of the matching account's own store.

        Outlook's mail.Save() always drops into the DEFAULT account's
        Drafts, ignoring SendUsingAccount — so a draft meant to go out
        from account B silently lands in account A's Drafts when A is the
        default. We locate the chosen account's delivery store and return
        its Drafts folder so the caller can Move() the saved item there.
        """
        if not address or self._app is None:
            return None
        try:
            ns = self._app.GetNamespace("MAPI")
            acc = self._account_by_address(address)
            # Account.DeliveryStore → that mailbox's own folder tree.
            store = getattr(acc, "DeliveryStore", None) if acc else None
            if store is not None:
                return store.GetDefaultFolder(_OL_FOLDER_DRAFTS)
            # Fallback: scan stores for one whose display name matches.
            for i in range(1, ns.Stores.Count + 1):
                st = ns.Stores.Item(i)
                if address.lower() in str(st.DisplayName or "").lower():
                    return st.GetDefaultFolder(_OL_FOLDER_DRAFTS)
        except Exception:  # noqa: BLE001
            pass
        return None

    def create(
        self, email: OutgoingEmail, *, send: bool, from_account: str = "",
    ) -> SendOutcome:
        """Create one message as a draft (send=False) or send it (send=True).

        `from_account` selects which configured Outlook account sends —
        when given and matched, the message goes out from that identity
        (SendUsingAccount). Missing attachment files fail THAT message
        only — the batch keeps going.
        """
        if self._app is None:
            raise OutlookUnavailableError("Outlook session not started.")
        try:
            mail = self._app.CreateItem(_OL_MAIL_ITEM)
            mail.BodyFormat = _OL_FORMAT_PLAIN
            acc = self._account_by_address(from_account)
            if acc is not None:
                # Set both the send account and the From display so the
                # message leaves as the chosen identity.
                try:
                    mail.SendUsingAccount = acc
                    mail.SentOnBehalfOfName = from_account
                except Exception:  # noqa: BLE001 — non-fatal; falls back to default
                    pass
            mail.To = email.to
            if email.cc:
                mail.CC = email.cc
            if email.bcc:
                mail.BCC = email.bcc
            mail.Subject = email.subject
            mail.Body = email.body
            for path in email.attachments:
                p = Path(path)
                if not p.is_file():
                    return SendOutcome(
                        to=email.to, status="FAILED",
                        error=f"Attachment not found: {p}",
                    )
                # Outlook wants an absolute string path.
                mail.Attachments.Add(str(p.resolve()))

            # Resolve recipient names against the address book so a bad
            # address surfaces now rather than bouncing later.
            try:
                mail.Recipients.ResolveAll()
            except Exception:  # noqa: BLE001 — non-fatal; Outlook still sends
                pass

            if send:
                mail.Send()
                return SendOutcome(to=email.to, status="SENT")
            mail.Save()  # lands in the DEFAULT account's Drafts
            # Move it into the chosen account's own Drafts so a draft
            # meant to send from a given account actually appears in that
            # mailbox, not whichever account is the Outlook default.
            if from_account:
                drafts = self._drafts_folder_for(from_account)
                if drafts is not None:
                    try:
                        mail = mail.Move(drafts)
                    except Exception:  # noqa: BLE001 — keep the draft where it is
                        log.warning(
                            "Couldn't move draft to %s Drafts; left in default.",
                            from_account,
                        )
            entry_id = ""
            try:
                entry_id = str(mail.EntryID)
            except Exception:  # noqa: BLE001
                pass
            return SendOutcome(to=email.to, status="DRAFTED", entry_id=entry_id)
        except Exception as exc:  # noqa: BLE001 — COM raises broad types
            log.exception("Outlook create failed for %s", email.to)
            return SendOutcome(
                to=email.to, status="FAILED", error=f"{type(exc).__name__}: {exc}",
            )


def list_outlook_accounts() -> list[str]:
    """Return the SMTP addresses of every account configured in Outlook.

    Empty list if Outlook isn't available. Used by the GUI's
    'Send from account' picker so the user chooses which identity sends.
    """
    try:
        with OutlookSession() as ol:
            if ol._app is None:
                return []
            ns = ol._app.GetNamespace("MAPI")
            out: list[str] = []
            for i in range(1, ns.Accounts.Count + 1):
                addr = str(ns.Accounts.Item(i).SmtpAddress or "").strip()
                if addr:
                    out.append(addr)
            return out
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# MX auto-detect — fill the right SMTP host from the sender's domain
# ---------------------------------------------------------------------------

# Known mail-host fingerprints → (preset label, smtp host, port, note).
_MX_FINGERPRINTS = (
    ("outlook.com", "Office 365 / Outlook.com", "smtp.office365.com", 587,
     "This domain is on Microsoft 365. SMTP is OFTEN BLOCKED by the tenant "
     "— if login fails, use the Outlook desktop transport instead."),
    ("google.com", "Gmail / Google Workspace", "smtp.gmail.com", 587,
     "This domain is on Google. Use an app password (2-step verification "
     "required)."),
    ("googlemail.com", "Gmail / Google Workspace", "smtp.gmail.com", 587, ""),
    ("yahoodns.net", "Yahoo Mail", "smtp.mail.yahoo.com", 587, ""),
    ("zoho.com", "Custom (enter host/port)", "smtp.zoho.com", 587, ""),
)


def detect_mail_host(email_or_domain: str, *, timeout: int = 8) -> dict | None:
    """Resolve a domain's MX records and map them to SMTP settings.

    Returns a dict {preset, host, port, note, mx} or None if nothing
    could be determined. Uses the system `nslookup` so we add no Python
    dependency. Best-effort: any failure → None (caller keeps manual entry).
    """
    import re
    import subprocess

    domain = email_or_domain.strip()
    if "@" in domain:
        domain = domain.split("@", 1)[1]
    domain = domain.strip().lower()
    if not domain or "." not in domain:
        return None
    try:
        proc = subprocess.run(
            ["nslookup", "-type=MX", domain],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return None
    text = (proc.stdout or "") + (proc.stderr or "")
    # Collect all 'mail exchanger = host' targets.
    mx_hosts = re.findall(r"mail exchanger\s*=\s*([^\s]+)", text, re.IGNORECASE)
    mx_hosts = [h.rstrip(".").lower() for h in mx_hosts]
    if not mx_hosts:
        return None
    joined = " ".join(mx_hosts)
    for needle, preset, host, port, note in _MX_FINGERPRINTS:
        if needle in joined:
            return {
                "preset": preset, "host": host, "port": port,
                "note": note, "mx": mx_hosts[0],
            }
    # Unknown provider: a reasonable guess is mail.<domain> but flag it.
    return {
        "preset": "Custom (enter host/port)",
        "host": "",
        "port": 587,
        "note": f"MX is {mx_hosts[0]} — provider not recognised; enter the "
                "SMTP host manually (often shown in your webmail settings).",
        "mx": mx_hosts[0],
    }


def outlook_available() -> bool:
    """Cheap probe: can we construct an Outlook COM session right now?"""
    try:
        with OutlookSession() as ol:
            return ol._app is not None
    except OutlookUnavailableError:
        return False
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# SMTP backend — provider-agnostic (Gmail, Workspace, Office 365, any host)
# ---------------------------------------------------------------------------

# Convenience presets so users don't memorise host/port. "Custom" lets
# them type their own. App-password guidance differs per provider; the
# GUI shows the right hint.
SMTP_PRESETS: dict[str, tuple[str, int]] = {
    "Gmail / Google Workspace": ("smtp.gmail.com", 587),
    "Office 365 / Outlook.com": ("smtp.office365.com", 587),
    "Yahoo Mail": ("smtp.mail.yahoo.com", 587),
    "Custom (enter host/port)": ("", 587),
}

# Credentials live in Windows Credential Manager via keyring, never on
# disk. Keyed by the sender address so multiple accounts coexist.
_KEYRING_SERVICE = "TravelOpsConsole.BulkMailer.SMTP"


class SMTPConfigError(MailerError):
    """SMTP host/credentials missing or malformed."""


class SMTPAuthError(MailerError):
    """Server rejected the username/password (often: need an app password)."""


def save_smtp_password(sender: str, password: str) -> None:
    """Persist the SMTP password for `sender` to the OS credential store."""
    import keyring
    keyring.set_password(_KEYRING_SERVICE, sender, password)


def load_smtp_password(sender: str) -> str | None:
    import keyring
    try:
        return keyring.get_password(_KEYRING_SERVICE, sender)
    except Exception:  # noqa: BLE001 — keyring backend hiccup shouldn't crash
        log.warning("keyring read failed for %s", sender)
        return None


def _build_mime(email: "OutgoingEmail", sender: str):
    """Compose a plain-text MIME message with attachments."""
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = email.to
    if email.cc:
        msg["Cc"] = email.cc
    # BCC is intentionally NOT added as a header (that's what makes it
    # blind) — it's only passed to the SMTP envelope recipient list.
    msg["Subject"] = email.subject
    msg.set_content(email.body)
    for path in email.attachments:
        p = Path(path)
        data = p.read_bytes()
        # Generic binary subtype keeps .xlsx/.pdf/etc. intact.
        msg.add_attachment(
            data, maintype="application", subtype="octet-stream", filename=p.name,
        )
    return msg


def _envelope_recipients(email: "OutgoingEmail") -> list[str]:
    """All addresses that actually receive the message (To + CC + BCC)."""
    out: list[str] = []
    for blob in (email.to, email.cc, email.bcc):
        for addr in re_split_addresses(blob):
            if addr and addr not in out:
                out.append(addr)
    return out


def re_split_addresses(blob: str) -> list[str]:
    """Split a 'a@x.com; b@y.com, c@z.com' string into clean addresses."""
    import re
    if not blob:
        return []
    return [a.strip() for a in re.split(r"[;,]", blob) if a.strip()]


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    sender: str
    password: str
    use_starttls: bool = True


class SMTPMailer:
    """Sends or drafts via SMTP. Works with any provider.

    `send()` dispatches over the wire. `draft()` writes a .eml file to a
    folder so the user can review (and even drag into any mail client)
    before sending — preserving the "review first" guarantee that
    Outlook draft-mode gives, but provider-independent.
    """

    def __init__(self, settings: SMTPSettings) -> None:
        if not settings.host or not settings.sender:
            raise SMTPConfigError("SMTP host and sender address are required.")
        self.s = settings
        self._conn = None

    def __enter__(self) -> "SMTPMailer":
        import smtplib
        try:
            conn = smtplib.SMTP(self.s.host, self.s.port, timeout=30)
            conn.ehlo()
            if self.s.use_starttls:
                conn.starttls()
                conn.ehlo()
            if self.s.password:
                conn.login(self.s.sender, self.s.password)
            self._conn = conn
        except smtplib.SMTPAuthenticationError as exc:
            raise SMTPAuthError(
                "SMTP login rejected. For Gmail/Workspace and Office 365 you "
                "usually need an APP PASSWORD (not your normal password), with "
                "2-step verification enabled."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise SMTPConfigError(
                f"Couldn't connect to {self.s.host}:{self.s.port} — {exc}"
            ) from exc
        return self

    def __exit__(self, *_exc) -> None:
        if self._conn is not None:
            try:
                self._conn.quit()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def send(self, email: "OutgoingEmail") -> SendOutcome:
        if self._conn is None:
            raise SMTPConfigError("SMTP not connected.")
        try:
            msg = _build_mime(email, self.s.sender)
            recipients = _envelope_recipients(email)
            if not recipients:
                return SendOutcome(to=email.to, status="FAILED", error="No recipients")
            self._conn.send_message(msg, from_addr=self.s.sender, to_addrs=recipients)
            return SendOutcome(to=email.to, status="SENT")
        except FileNotFoundError as exc:
            return SendOutcome(to=email.to, status="FAILED", error=f"Attachment: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("SMTP send failed for %s", email.to)
            return SendOutcome(to=email.to, status="FAILED", error=f"{type(exc).__name__}: {exc}")

    def draft(self, email: "OutgoingEmail", out_dir: Path) -> SendOutcome:
        """Write the composed message as a .eml file for review."""
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            msg = _build_mime(email, self.s.sender)
            if email.bcc:
                # For a review file we DO show BCC so the reviewer sees it.
                msg["Bcc"] = email.bcc
            safe = "".join(c for c in email.to if c.isalnum() or c in "._@-")
            path = out_dir / f"{safe or 'email'}.eml"
            path.write_bytes(bytes(msg))
            return SendOutcome(to=email.to, status="DRAFTED", entry_id=str(path))
        except FileNotFoundError as exc:
            return SendOutcome(to=email.to, status="FAILED", error=f"Attachment: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("SMTP draft failed for %s", email.to)
            return SendOutcome(to=email.to, status="FAILED", error=f"{type(exc).__name__}: {exc}")
