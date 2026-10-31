"""Attaching local files and keeping attachments, from create_draft through guarded send."""

import asyncio
import base64
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

import pytest
from attachment_fixtures import PNG, FakeImap, client, mail_with, part, pdf_bytes
from attachment_fixtures import settings as settings  # noqa: F401
from test_mail import FakeMessage, unquote
from test_send import DRAFT_ID, RECIPIENT, SENDER, Host, redeem
from test_send import local_certificate as local_certificate
from test_send import rig as rig
from test_send_reader import response
from view_assertions import view_data
from view_browser import browser, requests, run_view, tool_result

from proton_mail_mcp import attachments as attachments_module
from proton_mail_mcp import mcp_apps, views
from proton_mail_mcp.action_log import ActionLog
from proton_mail_mcp.mail import MailClient, MailValidationError, draft_audit
from proton_mail_mcp.security import Credentials
from proton_mail_mcp.send import SendService
from proton_mail_mcp.server import create_mcp

SOURCE = "<source@example.test>"


def parsed(raw):
    return BytesParser(policy=policy.SMTP).parsebytes(raw)


def files_of(raw):
    return [
        (item.get_filename(), item.get_content_type(), item.get_payload(decode=True))
        for item in parsed(raw).iter_attachments()
    ]


class Mailbox(FakeImap):
    """Serves a source message and stores each appended draft for the send path."""

    def __init__(self, source=None, *, drafts_uid="7"):
        super().__init__({"INBOX": {"3": source} if source else {}, "Drafts": {}})
        self.drafts_uid = drafts_uid

    def append(self, folder, flags, internaldate, payload):
        result = super().append(folder, flags, internaldate, payload)
        stored = FakeMessage(payload, b"", {})
        self.folders[unquote(folder)] = {self.drafts_uid: stored}
        return result

    def uid(self, command, *args):
        if command == "FETCH" and "BODY.PEEK[]" in args[1]:
            self.calls.append((command, *args))
            return "OK", response(self.folders[self.current][args[0]].headers)
        return super().uid(command, *args)


def sending_client(rig, monkeypatch, folder=None, source=None):
    """A real MailClient over the double, sharing the rig's token store and SMTP recorder."""
    configured = replace(rig.settings, attachments_dir=str(folder) if folder else None)
    fake = Mailbox(source)
    mail = MailClient(configured, credentials_provider=Mock(side_effect=AssertionError))

    @contextmanager
    def session(deadline=None):
        yield fake, Credentials(SENDER, "synthetic-password")

    monkeypatch.setattr(mail, "_session", session)
    monkeypatch.setattr("proton_mail_mcp.mail.make_msgid", lambda: DRAFT_ID)
    mail.credentials_provider = rig.client.credentials_provider
    service = SendService(mail, configured, tokens=rig.tokens, log=rig.log)
    rig.service = service
    return mail, fake, service


@pytest.fixture
def folder(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "report.pdf").write_bytes(b"%PDF-1.4 report bytes \x00\xff")
    (allowed / "rows.csv").write_bytes(b"a,b\r\n1,2\r\n")
    return allowed


def test_a_local_file_goes_through_the_draft_preview_and_form_to_smtp(rig, monkeypatch, folder):
    mail, fake, service = sending_client(rig, monkeypatch, folder)
    created = mail.create_draft(
        RECIPIENT, subject="Report", body="Attached.", attachments=["report.pdf", "rows.csv"]
    )
    assert created == DRAFT_ID
    [(_, flags, _, raw)] = fake.appended
    assert flags == r"(\Draft)"
    message = parsed(raw)
    assert message.defects == []
    assert message.get_body(("plain",)).get_content().strip() == "Attached."
    data = (folder / "report.pdf").read_bytes()
    assert files_of(raw) == [
        ("report.pdf", "application/pdf", data),
        ("rows.csv", "text/csv", b"a,b\r\n1,2\r\n"),
    ]
    assert not rig.smtp.calls, "creating a draft never reaches SMTP"

    preview = service.preview_send(created)
    assert preview["approval"] == "host_confirmation"
    assert [(file["name"], file["size"]) for file in preview["attachments"]] == [
        ("report.pdf", len(data)),
        ("rows.csv", 10),
    ]
    host = Host()
    assert redeem(rig, preview["token"], host)["sent"] is True
    assert len(host.prompts) == 1
    _, _, delivered = rig.smtp.sent[0]
    assert files_of(delivered)[0] == ("report.pdf", "application/pdf", data)


