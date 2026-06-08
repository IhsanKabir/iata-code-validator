"""Tests for the Bulk Mailer: mapping reader, templating, send-log.

COM/Outlook is never touched here — those paths are Windows-desktop
integration only. We test the pure logic that decides WHO gets WHAT and
WHETHER a row already went out.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from src.mailer_io import MailRow, read_mapping, render_template
from src.mailer_log import MailerLog


# ---------------------------------------------------------------------------
# render_template
# ---------------------------------------------------------------------------


def test_render_fills_known_placeholders():
    body, missing = render_template(
        "Dear {name}, route {Route}.", {"name": "Karim", "route": "DAC-DXB"},
    )
    assert body == "Dear Karim, route DAC-DXB."
    assert missing == []


def test_render_case_insensitive():
    body, missing = render_template("Hi {NAME}", {"name": "Nila"})
    assert body == "Hi Nila"
    assert missing == []


def test_render_leaves_unknown_and_reports():
    body, missing = render_template("Hi {name}, {oops}", {"name": "X"})
    assert body == "Hi X, {oops}"
    assert missing == ["oops"]


# ---------------------------------------------------------------------------
# read_mapping
# ---------------------------------------------------------------------------


def _mapping(tmp_path, rows, header=("Email", "Name", "File", "CC", "BCC")):
    p = tmp_path / "map.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(list(header))
    for r in rows:
        ws.append(list(r))
    wb.save(p)
    return p


def _touch(tmp_path, name):
    f = tmp_path / name
    f.write_bytes(b"x")
    return f


def test_read_mapping_resolves_files_and_flags_missing(tmp_path):
    attach = tmp_path / "files"
    attach.mkdir()
    _touch(attach, "a.xlsx")
    p = _mapping(tmp_path, [
        ("ops@x.com", "Karim", "a.xlsx", "", ""),
        ("rm@y.com", "Nila", "ghost.xlsx", "", ""),   # missing file
        ("bad-email", "Z", "a.xlsx", "", ""),          # invalid email
    ])
    rows, warnings = read_mapping(p, attach)
    assert warnings == []
    assert len(rows) == 3
    ok, miss, bad = rows
    assert ok.is_valid and ok.attachments[0].name == "a.xlsx"
    assert "file not found: ghost.xlsx" in miss.issues
    assert any("invalid email" in i for i in bad.issues)


def test_read_mapping_requires_email_and_file_columns(tmp_path):
    p = _mapping(tmp_path, [("Karim",)], header=("Name",))
    rows, warnings = read_mapping(p, tmp_path)
    assert any("Email" in w for w in warnings)
    assert any("File" in w for w in warnings)


def test_read_mapping_multi_attachment_split(tmp_path):
    attach = tmp_path / "f"
    attach.mkdir()
    _touch(attach, "a.xlsx")
    _touch(attach, "b.xlsx")
    p = _mapping(tmp_path, [("ops@x.com", "K", "a.xlsx; b.xlsx", "", "")])
    rows, _ = read_mapping(p, attach)
    assert [pp.name for pp in rows[0].attachments] == ["a.xlsx", "b.xlsx"]


def test_read_mapping_carries_all_columns_for_templating(tmp_path):
    attach = tmp_path / "f"
    attach.mkdir()
    _touch(attach, "a.xlsx")
    p = _mapping(
        tmp_path,
        [("ops@x.com", "Karim", "a.xlsx", "", "", "DAC-DXB")],
        header=("Email", "Name", "File", "CC", "BCC", "Route"),
    )
    rows, _ = read_mapping(p, attach)
    body, missing = render_template("{name} / {route}", rows[0].fields)
    assert body == "Karim / DAC-DXB"
    assert missing == []


def test_read_mapping_skips_blank_rows(tmp_path):
    attach = tmp_path / "f"
    attach.mkdir()
    _touch(attach, "a.xlsx")
    p = _mapping(tmp_path, [
        ("ops@x.com", "K", "a.xlsx", "", ""),
        ("", "", "", "", ""),  # blank → skipped
    ])
    rows, _ = read_mapping(p, attach)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# MailerLog
# ---------------------------------------------------------------------------


def test_log_records_and_detects_sent(tmp_path):
    log = MailerLog(tmp_path / "m.sqlite")
    camp, email, subj = "june.xlsx", "ops@x.com", "Report"
    assert not log.already_sent(camp, email, subj)
    log.record(camp, email, subj, "SENT")
    assert log.already_sent(camp, email, subj)
    assert log.sent_count(camp) == 1


def test_log_drafted_does_not_count_as_sent(tmp_path):
    log = MailerLog(tmp_path / "m.sqlite")
    log.record("c", "a@b.com", "S", "DRAFTED")
    assert not log.already_sent("c", "a@b.com", "S")  # drafts can be re-run
    assert log.sent_count("c") == 0


def test_log_campaign_isolation(tmp_path):
    log = MailerLog(tmp_path / "m.sqlite")
    log.record("camp1", "a@b.com", "S", "SENT")
    assert log.already_sent("camp1", "a@b.com", "S")
    assert not log.already_sent("camp2", "a@b.com", "S")  # different campaign


def test_log_clear_campaign(tmp_path):
    log = MailerLog(tmp_path / "m.sqlite")
    log.record("c", "a@b.com", "S", "SENT")
    log.clear_campaign("c")
    assert not log.already_sent("c", "a@b.com", "S")


# ---------------------------------------------------------------------------
# SMTP backend: MIME build, envelope recipients, .eml draft
# ---------------------------------------------------------------------------

from email import message_from_bytes

from src.mailer_client import (
    OutgoingEmail,
    SMTPSettings,
    SMTPMailer,
    _build_mime,
    _envelope_recipients,
    re_split_addresses,
)


def _email(tmp_path, **kw):
    f = tmp_path / "rep.xlsx"
    f.write_bytes(b"PK\x03\x04 fake xlsx")
    defaults = dict(
        to="ops@x.com", subject="Report", body="Hi {name}",
        attachments=(f,), cc="", bcc="",
    )
    defaults.update(kw)
    return OutgoingEmail(**defaults)


def test_build_mime_headers_and_attachment(tmp_path):
    msg = _build_mime(_email(tmp_path, cc="m@x.com"), "me@send.com")
    assert msg["From"] == "me@send.com"
    assert msg["To"] == "ops@x.com"
    assert msg["Cc"] == "m@x.com"
    # BCC is never a header (keeps it blind)
    assert msg["Bcc"] is None
    atts = [p for p in msg.iter_attachments()]
    assert len(atts) == 1
    assert atts[0].get_filename() == "rep.xlsx"


def test_envelope_includes_to_cc_bcc(tmp_path):
    e = _email(tmp_path, to="a@x.com", cc="b@x.com; c@x.com", bcc="d@x.com")
    rcpts = _envelope_recipients(e)
    assert set(rcpts) == {"a@x.com", "b@x.com", "c@x.com", "d@x.com"}


def test_envelope_dedups(tmp_path):
    e = _email(tmp_path, to="a@x.com", cc="a@x.com", bcc="")
    assert _envelope_recipients(e) == ["a@x.com"]


def test_re_split_addresses():
    assert re_split_addresses("a@x.com; b@y.com, c@z.com") == [
        "a@x.com", "b@y.com", "c@z.com",
    ]
    assert re_split_addresses("") == []


def test_smtp_draft_writes_eml(tmp_path):
    out = tmp_path / "drafts"
    s = SMTPSettings(host="smtp.x.com", port=587, sender="me@send.com", password="pw")
    # draft() doesn't open a connection, so call it on a bare instance.
    mailer = SMTPMailer.__new__(SMTPMailer)
    mailer.s = s
    mailer._conn = None
    outcome = mailer.draft(_email(tmp_path, bcc="boss@x.com"), out)
    assert outcome.status == "DRAFTED"
    eml = Path(outcome.entry_id)
    assert eml.exists() and eml.suffix == ".eml"
    parsed = message_from_bytes(eml.read_bytes())
    assert parsed["To"] == "ops@x.com"
    assert parsed["Bcc"] == "boss@x.com"   # shown in review file


def test_smtp_settings_requires_host_and_sender():
    import pytest as _p
    from src.mailer_client import SMTPConfigError
    with _p.raises(SMTPConfigError):
        SMTPMailer(SMTPSettings(host="", port=587, sender="me@x.com", password="p"))
    with _p.raises(SMTPConfigError):
        SMTPMailer(SMTPSettings(host="h", port=587, sender="", password="p"))


# ---------------------------------------------------------------------------
# MX auto-detect (mocked nslookup — no network)
# ---------------------------------------------------------------------------

from src import mailer_client as _mc


class _Proc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""


def _patch_nslookup(monkeypatch, stdout):
    """detect_mail_host does `import subprocess` locally → patch the stdlib."""
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(stdout))


def test_detect_mail_host_office365(monkeypatch):
    _patch_nslookup(
        monkeypatch,
        "acme.com  mail exchanger = acme-com.mail.protection.outlook.com",
    )
    info = _mc.detect_mail_host("user@acme.com")
    assert info["preset"] == "Office 365 / Outlook.com"
    assert info["host"] == "smtp.office365.com"
    assert "Microsoft 365" in info["note"]


def test_detect_mail_host_google(monkeypatch):
    _patch_nslookup(monkeypatch, "x.com  mail exchanger = alt1.aspmx.l.google.com")
    info = _mc.detect_mail_host("a@x.com")
    assert info["host"] == "smtp.gmail.com"


def test_detect_mail_host_unknown_provider(monkeypatch):
    _patch_nslookup(monkeypatch, "x.com  mail exchanger = mail.someisp.net")
    info = _mc.detect_mail_host("a@x.com")
    assert info["host"] == ""           # not recognised → manual entry
    assert "someisp" in info["note"]


def test_detect_mail_host_bad_input():
    assert _mc.detect_mail_host("notanemail") is None
    assert _mc.detect_mail_host("") is None


# ---------------------------------------------------------------------------
# Graph backend: message JSON build (no network, no MSAL)
# ---------------------------------------------------------------------------

from src import graph_mailer as _gm


def _graph_sess():
    s = _gm.GraphSession.__new__(_gm.GraphSession)
    s.access_token = "tok"
    s.account = "user@acme.com"
    s._app = None
    return s


def test_graph_message_json_recipients_and_attachment(tmp_path):
    f = tmp_path / "rep.xlsx"
    f.write_bytes(b"PK\x03\x04data")
    msg = _graph_sess()._message_json(OutgoingEmail(
        to="a@x.com", subject="S", body="Hi", attachments=(f,),
        cc="c1@x.com; c2@x.com", bcc="b@x.com",
    ))
    assert msg["subject"] == "S"
    assert msg["body"] == {"contentType": "Text", "content": "Hi"}
    assert msg["toRecipients"] == [{"emailAddress": {"address": "a@x.com"}}]
    assert [r["emailAddress"]["address"] for r in msg["ccRecipients"]] == ["c1@x.com", "c2@x.com"]
    assert msg["bccRecipients"] == [{"emailAddress": {"address": "b@x.com"}}]
    att = msg["attachments"][0]
    assert att["@odata.type"] == "#microsoft.graph.fileAttachment"
    assert att["name"] == "rep.xlsx"
    import base64
    assert base64.b64decode(att["contentBytes"]) == b"PK\x03\x04data"


def test_graph_message_json_omits_empty_cc_bcc(tmp_path):
    msg = _graph_sess()._message_json(OutgoingEmail(
        to="a@x.com", subject="S", body="B", attachments=(),
    ))
    assert "ccRecipients" not in msg
    assert "bccRecipients" not in msg
    assert "attachments" not in msg
