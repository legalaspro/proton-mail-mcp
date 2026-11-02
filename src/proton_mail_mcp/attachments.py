"""Message attachments: list, read and save them, within fixed size caps.

Local files are touched only inside the allowed folder (PROTON_MCP_ATTACHMENTS_DIR, or
else a private per-user default), by path steps relative to it. Every step opens below an
already open directory and never follows a link, so a name cannot lead outside the
folder, not even through a link that appears after a check. Saving creates a new private
file and never overwrites one.

Attachment bytes and names come from senders. Names are cleaned before use and shown
without invisible characters; PDFs are parsed in a separate process, which has its own
time and memory limits and never sees this server's environment.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import json
import mimetypes
import os
import quopri
import re
import stat
import subprocess
import sys
import unicodedata
from contextlib import suppress
from typing import Any

from . import localfs
from .mail import (
    MailValidationError,
    _addresses,
    _checked,
    _literal,
    _message_id,
    _mime_parts,
    _raw_header_value,
    check_from_address,
)
from .rules_store import _descend

_MIB = 1024 * 1024
# What read_attachment fetches and decodes, and the part of it returned as an image.
MAX_READ_BYTES = 10 * _MIB
MAX_IMAGE_BYTES = 5 * _MIB
# Proton's own limit for the attachments of one message.
MAX_SAVE_BYTES = 25 * _MIB
# All of one draft's attachments: their base64 still fits the 10 MiB limit of a draft that
# can be sent. create_draft then checks the complete message.
MAX_UPLOAD_BYTES = 7 * _MIB
MAX_DRAFT_ATTACHMENTS = 20
MAX_TEXT_CHARS = 65536
MAX_PDF_PAGES = 1000
MAX_PDF_CHARS = 2_000_000
_PDF_SECONDS = 30
_PDF_MEMORY_BYTES = 1024 * _MIB
# Transfer encoding adds a third, and line ends a little more.
_ENCODED_FACTOR = 1.5
MAX_NAME_BYTES = 200
_MAX_NAME_CHARS = 1024
_MAX_DISPLAY_CHARS = 255
_MAX_NAME_TRIES = 100
_TEXT_TYPES = frozenset({"text/plain", "text/csv"})
_TEXT_SUFFIXES = (".txt", ".csv")
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_LOOKUP_INCOMPLETE = "Message lookup incomplete; retry the call"
_MESSAGE_CHANGED = "The message changed while it was being read; retry the call"
_NO_INDEX = "No attachment with that index; use list_attachments"
_SETTING = "PROTON_MCP_ATTACHMENTS_DIR"
_BAD_NAME = f"File names must be plain relative paths inside the allowed folder ({_SETTING})"
_NOT_A_FILE = f"No readable regular file with that name in the allowed folder ({_SETTING})"
_NO_FOLDER = f"Allowed attachment folder ({_SETTING}) is missing or a symbolic link"
_FOLDER_NOT_CREATED = f"Allowed attachment folder ({_SETTING}) could not be created"
_TOO_MUCH = f"Attachments exceed the {MAX_UPLOAD_BYTES // _MIB} MiB limit of one draft"
_TOO_MANY = f"A draft can hold at most {MAX_DRAFT_ATTACHMENTS} attachments"
# The server's BODYSTRUCTURE gives the encoded count only; decoding needs the file's bytes.
ENCODED_SIZE_NOTE = (
    "An attachment's size is the transfer-encoded byte count stored in the message; base64 is "
    "about a third larger than the file. preview_send and save_attachment report the file's "
    "decoded bytes."
)
# A composer lists both: a local file's own size, and a saved message's encoded one.
ATTACHED_SIZE_NOTE = (
    "A size with size_encoded true is the transfer-encoded byte count stored in the message; "
    "base64 is about a third larger than the file. Any other size is the file's decoded bytes, "
    "as preview_send reports them."
)
# A message type a sender wrote, as one token, and a charset name.
_MIME_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*")
_CHARSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,39}")
# Control characters in a sender's text; tab, line feed and carriage return stay.
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Some PDF fonts map a hyphen or dash glyph to U+0000, so one inside a word or ID is kept as
# a hyphen. [^\W_] is str.isalnum for one character.
_JOINED_NUL = re.compile(r"(?<=[^\W_])\x00(?=[^\W_])")
# A line end may be CRLF. Possessive: a plain group holds 1.2 GiB for 10 MiB of newlines.
_NEWLINE_RUN = re.compile(r"(?:\r?\n){3,}+")
# Hyphens in PDF text. A PDF does no line breaking of its own, so U+00AD here is a painted
# hyphen (code 0xAD of WinAnsi, which pypdf returns as U+00AD) that a reader would not see.
_PDF_HYPHENS = str.maketrans(dict.fromkeys("\u00ad\u2010\u2011\u2212", "-"))

# A hostile PDF can only waste this process's bounded time and memory. It gets no
# environment, so it never holds the server's token or the Bridge password.
_PDF_WORKER = """
import io, json, logging, sys
{limits}
logging.disable(logging.CRITICAL)
from pypdf import PdfReader
reader = PdfReader(io.BytesIO(sys.stdin.buffer.read()), strict=False)
if reader.is_encrypted and not reader.decrypt(""):
    print(json.dumps({{"encrypted": True}}))
    raise SystemExit
