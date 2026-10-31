"""Bridge UID enumeration regressions through a local socketpair peer."""

import json
import re
import time
from dataclasses import replace

import pytest
from action_records import execution_log
from test_cleanup_socket import mutations
from test_cleanup_socket import socket_mailbox as socket_mailbox

from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.mail import CallDeadlineExceeded, MailError, MailValidationError
from proton_mail_mcp.search import _uidvalidity


def preview(backend, tmp_path, operation):
    if operation == "action":
        return backend.preview_action("archive", query={"folder": "INBOX", "subject": "newsletter"})
    path = tmp_path / "synthetic-bridge-rules.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [
                    {
                        "field": "subject",
                        "contains": "newsletter",
                        "folder": "Folders/Reviewed",
                    }
                ],
            }
        )
    )
    return FolderRulesService(backend.cleanup, path).preview_rules("INBOX")


def enumeration(command):
    return command == b"UID SEARCH CHARSET UTF-8 ALL"


def assert_read_only(peer, log_path, *, matched_membership=False):
    assert not mutations(peer.commands)
    assert not execution_log(log_path)
    header_searches = [
        (folder, command)
        for folder, command in peer.commands
        if command.startswith(b"UID SEARCH") and re.search(rb"\bHEADER\b", command)
    ]
    if matched_membership:
        # Rules replace All Mail's full UID enumeration with matched-ID lookup.
        assert all(
            folder == "All Mail" and re.search(rb"\bHEADER\s+Message-ID\b", command, re.I)
            for folder, command in header_searches
        )
        assert not any(
            folder == "All Mail" and enumeration(command) for folder, command in peer.commands
        )
    else:
        assert not header_searches


@pytest.mark.parametrize("reject_uid_search", ["empty", "all"])
@pytest.mark.parametrize("operation", ["action", "rules"])
def test_bridge_reproduction_real_match_previews(
    socket_mailbox, tmp_path, reject_uid_search, operation
):
    backend, peer, log_path = socket_mailbox(reject_uid_search=reject_uid_search)
    result = preview(backend, tmp_path, operation)
    assert result["count"] == 2 and result["token"]
    assert not result["blocked"]
    assert any(enumeration(command) for _, command in peer.commands)
    assert not any(
        command.startswith(b"UID SEARCH") and b"1:*" in command for _, command in peer.commands
    )
    assert_read_only(peer, log_path, matched_membership=operation == "rules")


@pytest.mark.parametrize("operation", ["action", "rules"])
def test_empty_folders_are_selected_but_never_enumerated(socket_mailbox, tmp_path, operation):
    backend, peer, log_path = socket_mailbox(reject_uid_search="all")
    empty = {folder for folder, messages in peer.folders.items() if not messages}
    assert preview(backend, tmp_path, operation)["token"]
    selected = {
        command.split(b" ", 1)[1].strip(b'"').decode()
        for _, command in peer.commands
        if command.startswith(b"EXAMINE ")
    }
    assert empty <= selected
    assert not any(
        folder in empty and command.startswith((b"UID SEARCH", b"UID FETCH"))
        for folder, command in peer.commands
    )
    assert_read_only(peer, log_path, matched_membership=operation == "rules")


@pytest.mark.parametrize(
    "status,text",
    [
        (b"NO", b"no such message"),
        (b"NO", b"NO SUCH MESSAGE"),
        (b"NO", b"permission refused"),
        (b"BAD", b"unsupported command"),
        (b"BAD", b"no such message"),
    ],
)
@pytest.mark.parametrize("operation", ["action", "rules"])
def test_enumeration_errors_with_positive_exists_refuse_without_token(
    socket_mailbox, tmp_path, monkeypatch, status, text, operation
):
    backend, peer, log_path = socket_mailbox()
    intercepted = []

    def response(folder, command_name, command, data):
        if enumeration(command):
            assert peer.folders[folder]
            intercepted.append(folder)
            tag = data.rsplit(b"\r\n", 2)[1].split(b" ", 1)[0]
            return tag + b" " + status + b" " + text + b"\r\n"
        return data

    def issue(*args, **kwargs):
        pytest.fail("Incomplete enumeration must never issue a token")

    peer.response_hook = response
    monkeypatch.setattr(backend.cleanup.tokens, "issue", issue)
    with pytest.raises((MailError, MailValidationError)):
        preview(backend, tmp_path, operation)
    assert intercepted
    assert_read_only(peer, log_path)