# As reported, one PDF: 29218 bytes, stored as base64 in lines of 76 characters.
REPORTED_PDF = b"%PDF-1.4 " + bytes(29218 - 9)
REPORTED_OCTETS = len(base64.encodebytes(REPORTED_PDF).strip().replace(b"\n", b"\r\n"))


def test_the_list_labels_its_encoded_size_and_the_preview_shows_the_decoded_one(rig, monkeypatch):
    data, octets = REPORTED_PDF, REPORTED_OCTETS
    assert octets == 39984
    source = mail_with(SOURCE, part("report.pdf", "application/pdf", data, size=octets))
    mail, _, service = sending_client(rig, monkeypatch, source=source)
    listed = mail.list_attachments(SOURCE)
    assert listed["attachments"][0]["size"] == octets
    assert "transfer-encoded" in listed["size_note"] and "decoded" in listed["size_note"]
    forwarded = mail.forward_message(SOURCE, RECIPIENT, attachments=[1])
    assert forwarded["attachments"][0]["size"] == octets
    assert forwarded["size_note"] == listed["size_note"]
    created = mail.create_draft(
        RECIPIENT, subject="S", body="B", attachments_from=SOURCE, attachment_indexes=[1]
    )
    preview = service.preview_send(created)
    assert preview["attachments"][0]["size"] == len(data)
    assert files_of(mail.read_draft(created)["raw"])[0][2] == data
    host = Host()
    assert redeem(rig, preview["token"], host)["sent"] is True
    assert f'"report.pdf" ("application/pdf", {len(data)} bytes)' in host.prompts[0]


SIZES = """
await t.waitFor(() => t.doc.querySelector(".chip"), "the files");
t.check("chips", Array.from(t.doc.querySelectorAll(".chip")).map((chip) => chip.textContent));
"""


@browser
def test_every_view_marks_the_encoded_size_of_one_file(rig, monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "report.pdf").write_bytes(REPORTED_PDF)
    source = mail_with(
        SOURCE, part("report.pdf", "application/pdf", REPORTED_PDF, size=REPORTED_OCTETS)
    )
    mail, _, service = sending_client(rig, monkeypatch, allowed, source=source)
    read = mail.read_thread(SOURCE, include_thread=False)
    local = mail.local_attachments(["report.pdf"])
    created = mail.create_draft(RECIPIENT, subject="S", body="B", attachments=["report.pdf"])

    def chips(name, shown):
        result = tool_result(mcp_apps.view_result(*shown))
        return run_view(tmp_path, name, result, SIZES)["checks"]["chips"]

    encoded = "report.pdfapplication/pdf · 39 KB encoded"
    decoded = "report.pdfapplication/pdf · 29 KB"
    assert chips("thread", views.thread_view(read, SOURCE)) == [encoded]
    assert chips("reply", views.reply_view(read, SOURCE, own={SENDER}, local=local)) == [
        decoded,
        encoded,
    ]
    assert chips("reply", views.reply_view(read, SOURCE, own={SENDER}, edit=True)) == [encoded]
    assert chips("review_send", views.review_view(service.review_send(created))) == [decoded]


def test_an_attachment_changed_after_the_preview_refuses_delivery(rig, monkeypatch, folder):
    mail, fake, service = sending_client(rig, monkeypatch, folder)
    created = mail.create_draft(RECIPIENT, subject="R", body="B", attachments=["report.pdf"])
    preview = service.preview_send(created)
    stored = fake.folders["Drafts"]["7"]
    (folder / "report.pdf").write_bytes(b"different")
    mail.create_draft(RECIPIENT, subject="R", body="B", attachments=["report.pdf"])
    assert fake.folders["Drafts"]["7"].headers != stored.headers
    with pytest.raises(MailValidationError, match="changed"):
        redeem(rig, preview["token"], Host())
    assert rig.smtp.sent == []