pages, chars, failed = [], 0, 0
total = len(reader.pages)
for index in range(min(total, {pages})):
    try:
        text = (reader.pages[index].extract_text() or "")[:{chars}]
    except Exception:
        failed += 1
        continue
    pages.append(text)
    chars += len(text)
    if chars >= {chars}:
        break
text = "\\n\\n".join(pages)
print(json.dumps({{"pages": total, "read": len(pages) + failed, "failed": failed, "text": text}}))
"""
_PDF_LIMITS = """
import resource
for name, value in (("RLIMIT_AS", {memory}), ("RLIMIT_CPU", {seconds}), ("RLIMIT_FSIZE", 0)):
    try:
        resource.setrlimit(getattr(resource, name), (value, value))
    except (ValueError, OSError):
        pass
"""
# Windows has no rlimits, so the worker joins a job of its own, with the same memory and CPU time
# and one process. Nothing there forbids writing files. Without the limits it does not parse.
_PDF_WINDOWS_LIMITS = """
import ctypes
from ctypes import wintypes
class Basic(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD), ("min_set", ctypes.c_size_t), ("max_set", ctypes.c_size_t),
        ("processes", wintypes.DWORD), ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD),
    ]
class Extended(ctypes.Structure):
    _fields_ = [
        ("basic", Basic), ("io", ctypes.c_uint64 * 6), ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]
kernel32 = ctypes.WinDLL("kernel32")
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
]
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
limits = Extended(process_memory={memory})
limits.basic.process_time = {seconds} * 10000000
limits.basic.processes = 1
# JOB_OBJECT_LIMIT_PROCESS_TIME, _ACTIVE_PROCESS and _PROCESS_MEMORY.
limits.basic.flags = 0x2 | 0x8 | 0x100
job = kernel32.CreateJobObjectW(None, None)
if not (
    job
    # JobObjectExtendedLimitInformation.
    and kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
    and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
):
    raise SystemExit(1)
"""


def _invisible(char: str) -> bool:
    category = unicodedata.category(char)
    return category[0] == "C" or category in {"Zl", "Zp"}


def _visible(value: str) -> str:
    return "".join("_" if _invisible(char) else char for char in value)


def visible_name(value: str, limit: int = _MAX_DISPLAY_CHARS) -> str:
    """A sender's file name for display: controls and invisible characters become "_".

    A shortened name keeps its end, so a long one cannot push its real extension away.
    """
    shown = _visible(value)
    return shown if len(shown) <= limit else shown[: limit - 19] + "..." + shown[-16:]


def visible_type(value: Any) -> str | None:
    """A content type a sender wrote, for display."""
    return visible_name(value, 100) if isinstance(value, str) else None


def safe_filename(value: Any, fallback: str) -> str:
    """A sender's file name as one harmless name: no path, invisible character or leading dot."""
    # "=?" would be decoded once more when the name is written to a draft's header, past
    # everything below that cleans it.
    name = _visible(value).replace("=?", "=_") if isinstance(value, str) else ""
    name = re.split(r"[/\\]", name)[-1].lstrip(" .-").rstrip(" .") or fallback
    stem, extension = os.path.splitext(name)
    if len(extension.encode("utf-8")) > 16:
        stem, extension = name, ""
    room = MAX_NAME_BYTES - len(extension.encode("utf-8"))
    return stem.encode("utf-8")[:room].decode("utf-8", "ignore") + extension


