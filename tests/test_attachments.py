"""list_attachments, read_attachment and save_attachment over an in-memory IMAP double."""

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import anyio
import pytest
from attachment_fixtures import PNG, FakeImap, client, mail_with, part, pdf_bytes
from attachment_fixtures import settings as settings  # noqa: F401
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ImageContent, TextContent
from windows_skips import has_mode

from proton_mail_mcp import attachments as attachments_module
from proton_mail_mcp.action_log import ActionLog
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.server import SECURITY_NOTICE, create_mcp

MESSAGE = "<files@example.com>"
TOKEN = "attachment-tests-token-" + "x" * 32


def reader(settings, *parts, **options):
    fake = FakeImap({"INBOX": {"7": mail_with(MESSAGE, *parts, **options)}})
    return client(settings, fake), fake


def writes(fake):
    return [call for call in fake.calls if call[0] in {"APPEND", "STORE", "COPY", "EXPUNGE"}]


def content_fetches(fake):
    return [call[2] for call in fake.calls if call[0] == "FETCH" and call[2].startswith("(BODY.")]


def test_list_gives_index_name_type_and_size_and_fetches_no_attachment(settings):
    named = part("plan.pdf", "application/pdf", b"%PDF-1.4 x", size=48213)
    unnamed = part(None, "image/png", PNG, size=9)
    mail, fake = reader(settings, named, unnamed)
    assert mail.list_attachments(MESSAGE) == {
        "message_id": MESSAGE,
        "attachments": [
            {"index": 1, "filename": "plan.pdf", "type": "application/pdf", "size": 48213},
            {"index": 2, "filename": None, "type": "image/png", "size": 9},
        ],
        "size_note": attachments_module.ENCODED_SIZE_NOTE,
    }
    assert not writes(fake)
    # Only the text part's PEEK: no attachment content moves.
    assert all("BODY.PEEK[1]" in query for query in content_fetches(fake))


def test_listed_names_hide_invisible_characters_a_sender_chose():
    assert attachments_module.visible_name("invoice\u202efdp.exe") == "invoice_fdp.exe"
    assert attachments_module.visible_name("a\r\nFrom: b") == "a__From: b"


def test_a_shortened_name_keeps_its_real_extension():
    shown = attachments_module.visible_name("a" * 400 + ".exe")
    assert len(shown) == 255 and shown.endswith("a.exe") and "..." in shown
    assert attachments_module.visible_name("short.pdf") == "short.pdf"
    assert len(attachments_module.visible_type("application/" + "x" * 400)) == 100
    assert attachments_module.visible_type(None) is None


def test_a_missing_message_and_an_incomplete_read_are_told_apart(settings):
    mail, _ = reader(settings, part("a.txt", "text/plain", b"x"))
    with pytest.raises(MailValidationError, match="Message not found"):
        mail.list_attachments("<absent@example.com>")
    incomplete = SimpleNamespace(
        read_thread=lambda *a, **k: {"messages": [], "partial": True}, settings=settings
    )
    with pytest.raises(MailValidationError, match="lookup incomplete"):
        attachments_module.list_attachments(incomplete, MESSAGE)
    # A read that stopped before the structure knows no attachments; that is not "none".
    stopped = {"message_id": MESSAGE, "attachments": [], "body_size_bytes": None, "truncated": True}
    incomplete.read_thread = lambda *a, **k: {"messages": [stopped], "partial": True}
    with pytest.raises(MailValidationError, match="lookup incomplete"):
        attachments_module.list_attachments(incomplete, MESSAGE)


@pytest.mark.parametrize(
    "name,kind,data,charset,expected",
    [
        ("notes.txt", "text/plain", "Grüße\r\n".encode(), "UTF-8", "Grüße\r\n"),
        ("notes.txt", "text/plain", "Grüße".encode("latin-1"), "ISO-8859-1", "Grüße"),
        ("rows.csv", "text/csv", b"a,b\r\n1,2\r\n", None, "a,b\r\n1,2\r\n"),
        # Common senders label these as generic binary.
        ("rows.csv", "application/octet-stream", b"a,b\n", None, "a,b\n"),
        ("wide.txt", "text/plain", "wide".encode("utf-16"), None, "wide"),
        ("bom.txt", "text/plain", b"\xef\xbb\xbfbom", None, "bom"),
        ("odd.txt", "text/plain", b"plain", "no-such-charset", "plain"),
    ],
)
def test_text_and_csv_attachments_read_as_text(settings, name, kind, data, charset, expected):
    mail, fake = reader(settings, part(name, kind, data, charset=charset))
    result = mail.read_attachment(MESSAGE, 1)
    assert result["kind"] == "text" and result["text"] == expected
    assert result["filename"] == name and result["decoded_size"] == len(data)
    assert result["truncated"] is False and result["next_offset"] is None
    assert not writes(fake)
    assert all("PEEK" in query for query in content_fetches(fake))


