"""Bridge/gluon-like IMAP peer over socket.socketpair() (AF_UNIX, never TCP).

The real stdlib imaplib client is used by the code under review. Semantics are
modelled on proton-bridge internal/services/imapservice/connector.go (master
b9c5dac) and gluon internal/state (dev 2561acf), as read for this review:

* every message has exactly one folder (INBOX, Drafts, Sent, Archive, Spam,
  Trash or Folders/*), any number of user labels (Labels/*), a Starred bit, and
  is visible in "All Mail"; each mailbox view has its own UID space.
* \\Deleted is stored per mailbox (gluon SetMailboxMessagesDeletedFlag) and is
  never sent to Proton.
* UID EXPUNGE <set> removes only \\Deleted UIDs in <set>; plain EXPUNGE and CLOSE
  on a read-write selection remove every \\Deleted UID (gluon handleClose).
  SELECT/EXAMINE/LOGOUT never expunge (gluon state.close()).
* removing from a mailbox (expunge) = Bridge RemoveMessagesFromMailbox:
  user label -> unlabel only; Trash/Drafts -> unlabel + PERMANENT DELETE when no
  user label/star remains; other folders -> unlabel (modelled: message loses its
  folder); All Mail/Scheduled -> NO.
* MOVE = Bridge moveMessagesWithUnlabelCallOnFolders: LabelMessages(to) then,
  if from is a user label or to is a folder/system mailbox, unlabel(from).
  Labelling a folder changes the message's single folder; labelling Starred or a
  user label adds membership. Unlabelling the message's *current* folder is an
  API-defined operation we cannot observe; it is modelled as a no-op and logged.
* COPY = Bridge AddMessagesToMailbox = LabelMessages(to) (a folder target moves).

Every forbidden or dangerous operation is appended to ``violations``.
"""

from __future__ import annotations

import imaplib
import re
import socket
import threading
from contextlib import suppress
from email import policy
from email.parser import BytesParser
from functools import lru_cache

from bridge_costs import (
    LITERAL_SEARCH_KEYS,
    WorkCounters,
    envelope_from_headers,
    literal_fetch,
    search_keys,
)

from proton_mail_mcp.mail import _encode_folder

SYSTEM_FOLDERS = ("INBOX", "Drafts", "Sent", "Archive", "Spam", "Trash")
FOLDER_ATTRS = {
    "Trash": r"\Trash",
    "Spam": r"\Junk",
    "Archive": r"\Archive",
    "Sent": r"\Sent",
    "Drafts": r"\Drafts",
}


@lru_cache(maxsize=100_000)
def _header_fields(raw):
    # SEARCH OR evaluates many terms against the same immutable header bytes.
    # Cache parsing only; changed headers have a distinct key.
    parsed = BytesParser(policy=policy.compat32).parsebytes(raw + b"\r\n")
    values = {
        name.lower(): " ".join(str(value) for value in parsed.get_all(name))
        for name in parsed.keys()
    }
    # SEARCH sees decoded subjects and complete raw identity fields. compat32
    # stringification replaces UTF-8 octets and would hide valid literal hits.
    decoded = BytesParser(policy=policy.default).parsebytes(raw + b"\r\n")
    values["subject"] = " ".join(str(value) for value in decoded.get_all("Subject", []))
    values["message-id"] = " ".join(
        value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        for name, value in parsed.raw_items()
        if name.lower() == "message-id"
    )
    return values


def hdr(
    mid,
    from_="Sender <sender@example.com>",
    subject="Hello",
    extra=b"",
    to="reader@example.test",
    date="Fri, 16 Oct 2026 09:00:00 +0000",
):
    lines = []
    if mid is not None:
        lines.append(f"Message-ID: {mid}")
    if from_ is not None:
        lines.append(f"From: {from_}")
    lines += [f"To: {to}", f"Subject: {subject}", f"Date: {date}"]
    raw = ("\r\n".join(lines) + "\r\n").encode("utf-8")
    return extra + raw