def _steps(name: Any) -> list[str]:
    """The path steps of a name inside the allowed folder; anything else refuses."""
    if (
        not isinstance(name, str)
        or not name
        or len(name) > _MAX_NAME_CHARS
        or "\\" in name
        or any(_invisible(char) for char in name)
    ):
        raise MailValidationError(_BAD_NAME)
    steps = name.split("/")
    # Covers absolute paths, "a//b", "." and ".."; hidden files stay out of reach as well.
    # On Windows also drives (C:x), streams, devices and short names.
    if any(not step or step.startswith(".") or localfs.reserved_name(step) for step in steps):
        raise MailValidationError(_BAD_NAME)
    return steps


def _open_root(settings: Any, *, create: bool = False) -> int:
    """The allowed folder, open: a real directory that not everyone can write to.

    create makes the per-user default as mkdir -p does, only the last one private, and
    changes nothing that exists. A folder the operator named must exist already.
    """
    root = settings.attachments_dir
    if not root:
        raise MailValidationError(f"No allowed attachment folder is configured; set {_SETTING}")
    if create and settings.attachments_dir_default:
        try:
            os.makedirs(root, 0o700, exist_ok=True)
        except FileExistsError:
            pass  # Something other than a folder is there; refused below.
        except OSError:
            raise MailValidationError(_FOLDER_NOT_CREATED) from None
    try:
        descriptor = localfs.open(
            root, os.O_RDONLY | localfs.O_DIRECTORY | localfs.O_NOFOLLOW | localfs.O_CLOEXEC
        )
    except OSError:
        raise MailValidationError(_NO_FOLDER) from None
    if localfs.world_writable(os.fstat(descriptor)):
        os.close(descriptor)
        raise MailValidationError(
            f"Allowed attachment folder ({_SETTING}) must not be writable by everyone"
        )
    # A saved file takes its access rules from this folder on Windows.
    refusal = localfs.acl_refusal(descriptor)
    if refusal:
        os.close(descriptor)
        raise MailValidationError(f"Allowed attachment folder ({_SETTING}) {refusal}")
    return descriptor


def _open_local(settings: Any, name: Any) -> tuple[int, os.stat_result]:
    """Open a regular file by its steps below the allowed folder, following no link."""
    steps = _steps(name)
    root = _open_root(settings)
    try:
        folder = _descend(root, steps[:-1])
        try:
            # O_NONBLOCK: opening a FIFO must refuse below, not wait for a writer.
            descriptor = localfs.open(
                steps[-1],
                os.O_RDONLY | localfs.O_NOFOLLOW | localfs.O_NONBLOCK | localfs.O_CLOEXEC,
                dir_fd=folder,
            )
        finally:
            os.close(folder)
    except OSError:
        raise MailValidationError(_NOT_A_FILE) from None
    finally:
        os.close(root)
    info = os.fstat(descriptor)
    # A second name could be a hard link to a file outside the folder.
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise MailValidationError(_NOT_A_FILE)
    return descriptor, info


def _read_local(settings: Any, name: str) -> bytes:
    descriptor, info = _open_local(settings, name)
    with os.fdopen(descriptor, "rb") as stream:
        if info.st_size > MAX_UPLOAD_BYTES:
            raise MailValidationError(_TOO_MUCH)
        # Bound the read itself: the file can grow after the size check.
        data = stream.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise MailValidationError(_TOO_MUCH)
    return data


def _guess_type(name: str) -> str:
    kind, encoding = mimetypes.guess_type(name, strict=True)
    return kind if kind and not encoding else "application/octet-stream"


def _name_list(names: Any) -> list[str]:
    if names is None:
        return []
    if not isinstance(names, list) or len(set(map(repr, names))) != len(names):
        raise MailValidationError("attachments must list distinct file names")
    if len(names) > MAX_DRAFT_ATTACHMENTS:
        raise MailValidationError(_TOO_MANY)
    return names


