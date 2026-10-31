"""Synthetic attachments: real file bytes in the BODYSTRUCTURE shape Bridge reports."""

import base64
import quopri

from test_mail import PLAIN, FakeImap, client, message  # noqa: F401
from test_mail import settings as settings  # noqa: F401


def pdf_bytes(*pages, font=b""):
    """A small valid PDF with one line of text per page; font adds to its font entry."""
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica" + font + b" >>",
    }
    kids = []
    number = 4
    for text in pages:
        stream = b"BT /F1 12 Tf 72 720 Td (" + text.encode("ascii") + b") Tj ET"
        objects[number] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents "
            + f"{number + 1} 0 R".encode()
            + b" /Resources << /Font << /F1 3 0 R >> >> >>"
        )
        objects[number + 1] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
        kids.append(f"{number} 0 R")
        number += 2
    objects[2] = f"<< /Type /Pages /Count {len(pages)} /Kids [{' '.join(kids)}] >>".encode()
    output = b"%PDF-1.4\n"
    offsets = {}
    for key in sorted(objects):
        offsets[key] = len(output)
        output += f"{key} 0 obj\n".encode() + objects[key] + b"\nendobj\n"
    start = len(output)
    output += f"xref\n0 {number}\n0000000000 65535 f \n".encode()
    for key in range(1, number):
        output += f"{offsets[key]:010d} 00000 n \n".encode()
    output += f"trailer\n<< /Size {number} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    return output


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def encoded(data, encoding):
    if encoding == "BASE64":
        text = base64.encodebytes(data).replace(b"\n", b"\r\n")
    elif encoding == "QUOTED-PRINTABLE":
        text = quopri.encodestring(data)
    else:
        text = data
    return text


def part(name, kind, data, *, encoding="BASE64", charset=None, size=None, inline_name=False):
    """One attachment part: its BODYSTRUCTURE entry and encoded section."""
    main, sub = kind.upper().split("/")
    body = encoded(data, encoding)
    parameters = (
        b'("CHARSET" "' + charset.encode() + b'")'
        if charset
        else (b'("NAME" "' + name.encode() + b'")' if inline_name and name else b"NIL")
    )
    disposition = (
        b'("ATTACHMENT" ("FILENAME" "' + name.encode() + b'"))' if name else b'("ATTACHMENT" NIL)'
    )
    octets = len(body) if size is None else size
    lines = b" 1" if main == "TEXT" else b""
    structure = (
        b'("'
        + main.encode()
        + b'" "'
        + sub.encode()
        + b'" '
        + parameters
        + b' NIL NIL "'
        + encoding.encode()
        + b'" '
        + str(octets).encode()
        + lines
        + b" NIL "
        + disposition
        + b" NIL)"
    )
    return structure, body


def mail_with(identifier, *parts, flags=None, text=b"Hello plain text\r\n", **options):
    """A message with a text part and the given attachment parts."""
    plain = PLAIN.replace(b'"7BIT" 24', b'"7BIT" ' + str(len(text)).encode())
    structure = (
        b"(" + plain + b" " + b" ".join(entry[0] for entry in parts) + b' "MIXED" ("BOUNDARY" "x"))'
    )
    sections = {"1": text, **{str(i): entry[1] for i, entry in enumerate(parts, start=2)}}
    return message(identifier, structure=structure, sections=sections, flags=flags, **options)