def test_declining_the_form_sends_no_attachment(rig, monkeypatch, folder):
    mail, _, service = sending_client(rig, monkeypatch, folder)
    created = mail.create_draft(RECIPIENT, subject="R", body="B", attachments=["report.pdf"])
    preview = service.preview_send(created)
    with pytest.raises(MailValidationError):
        redeem(rig, preview["token"], Host("decline"))
    assert rig.smtp.sent == []


@pytest.mark.parametrize(
    "hostile,expected",
    [
        ('quote"semi;colon=1.pdf', 'quote"semi;colon=1.pdf'),
        ("Grüße und 日本語.pdf", "Grüße und 日本語.pdf"),
        ("a" * 300 + ".pdf", "a" * 196 + ".pdf"),
        ("spaced  name (final).pdf", "spaced  name (final).pdf"),
    ],
)
def test_sender_names_survive_in_a_kept_attachment_and_stay_sendable(
    rig, monkeypatch, hostile, expected
):
    source = mail_with(SOURCE, part("placeholder", "application/pdf", b"%PDF kept"))
    mail, fake, service = sending_client(rig, monkeypatch, source=source)
    named = {"filename": hostile, "type": "application/pdf", "charset": "utf-8"}
    monkeypatch.setattr(
        attachments_module, "_fetch_parts", lambda *a, **k: [(1, named, b"%PDF kept")]
    )
    created = mail.create_draft(
        RECIPIENT, subject="S", body="B", attachments_from=SOURCE, attachment_indexes=[1]
    )
    [(_, _, _, raw)] = fake.appended
    assert files_of(raw) == [(expected, "application/pdf", b"%PDF kept")]
    assert service.preview_send(created)["attachments"][0]["name"] == expected


def test_non_ascii_text_and_names_survive_to_the_form_and_smtp(rig, monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "Übersicht 日本語.csv").write_bytes(b"a,b")
    mail, fake, service = sending_client(rig, monkeypatch, allowed)
    created = mail.create_draft(
        RECIPIENT,
        subject="Résumé 日本語",
        body="Grüße an alle\n" + "lange Zeile " * 40,
        attachments=["Übersicht 日本語.csv"],
    )
    [(_, _, _, raw)] = fake.appended
    assert parsed(raw).defects == []
    assert files_of(raw) == [("Übersicht 日本語.csv", "text/csv", b"a,b")]
    preview = service.preview_send(created)
    assert preview["subject"] == "Résumé 日本語"
    assert preview["attachments"][0]["name"] == "Übersicht 日本語.csv"
    review = service.review_send(created)
    assert review["body_preview"]["text"].startswith("Grüße an alle")
    host = Host()
    assert redeem(rig, preview["token"], host)["sent"] is True
    # The form's first line shows the name's own letters; its JSON details below stay ASCII.
    first, details = host.prompts[0].split("\n")
    assert '"Übersicht 日本語.csv"' in first
    assert (
        details.isascii()
        and json.loads(details)["attachments"][0]["name"] == preview["attachments"][0]["name"]
    )
    assert files_of(rig.smtp.sent[0][2])[0][0] == "Übersicht 日本語.csv"


def test_a_draft_keeps_chosen_attachments_byte_for_byte(rig, monkeypatch):
    first = bytes(range(256)) * 40
    source = mail_with(
        SOURCE,
        part("first.bin", "application/octet-stream", first),
        part("skip.txt", "text/plain", b"not chosen"),
        part("third.png", "image/png", PNG),
    )
    mail, fake, service = sending_client(rig, monkeypatch, source=source)
    created = mail.create_draft(
        RECIPIENT, subject="S", body="B", attachments_from=SOURCE, attachment_indexes=[1, 3]
    )
    [(_, _, _, raw)] = fake.appended
    assert files_of(raw) == [
        ("first.bin", "application/octet-stream", first),
        ("third.png", "image/png", PNG),
    ]
    assert len(service.preview_send(created)["attachments"]) == 2