def local_attachments(client: Any, names: Any) -> list[dict[str, Any]]:
    """What named files in the allowed folder would attach: name, type and size."""
    rows = []
    for name in _name_list(names):
        descriptor, info = _open_local(client.settings, name)
        os.close(descriptor)
        if info.st_size > MAX_UPLOAD_BYTES:
            raise MailValidationError(_TOO_MUCH)
        rows.append({"name": name, "type": _guess_type(name), "size": info.st_size})
    return rows


def _write_new(settings: Any, name: str, data: bytes) -> str:
    """Create a new private file in the allowed folder; an existing name gets a number."""
    stem, extension = os.path.splitext(name)
    root = _open_root(settings)
    try:
        for attempt in range(_MAX_NAME_TRIES):
            candidate = f"{stem} ({attempt}){extension}" if attempt else name
            try:
                # O_EXCL also refuses a link at this name, even one pointing nowhere.
                descriptor = localfs.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | localfs.O_NOFOLLOW | localfs.O_CLOEXEC,
                    0o600,
                    dir_fd=root,
                )
            except FileExistsError:
                continue
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
            except BaseException:
                with suppress(OSError):
                    localfs.unlink(candidate, dir_fd=root)
                raise
            return candidate
    except OSError:
        raise MailValidationError(
            f"Attachment could not be written to the allowed folder ({_SETTING})"
        ) from None
    finally:
        os.close(root)
    raise MailValidationError(f"Too many files with this name in the allowed folder ({_SETTING})")


def _message(client: Any, message_id: str, *, text: bool = False) -> dict[str, Any]:
    """The one requested message, read with read_thread's bounds and lookup."""
    message_id = _message_id(message_id)
    result = client.read_thread(message_id, include_thread=False)
    item = next(
        (entry for entry in result.get("messages") or [] if entry.get("message_id") == message_id),
        None,
    )
    if item is None:
        if result.get("partial") or result.get("timed_out"):
            raise MailValidationError(_LOOKUP_INCOMPLETE)
        raise MailValidationError("Message not found; check its Message-ID")
    # A read that stopped before the MIME structure lists no attachments; that is not "none".
    # One that lost the text would forward nothing but the original's headers.
    if item.get("body_size_bytes") is None or (
        text and (result.get("timed_out") or result.get("partial"))
    ):
        raise MailValidationError(_LOOKUP_INCOMPLETE)
    return item


def _decoded(raw: bytes, encoding: str) -> bytes:
    if encoding == "base64":
        try:
            return base64.b64decode(b"".join(raw.split()))
        except binascii.Error:
            raise MailValidationError("Attachment could not be decoded") from None
    if encoding == "quoted-printable":
        return quopri.decodestring(raw)
    if encoding in {"", "7bit", "8bit", "binary"}:
        return raw
    raise MailValidationError("Attachment uses an unsupported transfer encoding")


def _fetch_parts(
    client: Any,
    item: dict[str, Any],
    indexes: list[int],
    limit: int,
    too_large: str,
    *,
    deadline: Any = None,
) -> list[tuple[int, dict[str, Any], bytes]]:
    """Fetch and decode the chosen attachments of a message, at most limit bytes in total."""
    from .search import _uidvalidity

    handle = item["alias"]
    uid = handle["uid"]
    deadline = deadline or client._deadline()
    encoded_limit = int(limit * _ENCODED_FACTOR)
    fetched = []
    total = 0
    with client._session(deadline=deadline) as (connection, _):
        # As in read_thread, content reads do no index maintenance.
        connection._search_index = None
        client._select(connection, handle["folder"])
        record = client._metadata(
            connection, [uid], deadline=deadline, internaldate=False, identity_headers_only=True
        ).get(uid)
        if (
            record is None
            or _uidvalidity(connection) != handle["uidvalidity"]
            or _message_id(_raw_header_value(record["headers"])) != item["message_id"]
        ):
            raise MailValidationError(_MESSAGE_CHANGED)
        parts = [part for part in _mime_parts(record["structure"]) if part["attachment"]]
        if any(not 1 <= index <= len(parts) for index in indexes):
            raise MailValidationError(_NO_INDEX)
        chosen = [parts[index - 1] for index in indexes]
        if sum(part["size"] for part in chosen) > encoded_limit:
            raise MailValidationError(too_large)
        for index, part in zip(indexes, chosen):
            deadline.check(connection)
            # One byte past the limit tells a larger part from one that fits exactly.
            query = f"(BODY.PEEK[{part['section']}]<0.{encoded_limit + 1}>)"
            raw = _literal(_checked(connection.uid("FETCH", uid, query)))
            if len(raw) > encoded_limit:
                raise MailValidationError(too_large)
            data = _decoded(raw, part["encoding"])
            total += len(data)
            if total > limit:
                raise MailValidationError(too_large)
            fetched.append((index, part, data))
    return fetched


