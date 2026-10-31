"""Stateful IMAP peer over real AF_UNIX sockets, with no network connections.

Only STARTTLS encryption is simulated. Command framing, literals, UID response
codes, mailbox selection, and all parsing use Python's real imaplib client.
"""

import imaplib
import re
import shlex
import socket
import threading
from contextlib import suppress
from email import policy
from email.parser import BytesParser
from functools import lru_cache

from bridge_costs import envelope_from_headers
from test_mail import PLAIN, message


@lru_cache(maxsize=100_000)
def _search_headers(raw):
    headers = BytesParser(policy=policy.default).parsebytes(raw)
    values = {
        name.lower(): " ".join(str(value) for value in headers.get_all(name))
        for name in headers.keys()
        if name.lower() != "message-id"
    }
    # Message-ID SEARCH sees every raw field, including malformed multi-ID
    # fields which the structured header parser would silently truncate.
    values["message-id"] = " ".join(
        value for name, value in headers.raw_items() if name.lower() == "message-id"
    )
    return values


class CleanupIMAPPeer:
    def __init__(
        self,
        *,
        capabilities=("IMAP4rev1", "STARTTLS", "MOVE", "UIDPLUS"),
        pre_auth_capabilities=None,
        login_capability_response=None,
        login_untagged_capabilities=None,
        clock=None,
        command_latency=0.0,
        reject_uid_search=None,
    ):
        if command_latency and clock is None:
            raise ValueError("Synthetic command latency requires a clock")
        self.clock, self.command_latency = clock, command_latency
        self.capabilities = capabilities
        self.pre_auth_capabilities = (
            capabilities if pre_auth_capabilities is None else pre_auth_capabilities
        )
        assert login_capability_response in (None, "tagged", "untagged")
        self.login_capability_response = login_capability_response
        self.login_untagged_capabilities = login_untagged_capabilities
        first = message("<newsletter-1@updates.example.test>", subject="October newsletter")
        second = message("<newsletter-2@updates.example.test>", subject="October newsletter")
        self.folders = {
            "INBOX": {"7": first.headers, "8": second.headers},
            "Archive": {},
            "Trash": {},
            "Spam": {},
            "Sent": {},
            "Drafts": {},
            "Folders/Reviewed": {},
            "Labels/Review": {},
            "All Mail": {"101": first.headers, "102": second.headers},
        }
        self.uidvalidities = {folder: str(index + 40) for index, folder in enumerate(self.folders)}
        self.next_uid = {folder: 501 for folder in self.folders}
        self.deleted = {folder: set() for folder in self.folders}
        self.commands = []
        self.errors = []
        self.stopped = threading.Event()
        self.peers = []
        self.mutations = 0
        self.stall_mutation = None
        # Optional test clock/fault hook. Protocol handling still runs through
        # socketpair and Python's actual imaplib reader and literal parser.
        self.command_hook = None
        assert reject_uid_search in (None, "empty", "all")
        self.reject_uid_search = reject_uid_search
        self.response_hook = None

    def factory(self):
        peer = self

        class SocketIMAP(imaplib.IMAP4):
            def _create_socket(self, timeout):
                local, remote = socket.socketpair()
                local.settimeout(timeout)
                worker = threading.Thread(target=peer.serve, args=(remote,), daemon=True)
                peer.peers.append((local, remote, worker))
                worker.start()
                return local

            def starttls(self, ssl_context=None):
                # Preserve the STARTTLS protocol command, without wrapping the
                # socket in TLS. Certificate behavior has separate unit tests.
                return self._simple_command("STARTTLS")

        return SocketIMAP

    def close(self):
        self.stopped.set()
        for local, remote, worker in self.peers:
            for sock in (local, remote):
                with suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                sock.close()
            worker.join(timeout=1)
            assert not worker.is_alive(), "Mock IMAP server worker did not stop"
        assert not self.errors, self.errors

    def identifiers(self, folder):
        return {
            str(BytesParser(policy=policy.default).parsebytes(raw)["Message-ID"])
            for raw in self.folders[folder].values()
        }

    @staticmethod
    def uids(sequence, available):
        result = []
        for part in sequence.split(","):
            if ":" in part:
                start, end = part.split(":")
                end = max(map(int, available), default=0) if end == "*" else int(end)
                result.extend(str(uid) for uid in range(int(start), end + 1))
            else:
                result.append(part)
        return [uid for uid in result if uid in available]

    @staticmethod
    def matches(raw, criteria):
        headers = _search_headers(raw)
        terms = list(criteria)
        if terms[:2] == ["CHARSET", "UTF-8"]:
            terms = terms[2:]

        def criterion():
            field = terms.pop(0).upper()
            if field == "ALL":
                return True
            if field == "OR":
                left, right = criterion(), criterion()
                return left or right
            if field == "HEADER":
                field = terms.pop(0)
            value = terms.pop(0)
            if field in ("SINCE", "BEFORE", "ON"):
                return True
            haystack = raw.decode("utf-8") if field == "TEXT" else headers.get(field.lower(), "")
            return value.casefold() in haystack.casefold()

        result = []
        while terms:
            result.append(criterion())
        return all(result)

    @staticmethod
    def fetch(sequence, uid, raw, query):
        assert b"BODY[" not in query and not re.search(rb"RFC822(?!\.SIZE)", query)
        data = f"* {sequence} FETCH (UID {uid}".encode()
        if b"ENVELOPE" in query:
            data += b" ENVELOPE " + envelope_from_headers(raw)
        if b"RFC822.SIZE" in query:
            data += b" RFC822.SIZE " + str(len(raw)).encode()
        if b"FLAGS" in query:
            data += b" FLAGS ()"
        if b"INTERNALDATE" in query:
            data += b' INTERNALDATE "17-Oct-2026 09:00:00 +0000"'
        if b"BODYSTRUCTURE" in query:
            data += b" BODYSTRUCTURE " + PLAIN
        if b"BODY.PEEK[HEADER" in query:
            field_match = re.search(rb"HEADER.FIELDS \(([^)]+)\)", query)
            if field_match:
                names = field_match.group(1).upper().split()
                headers = BytesParser(policy=policy.default).parsebytes(raw)
                raw = (
                    b"".join(
                        name.encode() + b": " + value.encode() + b"\r\n"
                        for name, value in headers.raw_items()
                        if name.upper().encode() in names
                    )
                    + b"\r\n"
                )
            data += b" BODY[HEADER] {" + str(len(raw)).encode() + b"}\r\n" + raw
        return data + b")\r\n"

    def serve(self, sock):
        selected = None
        readonly = True
        authenticated = False
        try:
            with sock, sock.makefile("rb") as reader:
                sock.sendall(b"* OK mock cleanup mailbox ready\r\n")
                while line := reader.readline():
                    tag, _, command = line.rstrip(b"\r\n").partition(b" ")
                    words = shlex.split(command.decode())
                    operation = words[1].upper() if words[0] == "UID" else words[0].upper()
                    # Record commands for safety assertions, omitting dummy credentials.
                    self.commands.append((selected, b"LOGIN" if operation == "LOGIN" else command))
                    if self.clock is not None:
                        self.clock.elapsed += self.command_latency
                    if self.command_hook is not None:
                        self.command_hook(selected, operation, command)
                    done = tag + b" OK done\r\n"
                    response = done
                    if operation == "CAPABILITY":
                        capabilities = (
                            self.capabilities if authenticated else self.pre_auth_capabilities
                        )
                        response = b"* CAPABILITY " + " ".join(capabilities).encode()
                        response += b"\r\n" + done
                    elif operation == "LOGIN":
                        authenticated = True
                        capabilities = " ".join(self.capabilities).encode()
                        if self.login_capability_response == "tagged":
                            response = tag + b" OK [CAPABILITY " + capabilities + b"] logged in\r\n"
                            if self.login_untagged_capabilities is not None:
                                response = (
                                    b"* CAPABILITY "
                                    + " ".join(self.login_untagged_capabilities).encode()
                                    + b"\r\n"
                                    + response
                                )
                        elif self.login_capability_response == "untagged":
                            response = b"* CAPABILITY " + capabilities + b"\r\n" + done
                    elif operation == "STARTTLS":
                        pass
                    elif operation == "LIST":
                        response = b""
                        for folder in self.folders:
                            flags = r"\All" if folder == "All Mail" else r"\HasNoChildren"
                            response += f'* LIST ({flags}) "/" "{folder}"\r\n'.encode()
                        response += done
                    elif operation in ("SELECT", "EXAMINE"):
                        selected = words[1]
                        readonly = operation == "EXAMINE"
                        assert selected in self.folders
                        response = f"* {len(self.folders[selected])} EXISTS\r\n".encode()
                        response += (
                            f"* OK [UIDVALIDITY {self.uidvalidities[selected]}] stable\r\n".encode()
                        )
                        response += f"* OK [UIDNEXT {self.next_uid[selected]}] next\r\n".encode()
                        response += done
                    elif operation == "SEARCH":
                        result = []
                        criteria = words[2:]
                        if criteria[:2] == ["CHARSET", "UTF-8"]:
                            criteria = criteria[2:]
                        available = self.folders[selected]
                        if criteria[:1] == ["UID"]:
                            available = self.uids(criteria[1], available)
                            criteria = criteria[2:]
                        for uid in available:
                            if self.stopped.is_set():
                                return
                            if self.matches(self.folders[selected][uid], criteria):
                                result.append(uid)
                        response = b"* SEARCH " + " ".join(result).encode() + b"\r\n" + done
                        if words[-2:] == ["UID", "1:*"] and (
                            self.reject_uid_search == "all"
                            or self.reject_uid_search == "empty"
                            and not self.folders[selected]
                        ):
                            response = tag + b" NO no such message\r\n"
                    elif operation == "FETCH":
                        match = re.fullmatch(rb"UID FETCH (\S+) (.+)", command)
                        sequence, query = match.groups()
                        response = (
                            b"".join(
                                self.fetch(index, uid, self.folders[selected][uid], query)
                                for index, uid in enumerate(
                                    self.uids(sequence.decode(), self.folders[selected]), start=1
                                )
                            )
                            + done
                        )
                    elif operation in ("MOVE", "COPY", "STORE", "EXPUNGE"):
                        assert words[0] == "UID", "Mutations must be UID-scoped"
                        assert not readonly, "A mutation reached a read-only selection"
                        self.mutations += 1
                        if self.stall_mutation == self.mutations:
                            self.stopped.wait(3)
                            return
                        uids = self.uids(words[2], self.folders[selected])
                        if operation in ("MOVE", "COPY"):
                            target = words[3]
                            assert target in self.folders, "Implicit mailbox creation forbidden"
                            assert operation != "MOVE" or "MOVE" in {
                                atom.upper() for atom in self.capabilities
                            }
                            new_uids = []
                            for uid in uids:
                                new_uid = str(self.next_uid[target])
                                self.next_uid[target] += 1
                                self.folders[target][new_uid] = self.folders[selected][uid]
                                new_uids.append(new_uid)
                                if operation == "MOVE":
                                    del self.folders[selected][uid]
                            response = (
                                tag
                                + (
                                    f" OK [COPYUID {self.uidvalidities[target]} "
                                    f"{','.join(uids)} {','.join(new_uids)}] done\r\n"
                                ).encode()
                            )
                        else:
                            assert selected.startswith("Labels/"), "Expunge outside Labels"
                            assert "UIDPLUS" in {atom.upper() for atom in self.capabilities}
                            if operation == "STORE":
                                if command.endswith(rb"+FLAGS.SILENT (\Deleted)"):
                                    self.deleted[selected].update(uids)
                                else:
                                    assert command.endswith(rb"-FLAGS.SILENT (\Deleted)")
                                    self.deleted[selected].difference_update(uids)
                            else:
                                assert all(uid in self.deleted[selected] for uid in uids)
                                for uid in uids:
                                    del self.folders[selected][uid]
                                    self.deleted[selected].remove(uid)
                    elif operation == "LOGOUT":
                        sock.sendall(b"* BYE done\r\n" + done)
                        return
                    else:
                        raise AssertionError(f"Unexpected mock IMAP command: {operation}")
                    if self.response_hook is not None:
                        response = self.response_hook(selected, operation, command, response)
                    sock.sendall(response)
        except OSError:
            pass
        except Exception as error:
            self.errors.append(error)