@pytest.mark.parametrize(
    "arguments,error",
    [
        ({"attachments_from": SOURCE}, "go together"),
        ({"attachment_indexes": [1]}, "go together"),
        ({"attachments_from": SOURCE, "attachment_indexes": [9]}, "No attachment with that index"),
        ({"attachments_from": SOURCE, "attachment_indexes": [1, 1]}, "distinct positive"),
        ({"attachments_from": SOURCE, "attachment_indexes": [0]}, "distinct positive"),
        ({"attachments_from": "<absent@x.test>", "attachment_indexes": [1]}, "Message not found"),
        ({"attachments": ["a.txt", "a.txt"]}, "distinct file names"),
        ({"attachments": "a.txt"}, "distinct file names"),
    ],
)
def test_bad_attachment_arguments_refuse_and_no_draft_is_written(
    rig, monkeypatch, tmp_path, arguments, error
):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "a.txt").write_bytes(b"x")
    source = mail_with(SOURCE, part("a.txt", "text/plain", b"x"))
    mail, fake, _ = sending_client(rig, monkeypatch, allowed, source)
    with pytest.raises(MailValidationError, match=error):
        mail.create_draft(RECIPIENT, subject="S", body="B", **arguments)
    assert fake.appended == []


def test_too_many_attachments_refuse(rig, monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    names = [f"f{index}.txt" for index in range(21)]
    for name in names:
        (allowed / name).write_bytes(b"x")
    mail, fake, _ = sending_client(rig, monkeypatch, allowed)
    with pytest.raises(MailValidationError, match="at most 20 attachments"):
        mail.create_draft(RECIPIENT, subject="S", body="B", attachments=names)
    assert fake.appended == []


def test_the_upload_cap_counts_all_of_a_drafts_attachments(rig, monkeypatch, tmp_path):
    monkeypatch.setattr(attachments_module, "MAX_UPLOAD_BYTES", 10)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "a.txt").write_bytes(b"x" * 6)
    (allowed / "b.txt").write_bytes(b"x" * 6)
    source = mail_with(SOURCE, part("c.txt", "text/plain", b"y" * 6))
    mail, fake, _ = sending_client(rig, monkeypatch, allowed, source)
    with pytest.raises(MailValidationError, match="limit of one draft"):
        mail.create_draft(RECIPIENT, subject="S", body="B", attachments=["a.txt", "b.txt"])
    with pytest.raises(MailValidationError, match="limit"):
        mail.create_draft(
            RECIPIENT,
            subject="S",
            body="B",
            attachments=["a.txt"],
            attachments_from=SOURCE,
            attachment_indexes=[1],
        )
    assert fake.appended == []


def test_the_whole_draft_must_stay_within_the_send_limit(rig, monkeypatch, folder):
    monkeypatch.setattr("proton_mail_mcp.mail.MAX_DRAFT_BYTES", 2048)
    mail, fake, _ = sending_client(rig, monkeypatch, folder)
    with pytest.raises(MailValidationError, match="10 MiB send limit"):
        mail.create_draft(RECIPIENT, subject="S", body="x" * 4096, attachments=["rows.csv"])
    assert fake.appended == []
    # A plain draft keeps its old behaviour: preview refuses an oversized one.
    mail.create_draft(RECIPIENT, subject="S", body="x" * 4096)
    assert len(fake.appended) == 1


def test_a_copied_message_or_multipart_part_stays_an_opaque_file():
    fresh = BytesParser(policy=policy.SMTP).parsebytes(b"Subject: x\r\n\r\nbody\r\n")
    attachments_module.add_files(
        fresh,
        [
            {
                "name": "note.eml",
                "type": "message/rfc822",
                "charset": None,
                "data": b"X: y\r\n\r\nz",
            },
            {"name": "odd", "type": "not a type", "charset": None, "data": b"odd"},
            {"name": "t.txt", "type": "text/plain", "charset": "latin-1", "data": b"\xe4"},
            {"name": "u.txt", "type": "text/plain", "charset": 'x"; y=1', "data": b"u"},
        ],
    )
    rows = list(fresh.iter_attachments())
    assert [row.get_content_type() for row in rows[:2]] == ["application/octet-stream"] * 2
    assert rows[0].get_payload(decode=True) == b"X: y\r\n\r\nz"
    assert rows[2].get_param("charset") == "latin-1"
    assert rows[3].get_param("charset") is None


