"""The key safety point: a person sees the From address and every file before approving."""

import json
from email import policy
from email.message import EmailMessage
from urllib.parse import quote

import pytest
from attachment_fixtures import PNG, mail_with, part
from test_attachment_drafts import SOURCE, sending_client
from test_send import DRAFT_ID, RECIPIENT, SENDER, Host, draft, redeem
from test_send import local_certificate as local_certificate
from test_send import rig as rig
from view_browser import browser, requests, run_view, tool_result

from proton_mail_mcp import mcp_apps, views
from proton_mail_mcp.mail import MailValidationError

PDF = b"%PDF-1.4 approval bytes"


def source_mail():
    return mail_with(
        SOURCE,
        part("board minutes.pdf", "application/pdf", PDF),
        part("holiday.png", "image/png", PNG),
    )


def attached_draft(rig, monkeypatch, tmp_path, source=None):
    allowed = tmp_path / "allowed"
    allowed.mkdir(exist_ok=True)
    (allowed / "report.pdf").write_bytes(PDF)
    mail, fake, service = sending_client(rig, monkeypatch, allowed, source)
    return mail, fake, service


def test_the_send_form_names_the_sender_and_every_file_before_anything_is_sent(
    rig, monkeypatch, tmp_path
):
    mail, _, service = attached_draft(rig, monkeypatch, tmp_path, source=source_mail())
    created = mail.create_draft(
        RECIPIENT,
        subject="Files",
        body="See attached.",
        attachments=["report.pdf"],
        attachments_from=SOURCE,
        attachment_indexes=[2],
    )
    preview = service.preview_send(created)

    def nothing_sent_yet():
        assert rig.smtp.calls == []

    host = Host(during=nothing_sent_yet)
    assert redeem(rig, preview["token"], host)["sent"] is True
    [prompt] = host.prompts
    first, details = prompt.split("\n", 1)
    # On the first line, in words, not only in the details below.
    assert f'From: "{SENDER}".' in first
    assert "Attached files (2):" in first
    assert f'"report.pdf" ("application/pdf", {len(PDF)} bytes)' in first
    assert f'"holiday.png" ("image/png", {len(PNG)} bytes)' in first
    assert preview["draft_hash"][:16] in first
    listed = json.loads(details)
    assert listed["from"] == SENDER
    assert [(file["name"], file["type"], file["size"]) for file in listed["attachments"]] == [
        ("report.pdf", "application/pdf", len(PDF)),
        ("holiday.png", "image/png", len(PNG)),
    ]


def test_the_send_form_says_when_nothing_is_attached(rig):
    preview = rig.service.preview_send(DRAFT_ID)
    host = Host()
    redeem(rig, preview["token"], host)
    assert f'From: "{SENDER}". Attached files (0): none.' in host.prompts[0].split("\n")[0]


def test_declining_a_form_that_named_a_file_sends_nothing(rig):
    rig.client.raw = draft(attachment=b"bytes")
    preview = rig.service.preview_send(DRAFT_ID)
    host = Host("decline")
    with pytest.raises(MailValidationError):
        redeem(rig, preview["token"], host)
    assert '"report.bin"' in host.prompts[0].split("\n")[0]
    assert rig.smtp.sent == []


def raw_named(filename_header):
    """A draft another client saved, whose file name no local check has cleaned."""
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = SENDER
    message["To"] = RECIPIENT
    message["Subject"] = "Named"
    message["Message-ID"] = DRAFT_ID
    message.set_content("Body")
    message.add_attachment(b"x", maintype="application", subtype="pdf", filename="placeholder")
    message.set_boundary("named-boundary")
    raw = message.as_bytes(policy=policy.SMTP.clone(max_line_length=0))
    return raw.replace(b'filename="placeholder"', filename_header)


