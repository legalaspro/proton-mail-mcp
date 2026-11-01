"""Guarded cleanup round trips through the actual IMAP wire parser, offline."""

import copy
import json
from dataclasses import replace

import pytest
from action_records import execution_log
from action_records import execution_records as replay_records
from mock_imap_cleanup import CleanupIMAPPeer
from starlette.testclient import TestClient
from test_mail import message
from windows_skips import has_mode

from proton_mail_mcp import guarded
from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import MailClient, MailValidationError
from proton_mail_mcp.security import Credentials
from proton_mail_mcp.server import create_app


@pytest.fixture
def socket_mailbox(tmp_path):
    peers = []

    def build(**peer_options):
        peer = CleanupIMAPPeer(**peer_options)
        peers.append(peer)
        settings = Settings(
            token="offline-socket-test-token-" + "x" * 32,
            bridge_user="reader@example.test",
            imap_port=31337,
            action_log_path=str(tmp_path / "actions.jsonl"),
            timeout=2,
            call_timeout=10,
            execute_timeout=10,
        )
        backend = MailClient(
            settings, lambda _: Credentials("reader@example.test", "dummy"), peer.factory()
        )
        return backend, peer, tmp_path / "actions.jsonl"

    yield build
    for peer in peers:
        peer.close()


def mutations(commands):
    return [
        (folder, command)
        for folder, command in commands
        if command.startswith((b"UID MOVE", b"UID COPY", b"UID STORE", b"UID EXPUNGE"))
    ]


def folder_contents(peer):
    return {folder: peer.identifiers(folder) for folder in peer.folders}


@pytest.mark.parametrize(
    "action,params,target",
    [
        ("move_to_trash", {}, "Trash"),
        ("archive", {}, "Archive"),
        ("move_to_folder", {"folder": "Folders/Reviewed"}, "Folders/Reviewed"),
        ("add_label", {"label": "Review"}, "Labels/Review"),
        ("remove_label", {"label": "Review"}, "Labels/Review"),
    ],
)
def test_socket_preview_confirm_undo_roundtrip(socket_mailbox, action, params, target):
    backend, peer, log_path = socket_mailbox()
    source = "INBOX"
    if action == "remove_label":
        source = target
        peer.folders[source] = copy.deepcopy(peer.folders["INBOX"])
    original = folder_contents(peer)
    original_uids = copy.deepcopy(peer.folders)

    preview = backend.preview_action(action, query={"folder": source}, params=params)
    assert preview["count"] == 2 and len(preview["samples"]) == 2
    assert preview["blocked"] == [] and preview["token"]
    assert peer.folders == original_uids
    assert mutations(peer.commands) == []
    assert not any(command.startswith(b"SELECT ") for _, command in peer.commands)

    completed = getattr(backend, action)(preview["token"])
    assert completed["partial"] is False
    assert completed["log_ids"][0]
    if action == "remove_label":
        assert peer.identifiers(target) == set()
        assert peer.identifiers("All Mail") == original["All Mail"]
    else:
        assert peer.identifiers(target) == original[source]
        if action != "add_label":
            assert peer.identifiers(source) == set()

    first_log = log_path.read_bytes()
    with pytest.raises(MailValidationError):
        getattr(backend, action)(preview["token"])
    assert log_path.read_bytes() == first_log
    restored = backend.undo_action(completed["log_ids"][0])
    assert restored["partial"] is False
    assert folder_contents(peer) == original
    assert log_path.read_bytes().startswith(first_log)
    records = replay_records([json.loads(line) for line in log_path.read_text().splitlines()])
    assert [record["action"] for record in records] == [action, "undo_claim", "undo_action"]
    assert has_mode(log_path, 0o600)
    assert preview["token"] not in log_path.read_text()
    for folder, command in mutations(peer.commands):
        if b"EXPUNGE" in command or b"STORE" in command:
            assert folder.startswith("Labels/")
            assert b"," not in command.split()[2] and b":" not in command.split()[2]
    with pytest.raises(MailValidationError):
        backend.undo_action(completed["log_ids"][0])


