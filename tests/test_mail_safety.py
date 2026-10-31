"""Record and reject changes to existing mail for every mailbox tool."""

import re
from types import SimpleNamespace

import pytest
from test_mail import PLAIN, FakeImap, client, message

from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import CallDeadlineExceeded, MailClient, _Deadline, _TimedConnection

FORBIDDEN_COMMANDS = {
    "STORE",
    "COPY",
    "MOVE",
    "EXPUNGE",
    "DELETE",
    "CLOSE",
    "CREATE",
    "RENAME",
    "SUBSCRIBE",
    "UNSUBSCRIBE",
}
READ_COMMANDS = {
    "STARTTLS",
    "LOGIN",
    "CAPABILITY",
    "LIST",
    "EXAMINE",
    "STATUS",
    "SEARCH",
    "FETCH",
    "LOGOUT",
}


class ReadOnlyImap(FakeImap):
    """Guard both UID commands and direct IMAP mutation methods."""

    def __getattr__(self, name):
        if name.upper() in FORBIDDEN_COMMANDS:

            def forbidden(*args, **kwargs):
                self.calls.append((name.upper(),))
                raise AssertionError("Existing mail must never be modified")

            return forbidden
        raise AttributeError(name)

    def uid(self, command, *args):
        assert command.upper() not in FORBIDDEN_COMMANDS
        assert command.upper() in {"SEARCH", "FETCH"}
        if command.upper() == "FETCH":
            query = args[1]
            # BODYSTRUCTURE, INTERNALDATE and RFC822.SIZE are metadata; every body/header
            # section must use PEEK to preserve the existing Seen flag.
            assert not re.search(
                r"\b(?:BODY\[|RFC822\b(?!\.SIZE(?=[\s)]|$)))", query, re.IGNORECASE
            )
        return super().uid(command, *args)

    def select(self, folder, *, readonly=False):
        assert readonly is True
        return super().select(folder, readonly=readonly)

    def append(self, folder, flags, internaldate, payload):
        assert folder == '"Drafts"'
        assert flags == r"(\Draft)"
        return super().append(folder, flags, internaldate, payload)


@pytest.mark.parametrize(
    "operation",
    [
        "search",
        "preview",
        "search_cursor",
        "search_text",
        "search_no_snippets",
        "search_deadline",
        "thread",
        "single_message",
        "draft",
        "reply",
        "reply_timeout",
    ],
)
def test_tools_never_modify_existing_mail_or_fetch_without_peek(operation, monkeypatch):
    fake = ReadOnlyImap(
        {
            "INBOX": {
                "1": message("<root@example.com>"),
                "2": message("<other@example.com>"),
            },
            "All Mail": {
                "101": message("<root@example.com>"),
                "102": message("<child@example.com>", reply="<root@example.com>"),
            },
            "Drafts": {},
            "Trash": {},
        },
        folder_flags={"All Mail": r"\All"},
    )
    settings = Settings(
        token="a-test-token-that-is-at-least-32-characters", bridge_user="reader@example.com"
    )
    mailbox = client(settings, fake)
    if operation == "search":
        mailbox.search_mail(subject="Hello", since="2026-01-01")
    elif operation == "preview":
        found = mailbox.preview_action("move_to_trash", query={"folder": "INBOX"})
        assert found["token"]
        assert found["count"] == 2
    elif operation == "search_cursor":
        first = mailbox.search_mail(limit=1, include_snippets=False)
        assert first["next_cursor"]
        mailbox.search_mail(limit=1, cursor=first["next_cursor"], include_snippets=False)
    elif operation == "search_text":
        mailbox.search_mail(text="Hello")
    elif operation == "search_no_snippets":
        mailbox.search_mail(subject="Hello", include_snippets=False)
    elif operation == "search_deadline":

        def timed_out(*args, **kwargs):
            raise CallDeadlineExceeded("Synthetic deadline")

        monkeypatch.setattr(mailbox, "_body", timed_out)
        assert mailbox.search_mail()["partial"] is True
    elif operation == "thread":
        mailbox.read_thread("<root@example.com>")
    elif operation == "single_message":
        mailbox.read_thread("<root@example.com>", include_thread=False)
    else:
        if operation == "reply_timeout":

            def timed_out(*args, **kwargs):
                raise CallDeadlineExceeded("Synthetic lookup deadline")

            monkeypatch.setattr(mailbox, "_header_search", timed_out)
        mailbox.create_draft(
            "other@example.com",
            subject="Draft",
            body="New draft body",
            in_reply_to="<root@example.com>" if operation.startswith("reply") else None,
        )

    commands = {call[0].upper() for call in fake.calls}
    assert not commands & FORBIDDEN_COMMANDS
    assert all(call[2] is True for call in fake.calls if call[0] == "EXAMINE")
    if operation == "draft" or operation.startswith("reply"):
        assert commands <= READ_COMMANDS | {"APPEND"}
        assert len(fake.appended) == 1
        assert fake.appended[0][:3] == ('"Drafts"', r"(\Draft)", None)
    else:
        assert commands <= READ_COMMANDS
        assert fake.appended == []