def test_a_file_name_cannot_forge_lines_or_hide_its_extension_in_the_form(rig):
    forged = b"filename*=utf-8''x%0D%0AFrom%3A%20boss%40evil.test%0D%0A.pdf"
    rig.client.raw = raw_named(forged)
    preview = rig.service.preview_send(DRAFT_ID)
    assert "\n" not in preview["attachments"][0]["name"]
    assert preview["attachments"][0]["name"].startswith("x__From: boss@evil.test")
    host = Host()
    redeem(rig, preview["token"], host)
    # Only the first line and the JSON details: the name cannot start a line.
    assert len(host.prompts[0].split("\n")) == 2
    rig.smtp.sent.clear()
    rig.client.raw = raw_named(b"filename*=utf-8''invoice%E2%80%AEfdp.exe")
    # The right-to-left override is what turns exe.pdf round on screen.
    [file] = rig.service.preview_send(DRAFT_ID)["attachments"]
    assert file["name"] == "invoice_fdp.exe"
    # A long name cannot push its real extension off the form's first line.
    long_name = b"filename=" + b"a" * 300 + b".exe"
    rig.client.raw = raw_named(long_name)
    preview = rig.service.preview_send(DRAFT_ID)
    host = Host("decline")
    with pytest.raises(MailValidationError):
        redeem(rig, preview["token"], host)
    first = host.prompts[0].split("\n")[0]
    assert '.exe" ("application/pdf"' in first and "a" * 90 not in first


def test_the_forms_first_line_shows_a_names_letters_and_no_line_break(rig):
    hidden = "".join(map(chr, (0x2028, 0x2029, 0x85, 0x0B, 0x0C, 0x1C, 0x202E, 0x200B)))
    rig.client.raw = raw_named(b"filename*=utf-8''" + quote(f"Отчёт 日本語{hidden}.pdf").encode())
    preview = rig.service.preview_send(DRAFT_ID)
    host = Host()
    redeem(rig, preview["token"], host)
    # splitlines breaks on every character above that a plain split on "\n" would miss.
    first, details = host.prompts[0].splitlines()
    shown = "Отчёт 日本語" + "_" * len(hidden) + ".pdf"
    assert f'"{shown}"' in first
    # The details below stay ASCII, so a look-alike letter can be checked there.
    assert details.isascii() and json.loads(details)["attachments"][0]["name"] == shown


def composer_result(**options):
    entry = {
        "message_id": "<source@example.test>",
        "from": "Alice <alice@example.test>",
        "to": "reader@example.test",
        "subject": "Plans",
        "date": "Wed, 28 Oct 2026 10:00:00 +0000",
        "headers": {},
        "text": "Text",
        "attachments": [
            {"filename": "plan.pdf", "type": "application/pdf", "size": 2048},
        ],
        "truncated": False,
    }
    return views.reply_view(
        {"messages": [entry], "partial": False},
        "<source@example.test>",
        own={"reader@example.test"},
        sender="reader@example.test",
        **options,
    )


def test_composer_data_names_the_sender_and_files_before_saving():
    text, view = composer_result(edit=True)
    assert text["from"] == view["from"] == "reader@example.test"
    assert view["attached"][0]["name"] == "plan.pdf"
    assert view["attached"][0]["checked"] is True
    # A new draft says the same.
    new_text, new_view = views.new_draft_view(
        "a@example.test",
        None,
        "S",
        "B",
        local=[{"name": "a.txt", "type": "text/plain", "size": 1}],
        sender="reader@example.test",
    )
    assert new_text["from"] == new_view["from"] == "reader@example.test"
    assert new_view["attached"][0]["name"] == "a.txt"
    # A local file's size is its own; a saved message's is the encoded count.
    assert view["attached"][0]["size_encoded"] is True
    assert "size_encoded" not in new_view["attached"][0]


def test_the_account_address_is_the_one_a_draft_is_written_from():
    assert views.account_address("reader@example.test") == "reader@example.test"
    assert views.account_address("Reader <reader@example.test>") == "reader@example.test"
    assert views.account_address("not an address") == "not an address"


BEFORE_SAVE = """
const doc = t.doc;
await t.waitFor(() => doc.getElementById("to").value, "the prefilled fields");
t.check("from_visible", t.visible(doc.getElementById("from-row")));
t.check("from", doc.getElementById("from").textContent);
t.check("attached_visible", t.visible(doc.getElementById("attached-row")));
const chips = Array.from(doc.querySelectorAll("#attached .chip"));
t.check("attached", chips.map((chip) => chip.textContent));
t.check("calls", t.requests("tools/call").length);
"""


