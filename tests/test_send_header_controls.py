"""A control character in any top-level draft header refuses; folds and real headers do not."""

import base64
from email import policy
from email.errors import MessageError
from email.message import Message
from email.parser import BytesParser

import pytest
from test_send import DRAFT_ID, RECIPIENT, SENDER, Host, records, redeem
from test_send import local_certificate as local_certificate
from test_send_own_recipients import bridge_draft
from test_send_token_approval import assert_burned
from test_send_token_approval import own_rig as own_rig
from test_send_token_approval import rig as rig

from proton_mail_mcp.mail import MailValidationError

REFUSAL = "Send headers must contain no control characters"
MALFORMED = "Malformed draft content cannot be sent"
OUTSIDE = b"outside@example.test"
CC_LINE = b"Cc: " + OUTSIDE
EIGHT_BIT = "é".encode("utf-8")
ENCODED_CC = b"=?utf-8?q?a=0D=0ACc:_" + OUTSIDE + b"?="
# str.splitlines ends a line on these besides CR and LF; Python before 3.12.4 folds with it.
LINE_BREAKS = [b"\x0b", b"\x0c", b"\x1c", b"\x1d", b"\x1e"]
NESTED_ATTACHMENT = (
    b'Content-Type: multipart/mixed; boundary="c"\r\n'
    b"Content-Disposition: attachment; filename=nested.bin\r\n\r\n"
    b"--c\r\nContent-Type: text/plain\r\n%s\r\nInner\r\n--c--"
)


def hex_id(value):
    return f"{value[0]:02x}"


def draft_with(line):
    """A Bridge-shaped draft to oneself with this raw header line last, in place of its namesake."""
    head, body = bridge_draft(RECIPIENT).split(b"\r\n\r\n", 1)
    prefix = line.split(b":", 1)[0] + b":"
    kept = [item for item in head.split(b"\r\n") if not item.startswith(prefix)]
    return b"\r\n".join([*kept, line]) + b"\r\n\r\n" + body


def multipart_draft(part):
    """A Bridge-shaped multipart draft holding this raw part after its text part."""
    head = bridge_draft(RECIPIENT).split(b"\r\n\r\n", 1)[0]
    return (
        head + b'\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain\r\n\r\nSynthetic text.\r\n"
        b"--b\r\n" + part + b"\r\n--b--\r\n"
    )


def assert_refused(rig, raw, message=REFUSAL):
    rig.client.raw = raw
    for read in (rig.service.preview_send, rig.service.review_send):
        with pytest.raises(MailValidationError) as refused:
            read(DRAFT_ID)
        # The tools report this exact type as a validation error, anything else as a Bridge fault.
        assert type(refused.value) is MailValidationError
        assert str(refused.value) == message
    assert rig.tokens._grants == {} and rig.smtp.calls == []


@pytest.mark.parametrize(
    "char",
    [bytes([code]) for code in (*range(32), 127) if code not in (9, 10, 13)],
    ids=hex_id,
)
def test_every_other_c0_control_and_del_refuse(own_rig, char):
    assert_refused(own_rig, draft_with(b"X-Note: a" + char + b"b"))


@pytest.mark.parametrize("char", [b"\x00", b"\x0b", b"\x1f", b"\x7f"], ids=hex_id)
@pytest.mark.parametrize(
    "line",
    [
        b"References: <a@example.test>%s",
        b"In-Reply-To: <a@example.test>%s",
        b"Date: Sun, 18 Oct 2026 12:00:00 +0000%s",
        b"Mime-Version: 1.0%s",
        b"Content-Type: text/plain%s",
        b"Content-Transfer-Encoding: 7bit%s",
        b'From: "Plain%sSender" <' + SENDER.encode() + b">",
        b"X-Note: folded\r\n line%s",
        b"X-Note: %s",
    ],
    ids=ascii,
)
def test_control_refuses_in_every_kind_of_header(own_rig, char, line):
    assert_refused(own_rig, draft_with(line.replace(b"%s", char)))


@pytest.mark.parametrize("char", LINE_BREAKS, ids=hex_id)
@pytest.mark.parametrize(
    "line",
    [
        b"X-Note: a%s" + CC_LINE,
        b"X-Note: a\r\n b%s" + CC_LINE,
        b"X-Note: " + EIGHT_BIT + b"%s" + CC_LINE,
        b"X-Note: a%s%sb",  # A blank line would end the header block early.
    ],
    ids=["plain", "in-a-fold", "with-8-bit", "blank-line"],
)
def test_hidden_header_line_refuses_before_any_serialization(own_rig, monkeypatch, char, line):
    # No generator runs, so the result cannot depend on whether this Python's has a guard.
    def reached(*args, **kwargs):
        raise AssertionError("the draft reached serialization")

    monkeypatch.setattr(Message, "as_bytes", reached)
    assert_refused(own_rig, draft_with(line.replace(b"%s", char)))