def test_long_text_comes_in_pieces_until_next_offset_is_null(settings):
    text = "x" * attachments_module.MAX_TEXT_CHARS + "tail"
    mail, _ = reader(settings, part("long.txt", "text/plain", text.encode()))
    first = mail.read_attachment(MESSAGE, 1)
    assert len(first["text"]) == attachments_module.MAX_TEXT_CHARS
    assert first["truncated"] is True and first["total_chars"] == len(text)
    second = mail.read_attachment(MESSAGE, 1, first["next_offset"])
    assert second["text"] == "tail" and second["next_offset"] is None
    assert second["truncated"] is False
    with pytest.raises(MailValidationError, match="offset"):
        mail.read_attachment(MESSAGE, 1, -1)


def test_read_text_loses_control_characters_and_long_newline_runs(settings, monkeypatch):
    controls = "".join(map(chr, [*range(0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F]))
    dirty = (
        f"{controls}tab\tcr\rcrlf\r\nlf\nblank\n\ntwo"
        "\n\n\nthree\n\n\n\n\nfive\r\n\r\n\r\ncrlf-run\n\r\n\nmixed"
        # A removed character must not leave a run of newlines behind.
        "\n\x00\n\x0b\n\x7f\nhidden"
    )
    clean = "tab\tcr\rcrlf\r\nlf\nblank\n\ntwo\n\nthree\n\nfive\n\ncrlf-run\n\nmixed\n\nhidden"
    # Only a UTF-16 file reads as text with NUL bytes in it.
    mail, _ = reader(settings, part("a.txt", "text/plain", dirty.encode("utf-16")))
    result = mail.read_attachment(MESSAGE, 1)
    assert result["text"] == clean and result["total_chars"] == len(clean)

    # The size cap and the offsets count the cleaned text.
    limit = attachments_module.MAX_TEXT_CHARS
    long = "\x01" * 10 + "x" * limit + "\n\n\n\ntail"
    mail, _ = reader(settings, part("long.txt", "text/plain", long.encode()))
    first = mail.read_attachment(MESSAGE, 1)
    assert first["text"] == "x" * limit and first["next_offset"] == limit
    assert first["total_chars"] == limit + 6
    assert mail.read_attachment(MESSAGE, 1, limit)["text"] == "\n\ntail"

    # PDF text gets the same, and text of only controls counts as no text.
    pages = {"pages": 1, "read": 1, "failed": 0, "text": dirty}
    monkeypatch.setattr(attachments_module, "_pdf_text", lambda data: pages)
    mail, _ = reader(settings, part("a.pdf", "application/pdf", pdf_bytes("x")))
    assert mail.read_attachment(MESSAGE, 1)["text"] == clean
    pages["text"] = controls
    result = mail.read_attachment(MESSAGE, 1)
    assert result["text"] == "" and "may be a scan" in result["note"]


def test_pdf_text_is_extracted_page_by_page(settings):
    data = pdf_bytes("First page text", "Second page text")
    mail, fake = reader(settings, part("report.pdf", "application/pdf", data))
    result = mail.read_attachment(MESSAGE, 1)
    assert result["kind"] == "pdf"
    assert "First page text" in result["text"] and "Second page text" in result["text"]
    assert (result["pages"], result["pages_read"], result["pages_failed"]) == (2, 2, 0)
    assert result["truncated"] is False and "note" not in result
    assert not writes(fake)


def test_pdf_hyphens_stay_visible_and_other_characters_are_unchanged(settings):
    # A PDF paints code 0xAD of WinAnsi as a hyphen; pypdf returns it as an invisible U+00AD.
    font = (
        b" /Encoding << /BaseEncoding /WinAnsiEncoding"
        b" /Differences [128 /hyphentwo /nonbreakinghyphen /minus] >>"
    )
    line = (
        r"REF-0001 REF\2550002 REF\2000003 REF\2010004 REF\2020005 REF\2260006 REF\0010007"
        r" REF\2270008"
    )
    mail, _ = reader(settings, part("a.pdf", "application/pdf", pdf_bytes(line, font=font)))
    assert mail.read_attachment(MESSAGE, 1)["text"] == (
        "REF-0001 REF-0002 REF-0003 REF-0004 REF-0005 REF\u20130006 REF0007 REF\u20140008"
    )

    # A hyphen at a line end stays too; this fixture puts one line on each page.
    data = pdf_bytes(r"opti\255", "mal", font=font)
    mail, _ = reader(settings, part("a.pdf", "application/pdf", data))
    assert mail.read_attachment(MESSAGE, 1)["text"] == "opti-\n\nmal"

    # In a text file these characters are the sender's own.
    text = "a\u00adb\u2010c\u2011d\u2212e"
    mail, _ = reader(settings, part("a.txt", "text/plain", text.encode(), charset="utf-8"))
    assert mail.read_attachment(MESSAGE, 1)["text"] == text


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("abc\x00def", "abc-def"),
        ("REF\x000001", "REF-0001"),
        ("1234\x005678\x00012", "1234-5678-012"),
        ("a\x00b\x00c", "a-b-c"),
        ("Привет\x00мир", "Привет-мир"),
        ("日本\x00語", "日本-語"),
        # Anything else beside it, including another NUL, is not a joined hyphen.
        ("12\x00 34", "12 34"),
        ("12 \x0034", "12 34"),
        ("\x00abc", "abc"),
        ("abc\x00", "abc"),
        ("\x00", ""),
        ("abc\x00\x00def", "abcdef"),
        ("abc\x00\x00\x00def", "abcdef"),
        ("a_\x00b", "a_b"),
        ("a\x00$5", "a$5"),
        ("a\x00\nb", "a\nb"),
        ("a\x00\x01b", "ab"),
        # Only NUL: other controls between letters are still removed.
        ("a\x01b", "ab"),
    ],
)
def test_clean_text_keeps_only_a_nul_between_alphanumerics_as_a_hyphen(raw, clean):
    assert attachments_module._clean_text(raw) == clean