@browser
def test_the_composer_shows_the_sender_and_files_before_anything_is_saved(tmp_path):
    result = tool_result(mcp_apps.view_result(*composer_result(edit=True)))
    report = run_view(tmp_path, "reply", result, BEFORE_SAVE)
    checks = report["checks"]
    assert checks["from_visible"] is True and checks["from"] == "reader@example.test"
    assert checks["attached_visible"] is True
    assert checks["attached"] == ["plan.pdfapplication/pdf · 2.0 KB encoded"]
    assert checks["calls"] == 0


REVIEW = """
const doc = t.doc;
await t.waitFor(() => doc.getElementById("hash").textContent, "the review");
const rows = Array.from(doc.querySelectorAll("#fields .field")).map((row) => [
  row.querySelector(".key").textContent,
  row.querySelector(".value").textContent,
]);
t.check("rows", rows);
t.check("rows_visible", Array.from(doc.querySelectorAll("#fields .field")).every(t.visible));
t.check("chips", Array.from(doc.querySelectorAll("#fields .chip")).map((chip) => chip.textContent));
t.check("calls", t.requests("tools/call").length);
"""


@browser
def test_the_send_review_shows_the_sender_and_each_file_of_a_real_draft(rig, monkeypatch, tmp_path):
    mail, _, service = attached_draft(rig, monkeypatch, tmp_path, source=source_mail())
    created = mail.create_draft(RECIPIENT, subject="Files", body="B", attachments=["report.pdf"])
    result = tool_result(mcp_apps.view_result(*views.review_view(service.review_send(created))))
    report = run_view(tmp_path, "review_send", result, REVIEW)
    checks = report["checks"]
    rows = dict(checks["rows"])
    assert rows["From"] == SENDER and rows["To"] == RECIPIENT
    assert checks["rows_visible"] is True
    assert checks["chips"] == [f"report.pdfapplication/pdf · {len(PDF)} B"]
    assert checks["calls"] == 0


@browser
def test_a_draft_without_files_says_so_in_the_review(rig, tmp_path):
    result = tool_result(
        mcp_apps.view_result(*views.review_view(rig.service.review_send(DRAFT_ID)))
    )
    report = run_view(tmp_path, "review_send", result, REVIEW)
    assert dict(report["checks"]["rows"])["Attached"] == "None"


@browser
def test_a_forward_uses_the_same_review_view(rig, monkeypatch, tmp_path):
    mail, _, service = sending_client(rig, monkeypatch, source=source_mail())
    forwarded = mail.forward_message(SOURCE, RECIPIENT, attachments=[1], note="FYI")
    text, view = views.review_view(service.review_send(forwarded["draft_id"]))
    assert view["view"] == "review_send"
    result = tool_result(mcp_apps.view_result(text, view))
    report = run_view(tmp_path, "review_send", result, REVIEW)
    checks = report["checks"]
    assert dict(checks["rows"])["From"] == SENDER
    assert checks["chips"] == [f"board minutes.pdfapplication/pdf · {len(PDF)} B"]


THREAD = """
const doc = t.doc;
await t.waitFor(() => doc.querySelector(".chip"), "the attachments");
t.check("chips", Array.from(doc.querySelectorAll(".chip")).map((chip) => chip.textContent));
const forward = t.button("Forward");
t.check("forward", !!forward);
forward.click();
await t.waitFor(() => t.requests("ui/message").length === 1, "the chat message");
t.check("message", t.requests("ui/message")[0].params.content[0].text);
t.check("calls", t.requests("tools/call").length);
"""


def thread_result():
    message = {
        "message_id": "<a@example.test>",
        "from": "Alice <alice@example.test>",
        "to": "reader@example.test",
        "subject": "Files",
        "date": "Wed, 28 Oct 2026 10:00:00 +0000",
        "headers": {},
        "text": "Hello",
        "attachments": [
            {"filename": "plan.pdf", "type": "application/pdf", "size": 2048},
            {"filename": "bad‮gnp.exe", "type": "application/x-msdownload", "size": 10},
            {"filename": None, "type": "image/png", "size": 9},
            {"filename": "unknown.bin", "type": "application/octet-stream"},
        ],
        "truncated": False,
    }
    text, view = views.thread_view({"messages": [message], "partial": False}, "<a@example.test>")
    return tool_result(mcp_apps.view_result(text, view))


