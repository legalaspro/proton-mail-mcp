"""A draft as Bridge stores it: non-ASCII attachment names as encoded words inside quotes.

The doubles elsewhere replay Python's own bytes, which use filename*=. These bytes are the
form Bridge writes (pkg/message/build.go getAttachmentPartHeader, pinned by Bridge's
TestBuildAttachmentWithExoticFilename), in go-message's field order and folding.
"""

import base64
import hashlib

import pytest
from test_send import DRAFT_ID, RECIPIENT, SENDER, Host, redeem
from test_send import local_certificate as local_certificate
from test_send import rig as rig

from proton_mail_mcp.mail import MailValidationError

BOUNDARY = b"0123456789abcdef" * 4
CONTENT = b"%PDF-1.4 kept bytes"
RESUME = b"=?utf-8?q?R=C3=A9sum=C3=A9.pdf?="
RESUME_DISPOSITION = b'Content-Disposition: attachment; filename="' + RESUME + b'"'
RESUME_TYPE = (
    b'Content-Type: application/pdf; filename="' + RESUME + b'";\r\n name="' + RESUME + b'"'
)
RESUME_HEADERS = RESUME_DISPOSITION + b"\r\n" + RESUME_TYPE


def stored(*attachment_headers, multipart=b"multipart/mixed;\r\n boundary=" + BOUNDARY, text=None):
    """A multipart draft with Bridge's top headers, a text part and these attachment parts."""
    text = text or b"Content-Type: text/plain; charset=utf-8"
    lines = [
        b"Subject: A draft",
        f'From: "Sender" <{SENDER}>'.encode(),
        f"To: <{RECIPIENT}>".encode(),
        f"Message-Id: {DRAFT_ID}".encode(),
        b"Date: Sat, 31 Oct 2026 12:00:00 +0000",
        b"Mime-Version: 1.0",
        b"Content-Type: " + multipart,
        b"",
        b"--" + BOUNDARY,
        b"Content-Transfer-Encoding: quoted-printable",
        text,
        b"",
        b"Hello=",
        b"",
    ]
    for headers in attachment_headers:
        lines += [
            b"--" + BOUNDARY,
            b"Content-Transfer-Encoding: base64",
            headers,
            b"",
            base64.b64encode(CONTENT),
        ]
    return b"\r\n".join([*lines, b"--" + BOUNDARY + b"--", b""])


def named(word, kind=b"application/pdf"):
    """An attachment's two headers with this encoded word in all three places Bridge sets it."""
    return (
        b'Content-Disposition: attachment;\r\n filename="' + word + b'"\r\n'
        b"Content-Type: " + kind + b';\r\n filename="' + word + b'";\r\n name="' + word + b'"'
    )


def test_a_non_ascii_name_as_bridge_stores_it_previews_and_sends_unchanged(rig):
    raw = stored(RESUME_HEADERS)
    rig.client.raw = raw
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["attachments"] == [
        {
            "name": "Résumé.pdf",
            "type": "application/pdf",
            "size": len(CONTENT),
            "sha256": hashlib.sha256(CONTENT).hexdigest(),
        }
    ]
    assert rig.service.review_send(DRAFT_ID)["attachments"] == preview["attachments"]
    host = Host()
    assert redeem(rig, preview["token"], host)["sent"] is True
    assert '"Résumé.pdf"' in host.prompts[0].split("\n")[0]
    # What goes out is what Bridge stored, byte for byte: nothing is rewritten to pass.
    assert rig.smtp.sent == [(SENDER, (RECIPIENT,), raw)]


