"""Implicit sources use complete preview certificates and exact bound UID reads."""

import re

import pytest
from action_records import execution_log
from bridge_sim import Msg, hdr
from test_cleanup_mutations import uid_values
from test_cleanup_resume import records
from test_cleanup_resume import slow_cleanup as slow_cleanup
from test_cleanup_socket import mutations
from test_cleanup_socket import socket_mailbox as socket_mailbox

from proton_mail_mcp.mail import MailValidationError

ACTION = "move_to_folder"
TARGET = "Folders/Reviewed"


def test_id_sources_in_distinct_folders_use_only_exact_bound_identity_sets(socket_mailbox):
    client, peer, _ = socket_mailbox()
    peer.folders["Archive"]["500"] = peer.folders["INBOX"].pop("8")
    peer.next_uid["Archive"] = 501
    preview = client.preview_action(
        ACTION,
        ids=["<newsletter-1@updates.example.test>", "<newsletter-2@updates.example.test>"],
        params={"folder": TARGET},
    )
    assert preview["token"] and preview["count"] == 2
    grant = client.cleanup.tokens.inspect(preview["token"], ACTION)
    assert {row["folder"] for row in grant.messages} == {"INBOX", "Archive"}
    assert grant.bounded_recovery
    allowed = {}
    originals = {}
    for row in grant.messages:
        assert row["implicit_source_states"]
        originals[row["folder"], row["uid"]] = row["message_id"]
        for member in [row, *row["memberships"]]:
            allowed.setdefault(member["folder"], set()).add(member["uid"])
    before = len(peer.commands)
    result = client.move_to_folder(preview["token"])
    commands = peer.commands[before:]
    assert result["done"] == 2 and not result["partial"]
    assert not any(command.startswith(b"UID SEARCH") for _, command in commands)
    # Outcome verification reads only the new destination UIDs named by COPYUID.
    allowed[TARGET] = {row["uid_after"] for row in result["messages"]}
    served = {}
    for folder, command in commands:
        if command.startswith(b"UID FETCH"):
            uids = set(command.split()[2].decode("ascii").split(","))
            assert uids and uids <= allowed[folder]
            served.setdefault(folder, set()).update(uids)
    assert served == allowed
    actual = [
        (folder, command.split()[2].decode("ascii")) for folder, command in mutations(commands)
    ]
    assert len(actual) == len(set(actual)) == len(originals)
    assert set(actual) == set(originals)
    assert {
        (row["source_folder"], row["uid_before"]): row["message_id"] for row in result["messages"]
    } == originals


@pytest.mark.parametrize("selector", ["query", "ids"])
@pytest.mark.parametrize("folder", ["Archive", TARGET])
def test_implicit_sources_keep_new_same_identity_copy_ambiguity_refusal(
    socket_mailbox, selector, folder
):
    client, peer, log_path = socket_mailbox()
    selection = (
        {"query": {"subject": "October newsletter"}}
        if selector == "query"
        else {"ids": ["<newsletter-1@updates.example.test>"]}
    )
    preview = client.preview_action(ACTION, **selection, params={"folder": TARGET})
    assert preview["token"]
    grant = client.cleanup.tokens.inspect(preview["token"], ACTION)
    assert all("implicit_source_states" in row for row in grant.messages)
    arrival_uid = peer.next_uid[folder]
    peer.folders[folder][str(arrival_uid)] = peer.folders["INBOX"]["7"]
    peer.next_uid[folder] = arrival_uid + 1
    before = len(peer.commands)
    with pytest.raises(MailValidationError, match="ambiguous"):
        client.move_to_folder(preview["token"])
    commands = peer.commands[before:]
    assert any(command.startswith(b"UID SEARCH") for _, command in commands)
    assert not mutations(commands) and not execution_log(log_path)
    client.cleanup.tokens.inspect(preview["token"], ACTION)


def test_implicit_source_without_uidnext_retains_complete_discovery(socket_mailbox):
    client, peer, log_path = socket_mailbox()

    def omit_uidnext(folder, operation, command, response):
        if folder == "Archive":
            return re.sub(rb"\* OK \[UIDNEXT [0-9]+\][^\r\n]*\r\n", b"", response)
        return response

    peer.response_hook = omit_uidnext
    preview = client.preview_action(
        ACTION, query={"subject": "October newsletter"}, params={"folder": TARGET}
    )
    assert preview["token"]
    grant = client.cleanup.tokens.inspect(preview["token"], ACTION)
    assert all("implicit_source_states" not in row for row in grant.messages)
    assert not grant.bounded_recovery
    peer.folders["Archive"]["500"] = peer.folders["INBOX"]["7"]
    peer.next_uid["Archive"] = 501
    before = len(peer.commands)
    with pytest.raises(MailValidationError, match="ambiguous"):
        client.move_to_folder(preview["token"])
    commands = peer.commands[before:]
    assert any(command.startswith(b"UID SEARCH") for _, command in commands)
    assert not mutations(commands) and not execution_log(log_path)