@browser
def test_the_thread_lists_each_attachments_name_type_and_size_and_offers_forward(tmp_path):
    report = run_view(tmp_path, "thread", thread_result(), THREAD)
    checks = report["checks"]
    assert checks["chips"] == [
        "plan.pdfapplication/pdf · 2.0 KB encoded",
        "bad_gnp.exeapplication/x-msdownload · 10 B encoded",
        "(unnamed)image/png · 9 B encoded",
        # No size, so nothing to mark.
        "unknown.binapplication/octet-stream",
    ]
    # The chat message quotes a position and a hexadecimal reference, never mail text.
    message = checks["message"]
    assert message.startswith("Forward message 1 of the Proton Mail thread view (view_ref msg-")
    assert "forward_message" in message and "plan.pdf" not in message
    assert checks["calls"] == 0
    assert requests(report, "tools/call") == []


LONG_NAME = "invoice_2026_january_final_signed_copy_" * 3 + "for_the_accounting_review.pdf.exe"
LONG_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
WHOLE_NAME = """
const doc = t.doc;
await t.waitFor(() => doc.querySelectorAll(".chip").length === 2, "the files");
const [long, short] = doc.querySelectorAll(".chip");
const label = long.querySelector(".label");
const end = doc.createRange();
end.setStart(label.firstChild, label.textContent.length - ".pdf.exe".length);
end.setEnd(label.firstChild, label.textContent.length);
const shown = end.getBoundingClientRect();
const box = long.getBoundingClientRect();
t.check("text", label.textContent);
t.check("overflows", long.scrollWidth > long.clientWidth);
t.check("end_in_chip", shown.right <= box.right && shown.bottom <= box.bottom);
t.check("chip_in_view", box.right <= doc.documentElement.clientWidth);
function lines(node) {
  const text = doc.createRange();
  text.selectNodeContents(node);
  return new Set(Array.from(text.getClientRects(), (rect) => Math.round(rect.top))).size;
}
t.check("long_lines", lines(label));
t.check("short_lines", lines(short.querySelector(".label")));
"""


def long_name_review():
    return views.review_view(
        {
            "draft_id": DRAFT_ID,
            "from": SENDER,
            "from_alias": False,
            "to": [RECIPIENT],
            "cc": [],
            "bcc": [],
            "subject": "Files",
            "attachments": [
                {"name": LONG_NAME, "type": LONG_TYPE, "size": 12, "sha256": "a" * 64},
                {"name": "short.pdf", "type": "application/pdf", "size": 12, "sha256": "b" * 64},
            ],
            "draft_hash": "c" * 64,
            "action_hash": "d" * 64,
            "approval": "host_confirmation",
            "caps": {},
            "body_preview": {"type": "text/plain", "text": "B", "truncated": False},
        }
    )


def long_name_message():
    return {
        "message_id": "<a@example.test>",
        "from": SENDER,
        "to": RECIPIENT,
        "subject": "Files",
        "date": "Wed, 28 Oct 2026 10:00:00 +0000",
        "headers": {},
        "text": "Hello",
        "attachments": [
            {"filename": LONG_NAME, "type": LONG_TYPE, "size": 12},
            {"filename": "short.pdf", "type": "application/pdf", "size": 12},
        ],
        "truncated": False,
    }


def long_name_composer():
    return views.reply_view(
        {"messages": [long_name_message()], "partial": False},
        "<a@example.test>",
        own={SENDER},
        edit=True,
        sender=SENDER,
    )


def long_name_thread():
    return views.thread_view(
        {"messages": [long_name_message()], "partial": False}, "<a@example.test>"
    )


@browser
@pytest.mark.parametrize(
    "view,result",
    [
        ("review_send", long_name_review),
        ("reply", long_name_composer),
        ("thread", long_name_thread),
    ],
)
def test_a_long_file_name_shows_whole_with_its_real_extension(tmp_path, view, result):
    report = run_view(tmp_path, view, tool_result(mcp_apps.view_result(*result())), WHOLE_NAME)
    checks = report["checks"]
    assert checks["text"] == LONG_NAME
    assert checks["overflows"] is False
    assert checks["end_in_chip"] is True and checks["chip_in_view"] is True
    # Across the chip's width, not squeezed into a column beside a long type or a checkbox.
    assert checks["long_lines"] <= 3
    assert checks["short_lines"] == 1