def test_socket_protected_reply_to_blocks_entire_preview(socket_mailbox):
    backend, peer, log_path = socket_mailbox()
    peer.folders["INBOX"]["8"] = peer.folders["INBOX"]["8"].replace(
        b"\r\n\r\n", b"\r\nReply-To: alerts@mail.broker.example.org\r\n\r\n"
    )
    before = copy.deepcopy(peer.folders)
    preview = backend.preview_action("move_to_trash", query={"folder": "INBOX"})
    assert preview["count"] == 2
    assert preview["blocked"] and not preview["token"]
    assert peer.folders == before and not mutations(peer.commands)
    assert not execution_log(log_path)


@pytest.mark.parametrize("change", ["uidvalidity", "message_id", "missing_uid", "reply_to"])
def test_socket_confirmation_rechecks_identity_and_protection(socket_mailbox, change):
    backend, peer, _ = socket_mailbox()
    preview = backend.preview_action("move_to_trash", query={"folder": "INBOX"})
    if change == "uidvalidity":
        peer.uidvalidities["INBOX"] = "999"
    elif change == "message_id":
        peer.folders["INBOX"]["7"] = message("<replacement@example.test>").headers
    elif change == "missing_uid":
        del peer.folders["INBOX"]["7"]
    else:
        peer.folders["INBOX"]["8"] = peer.folders["INBOX"]["8"].replace(
            b"\r\n\r\n", b"\r\nReply-To: friend@gmail.com\r\n\r\n"
        )
    before = copy.deepcopy(peer.folders)
    with pytest.raises(MailValidationError):
        backend.move_to_trash(preview["token"])
    assert peer.folders == before and not mutations(peer.commands)


def test_socket_new_arrival_is_outside_approved_uid_set(socket_mailbox):
    backend, peer, _ = socket_mailbox()
    approved = peer.identifiers("INBOX")
    preview = backend.preview_action("move_to_trash", query={"folder": "INBOX"})
    peer.folders["INBOX"]["9"] = message("<new@example.test>").headers
    completed = backend.move_to_trash(preview["token"])
    assert completed["partial"] is False
    assert peer.identifiers("Trash") == approved
    assert peer.identifiers("INBOX") == {"<new@example.test>"}
    assert all(command.split()[2] != b"9" for _, command in mutations(peer.commands))


def test_socket_preview_over_cap_reports_count_without_token(socket_mailbox):
    backend, peer, _ = socket_mailbox()
    peer.folders["INBOX"] = {
        str(uid): message(f"<newsletter-{uid}@updates.example.test>").headers
        for uid in range(1, 202)
    }
    preview = backend.preview_action("archive", query={"folder": "INBOX"})
    assert preview["count"] == 201
    assert len(preview["samples"]) == 10
    assert preview["token"] is None
    assert mutations(peer.commands) == []


def test_socket_undo_reports_missing_message_and_holds_it_for_inspection(socket_mailbox):
    backend, peer, log_path = socket_mailbox()
    preview = backend.preview_action("move_to_trash", query={"folder": "INBOX"})
    completed = backend.move_to_trash(preview["token"])
    missing_uid = next(iter(peer.folders["Trash"]))
    del peer.folders["Trash"][missing_uid]
    restored = backend.undo_action(completed["log_ids"][0])
    assert restored["partial"] is True
    assert restored["count"] == 1 and len(restored["missing"]) == 1
    assert len(peer.identifiers("INBOX")) == 1
    assert peer.identifiers("Trash") == set()
    assert (
        len(replay_records([json.loads(line) for line in log_path.read_text().splitlines()])) == 3
    )
    with pytest.raises(MailValidationError, match="unresolved"):
        backend.undo_action(completed["log_ids"][0])


@pytest.mark.parametrize(
    "action,params", [("move_to_trash", {}), ("remove_label", {"label": "Review"})]
)
def test_socket_missing_capability_refuses_before_mutation(socket_mailbox, action, params):
    backend, peer, _ = socket_mailbox(capabilities=("IMAP4rev1", "STARTTLS"))
    source = "INBOX"
    if action == "remove_label":
        source = "Labels/Review"
        peer.folders[source] = copy.deepcopy(peer.folders["INBOX"])
    before = copy.deepcopy(peer.folders)
    preview = backend.preview_action(action, query={"folder": source}, params=params)
    with pytest.raises(MailValidationError):
        getattr(backend, action)(preview["token"])
    assert peer.folders == before and not mutations(peer.commands)