def test_pdf_nul_between_alphanumerics_reads_as_a_hyphen(settings):
    data = pdf_bytes(r"REF\0000001 12\000 34 \000end\000")
    mail, _ = reader(settings, part("a.pdf", "application/pdf", data))
    assert mail.read_attachment(MESSAGE, 1)["text"] == "REF-0001 12 34 end"


def test_lone_surrogates_from_a_hostile_pdf_cannot_break_the_response(settings, monkeypatch):
    extracted = {"pages": 1, "read": 1, "failed": 0, "text": "bad \ud83d text"}
    monkeypatch.setattr(attachments_module, "_pdf_text", lambda data: extracted)
    mail, _ = reader(settings, part("a.pdf", "application/pdf", pdf_bytes("x")))
    assert mail.read_attachment(MESSAGE, 1)["text"] == "bad ? text"


def test_pdf_text_is_bounded_per_page_inside_the_worker():
    assert 'extract_text() or "")[:{chars}]' in attachments_module._PDF_WORKER


def test_a_pdf_labelled_as_binary_is_still_read_as_pdf(settings):
    data = pdf_bytes("Mislabelled")
    mail, _ = reader(settings, part("scan", "application/octet-stream", data))
    assert "Mislabelled" in mail.read_attachment(MESSAGE, 1)["text"]


def test_a_pdf_without_text_says_so(settings):
    data = pdf_bytes(" ")
    mail, _ = reader(settings, part("scan.pdf", "application/pdf", data))
    result = mail.read_attachment(MESSAGE, 1)
    assert result["text"].strip() == "" and "may be a scan" in result["note"]


def test_a_broken_or_password_protected_pdf_refuses(settings):
    mail, _ = reader(settings, part("bad.pdf", "application/pdf", b"%PDF-1.4\nnot really"))
    with pytest.raises(MailValidationError, match="could not be read"):
        mail.read_attachment(MESSAGE, 1)

    from io import BytesIO

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(pdf_bytes("Secret"))))
    writer.encrypt("a-password", algorithm="RC4-128")
    locked = BytesIO()
    writer.write(locked)
    mail, _ = reader(settings, part("locked.pdf", "application/pdf", locked.getvalue()))
    with pytest.raises(MailValidationError, match="password-protected"):
        mail.read_attachment(MESSAGE, 1)