@pytest.mark.parametrize(
    "value",
    [b"a\r b", b"a\r\tb", b"a\n b", b"a\n\tb", b"a\r\n b\r c", b"a\r\n b\n c", b"a\r \r\n b"],
    ids=ascii,
)
def test_bare_cr_or_lf_before_white_space_is_not_a_fold(own_rig, value):
    assert_refused(own_rig, draft_with(b"X-Note: " + value))


@pytest.mark.parametrize("line_end", [b"\r", b"\n"], ids=ascii)
def test_bare_cr_or_lf_before_white_space_refuses_in_a_content_header(own_rig, line_end):
    line = b"Content-Type: text/plain;" + line_end + b" charset=utf-8"
    assert_refused(own_rig, draft_with(line))


@pytest.mark.parametrize("value", [b"a\rb", b"a\nb"], ids=ascii)
def test_bare_cr_or_lf_before_text_stays_malformed(own_rig, value):
    assert_refused(own_rig, draft_with(b"X-Note: " + value), MALFORMED)


@pytest.mark.parametrize("line_end", [b"\r\n", b"\r", b"\n"], ids=ascii)
def test_injected_line_is_a_real_header_shown_and_delivered_as_parsed(own_rig, line_end):
    # The parser ends a line here, so nothing is hidden: the form names the extra recipient.
    rig = own_rig
    rig.client.raw = draft_with(b"X-Note: a" + line_end + CC_LINE)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "host_confirmation"
    assert preview["cc"] == [OUTSIDE.decode()]
    assert redeem(rig, preview["token"], Host())["sent"] is True
    assert rig.smtp.sent[0][1] == (RECIPIENT, OUTSIDE.decode())
    delivered = BytesParser(policy=policy.SMTP).parsebytes(rig.smtp.sent[0][2])
    assert (delivered["X-Note"], delivered["Cc"]) == ("a", OUTSIDE.decode())


@pytest.mark.parametrize("name", [b"X-Note", b"Content-ID", b"Thread-Topic", b"Keywords"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(EIGHT_BIT + b" " + ENCODED_CC, id="crlf-with-8-bit"),
        pytest.param(ENCODED_CC, id="crlf-ascii"),
        pytest.param(
            EIGHT_BIT + b" =?utf-8?b?" + base64.b64encode(b"a\r\n" + CC_LINE) + b"?=",
            id="base64",
        ),
        pytest.param(
            EIGHT_BIT + b" =?utf-8?q?a=0D?= =?utf-8?q?=0ACc:_" + OUTSIDE + b"?=",
            id="split-across-words",
        ),
        pytest.param(EIGHT_BIT + b" =?utf-8?q?=0ACc:_" + OUTSIDE + b"?=", id="lf"),
        pytest.param(EIGHT_BIT + b" =?utf-8?q?=0DCc:_" + OUTSIDE + b"?=", id="cr"),
        pytest.param(b"=?utf-8?q?a=0D=0A_b?=", id="looks-like-a-fold"),
        pytest.param(b"=?utf-8?q?a=0BCc:_" + OUTSIDE + b"?=", id="vt"),
        pytest.param(b"=?utf-8?q?a=00b?=", id="nul"),
        pytest.param(b"=?utf-8?q?a=7Fb?=", id="del"),
    ],
)
def test_encoded_word_decoding_to_a_control_refuses(own_rig, name, value):
    # Python before 3.12.5 writes a decoded line break out when a raw 8-bit byte forces a refold.
    assert_refused(own_rig, draft_with(name + b": " + value))


@pytest.mark.parametrize(
    "line",
    [
        b"X-Note: a\r\n b",
        b"X-Note: a\r\n\tb",
        b"X-Note: a\r\n b\r\n\tc",
        b"X-Note: a\r\n \t b",
        b"X-Note: a \r\n b",
        b"X-Note: a\t\r\n b",
        b"References: <a@example.test>\r\n <b@example.test>\r\n\t<c@example.test>",
        b'Content-Type: text/plain;\r\n charset="utf-8"',
        b'Content-Type: text/plain;\r\n\tcharset="utf-8"',
        b'From: "Plain Sender"\r\n <' + SENDER.encode() + b">",
        b"X-Note: a\tb",
        b"X-Note: a\t",
        b"X-Note: =?utf-8?q?caf=C3=A9?=",
        b"X-Note: =?utf-8?q?a=09b?=",
    ],
    ids=ascii,
)
def test_folds_tabs_and_clean_encoded_words_send_unchanged(own_rig, line):
    rig = own_rig
    rig.client.raw = draft_with(line)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "token"
    assert rig.service.review_send(DRAFT_ID)["draft_hash"] == preview["draft_hash"]
    assert redeem(rig, preview["token"], Host(support=False))["sent"] is True
    assert rig.smtp.sent[0][1] == (RECIPIENT,)
    assert line + b"\r\n" in rig.smtp.sent[0][2].split(b"\r\n\r\n", 1)[0] + b"\r\n"