@pytest.mark.parametrize("fault", ["no-such", "other-no", "bad", "count", "validity", "emptied"])
def test_enumeration_itself_rejects_incomplete_evidence(socket_mailbox, fault):
    from proton_mail_mcp.cleanup_lookup import enumerate_uids

    backend, peer, log_path = socket_mailbox()

    def response(folder, command_name, command, data):
        if not enumeration(command):
            return data
        if fault == "count":
            return data.replace(b"* SEARCH 7 8", b"* SEARCH 7")
        if fault == "validity":
            peer.uidvalidities[folder] = "999"
            return b"* OK [UIDVALIDITY 999] changed\r\n" + data
        if fault == "emptied":
            peer.folders[folder].clear()
        status = b"BAD" if fault == "bad" else b"NO"
        text = b"permission refused" if fault == "other-no" else b"no such message"
        tag = data.rsplit(b"\r\n", 2)[1].split(b" ", 1)[0]
        return tag + b" " + status + b" " + text + b"\r\n"

    peer.response_hook = response
    deadline = backend._deadline()
    with pytest.raises((MailError, MailValidationError)):
        with backend._session(deadline=deadline) as (connection, _):
            backend._select(connection, "INBOX")
            validity = _uidvalidity(connection)
            enumerate_uids(backend, connection, deadline, "INBOX", validity, "membership")
    assert_read_only(peer, log_path)


@pytest.mark.parametrize("payload", [b"7 invalid", b"7 8x", b"7 +8", b"7 0", b"7 7", b"7"])
@pytest.mark.parametrize("operation", ["action", "rules"])
def test_malformed_or_incomplete_uid_enumeration_refuses(
    socket_mailbox, tmp_path, monkeypatch, payload, operation
):
    backend, peer, log_path = socket_mailbox()
    intercepted = []

    def response(folder, command_name, command, data):
        if enumeration(command):
            intercepted.append(folder)
            return re.sub(rb"\* SEARCH [^\r\n]*", b"* SEARCH " + payload, data)
        return data

    peer.response_hook = response
    monkeypatch.setattr(
        backend.cleanup.tokens, "issue", lambda *a, **kw: pytest.fail("No token on bad inventory")
    )
    with pytest.raises((MailError, MailValidationError)):
        preview(backend, tmp_path, operation)
    assert intercepted
    assert_read_only(peer, log_path)


@pytest.mark.parametrize("drift", ["exists", "uidvalidity", "uidvalidity-transient", "uidnext"])
@pytest.mark.parametrize("stage", ["enumeration", "metadata", "reselect"])
@pytest.mark.parametrize("operation", ["action", "rules"])
def test_enumeration_scan_metadata_drift_keeps_arrivals_partial_and_refuses_other_changes(
    socket_mailbox, tmp_path, monkeypatch, drift, stage, operation
):
    backend, peer, log_path = socket_mailbox()
    enumerated = set()
    changed = []
    original_issue = backend.cleanup.tokens.issue

    def response(folder, command_name, command, data):
        if enumeration(command):
            enumerated.add(folder)
        trigger = (
            enumeration(command)
            if stage == "enumeration"
            else command_name == ("FETCH" if stage == "metadata" else "EXAMINE")
        )
        if folder in enumerated and trigger and not changed:
            changed.append(folder)
            if drift == "exists":
                peer.folders[folder]["999"] = peer.folders[folder][next(iter(peer.folders[folder]))]
                data = b"* 3 EXISTS\r\n" + data
            elif drift in ("uidvalidity", "uidvalidity-transient"):
                if drift == "uidvalidity":
                    peer.uidvalidities[folder] = "999"
                data = b"* OK [UIDVALIDITY 999] changed\r\n" + data
            else:
                peer.next_uid[folder] += 1
                data = b"* OK [UIDNEXT 502] changed\r\n" + data
        return data

    peer.response_hook = response
    monkeypatch.setattr(
        backend.cleanup.tokens, "issue", lambda *a, **kw: pytest.fail("No token on drift")
    )
    if operation == "rules" and drift == "exists" and stage in {"enumeration", "metadata"}:
        first = preview(backend, tmp_path, operation)
        assert first["partial"] and not first["total_is_exact"]
        assert first["token"] is None and first["next_cursor"]
        assert_read_only(peer, log_path)

        # Identical source copies remain separate physical move candidates;
        # the arrival must receive a fresh review on the continuation.
        monkeypatch.setattr(backend.cleanup.tokens, "issue", original_issue)
        rules = FolderRulesService(backend.cleanup, tmp_path / "synthetic-bridge-rules.json")
        resumed = rules.preview_rules("INBOX", cursor=first["next_cursor"])
        assert resumed["token"]
        assert resumed["count"] == resumed["matched_count"] == resumed["batch_count"] == 3
        grant = backend.cleanup.tokens.inspect(resumed["token"], "apply_rules")
        assert {row["uid"] for row in grant.selector["reviewed_rows"]} == {"7", "8", "999"}
        assert {row["uid"] for row in grant.messages} == {"7", "8", "999"}
    else:
        with pytest.raises((MailError, MailValidationError)):
            preview(backend, tmp_path, operation)
    assert changed
    assert_read_only(peer, log_path)