def test_pdf_parsing_runs_apart_without_this_servers_environment(settings, monkeypatch):
    monkeypatch.setenv("PROTON_BRIDGE_PASSWORD", "secret-bridge-password")
    seen = {}
    real = attachments_module.subprocess.Popen

    def spy(command, **options):
        seen.update(command=command, **options)
        return real(command, **options)

    monkeypatch.setattr(attachments_module.subprocess, "Popen", spy)
    mail, _ = reader(settings, part("a.pdf", "application/pdf", pdf_bytes("Hi")))
    assert "Hi" in mail.read_attachment(MESSAGE, 1)["text"]
    assert seen["env"] == {} and seen["cwd"] == "/" and seen["command"][1:3] == ["-I", "-B"]
    assert "RLIMIT_AS" in seen["command"][4] and "RLIMIT_CPU" in seen["command"][4]


def test_a_pdf_that_hangs_its_parser_is_stopped(settings, monkeypatch):
    processes = []
    real = attachments_module.subprocess.Popen

    def spy(command, **options):
        process = real(command[:3] + ["-c", "while True: pass"], **options)
        processes.append(process)
        return process

    monkeypatch.setattr(attachments_module.subprocess, "Popen", spy)
    monkeypatch.setattr(attachments_module, "_PDF_SECONDS", 0.2)
    mail, _ = reader(settings, part("hang.pdf", "application/pdf", pdf_bytes("x")))
    with pytest.raises(MailValidationError, match="took too long"):
        mail.read_attachment(MESSAGE, 1)
    assert processes[0].returncode is not None


def test_images_return_as_image_content_by_their_real_signature(settings):
    mail, _ = reader(settings, part("pixel", "application/octet-stream", PNG))
    result = mail.read_attachment(MESSAGE, 1)
    assert result["kind"] == "image" and result["mime_type"] == "image/png"
    assert base64.b64decode(result["image"]) == PNG
    assert "text" not in result


@pytest.mark.parametrize(
    "name,kind,data",
    [
        ("tool.exe", "application/octet-stream", b"MZ\x90\x00"),
        ("page.html", "text/html", b"<script>x()</script>"),
        ("logo.svg", "image/svg+xml", b"<svg onload='x()'/>"),
        ("archive.zip", "application/zip", b"PK\x03\x04"),
        # A claimed PDF or image without that content is not parsed as one.
        ("fake.pdf", "application/pdf", b"MZ not a pdf"),
        ("fake.png", "image/png", b"not a png"),
        ("fake.txt", "text/plain", b"binary\x00content"),
    ],
)
def test_other_content_refuses(settings, name, kind, data):
    mail, _ = reader(settings, part(name, kind, data))
    with pytest.raises(MailValidationError, match="Only PDF, TXT, CSV and image"):
        mail.read_attachment(MESSAGE, 1)


def test_unknown_index_refuses(settings):
    mail, fake = reader(settings, part("a.txt", "text/plain", b"x"))
    for index in (0, 2):
        with pytest.raises(MailValidationError, match="No attachment with that index"):
            mail.read_attachment(MESSAGE, index)
    assert all("BODY.PEEK[2]" not in query for query in content_fetches(fake))


def test_read_cap_refuses_by_the_declared_size_before_fetching(settings):
    declared = int(attachments_module.MAX_READ_BYTES * 1.5) + 1
    mail, fake = reader(settings, part("big.txt", "text/plain", b"x", size=declared))
    with pytest.raises(MailValidationError, match="10 MiB read limit"):
        mail.read_attachment(MESSAGE, 1)
    assert all("BODY.PEEK[2]" not in query for query in content_fetches(fake))


def test_read_cap_holds_when_the_declared_size_lies(settings, monkeypatch):
    monkeypatch.setattr(attachments_module, "MAX_READ_BYTES", 1024)
    mail, fake = reader(settings, part("big.txt", "text/plain", b"x" * 4096, size=10))
    with pytest.raises(MailValidationError, match="read limit"):
        mail.read_attachment(MESSAGE, 1)
    # The fetch itself asks for one byte past the encoded limit, not the whole part.
    assert "(BODY.PEEK[2]<0.1537>)" in content_fetches(fake)