def _fallback_name(index: int, part: dict[str, Any]) -> str:
    return f"attachment-{index}{mimetypes.guess_extension(part['type']) or ''}"


def copied_files(
    client: Any,
    item: dict[str, Any],
    indexes: list[int],
    *,
    limit: int | None = None,
    deadline: Any = None,
) -> list[dict[str, Any]]:
    """The chosen attachments of a read message, ready for a new draft."""
    limit = MAX_UPLOAD_BYTES if limit is None else limit
    return [
        {
            "name": safe_filename(part["filename"], _fallback_name(index, part)),
            "type": part["type"],
            "charset": part["charset"],
            "data": data,
        }
        for index, part, data in _fetch_parts(
            client, item, indexes, limit, _TOO_MUCH, deadline=deadline
        )
    ]


def check_indexes(indexes: Any) -> list[int]:
    if (
        not isinstance(indexes, list)
        or any(type(index) is not int or index < 1 for index in indexes)
        or len(set(indexes)) != len(indexes)
    ):
        raise MailValidationError("Attachment indexes must be distinct positive integers")
    return indexes


def draft_files(
    client: Any, names: Any, source: Any, indexes: Any, *, deadline: Any = None
) -> list[dict[str, Any]]:
    """What create_draft attaches: files of the allowed folder, then a message's attachments."""
    names = _name_list(names)
    indexes = check_indexes([] if indexes is None else indexes)
    if bool(indexes) != bool(source):
        raise MailValidationError("attachments_from and attachment_indexes go together")
    if len(names) + len(indexes) > MAX_DRAFT_ATTACHMENTS:
        raise MailValidationError(_TOO_MANY)
    files = []
    total = 0
    for name in names:
        data = _read_local(client.settings, name)
        total += len(data)
        if total > MAX_UPLOAD_BYTES:
            raise MailValidationError(_TOO_MUCH)
        base = name.rsplit("/", 1)[-1]
        files.append(
            {
                "name": safe_filename(base, "attachment"),
                "type": _guess_type(base),
                "charset": None,
                "data": data,
            }
        )
    if indexes:
        files.extend(
            copied_files(
                client,
                _message(client, source),
                indexes,
                limit=MAX_UPLOAD_BYTES - total,
                deadline=deadline,
            )
        )
    return files


def add_files(message: Any, files: list[dict[str, Any]]) -> None:
    """Attach files to a new draft, base64-encoded so that no content is parsed on its way."""
    for file in files:
        kind = file["type"] or ""
        # A message or multipart part in base64 is invalid MIME. It stays a file.
        if not _MIME_TYPE.fullmatch(kind) or kind.split("/")[0] in {"message", "multipart"}:
            kind = "application/octet-stream"
        maintype, subtype = kind.split("/")
        charset = file.get("charset")
        message.add_attachment(
            file["data"],
            maintype=maintype,
            subtype=subtype,
            filename=file["name"],
            params={"charset": charset}
            if maintype == "text" and charset and _CHARSET.fullmatch(charset)
            else None,
        )


def _row(index: int, filename: str | None, kind: Any, size: Any) -> dict[str, Any]:
    name = visible_name(filename) if filename else None
    return {"index": index, "filename": name, "type": visible_type(kind), "size": size}