@pytest.mark.parametrize(
    "word,kind,expected",
    [
        # Bridge's own test of this form.
        (
            b"=?utf-8?q?I_=C5=99eally_=C5=A1hould_lea=C5=99n_czech.png?=",
            b"image/png",
            "I řeally šhould leařn czech.png",
        ),
        (b"=?utf-8?q?=C3=9Cbersicht.csv?=", b"text/csv; charset=utf-8", "Übersicht.csv"),
        (
            b"=?utf-8?q?semi;comma,(paren)_=C3=A9.pdf?=",
            b"application/pdf",
            "semi;comma,(paren) é.pdf",
        ),
        (b"=?utf-8?q?=5Funder=3D=3F=C3=A9.pdf?=", b"application/pdf", "_under=?é.pdf"),
        (b"=?utf-8?q?=F0=9F=93=8E.pdf?=", b"application/pdf", chr(0x1F4CE) + ".pdf"),
        # A quote or backslash in the name arrives escaped inside the quotes. The form shows
        # the backslash too; the name stays readable and the draft sendable.
        (b'=?utf-8?q?say_\\"hi\\"_=C3=A9.pdf?=', b"application/pdf", 'say \\"hi\\" é.pdf'),
        (b"=?utf-8?q?back\\\\slash_=C3=A9.pdf?=", b"application/pdf", "back\\\\slash é.pdf"),
    ],
)
def test_other_names_in_bridges_form_preview_with_their_real_text(rig, word, kind, expected):
    rig.client.raw = stored(named(word, kind))
    [file] = rig.service.preview_send(DRAFT_ID)["attachments"]
    assert file["name"] == expected
    assert file["size"] == len(CONTENT)


def test_the_hash_covers_a_name_in_bridges_form(rig):
    rig.client.raw = stored(RESUME_HEADERS)
    preview = rig.service.preview_send(DRAFT_ID)
    rig.client.raw = stored(RESUME_HEADERS.replace(b"R=C3=A9sum", b"N=C3=A9sum"))
    with pytest.raises(MailValidationError, match="changed"):
        redeem(rig, preview["token"], Host())
    assert rig.smtp.sent == []


@pytest.mark.parametrize(
    "raw",
    [
        # The same form in a parameter that is not a name. A MIME reader takes the boundary
        # literally, while this parser decodes it: the form and the recipient would disagree.
        stored(RESUME_HEADERS, multipart=b'multipart/mixed; boundary="=?utf-8?q?abc?="'),
        stored(RESUME_HEADERS, text=b'Content-Type: text/plain; charset="=?utf-8?q?utf-8?="'),
        # Another header than the two Bridge writes names on.
        stored(RESUME_HEADERS + b'\r\nContent-Transfer-Encoding: "=?utf-8?q?base64?="'),
        # A parameter Bridge does not write on that header.
        stored(RESUME_TYPE + b'\r\nContent-Disposition: attachment; name="' + RESUME + b'"'),
        stored(named(RESUME).replace(b' name="', b' x-name="')),
        stored(named(RESUME).replace(b' name="', b' NAME="')),
        # A name that is not exactly one word in Bridge's encoding.
        stored(named(b"=?utf-8?q?report.pdf?=.exe")),
        stored(named(b"=?utf-8?q?report.pdf?=?=")),
        stored(named(b"=?utf-8?q?a?= =?utf-8?q?b?=")),
        stored(named(b"=?utf-8?q?a?=\r\n =?utf-8?q?b?=")),
        stored(named(b"=?utf-8?B?Li4vZXZpbC5leGU=?=")),
        stored(named(b"=?UTF-8?Q?r=C3=A9sum=C3=A9.pdf?=")),
        stored(named(b"=?iso-8859-1?q?r=E9sum=E9.pdf?=")),
        stored(named("=?utf-8?q?é.pdf?=".encode())),
        stored(named(b"=?utf-8?q?r=c3=a9sum=c3=a9.pdf?=")),
        # Bridge's form that decodes to something no name may hold.
        stored(named(b"=?utf-8?q?a=0D=0Ab.pdf?=")),
        stored(named(b"=?utf-8?q?a=00b.pdf?=")),
        stored(named(b"=?utf-8?q?a=FFb.pdf?=")),
        # A second name beside it, a broken parameter, and a cut multipart.
        stored(
            RESUME_TYPE
            + b'\r\nContent-Disposition: attachment; filename="'
            + RESUME
            + b'"; filename="b.exe"'
        ),
        stored(
            RESUME_TYPE + b'\r\nContent-Disposition: attachment; filename="' + RESUME + b'"; ;;='
        ),
        stored(RESUME_HEADERS).replace(b"--" + BOUNDARY + b"--\r\n", b""),
    ],
)
def test_anything_beyond_bridges_exact_form_still_refuses(rig, raw):
    rig.client.raw = raw
    with pytest.raises(MailValidationError, match="Malformed"):
        rig.service.preview_send(DRAFT_ID)
    assert rig.smtp.sent == []