def test_read_cap_counts_decoded_bytes_too(settings, monkeypatch):
    monkeypatch.setattr(attachments_module, "MAX_READ_BYTES", 1024)
    data = b"x" * 1025
    mail, _ = reader(settings, part("big.txt", "text/plain", data))
    with pytest.raises(MailValidationError, match="read limit"):
        mail.read_attachment(MESSAGE, 1)


def test_image_cap_is_lower_than_the_read_cap(settings, monkeypatch):
    monkeypatch.setattr(attachments_module, "MAX_IMAGE_BYTES", 8)
    mail, _ = reader(settings, part("a.png", "image/png", PNG))
    with pytest.raises(MailValidationError, match="Image exceeds"):
        mail.read_attachment(MESSAGE, 1)


def test_transfer_encodings_decode_and_others_refuse(settings):
    data = "Grüße = done\r\n".encode()
    mail, _ = reader(settings, part("q.txt", "text/plain", data, encoding="QUOTED-PRINTABLE"))
    assert mail.read_attachment(MESSAGE, 1)["text"] == data.decode()
    mail, _ = reader(settings, part("p.txt", "text/plain", b"plain", encoding="7BIT"))
    assert mail.read_attachment(MESSAGE, 1)["text"] == "plain"
    mail, _ = reader(settings, part("u.txt", "text/plain", b"x", encoding="X-UUENCODE"))
    with pytest.raises(MailValidationError, match="unsupported transfer encoding"):
        mail.read_attachment(MESSAGE, 1)
    broken = part("b.txt", "text/plain", b"")
    fake = FakeImap({"INBOX": {"7": mail_with(MESSAGE, broken)}})
    fake.folders["INBOX"]["7"].sections["2"] = b"abc"
    with pytest.raises(MailValidationError, match="could not be decoded"):
        client(settings, fake).read_attachment(MESSAGE, 1)


def test_a_message_replaced_under_its_handle_refuses(settings):
    mail, fake = reader(settings, part("a.txt", "text/plain", b"x"))
    item = attachments_module._message(mail, MESSAGE)
    fake.folders["INBOX"]["7"] = mail_with("<other@example.com>", part("a.txt", "text/plain", b"y"))
    with pytest.raises(MailValidationError, match="changed while it was being read"):
        attachments_module._fetch_parts(mail, item, [1], 1024, "too large")


def folder_settings(settings, tmp_path, **overrides):
    folder = tmp_path / "allowed"
    folder.mkdir()
    return replace(settings, attachments_dir=str(folder), **overrides), folder


def test_save_writes_the_decoded_bytes_as_a_private_new_file(settings, tmp_path):
    configured, folder = folder_settings(settings, tmp_path)
    data = b"\x00binary\xffbytes"
    mail, fake = reader(configured, part("plan.bin", "application/octet-stream", data))
    result = mail.save_attachment(MESSAGE, 1)
    assert result == {
        "saved": True,
        "filename": "plan.bin",
        "index": 1,
        "type": "application/octet-stream",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    saved = folder / "plan.bin"
    assert saved.read_bytes() == data
    assert has_mode(saved, 0o600)
    assert not writes(fake)
    assert all("PEEK" in query for query in content_fetches(fake))


def test_save_takes_a_chosen_name_and_never_overwrites(settings, tmp_path):
    configured, folder = folder_settings(settings, tmp_path)
    (folder / "plan.txt").write_bytes(b"kept")
    mail, _ = reader(configured, part("sender.txt", "text/plain", b"new"))
    assert mail.save_attachment(MESSAGE, 1, "plan.txt")["filename"] == "plan (1).txt"
    assert mail.save_attachment(MESSAGE, 1, "plan.txt")["filename"] == "plan (2).txt"
    assert (folder / "plan.txt").read_bytes() == b"kept"
    assert (folder / "plan (2).txt").read_bytes() == b"new"
    assert mail.save_attachment(MESSAGE, 1)["filename"] == "sender.txt"


def test_save_cap_refuses(settings, tmp_path, monkeypatch):
    configured, folder = folder_settings(settings, tmp_path)
    monkeypatch.setattr(attachments_module, "MAX_SAVE_BYTES", 16)
    mail, _ = reader(configured, part("big.txt", "text/plain", b"x" * 17))
    with pytest.raises(MailValidationError, match="save limit"):
        mail.save_attachment(MESSAGE, 1)
    assert list(folder.iterdir()) == []


def test_an_unnamed_part_gets_a_name_from_its_position_and_type(settings, tmp_path):
    configured, folder = folder_settings(settings, tmp_path)
    mail, _ = reader(configured, part(None, "image/png", PNG))
    assert mail.save_attachment(MESSAGE, 1)["filename"] == "attachment-1.png"
    assert (folder / "attachment-1.png").read_bytes() == PNG


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"PROTON_MCP_ATTACHMENTS_DIR": "/srv/mail-files"}, "/srv/mail-files"),
        ({"PROTON_MCP_ATTACHMENTS_DIR": "/srv/a b/mail files"}, "/srv/a b/mail files"),
    ],
)
def test_the_allowed_folder_setting_is_read(value, expected):
    base = {"PROTON_MCP_TOKEN": TOKEN, "PROTON_BRIDGE_USER": "reader@example.com"}
    assert Settings.from_env({**base, **value}).attachments_dir == expected