def list_attachments(client: Any, message_id: str) -> dict[str, Any]:
    item = _message(client, message_id)
    return {
        "message_id": item["message_id"],
        "attachments": [
            _row(index, entry["filename"], entry["type"], entry["size"])
            for index, entry in enumerate(item["attachments"], start=1)
        ],
        "size_note": ENCODED_SIZE_NOTE,
    }


def _image_type(data: bytes) -> str | None:
    for signature, kind in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return kind
    return "image/webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None


def _is_pdf(part: dict[str, Any], data: bytes) -> bool:
    if data.startswith(b"%PDF-"):
        return True
    declared = part["type"] == "application/pdf" or (part["filename"] or "").lower().endswith(
        ".pdf"
    )
    # PDF readers accept a little junk before the header; a declared PDF may have it.
    return declared and b"%PDF-" in data[:1024]


def _is_text(part: dict[str, Any], data: bytes) -> bool:
    declared = part["type"] in _TEXT_TYPES or (part["filename"] or "").lower().endswith(
        _TEXT_SUFFIXES
    )
    if not declared:
        return False
    return data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) or b"\0" not in data


def _text(part: dict[str, Any], data: bytes) -> str:
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig", errors="replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")
    try:
        # Punycode decodes in quadratic time, hours for a file at the read limit, and no
        # mail text uses it. A sender's label must not be able to stall the server.
        if codecs.lookup(part["charset"]).name == "punycode":
            raise LookupError
        text = data.decode(part["charset"], errors="replace")
    except (LookupError, ValueError):
        text = data.decode("utf-8", errors="replace")
    # UTF-7 and escape codecs can yield lone surrogates, which no response can carry.
    return text.encode("utf-8", "replace").decode("utf-8")


def _clean_text(text: str) -> str:
    """A sender's text without control characters or more than two newlines in a row."""
    # Controls first: one can sit between newlines and hide their run.
    return _NEWLINE_RUN.sub("\n\n", _CONTROLS.sub("", _JOINED_NUL.sub("-", text)))