@pytest.mark.parametrize("operation", ["action", "rules"])
def test_enumeration_deadline_closes_connection_without_reuse(
    socket_mailbox, tmp_path, monkeypatch, operation
):
    backend, peer, log_path = socket_mailbox()
    backend.settings = replace(backend.settings, timeout=1, call_timeout=0.4)
    peer.command_hook = lambda folder, name, command: (
        peer.stopped.wait(1.2) if enumeration(command) else None
    )
    monkeypatch.setattr(
        backend.cleanup.tokens, "issue", lambda *a, **kw: pytest.fail("No token after timeout")
    )
    started = time.monotonic()
    try:
        result = preview(backend, tmp_path, operation)
    except CallDeadlineExceeded:
        result = None
    assert time.monotonic() - started < 0.75
    if result is not None:
        assert result["token"] is None and result["partial"]
    assert any(enumeration(command) for _, command in peer.commands)
    assert all(local.fileno() == -1 for local, _, _ in peer.peers)
    assert not any(command.startswith(b"LOGOUT") for _, command in peer.commands)
    assert_read_only(peer, log_path)


@pytest.mark.parametrize("operation", ["action", "rules"])
def test_protected_copy_still_blocks_next_to_empty_folder(socket_mailbox, tmp_path, operation):
    backend, peer, log_path = socket_mailbox(reject_uid_search="all")
    for uid, raw in peer.folders["INBOX"].items():
        peer.folders["Archive"][uid] = raw.replace(b"sender@example.com", b"relative@gmail.com")
    assert not peer.folders["Drafts"]
    result = preview(backend, tmp_path, operation)
    assert result["token"] is None
    assert result["blocked"] or any(item["blocked"] for item in result.get("rules", []))
    assert_read_only(peer, log_path, matched_membership=operation == "rules")


@pytest.mark.parametrize("guard", ["duplicate", "ambiguous", "hidden-raw-field"])
def test_ambiguous_identity_still_blocks_next_to_empty_folder(
    socket_mailbox, tmp_path, monkeypatch, guard
):
    backend, peer, log_path = socket_mailbox(reject_uid_search="all")
    raw = peer.folders["INBOX"]["7"]
    if guard == "duplicate":
        peer.folders["Archive"]["9"] = raw.replace(
            b"\r\n\r\n", b"\r\nMessage-ID: <newsletter-1@updates.example.test>\r\n\r\n"
        )
    elif guard == "ambiguous":
        peer.folders["Archive"]["9"] = raw.replace(
            b"<newsletter-1@updates.example.test>",
            b"<newsletter-1@updates.example.test> <newsletter-2@updates.example.test>",
        )
    else:
        peer.folders["Archive"]["9"] = raw.replace(
            b"\r\n\r\n", b"\r\nMessage-ID: <hidden@example.test>\r\n\r\n"
        )
    assert not peer.folders["Drafts"]
    monkeypatch.setattr(
        backend.cleanup.tokens, "issue", lambda *a, **kw: pytest.fail("Identity guard lost")
    )
    with pytest.raises(MailValidationError):
        preview(backend, tmp_path, "rules")
    assert_read_only(peer, log_path)