@pytest.mark.parametrize(
    "line",
    [b"X-Note:\r\n a", b"X-Note:\r\n\ta", b"X-Note:\ta", b"X-Note: " + EIGHT_BIT],
    ids=ascii,
)
def test_other_valid_shapes_keep_their_approval(own_rig, line):
    # The wire's spelling of these differs by Python version; the approval does not.
    rig = own_rig
    rig.client.raw = draft_with(line)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "token"
    assert redeem(rig, preview["token"], Host(support=False))["sent"] is True
    delivered = BytesParser(policy=policy.SMTP).parsebytes(rig.smtp.sent[0][2])
    assert delivered["Cc"] is None and delivered["X-Note"] is not None


@pytest.mark.parametrize(
    "part",
    [
        pytest.param(
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Disposition: attachment; filename=report.bin\r\n"
            b"X-Part: a\x0b" + CC_LINE + b"\r\n\r\nQUJD",
            id="attachment",
        ),
        pytest.param(NESTED_ATTACHMENT.replace(b"%s", b"X-Part: a\x0bb\r\n"), id="nested"),
    ],
)
def test_part_header_never_raises_a_raw_generator_error(rig, part):
    # Parts are outside the top-level check. Where the generator still ends a line on VT and has
    # its guard, as Ubuntu's 3.12.3 does, this refuses there; other Pythons write the draft.
    rig.client.raw = multipart_draft(part)
    try:
        preview = rig.service.preview_send(DRAFT_ID)
    except MailValidationError as refused:
        assert type(refused) is MailValidationError and str(refused) == MALFORMED
    else:
        assert preview["cc"] == []
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(bridge_draft(RECIPIENT), id="message"),
        pytest.param(multipart_draft(NESTED_ATTACHMENT.replace(b"%s", b"")), id="part"),
    ],
)
def test_generator_refusal_becomes_the_clean_error(rig, monkeypatch, raw):
    # HeaderWriteError is a MessageError, the only name for it on every supported Python.
    class HeaderWriteError(MessageError):
        pass

    def refuse(*args, **kwargs):
        raise HeaderWriteError("folded header contains newline: b'X-Note: a\\r\\nCc: outside'")

    monkeypatch.setattr(Message, "as_bytes", refuse)
    assert_refused(rig, raw, MALFORMED)


def test_real_generator_refusal_of_a_part_header_becomes_the_clean_error(rig):
    # A real HeaderWriteError on 3.13, 3.14 and Ubuntu's 3.12.3; other Pythons refuse in _malformed.
    part = (
        b'Content-Type: multipart/mixed; boundary="c"; '
        b"x*=utf-8''a%0D%0ACc:%20outside%40evil.test" + b"w" * 80 + b"\r\n"
        b"Content-Disposition: attachment; filename=nested.bin\r\n\r\n"
        b"--c\r\nContent-Type: text/plain\r\n\r\nInner\r\n--c--"
    )
    assert_refused(rig, multipart_draft(part), MALFORMED)


def test_other_generator_errors_give_a_token_or_the_clean_refusal(own_rig):
    # UnicodeEncodeError on Python 3.12.2 to 3.12.6, including Ubuntu's; others write this draft.
    rig = own_rig
    rig.client.raw = draft_with(b"X-Note: =?utf-8?b?w6qcOp?=\xa9")
    try:
        preview = rig.service.preview_send(DRAFT_ID)
    except MailValidationError as refused:
        assert type(refused) is MailValidationError and str(refused) == MALFORMED
    else:
        assert preview["approval"] == "token" and preview["token"]
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b"X-Note: a\x0b" + CC_LINE, id="raw"),
        pytest.param(b"X-Note: " + EIGHT_BIT + b" " + ENCODED_CC, id="encoded"),
    ],
)
def test_control_added_after_preview_refuses_without_smtp(own_rig, line):
    rig = own_rig
    rig.client.raw = bridge_draft(RECIPIENT)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "token"
    rig.client.raw = draft_with(line)
    host = Host(support=False)
    with pytest.raises(MailValidationError, match="Draft changed; request a new preview"):
        redeem(rig, preview["token"], host)
    assert rig.smtp.calls == [] and host.prompts == []
    assert records(rig)[-1]["result"] == "draft_changed"
    assert_burned(rig, preview["token"])