def test_socket_deadline_records_completed_move_and_partial_batch(socket_mailbox, monkeypatch):
    # Admit this tiny two-folder batch (one MOVE per source folder), then exceed
    # its planning allowance on the second MOVE to exercise the real socket
    # watchdog and the uncertain-outcome journal.
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SECONDS", 0.25)
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SECONDS_PER_MESSAGE", 0.0)
    monkeypatch.setattr(guarded, "EXECUTE_LOG_RESERVE_SECONDS", 0.1)
    backend, peer, log_path = socket_mailbox()
    peer.folders["Archive"]["8"] = peer.folders["INBOX"].pop("8")
    ids = sorted(peer.identifiers("INBOX") | peer.identifiers("Archive"))
    preview = backend.preview_action("move_to_trash", ids=ids)
    assert preview["token"] and preview["count"] == 2
    backend.settings = replace(backend.settings, execute_timeout=1.0)
    peer.stall_mutation = 2
    result = backend.move_to_trash(preview["token"])
    assert result["partial"] is True and result["log_ids"][0]
    assert [row["status"] for row in result["messages"]] == ["done", "uncertain"]
    assert len(peer.identifiers("Trash")) == 1
    assert len(peer.identifiers("Archive")) == 1
    record = json.loads(log_path.read_text().splitlines()[-1])
    assert record["partial"] is True
    assert preview["token"] not in log_path.read_text()


def test_socket_interrupted_label_removal_undo_clears_only_label_flag(socket_mailbox, monkeypatch):
    monkeypatch.setattr(guarded, "EXECUTE_MUTATION_SECONDS_PER_MESSAGE", 0.25)
    monkeypatch.setattr(guarded, "EXECUTE_LOG_RESERVE_SECONDS", 0.1)
    backend, peer, _ = socket_mailbox()
    peer.folders["Labels/Review"] = copy.deepcopy(peer.folders["INBOX"])
    original = folder_contents(peer)
    preview = backend.preview_action(
        "remove_label", query={"folder": "Labels/Review"}, params={"label": "Review"}
    )
    backend.settings = replace(backend.settings, execute_timeout=1.0)
    peer.stall_mutation = 2
    interrupted = backend.remove_label(preview["token"])
    assert interrupted["partial"] is True
    assert interrupted["count"] == 0
    assert len(peer.deleted["Labels/Review"]) == 1
    assert folder_contents(peer) == original
    peer.stall_mutation = None
    backend.settings = replace(backend.settings, call_timeout=5)
    restored = backend.undo_action(interrupted["log_ids"][0])
    assert restored["partial"] is False
    assert not peer.deleted["Labels/Review"]
    assert folder_contents(peer) == original
    assert any(
        folder == "Labels/Review" and command.endswith(rb"-FLAGS.SILENT (\Deleted)")
        for folder, command in mutations(peer.commands)
    )


@pytest.mark.parametrize(
    "extra", [{"params": {"folder": "Trash"}}, {"ids": ["<other@example.test>"]}]
)
def test_mcp_extra_confirmation_arguments_refuse_without_mail_changes(socket_mailbox, extra):
    backend, peer, _ = socket_mailbox()
    preview = backend.preview_action("archive", query={"folder": "INBOX"})
    before = copy.deepcopy(peer.folders)
    # Stateless JSON routing; host confirmation has its own tests.
    settings = replace(backend.settings, confirm_mutations=False)
    with TestClient(create_app(settings, backend), base_url="http://127.0.0.1") as http:
        response = http.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {backend.settings.token}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "move",
                    "arguments": {"target": "Archive", "token": preview["token"], **extra},
                },
            },
        )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    assert preview["token"] not in response.text
    assert peer.folders == before and mutations(peer.commands) == []


def test_mcp_socket_dry_run_preview_confirm_undo(socket_mailbox):
    backend, peer, _ = socket_mailbox()
    app = create_app(replace(backend.settings, confirm_mutations=False), mail_client=backend)
    before = folder_contents(peer)
    with TestClient(app, base_url="http://127.0.0.1") as http:

        def invoke(name, arguments):
            response = http.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {backend.settings.token}",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert not result.get("isError"), result
            return result["structuredContent"]

        preview = invoke("preview_action", {"action": "archive", "query": {"folder": "INBOX"}})
        assert preview["count"] == 2 and not mutations(peer.commands)
        completed = invoke("move", {"target": "Archive", "token": preview["token"]})
        assert completed["partial"] is False
        restored = invoke("undo_action", {"log_id": completed["log_ids"][0]})
        assert restored["partial"] is False
        assert folder_contents(peer) == before