def _pdf_text(data: bytes) -> dict[str, Any]:
    windows = sys.platform == "win32"
    limits = (_PDF_WINDOWS_LIMITS if windows else _PDF_LIMITS).format(
        memory=_PDF_MEMORY_BYTES, seconds=_PDF_SECONDS
    )
    script = _PDF_WORKER.format(limits=limits, pages=MAX_PDF_PAGES, chars=MAX_PDF_CHARS)
    # -I and an empty environment: only the installed packages, none of this server's
    # settings, and no bytecode to write. A Windows program cannot start without SYSTEMROOT.
    with subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", script],
        env={"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")} if windows else {},
        cwd=os.path.abspath(os.sep),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        try:
            output, _ = process.communicate(data, timeout=_PDF_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise MailValidationError(
                "PDF text extraction took too long; save_attachment can keep the file"
            ) from None
    try:
        if process.returncode != 0:
            raise ValueError
        result = json.loads(output)
    except ValueError:
        raise MailValidationError(
            "This PDF could not be read; save_attachment can keep the file"
        ) from None
    if result.get("encrypted"):
        raise MailValidationError(
            "This PDF is password-protected; save_attachment can keep the file"
        )
    return result


def read_attachment(
    client: Any, message_id: str, attachment: int, offset: int = 0
) -> dict[str, Any]:
    """Text of a PDF, text or CSV attachment, or an image; other types refuse."""
    if type(offset) is not int or offset < 0:
        raise MailValidationError("offset must be a non-negative integer")
    item = _message(client, message_id)
    [(index, part, data)] = _fetch_parts(
        client,
        item,
        [attachment],
        MAX_READ_BYTES,
        f"Attachment exceeds the {MAX_READ_BYTES // _MIB} MiB read limit; "
        "save_attachment can keep the file",
    )
    result: dict[str, Any] = {
        **_row(index, part["filename"], part["type"], part["size"]),
        "decoded_size": len(data),
    }
    image = _image_type(data)
    if image is not None:
        if len(data) > MAX_IMAGE_BYTES:
            raise MailValidationError(
                f"Image exceeds the {MAX_IMAGE_BYTES // _MIB} MiB limit; "
                "save_attachment can keep the file"
            )
        return {
            **result,
            "kind": "image",
            "mime_type": image,
            "image": base64.b64encode(data).decode("ascii"),
        }
    more = False
    if _is_pdf(part, data):
        extracted = _pdf_text(data)
        # A hostile font map can yield lone surrogates, which no response can carry.
        text = extracted["text"].encode("utf-8", "replace").decode("utf-8")
        text = _clean_text(text.translate(_PDF_HYPHENS))
        more = extracted["read"] < extracted["pages"]
        result.update(
            kind="pdf",
            pages=extracted["pages"],
            pages_read=extracted["read"],
            pages_failed=extracted["failed"],
        )
        if not text.strip():
            result["note"] = (
                "No extractable text; it may be a scan. save_attachment keeps the file."
            )
    elif _is_text(part, data):
        text = _clean_text(_text(part, data))
        result["kind"] = "text"
    else:
        raise MailValidationError(
            "Only PDF, TXT, CSV and image attachments can be read; "
            "save_attachment can keep the file"
        )
    window = text[offset : offset + MAX_TEXT_CHARS]
    following = offset + len(window) if offset + len(window) < len(text) else None
    return {
        **result,
        "text": window,
        "offset": offset,
        "total_chars": len(text),
        "next_offset": following,
        "truncated": following is not None or more,
    }


def save_attachment(
    client: Any, message_id: str, attachment: int, filename: str = ""
) -> dict[str, Any]:
    """Save one attachment as a new file in the allowed folder; no folder, no mail read."""
    if filename and (len(_steps(filename)) != 1 or len(filename.encode()) > MAX_NAME_BYTES):
        raise MailValidationError(_BAD_NAME)
    os.close(_open_root(client.settings, create=True))
    item = _message(client, message_id)
    [(index, part, data)] = _fetch_parts(
        client,
        item,
        [attachment],
        MAX_SAVE_BYTES,
        f"Attachment exceeds the {MAX_SAVE_BYTES // _MIB} MiB save limit",
    )
    name = filename or localfs.plain_name(
        safe_filename(part["filename"], _fallback_name(index, part))
    )
    return {
        "saved": True,
        "filename": _write_new(client.settings, name, data),
        "index": index,
        "type": part["type"],
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def forward_message(
    client: Any,
    message_id: str,
    to: Any,
    cc: Any = None,
    *,
    attachments: Any,
    note: str = "",
    from_address: str = "",
) -> dict[str, Any]:
    """Save a new unsent draft forwarding one message with only the chosen attachments."""
    from .views import forward_fields

    # What create_draft would refuse is refused before reading any mail.
    if from_address:
        check_from_address(client.settings, from_address)
    _addresses(to, required=True)
    _addresses(cc)
    indexes = check_indexes(attachments)
    if len(indexes) > MAX_DRAFT_ATTACHMENTS:
        raise MailValidationError(_TOO_MANY)
    if not isinstance(note, str):
        raise MailValidationError("Forward note must be text")
    item = _message(client, message_id, text=True)
    rows = [
        _row(index, entry["filename"], entry["type"], entry["size"])
        for index, entry in enumerate(item["attachments"], start=1)
    ]
    if any(index > len(rows) for index in indexes):
        raise MailValidationError(_NO_INDEX)
    deadline = client._deadline()
    subject, body = forward_fields(item, note)
    draft_id = client.create_draft(
        to,
        cc,
        subject=subject,
        body=body,
        _files=copied_files(client, item, indexes, deadline=deadline) if indexes else [],
        _deadline=deadline,
        **({"from_address": from_address} if from_address else {}),
    )
    warnings = []
    if item.get("truncated"):
        warnings.append("The original message is longer; only its beginning is in the draft.")
    return {
        "draft_id": draft_id,
        "subject": subject,
        "attachments": [row for row in rows if row["index"] in indexes],
        "not_forwarded": [row for row in rows if row["index"] not in indexes],
        "size_note": ENCODED_SIZE_NOTE,
        "warnings": warnings,
        "next_steps": (
            "This is an unsent draft. To send it, call preview_send(draft_id), or review_send "
            "to show it first, then send_draft(token, draft_id): the user confirms From, "
            "recipients and every attachment in the host's form, or by a typed OK when the client "
            "has no form. compose_reply(draft_id=...) reopens it."
        ),
    }