def test_the_audit_counts_attachments_without_names():
    described = draft_audit(
        "a@b.test", None, "S", "B", attachments=["secret-name.pdf"], attachment_indexes=[1, 2]
    )
    assert described["attachment_count"] == 3
    assert "secret-name" not in json.dumps(described)
    assert "attachment_count" not in draft_audit("a@b.test", None, "S", "B")


def server_for(rig_settings, mail):
    return create_mcp(replace(rig_settings, confirm_mutations=False), mail)


def call(mcp, name, arguments):
    return asyncio.run(mcp._tool_manager.call_tool(name, arguments, convert_result=True))


def test_create_draft_tool_attaches_only_named_options(tmp_path, settings):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "a.txt").write_bytes(b"attached")
    configured = replace(
        settings,
        token="t" * 40,
        attachments_dir=str(allowed),
        confirm_mutations=False,
        action_log_path=str(tmp_path / "log.jsonl"),
    )
    fake = FakeImap({"Drafts": {}})
    mcp = create_mcp(configured, client(configured, fake))
    call(
        mcp,
        "create_draft",
        {"to": "x@example.com", "subject": "S", "body": "B", "attachments": ["a.txt"]},
    )
    [(_, _, _, raw)] = fake.appended
    assert files_of(raw) == [("a.txt", "text/plain", b"attached")]
    with ActionLog(configured.action_log_path).transaction(readonly=True) as transaction:
        finished = [row for row in transaction.records() if row.get("action") == "audit"][-1]
    assert finished["outcome"] == "completed" and finished["attachment_count"] == 1
    assert "a.txt" not in json.dumps(finished)
    with pytest.raises(Exception, match="plain relative paths"):
        call(
            mcp,
            "create_draft",
            {"to": "x@example.com", "subject": "S", "body": "B", "attachments": ["../a.txt"]},
        )
    assert len(fake.appended) == 1


def test_compose_reply_lists_local_files_before_they_are_saved(tmp_path, settings):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "plan.pdf").write_bytes(pdf_bytes("Plan"))
    configured = replace(
        settings,
        token="t" * 40,
        attachments_dir=str(allowed),
        confirm_mutations=False,
        action_log_path=str(tmp_path / "log.jsonl"),
    )
    fake = FakeImap({"Drafts": {}})
    mcp = create_mcp(configured, client(configured, fake))
    result = call(mcp, "compose_reply", {"to": "x@example.com", "attachments": ["plan.pdf"]})
    view = view_data(result)
    assert view["draft"]["attachments"] == ["plan.pdf"]
    size = (allowed / "plan.pdf").stat().st_size
    assert view["attached"] == [
        {
            "source": "local",
            "name": "plan.pdf",
            "type": "application/pdf",
            "size": size,
            "checked": True,
        }
    ]
    assert fake.appended == [] and fake.calls == []
    with pytest.raises(Exception, match="No readable regular file"):
        call(mcp, "compose_reply", {"to": "x@example.com", "attachments": ["absent.pdf"]})


def test_a_read_reports_a_messages_flags_only_when_asked(settings):
    draft = mail_with(
        "<draft@example.com>", part("kept.txt", "text/plain", b"kept"), flags={r"\Draft"}
    )
    fake = FakeImap({"INBOX": {"1": draft}})
    reader = client(settings, fake)
    [item] = reader.read_thread("<draft@example.com>", include_thread=False, include_flags=True)[
        "messages"
    ]
    assert item["flags"] == [r"\Draft"]
    assert (
        "flags"
        not in reader.read_thread("<draft@example.com>", include_thread=False)["messages"][0]
    )
    with pytest.raises(MailValidationError, match="include_flags must be a boolean"):
        reader.read_thread("<draft@example.com>", include_flags="yes")