@pytest.mark.parametrize("metadata_first", [False, True])
def test_batched_metadata_associates_uid_and_literals_in_either_protocol_order(metadata_first):
    """Real imaplib responses can put UID/structure after the header literal."""
    entries = [("92", message("<new@example.com>")), ("7", message("<old@example.com>"))]
    response = []
    for uid, item in entries:
        metadata = (
            f' UID {uid} INTERNALDATE "14-Oct-2026 10:00:00 +0000" BODYSTRUCTURE '.encode() + PLAIN
        )
        header = b" BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT)] {"
        header += str(len(item.headers)).encode() + b"}"
        prefix = b"3 (" + (metadata if metadata_first else b"") + header
        suffix = (b"" if metadata_first else metadata) + b")"
        response.extend([(prefix, item.headers), suffix])

    connection = SimpleNamespace(uid=lambda *args: ("OK", response))
    records = MailClient._metadata(connection, ["7", "92"], deadline=_Deadline(45, 30))
    assert str(records["92"]["headers"]["Message-ID"]) == "<new@example.com>"
    assert str(records["7"]["headers"]["Message-ID"]) == "<old@example.com>"
    assert records["92"]["structure"][0:2] == [b"TEXT", b"PLAIN"]
    assert records["7"]["internaldate"] > 0


def test_deadline_caps_socket_wait_and_stops_commands_before_they_start(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])
    waits = []
    calls = []
    connection = SimpleNamespace(
        sock=SimpleNamespace(settimeout=waits.append),
        uid=lambda *args: calls.append(args) or ("OK", []),
    )
    # Preserve the original 45-second I/O window plus the response reserve.
    deadline = _Deadline(50, 30)
    timed = _TimedConnection(connection, deadline)
    timed.uid("SEARCH", "ALL")
    assert waits == [30]
    clock[0] = 140
    timed.uid("SEARCH", "ALL")
    assert waits[-1] == 5
    # Stop I/O five seconds before the caller's response deadline.
    clock[0] = 145
    with pytest.raises(CallDeadlineExceeded):
        timed.uid("SEARCH", "ALL")
    assert clock[0] < deadline.end
    assert len(calls) == 2


def test_expired_session_shuts_down_without_another_mail_command(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])
    fake = ReadOnlyImap()
    closed = []
    fake.shutdown = lambda: closed.append(True)
    settings = Settings(
        token="a-test-token-that-is-at-least-32-characters", bridge_user="reader@example.com"
    )
    mailbox = client(settings, fake)
    with mailbox._session() as (connection, _):
        clock[0] += settings.call_timeout
    assert closed == [True]
    assert [call[0] for call in fake.calls] == ["STARTTLS", "LOGIN", "CAPABILITY"]


@pytest.mark.parametrize(
    "action,params,allowed",
    [
        ("move_to_trash", {}, {"MOVE"}),
        ("archive", {}, {"MOVE"}),
        ("move_to_folder", {"folder": "Folders/Review"}, {"MOVE"}),
        ("add_label", {"label": "Keep"}, {"COPY"}),
        ("remove_label", {"label": "Keep"}, {"STORE", "EXPUNGE"}),
    ],
)
def test_guarded_path_mutations_stay_within_action_allowlist(tmp_path, action, params, allowed):
    from test_cleanup_mutations import MutatingImap

    item = message("<cleanup@example.test>")
    fake = MutatingImap(
        {
            "INBOX": {"1": item},
            "All Mail": {"10": item},
            "Trash": {},
            "Archive": {},
            "Folders/Review": {},
            "Labels/Keep": {},
        }
    )
    if action == "remove_label":
        fake.folders["Labels/Keep"]["20"] = item
    settings = Settings(
        token="a-test-token-that-is-at-least-32-characters",
        bridge_user="reader@example.com",
        action_log_path=str(tmp_path / "actions.jsonl"),
    )
    mailbox = client(settings, fake)
    preview = mailbox.preview_action(action, ids=["<cleanup@example.test>"], params=params)
    assert preview["token"]
    result = getattr(mailbox, action)(preview["token"])
    assert result["partial"] is False
    mutation_commands = {call[0] for call in fake.calls} & FORBIDDEN_COMMANDS
    assert mutation_commands == allowed
    if action == "remove_label":
        assert ("STORE", "20", "+FLAGS.SILENT", r"(\Deleted)") in fake.calls
        assert ("EXPUNGE", "20") in fake.calls
        assert fake.folders["All Mail"] and fake.folders["INBOX"]