@pytest.mark.parametrize("copy_kind", ["identical", "different_fingerprint"])
def test_implicit_fallback_does_not_restore_incomplete_identity_certificates(
    socket_mailbox, copy_kind
):
    client, peer, log_path = socket_mailbox()
    preview = client.preview_action(
        ACTION, query={"subject": "October newsletter"}, params={"folder": TARGET}
    )
    assert preview["token"]
    duplicate = peer.folders["INBOX"]["7"]
    if copy_kind == "different_fingerprint":
        duplicate = duplicate.replace(b"October newsletter", b"Unapproved copy")
    arrival_uid = peer.next_uid["INBOX"]
    peer.folders["INBOX"][str(arrival_uid)] = duplicate
    peer.next_uid["INBOX"] = arrival_uid + 1
    before = len(peer.commands)
    with pytest.raises(MailValidationError):
        client.move_to_folder(preview["token"])
    commands = peer.commands[before:]
    assert any(command.startswith(b"UID SEARCH") for _, command in commands)
    assert not mutations(commands) and not execution_log(log_path)
    client.cleanup.tokens.inspect(preview["token"], ACTION)


def test_implicit_lost_response_with_arrival_completes_exact_original_remainder(slow_cleanup):
    case = slow_cleanup(count=30, timeout=24, mutation_seconds=2)
    preview = case.service.preview_action(
        ACTION,
        query={"from_": "notifications@example.invalid"},
        params={"folder": TARGET},
    )
    assert preview["token"]
    original = case.service.tokens.inspect(preview["token"], ACTION)

    def identity(row):
        return tuple(row[key] for key in ("folder", "uidvalidity", "uid", "message_id"))

    approved = {identity(row): row for row in original.messages}
    moved = []

    def observe(session, folder, operation, command):
        if operation == "MOVE":
            for uid in uid_values(command.split()[2]):
                item = case.peer.messages[case.peer.uidmap[folder][int(uid)]]
                moved.append((folder, str(case.peer.uidvalidity[folder]), uid, item.message_id()))

    case.peer.arrival_hook = observe
    results = [case.service.execute(ACTION, preview["token"])]
    assert results[0]["partial"] and results[0]["continuation_token"]
    resume_expiry = case.service.tokens.inspect(preview["token"], ACTION).expires
    assert resume_expiry > original.expires
    arrival = case.peer.add(
        Msg(hdr("<unapproved-arrival@example.invalid>", from_="notifications@example.invalid"))
    )
    # The partial response is lost. Reuse only the original token, despite the
    # arrival invalidating its source UIDNEXT optimization certificate.
    while results[-1]["partial"]:
        retry = case.service.tokens.inspect(preview["token"], ACTION)
        expected = set(approved) - set(moved)
        assert {identity(row) for row in retry.messages} == expected
        assert all(row == approved[identity(row)] for row in retry.messages)
        assert retry.expires == resume_expiry
        assert retry.expires_at == results[0]["expires_at"]
        started = case.clock.elapsed
        result = case.service.execute(ACTION, preview["token"])
        assert case.clock.elapsed - started < 50
        results.append(result)
        assert result["expires_at"] == results[0]["expires_at"]
        assert len(results) < 10 and result["done"] > 0 and not result["blocked"]
        assert not result["uncertain"] and result["run_id"] == results[0]["run_id"]
        assert len(moved) == len(set(moved)) == sum(call["done"] for call in results)
        assert set(moved) <= set(approved)
        journaled = [row for record in records(case) for row in record["messages"]]
        assert len(journaled) == len(moved) and all(row["status"] == "done" for row in journaled)
        assert {
            (row["source_folder"], row["uidvalidity_before"], row["uid_before"], row["message_id"])
            for row in journaled
        } == set(moved)
        assert result["run_done"] == len(moved)
        assert result["remaining"] == len(set(approved) - set(moved))
        if result["continuation_token"]:
            next_grant = case.service.tokens.inspect(result["continuation_token"], ACTION)
            assert {identity(row) for row in next_grant.messages} == set(approved) - set(moved)
    assert set(moved) == set(approved) and len(moved) == 30
    assert arrival.folder == "INBOX"
    assert not results[-1]["resume"] and results[-1]["continuation_token"] is None
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute(ACTION, preview["token"])