class Msg:
    _counter = 0

    def __init__(
        self, headers, folder="INBOX", labels=(), starred=False, body=b"Body text\r\n", flags=()
    ):
        Msg._counter += 1
        self.pid = f"P{Msg._counter}"
        self.headers = headers  # raw header block (bytes, CRLF, no blank line)
        self.body = body
        self.folder = folder
        self.labels = set(labels)
        self.starred = starred
        self.flags = set(flags)

    def __repr__(self):
        return (
            f"Msg({self.pid} {self.message_id()} folder={self.folder} "
            f"labels={sorted(self.labels)} starred={self.starred})"
        )

    def message_id(self):
        return _message_id(self.headers)


@lru_cache(maxsize=100_000)
def _message_id(raw):
    # Headers are immutable bytes; replacing them naturally invalidates the key.
    parsed = BytesParser(policy=policy.compat32).parsebytes(raw + b"\r\n")
    return parsed.get("Message-ID")


class Session:
    def __init__(self):
        self.selected = None
        self.readonly = True


class BridgeSim:
    def __init__(
        self,
        *,
        capabilities=("IMAP4rev1", "STARTTLS", "MOVE", "UIDPLUS"),
        show_all_mail=True,
        folders=("Folders/Reviewed", "Folders/Keep"),
        labels=("Labels/Review", "Labels/Keep"),
        copyuid=True,
        work=None,
        forbid_literals=False,
        clock=None,
        command_latency=0.0,
        strict_search=False,
    ):
        if command_latency and clock is None:
            raise ValueError("Synthetic command latency requires a clock")
        self.clock, self.command_latency = clock, command_latency
        self.work = work or WorkCounters(forbid_literals=forbid_literals)
        self.capabilities = list(capabilities)
        self.strict_search = strict_search
        self.search_rejections = []
        self.literal_modes = []
        self.show_all_mail = show_all_mail
        self.custom_folders = list(folders)
        self.user_labels = list(labels)
        self.copyuid = copyuid
        self.messages: dict[str, Msg] = {}
        self.lock = threading.RLock()
        self.uidvalidity: dict[str, int] = {}
        self.uidmap: dict[str, dict[int, str]] = {}
        self.next_uid: dict[str, int] = {}
        self.deleted: dict[str, set[int]] = {}
        self.commands: list[tuple[int, str | None, bool, str]] = []
        # Capture served UID identities before later MOVE commands change views.
        self.fetch_batches: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        self.violations: list[str] = []
        self.events: list[str] = []
        self.errors: list[BaseException] = []
        self.peers = []
        self.stopped = threading.Event()
        self.session_counter = 0
        # Hooks receive (session_id, selected, op, text), and return an optional
        # injected outcome such as "stall", "drop", or ("NO", message).
        self.hooks = []
        self.fetch_omit: set[tuple[str, str]] = set()  # (mailbox, pid) omitted from FETCH responses
        for index, name in enumerate(self.mailbox_names()):
            self.uidvalidity[name] = 1000 + index
            self.uidmap[name] = {}
            self.next_uid[name] = 1
            self.deleted[name] = set()

    # ----- model -------------------------------------------------------------
    def mailbox_names(self):
        names = ["INBOX", "Drafts", "Sent", "Starred", "Archive", "Spam", "Trash"]
        if self.show_all_mail:
            names.append("All Mail")
        return names + self.custom_folders + self.user_labels

    def is_folder(self, name):
        return name in SYSTEM_FOLDERS or name.startswith("Folders/")

    def is_user_label(self, name):
        return name.startswith("Labels/")

    def members(self, name):
        result = []
        for pid, msg in self.messages.items():
            if (
                name == "All Mail"
                or (name == "Starred" and msg.starred)
                or msg.folder == name
                or name in msg.labels
            ):
                result.append(pid)
        return result

    def sync(self):
        names = self.mailbox_names()
        if getattr(self.members, "__func__", None) is BridgeSim.members:
            # Build all views in one pass. Keep direct fixture changes visible on
            # every command, including folder/label changes and message removal.
            members = {name: [] for name in names}
            for pid, msg in self.messages.items():
                views = {msg.folder, *msg.labels, "All Mail"}
                if msg.starred:
                    views.add("Starred")
                for name in views:
                    if name in members:
                        members[name].append(pid)
        else:
            # Fault-injection tests override membership discovery deliberately.
            members = {}
            for name in names:
                current = set(self.members(name))
                members[name] = [pid for pid in self.messages if pid in current]
        for name in names:
            if name not in self.uidmap:
                self.uidvalidity[name] = 5000 + len(self.uidvalidity)
                self.uidmap[name], self.next_uid[name], self.deleted[name] = {}, 1, set()
            current = set(members[name])
            mapping = self.uidmap[name]
            for uid, pid in list(mapping.items()):
                if pid not in current:
                    del mapping[uid]
                    self.deleted[name].discard(uid)
            present = set(mapping.values())
            for pid in members[name]:
                if pid not in present:
                    mapping[self.next_uid[name]] = pid
                    self.next_uid[name] += 1

    def add(self, msg: Msg):
        with self.lock:
            self.messages[msg.pid] = msg
            self.sync()
        return msg

    def add_many(self, messages):
        """Populate a fixture without rebuilding every view after each row."""
        messages = list(messages)
        with self.lock:
            self.messages.update((msg.pid, msg) for msg in messages)
            self.sync()
        return messages

    def uid_of(self, name, pid):
        with self.lock:
            self.sync()
            for uid, other in self.uidmap[name].items():
                if other == pid:
                    return uid
        return None

    def where(self, pid):
        with self.lock:
            self.sync()
            return sorted(
                name for name in self.mailbox_names() if pid in self.uidmap[name].values()
            )

    def snapshot(self):
        with self.lock:
            self.sync()
            return {
                pid: (m.folder, tuple(sorted(m.labels)), m.starred)
                for pid, m in self.messages.items()
            }

    def reset_uidvalidity(self, name, renumber=True):
        """Simulate a UIDVALIDITY change; optionally renumber UIDs from 1 in reverse order."""
        with self.lock:
            self.sync()
            self.uidvalidity[name] += 7777
            pids = list(self.uidmap[name].values())
            if renumber:
                pids.reverse()
            self.uidmap[name] = {i + 1: pid for i, pid in enumerate(pids)}
            self.next_uid[name] = len(pids) + 1
            self.deleted[name] = set()

    # Bridge connector operations
    def label(self, pid, name):
        msg = self.messages[pid]
        if name in ("All Mail",):
            raise PermissionError("operation not allowed")
        if self.is_folder(name):
            msg.folder = name
        elif name == "Starred":
            msg.starred = True
        elif self.is_user_label(name):
            msg.labels.add(name)
        else:
            raise PermissionError("unknown mailbox")

    def unlabel(self, pid, name, *, why):
        msg = self.messages[pid]
        if name == "All Mail":
            raise PermissionError("operation not allowed")
        if self.is_user_label(name):
            msg.labels.discard(name)
        elif name == "Starred":
            msg.starred = False
        elif self.is_folder(name):
            if msg.folder == name:
                self.events.append(
                    f"UNLABEL-CURRENT-FOLDER {pid} {name} ({why}); modelled as no-op"
                )
        if name in ("Trash", "Drafts") and why == "expunge":
            remaining = msg.labels or msg.starred or (msg.folder not in (None, name))
            if not remaining:
                self.violations.append(f"PERMANENT DELETE of {pid} via expunge in {name}")
                del self.messages[pid]
            elif msg.folder == name:
                msg.folder = None
                self.violations.append(f"message {pid} lost its folder via expunge in {name}")
        elif self.is_folder(name) and why == "expunge" and msg.folder == name:
            msg.folder = None
            self.violations.append(f"message {pid} lost its folder via expunge in {name}")

    def remove_from_mailbox(self, name, pids):
        for pid in pids:
            if not self.is_user_label(name):
                self.violations.append(f"EXPUNGE outside Labels/: {name} {pid}")
            self.unlabel(pid, name, why="expunge")
            self.events.append(f"REMOVE {pid} from {name}")

    # ----- protocol ----------------------------------------------------------
    def factory(self):
        sim = self

        class SimIMAP(imaplib.IMAP4):
            def uid(self, command, *args):
                if sim.work.forbid_literals:
                    raw = b" ".join(
                        item if isinstance(item, bytes) else str(item).encode()
                        for item in args
                        if item is not None
                    )
                    assert not (command.upper() == "FETCH" and literal_fetch(raw)), (
                        "Cleanup literal FETCH forbidden"
                    )
                    assert not (
                        command.upper() == "SEARCH" and search_keys(raw) & LITERAL_SEARCH_KEYS
                    ), "Cleanup literal SEARCH forbidden"
                return super().uid(command, *args)

            def _create_socket(self, timeout):
                local, remote = socket.socketpair()
                local.settimeout(timeout)
                worker = threading.Thread(target=sim.serve, args=(remote,), daemon=True)
                sim.peers.append((local, remote, worker))
                worker.start()
                return local

            def starttls(self, ssl_context=None):
                return self._simple_command("STARTTLS")

        return SimIMAP

    def close(self):
        self.stopped.set()
        for local, remote, worker in self.peers:
            for sock in (local, remote):
                with suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                with suppress(OSError):
                    sock.close()
            worker.join(timeout=2)

    def _read_command(self, reader, sock):
        line = reader.readline()
        if not line:
            return None
        data = line
        while True:
            match = re.search(rb"\{(\d+)(\+?)\}\r\n$", data)
            if not match:
                break
            self.literal_modes.append(bool(match.group(2)))
            if self.strict_search and match.group(2) and "LITERAL+" not in self.capabilities:
                self.search_rejections.append("Unadvertised non-synchronizing literal")
            if not match.group(2):
                sock.sendall(b"+ go ahead\r\n")
            literal = reader.read(int(match.group(1)))
            data = data[: match.start()] + b"\x00LIT" + literal.hex().encode() + b"\x00"
            data += reader.readline()
        return data.rstrip(b"\r\n")

    @staticmethod
    def _tokens(text: bytes):
        tokens = []
        i = 0
        while i < len(text):
            c = text[i : i + 1]
            if c in b" ":
                i += 1
                continue
            if c == b'"':
                j = i + 1
                out = bytearray()
                while j < len(text) and text[j : j + 1] != b'"':
                    if text[j : j + 1] == b"\\":
                        j += 1
                    out += text[j : j + 1]
                    j += 1
                tokens.append(bytes(out).decode("utf-8"))
                i = j + 1
                continue
            if text[i : i + 4] == b"\x00LIT":
                j = text.index(b"\x00", i + 1)
                tokens.append(bytes.fromhex(text[i + 4 : j].decode()).decode("utf-8"))
                i = j + 1
                continue
            j = i
            while j < len(text) and text[j : j + 1] not in b" ":
                j += 1
            tokens.append(text[i:j].decode("utf-8"))
            i = j
        return tokens

    @staticmethod
    def _uids(spec, available):
        result = []
        top = max(available, default=0)
        available_set = set(available)
        for part in spec.split(","):
            if ":" in part:
                a, b = part.split(":")
                a = top if a == "*" else int(a)
                b = top if b == "*" else int(b)
                lo, hi = min(a, b), max(a, b)
                result.extend(u for u in available if lo <= u <= hi)
            else:
                u = top if part == "*" else int(part)
                if u in available_set:
                    result.append(u)
        return sorted(set(result))

    def _header_value(self, msg, field):
        return _header_fields(msg.headers).get(field.lower(), "")

    def _match(self, msg, terms):
        def criterion():
            key = terms.pop(0).upper()
            if key == "ALL":
                return True
            if key == "OR":
                left = criterion()
                right = criterion()
                return left or right
            if key == "NOT":
                return not criterion()
            if key in ("SEEN", "UNSEEN"):
                return ("\\Seen" in msg.flags) is (key == "SEEN")
            if key == "HEADER":
                field = terms.pop(0)
                value = terms.pop(0)
                return value.casefold() in self._header_value(msg, field).casefold()
            if key in ("SINCE", "BEFORE", "ON"):
                terms.pop(0)
                return True
            if key == "TEXT":
                value = terms.pop(0)
                hay = (msg.headers + msg.body).decode("utf-8", "replace")
                return value.casefold() in hay.casefold()
            if key in ("FROM", "TO", "SUBJECT"):
                value = terms.pop(0)
                return value.casefold() in self._header_value(msg, key.title()).casefold()
            raise AssertionError(f"unsupported search key {key}")

        results = []
        while terms:
            results.append(criterion())
        return all(results)

    def _fetch_headers(self, msg, names):
        out = []
        current = None
        for raw_line in msg.headers.split(b"\r\n"):
            if not raw_line:
                continue
            if raw_line[:1] in (b" ", b"\t"):
                if current:
                    out.append(raw_line)
                continue
            name = raw_line.split(b":", 1)[0].strip().upper()
            current = name.decode("ascii", "replace") in names
            if current:
                out.append(raw_line)
        return b"\r\n".join(out) + (b"\r\n" if out else b"") + b"\r\n"

    def serve(self, sock):
        self.session_counter += 1
        sid = self.session_counter
        session = Session()
        try:
            with sock, sock.makefile("rb") as reader:
                sock.sendall(
                    b"* OK [CAPABILITY " + " ".join(self.capabilities).encode() + b"] sim ready\r\n"
                )
                while not self.stopped.is_set():
                    data = self._read_command(reader, sock)
                    if data is None:
                        return
                    tag, _, command = data.partition(b" ")
                    words = self._tokens(command)
                    if not words:
                        continue
                    op = words[0].upper()
                    uid_op = op == "UID"
                    if uid_op:
                        op = words[1].upper()
                    shown = "LOGIN ***" if op == "LOGIN" else command.decode("utf-8", "replace")
                    with self.lock:
                        self.commands.append((sid, session.selected, session.readonly, shown))
                        if self.clock is not None:
                            self.clock.elapsed += self.command_latency
                    action = None
                    for hook in list(self.hooks):
                        action = hook(sid, session.selected, op, shown) or action
                    if action == "stall":
                        self.stopped.wait(5)
                        return
                    if action == "apply-then-stall":
                        self.handle(tag, op, uid_op, words, session, command)
                        self.events.append(f"APPLIED-NO-REPLY {shown}")
                        self.stopped.wait(5)
                        return
                    if action == "drop":
                        return
                    if isinstance(action, tuple) and action[0] == "NO":
                        sock.sendall(tag + b" NO " + action[1].encode() + b"\r\n")
                        continue
                    response = self.handle(tag, op, uid_op, words, session, command)
                    if isinstance(response, tuple) and response[0] == "post-stall":
                        # Apply the command, then never answer (server applied, reply lost)
                        self.stopped.wait(5)
                        return
                    sock.sendall(response)
                    if op == "LOGOUT":
                        return
        except OSError:
            pass
        except Exception as error:  # pragma: no cover - surfaced to probes
            self.errors.append(error)

    def handle(self, tag, op, uid_op, words, session, command):
        ok = tag + b" OK done\r\n"
        with self.lock:
            self.sync()
            if op == "CAPABILITY":
                return b"* CAPABILITY " + " ".join(self.capabilities).encode() + b"\r\n" + ok
            if op in ("STARTTLS", "LOGIN", "NOOP"):
                return ok
            if op == "LIST":
                out = b""
                names = self.mailbox_names()
                out += b'* LIST (\\Noselect \\HasChildren) "/" "Folders"\r\n'
                out += b'* LIST (\\Noselect \\HasChildren) "/" "Labels"\r\n'
                for name in names:
                    attrs = [r"\HasNoChildren"]
                    if name == "All Mail":
                        attrs.append(r"\All")
                    if name == "Starred":
                        attrs.append(r"\Flagged")
                    if name in FOLDER_ATTRS:
                        attrs.append(FOLDER_ATTRS[name])
                    escaped_name = _encode_folder(name).replace("\\", "\\\\").replace('"', '\\"')
                    out += f'* LIST ({" ".join(attrs)}) "/" "{escaped_name}"\r\n'.encode()
                return out + ok
            if op == "STATUS":
                name = next(
                    (name for name in self.mailbox_names() if _encode_folder(name) == words[1]),
                    words[1],
                )
                if name not in self.mailbox_names():
                    return tag + b" NO no such mailbox\r\n"
                fields = " ".join(words[2:]).strip("()").upper().split()
                values = {
                    "MESSAGES": len(self.uidmap[name]),
                    "UIDNEXT": self.next_uid[name],
                    "UIDVALIDITY": self.uidvalidity[name],
                }
                if any(field not in values for field in fields):
                    return tag + b" BAD unsupported STATUS field\r\n"
                escaped_name = _encode_folder(name).replace("\\", "\\\\").replace('"', '\\"')
                items = " ".join(f"{field} {values[field]}" for field in fields)
                return f'* STATUS "{escaped_name}" ({items})\r\n'.encode() + ok
            if op in ("SELECT", "EXAMINE"):
                name = next(
                    (name for name in self.mailbox_names() if _encode_folder(name) == words[1]),
                    words[1],
                )
                if name not in self.mailbox_names():
                    session.selected = None
                    return tag + b" NO no such mailbox\r\n"
                session.selected = name
                session.readonly = op == "EXAMINE"
                count = len(self.uidmap[name])
                out = f"* {count} EXISTS\r\n* 0 RECENT\r\n".encode()
                out += f"* OK [UIDVALIDITY {self.uidvalidity[name]}] UIDs valid\r\n".encode()
                out += f"* OK [UIDNEXT {self.next_uid[name]}] next\r\n".encode()
                rw = b"READ-ONLY" if session.readonly else b"READ-WRITE"
                return out + tag + b" OK [" + rw + b"] " + op.encode() + b"\r\n"
            if op == "LOGOUT":
                return b"* BYE logging out\r\n" + ok
            if op == "CLOSE":
                if session.selected and not session.readonly:
                    name = session.selected
                    doomed = [
                        self.uidmap[name][u]
                        for u in sorted(self.deleted[name])
                        if u in self.uidmap[name]
                    ]
                    self.violations.append(f"CLOSE on read-write {name} expunging {doomed}")
                    self.remove_from_mailbox(name, doomed)
                    self.sync()
                session.selected = None
                return ok
            if op in ("CREATE", "DELETE", "RENAME", "APPEND", "SUBSCRIBE", "UNSUBSCRIBE"):
                self.violations.append(f"forbidden command {op}: {command!r}")
                return tag + b" NO refused by simulator\r\n"
            if session.selected is None:
                return tag + b" BAD no mailbox selected\r\n"
            name = session.selected
            mapping = self.uidmap[name]
            if op == "SEARCH":
                if self.strict_search:
                    reason = None
                    if not command.isascii():
                        reason = "8-bit SEARCH octets outside literal"
                    literals = re.findall(rb"\x00LIT([0-9a-f]+)\x00", command)
                    if any(not bytes.fromhex(value.decode()).isascii() for value in literals):
                        if words[2:4] != ["CHARSET", "UTF-8"]:
                            reason = "Non-ASCII literal requires CHARSET UTF-8"
                    if reason:
                        self.search_rejections.append(reason)
                        return tag + b" BAD invalid SEARCH transport\r\n"
                self.work.charge("SEARCH", command, len(mapping))
                terms = words[2:] if uid_op else words[1:]
                if terms[:2] and terms[0].upper() == "CHARSET":
                    terms = terms[2:]
                available = mapping
                if terms[:1] == ["UID"]:
                    available = self._uids(terms[1], mapping)
                    terms = terms[2:]
                found = [
                    uid
                    for uid in available
                    if self._match(self.messages[mapping[uid]], list(terms))
                ]
                return b"* SEARCH " + " ".join(map(str, sorted(found))).encode() + b"\r\n" + ok
            if op == "FETCH":
                if not uid_op:
                    self.violations.append("non-UID FETCH")
                match = re.fullmatch(rb"UID FETCH (\S+) \((.*)\)", command, re.IGNORECASE)
                spec, query = match.group(1).decode(), match.group(2)
                fetched_uids = self._uids(spec, list(mapping))
                self.fetch_batches.append(
                    (
                        name,
                        tuple(
                            (str(uid), self.messages[mapping[uid]].message_id())
                            for uid in fetched_uids
                        ),
                    )
                )
                self.work.charge("FETCH", query, len(fetched_uids))
                if re.search(rb"BODY\[|RFC822(?!\.SIZE)", query, re.IGNORECASE):
                    self.violations.append(f"non-PEEK body fetch {query!r}")
                out = []
                ordered = sorted(mapping)
                positions = {uid: index for index, uid in enumerate(ordered, 1)}
                for uid in fetched_uids:
                    pid = mapping[uid]
                    if (name, pid) in self.fetch_omit:
                        continue
                    msg = self.messages[pid]
                    seq = positions[uid]
                    item = f"* {seq} FETCH (UID {uid}".encode()
                    if b"ENVELOPE" in query.upper():
                        item += b" ENVELOPE " + envelope_from_headers(msg.headers)
                    if b"INTERNALDATE" in query.upper():
                        item += b' INTERNALDATE "17-Oct-2026 09:00:00 +0000"'
                    if b"FLAGS" in query.upper():
                        item += b" FLAGS (" + " ".join(sorted(msg.flags)).encode() + b")"
                    if b"RFC822.SIZE" in query.upper():
                        item += b" RFC822.SIZE " + str(len(msg.headers) + len(msg.body)).encode()
                    if b"BODYSTRUCTURE" in query.upper():
                        # One text/plain part is enough for thread and snippet reads.
                        lines = msg.body.count(b"\n")
                        item += (
                            b' BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" '
                            + f"{len(msg.body)} {lines}".encode()
                            + b" NIL NIL NIL)"
                        )
                    section = re.search(
                        rb"BODY\.PEEK\[1\](?:<(\d+)\.(\d+)>)?", query, re.IGNORECASE
                    )
                    fm = re.search(
                        rb"BODY\.PEEK\[HEADER\.FIELDS \(([^)]*)\)\]", query, re.IGNORECASE
                    )
                    if fm:
                        names = {n.decode().upper() for n in fm.group(1).split()}
                        block = self._fetch_headers(msg, names)
                        item += (
                            b" BODY[HEADER.FIELDS ("
                            + fm.group(1)
                            + b")] {"
                            + str(len(block)).encode()
                            + b"}\r\n"
                            + block
                        )
                    elif re.search(rb"BODY\.PEEK\[HEADER\]", query, re.IGNORECASE):
                        block = msg.headers + b"\r\n"
                        item += b" BODY[HEADER] {" + str(len(block)).encode() + b"}\r\n" + block
                    elif section:
                        start = int(section.group(1) or 0)
                        block = msg.body[start:]
                        if section.group(2) is not None:
                            block = block[: int(section.group(2))]
                        origin = f"<{start}>" if section.group(1) is not None else ""
                        item += f" BODY[1]{origin} {{{len(block)}}}\r\n".encode() + block
                    out.append(item + b")\r\n")
                return b"".join(out) + ok
            # ----- mutations ---------------------------------------------------
            if op in ("STORE", "MOVE", "COPY", "EXPUNGE"):
                if session.readonly:
                    return tag + b" NO read-only mailbox\r\n"
            if op == "STORE":
                if not uid_op:
                    self.violations.append("non-UID STORE")
                spec, mode, flags = words[2], words[3].upper(), " ".join(words[4:])
                uids = self._uids(spec, list(mapping))
                if flags == r"(\Seen)" and mode in {"+FLAGS.SILENT", "-FLAGS.SILENT"}:
                    for uid in uids:
                        message_flags = self.messages[mapping[uid]].flags
                        if mode.startswith("+"):
                            message_flags.add(r"\Seen")
                        else:
                            message_flags.discard(r"\Seen")
                elif "\\DELETED" in flags.upper():
                    if not self.is_user_label(name):
                        self.violations.append(f"STORE \\Deleted outside Labels/: {name} {uids}")
                    if mode.startswith("+"):
                        self.deleted[name].update(uids)
                    elif mode.startswith("-"):
                        self.deleted[name].difference_update(uids)
                    else:
                        self.violations.append(f"STORE FLAGS replace {mode}")
                else:
                    self.violations.append(f"unexpected STORE {words}")
                self.events.append(f"STORE {name} {uids} {mode} {flags}")
                return ok
            if op == "EXPUNGE":
                if uid_op:
                    uids = [
                        u for u in self._uids(words[2], list(mapping)) if u in self.deleted[name]
                    ]
                    if "UIDPLUS" not in self.capabilities:
                        self.violations.append("UID EXPUNGE without UIDPLUS")
                else:
                    uids = sorted(u for u in self.deleted[name] if u in mapping)
                    self.violations.append(f"plain EXPUNGE in {name} (would expunge {uids})")
                pids = [mapping[u] for u in uids]
                self.remove_from_mailbox(name, pids)
                self.sync()
                return b"".join(f"* {i + 1} EXPUNGE\r\n".encode() for i, _ in enumerate(uids)) + ok
            if op in ("MOVE", "COPY"):
                if not uid_op:
                    self.violations.append(f"non-UID {op}")
                if op == "MOVE" and "MOVE" not in self.capabilities:
                    return tag + b" BAD MOVE unsupported\r\n"
                spec, target = words[2], words[3]
                if target not in self.mailbox_names():
                    return tag + b" NO [TRYCREATE] no such mailbox\r\n"
                if (
                    name in ("All Mail", "Scheduled")
                    and op == "MOVE"
                    or target in ("All Mail", "Scheduled")
                ):
                    return tag + b" NO operation not allowed\r\n"
                if op == "MOVE" and (
                    (name == "INBOX" and target == "Sent") or (name == "Sent" and target == "INBOX")
                ):
                    return tag + b" NO operation not allowed\r\n"
                uids = self._uids(spec, list(mapping))
                pids = [mapping[u] for u in uids]
                for pid in pids:
                    self.label(pid, target)
                    if op == "MOVE":
                        expunge_old = self.is_user_label(name) or not self.is_user_label(target)
                        if expunge_old:
                            self.unlabel(pid, name, why="move")
                    self.events.append(f"{op} {pid} {name} -> {target}")
                self.sync()
                # The lock and sync above already fix the destination UID view.
                # Rebuilding every mailbox once per moved row made a batched
                # MOVE spend quadratic time inside the fake server.
                destination_uids = {}
                for uid, pid in self.uidmap[target].items():
                    destination_uids.setdefault(pid, uid)
                new = [destination_uids.get(pid) for pid in pids]
                code = ""
                if self.copyuid and "UIDPLUS" in self.capabilities and new:
                    code = (
                        f"[COPYUID {self.uidvalidity[target]} {','.join(map(str, uids))} "
                        f"{','.join(map(str, new))}] "
                    )
                if op == "MOVE":
                    out = f"* OK {code}moved\r\n".encode() if code else b""
                    out += b"".join(f"* {i + 1} EXPUNGE\r\n".encode() for i, _ in enumerate(uids))
                    return out + ok
                return tag + f" OK {code}copied\r\n".encode()
            self.violations.append(f"unexpected command {op}")
            return tag + b" BAD unexpected\r\n"


def make_client(
    sim, tmp, *, call_timeout=20, timeout=5, protected=None, action_log=None, token_ttl=600.0
):
    from proton_mail_mcp.config import Settings
    from proton_mail_mcp.mail import MailClient
    from proton_mail_mcp.security import Credentials

    settings = Settings(
        token="probe-mcp-bearer-token-" + "B" * 32,
        bridge_user="reader@example.test",
        imap_port=31337,
        action_log_path=str(action_log or (tmp / "actions.jsonl")),
        protected_config_path=protected,
        timeout=timeout,
        call_timeout=call_timeout,
        execute_timeout=call_timeout,
        cleanup_token_ttl=token_ttl,
    )
    return MailClient(
        settings, lambda _: Credentials("reader@example.test", BRIDGE_PASSWORD), sim.factory()
    )


BRIDGE_PASSWORD = "dummy-password"


def mutations(sim):
    return [
        c
        for c in sim.commands
        if re.match(r"(?i)UID (MOVE|COPY|STORE|EXPUNGE)|EXPUNGE|CLOSE|STORE|MOVE|COPY", c[3])
    ]