@pytest.mark.parametrize(
    "value",
    [
        "relative/dir",
        "~/files",
        "/",
        "//",
        "/srv/a\nb",
        "/srv/a\x7f",
        # These open the filesystem root, past the check for it.
        "/.",
        "/srv/..",
        # A trailing slash makes the kernel follow a link despite O_NOFOLLOW.
        "/srv/files/",
        "/srv/./files",
        "/srv//files",
        "/srv/files/..",
    ],
)
def test_the_allowed_folder_setting_must_be_a_real_absolute_path(value):
    base = {"PROTON_MCP_TOKEN": TOKEN, "PROTON_BRIDGE_USER": "reader@example.com"}
    with pytest.raises(ConfigError, match="PROTON_MCP_ATTACHMENTS_DIR"):
        Settings.from_env({**base, "PROTON_MCP_ATTACHMENTS_DIR": value})


def mcp_for(settings, fake, tmp_path):
    configured = replace(
        settings, token=TOKEN, confirm_mutations=False, action_log_path=str(tmp_path / "log.jsonl")
    )
    return create_mcp(configured, client(configured, fake)), configured


def call(mcp, name, arguments):
    return asyncio.run(mcp._tool_manager.call_tool(name, arguments, convert_result=True))


def test_read_tool_returns_image_content_beside_its_description(settings, tmp_path):
    fake = FakeImap({"INBOX": {"7": mail_with(MESSAGE, part("a.png", "image/png", PNG))}})
    mcp, _ = mcp_for(settings, fake, tmp_path)
    result = call(mcp, "read_attachment", {"message_id": MESSAGE, "attachment": 1})
    text, image = result.content
    assert isinstance(text, TextContent) and isinstance(image, ImageContent)
    assert image.mimeType == "image/png" and base64.b64decode(image.data) == PNG
    described = json.loads(text.text)
    assert described["kind"] == "image" and "image" not in described


def test_read_tool_returns_text_as_one_block(settings, tmp_path):
    data = "hello Grüße".encode()
    fake = FakeImap(
        {"INBOX": {"7": mail_with(MESSAGE, part("a.txt", "text/plain", data, charset="UTF-8"))}}
    )
    mcp, _ = mcp_for(settings, fake, tmp_path)
    result = call(mcp, "read_attachment", {"message_id": MESSAGE, "attachment": 1})
    [block] = result.content
    assert json.loads(block.text)["text"] == "hello Grüße"
    # Readable, not escaped into a run of \\uXXXX: the model pays for every character.
    assert "Grüße" in block.text


@pytest.mark.parametrize(
    "charset,data,expected",
    [
        # These yield lone surrogates, which no response can carry.
        ("utf-7", b"+2AA-hello", "?hello"),
        ("unicode_escape", b"\\ud800hello", "?hello"),
        ("raw_unicode_escape", b"\\ud800hello", "?hello"),
        # These raise something other than LookupError.
        ("undefined", b"hello", "hello"),
        ("idna", b"hello", "hello"),
        # Quadratic time, hours for a file at the read limit: treated as unknown.
        ("punycode", b"hello-world", "hello-world"),
        ("PUNYCODE", b"hello-world", "hello-world"),
    ],
)
def test_a_senders_charset_cannot_stop_the_read_tool_returning_text(
    settings, tmp_path, charset, data, expected
):
    source = mail_with(MESSAGE, part("a.txt", "text/plain", data, charset=charset))
    mcp, _ = mcp_for(settings, FakeImap({"INBOX": {"7": source}}), tmp_path)
    [block] = call(mcp, "read_attachment", {"message_id": MESSAGE, "attachment": 1}).content
    assert json.loads(block.text)["text"] == expected
    block.text.encode("utf-8")