TICKS = """
const doc = t.doc;
await t.waitFor(() => doc.getElementById("to").value, "the prefilled fields");
const save = doc.getElementById("save");
const chips = () => Array.from(doc.querySelectorAll("#attached .chip"));
t.check("row", t.visible(doc.getElementById("attached-row")));
t.check("shown", chips().map((chip) => chip.textContent));
t.check("ticked", chips().map((chip) => chip.querySelector("input").checked));
// Change the choice, then put it back with Reset.
chips()[0].querySelector("input").click();
t.check("after_toggle", chips().map((chip) => chip.querySelector("input").checked));
doc.getElementById("reset").click();
t.check("after_reset", chips().map((chip) => chip.querySelector("input").checked));
chips()[0].querySelector("input").click();
save.click();
await t.waitFor(() => doc.querySelector("#saved .banner.ok"), "the saved draft");
await t.sleep(50);
t.check("disabled_after_save", save.disabled);
chips()[0].querySelector("input").click();
t.check("enabled_after_change", !save.disabled);
"""


def composer_result(attachments, *, edit, local=None):
    entry = {
        "message_id": "<source@example.test>",
        "from": "Alice <alice@example.test>",
        "to": "reader@example.test",
        "subject": "Plans",
        "date": "Wed, 28 Oct 2026 10:00:00 +0000",
        "headers": {},
        "text": "Text",
        "attachments": attachments,
        "truncated": False,
    }
    result = {"messages": [entry], "partial": False}
    return tool_result(
        mcp_apps.view_result(
            *views.reply_view(
                result,
                "<source@example.test>",
                own={"reader@example.test"},
                edit=edit,
                local=local,
            )
        )
    )


SAVED = {
    "content": [{"type": "text", "text": "<new@example.test>"}],
    "structuredContent": {"result": "<new@example.test>"},
}
FILES = [
    {"filename": "plan.pdf", "type": "application/pdf", "size": 2048},
    {"filename": "image.png", "type": "image/png", "size": 10},
]


@browser
def test_the_composer_saves_the_attachments_left_ticked(tmp_path):
    report = run_view(
        tmp_path,
        "reply",
        composer_result(FILES, edit=True),
        TICKS,
        tools={"create_draft": SAVED},
    )
    checks = report["checks"]
    assert checks["row"] is True
    assert checks["shown"] == [
        "plan.pdfapplication/pdf · 2.0 KB encoded",
        "image.pngimage/png · 10 B encoded",
    ]
    # An edit keeps everything unless the user unticks it; Reset restores the choice.
    assert checks["ticked"] == [True, True]
    assert checks["after_toggle"] == [False, True]
    assert checks["after_reset"] == [True, True]
    [call] = requests(report, "tools/call")
    arguments = call["params"]["arguments"]
    assert arguments["attachments_from"] == "<source@example.test>"
    assert arguments["attachment_indexes"] == [2]
    assert "attachments" not in arguments
    # Changing the ticks after a save leaves unsaved changes.
    assert checks["disabled_after_save"] is True and checks["enabled_after_change"] is True


@browser
def test_a_replys_composer_keeps_nothing_until_it_is_ticked(tmp_path):
    report = run_view(
        tmp_path,
        "reply",
        composer_result(
            FILES,
            edit=False,
            local=[{"name": "mine.csv", "type": "text/csv", "size": 3}],
        ),
        TICKS,
        tools={"create_draft": SAVED},
    )
    checks = report["checks"]
    assert checks["ticked"] == [True, False, False]
    [call] = requests(report, "tools/call")
    arguments = call["params"]["arguments"]
    # The scenario unticks the first row, the local file, so nothing is attached.
    assert not {"attachments", "attachments_from", "attachment_indexes"} & set(arguments)


def test_sha256_of_a_kept_file_is_in_the_preview(rig, monkeypatch, folder):
    mail, _, service = sending_client(rig, monkeypatch, folder)
    created = mail.create_draft(RECIPIENT, subject="R", body="B", attachments=["rows.csv"])
    [file] = service.preview_send(created)["attachments"]
    assert file["sha256"] == hashlib.sha256(b"a,b\r\n1,2\r\n").hexdigest()