@pytest.mark.parametrize("attachment", [0, -1, "1x", 1.5, None])
def test_tools_refuse_an_index_that_is_not_a_positive_integer(settings, tmp_path, attachment):
    fake = FakeImap({"INBOX": {"7": mail_with(MESSAGE, part("a.txt", "text/plain", b"x"))}})
    mcp, _ = mcp_for(settings, fake, tmp_path)
    for name in ("read_attachment", "save_attachment"):
        with pytest.raises(ToolError):
            call(mcp, name, {"message_id": MESSAGE, "attachment": attachment})
    assert fake.calls == []


def test_tool_marks_reads_as_read_only_and_saving_as_an_audited_write(settings, tmp_path):
    fake = FakeImap({"INBOX": {}})
    mcp, _ = mcp_for(settings, fake, tmp_path)
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    assert tools["list_attachments"].annotations.readOnlyHint is True
    assert tools["read_attachment"].annotations.readOnlyHint is True
    assert tools["save_attachment"].annotations.readOnlyHint is False
    assert tools["save_attachment"].annotations.destructiveHint is False
    # Views may not read or save attachments themselves.
    for name in ("list_attachments", "read_attachment", "save_attachment"):
        assert tools[name].meta["ui"]["visibility"] == ["model"]


def test_saving_is_audited_without_names_or_content(settings, tmp_path):
    configured, folder = folder_settings(settings, tmp_path)
    secret = b"attachment content that stays out of the log"
    fake = FakeImap({"INBOX": {"7": mail_with(MESSAGE, part("private.txt", "text/plain", secret))}})
    mcp, configured = mcp_for(configured, fake, tmp_path)
    call(mcp, "save_attachment", {"message_id": MESSAGE, "attachment": 1})
    assert (folder / "private.txt").read_bytes() == secret
    with ActionLog(configured.action_log_path).transaction(readonly=True) as transaction:
        records = [row for row in transaction.records() if row.get("action") == "audit"]
    assert [row["outcome"] for row in records] == ["started", "completed"]
    assert records[-1]["tool"] == "save_attachment"
    assert records[-1]["sha256"] == hashlib.sha256(secret).hexdigest()
    logged = json.dumps(records)
    assert "private.txt" not in logged and secret.decode() not in logged


def test_mail_content_results_start_with_a_security_notice(settings, tmp_path):
    source = mail_with(MESSAGE, part("a.txt", "text/plain", b"x"), part("a.png", "image/png", PNG))
    mcp, _ = mcp_for(settings, FakeImap({"INBOX": {"7": source}}), tmp_path)

    async def run(name, **arguments):
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool(name, {"message_id": MESSAGE, **arguments})
        assert result.isError is False
        return json.loads(result.content[0].text)

    for name in ("read_thread", "show_thread"):
        thread = anyio.run(run, name)
        assert next(iter(thread)) == "security_notice" and thread["messages"]
        assert thread["security_notice"] == SECURITY_NOTICE
    text = anyio.run(lambda: run("read_attachment", attachment=1))
    assert next(iter(text)) == "security_notice" and text["text"] == "x"
    assert text["security_notice"] == SECURITY_NOTICE
    # Only text output gets it: an image's text block just describes the file.
    assert "security_notice" not in anyio.run(lambda: run("read_attachment", attachment=2))


def test_image_content_reaches_a_client_over_a_real_session(settings, tmp_path):
    fake = FakeImap({"INBOX": {"7": mail_with(MESSAGE, part("a.png", "image/png", PNG))}})
    mcp, _ = mcp_for(settings, fake, tmp_path)

    async def run():
        async with create_connected_server_and_client_session(mcp) as session:
            return await session.call_tool(
                "read_attachment", {"message_id": MESSAGE, "attachment": 1}
            )

    result = anyio.run(run)
    assert result.isError is False
    assert [block.type for block in result.content] == ["text", "image"]
    assert base64.b64decode(result.content[1].data) == PNG
