"""Folder creation uses real IMAP framing against a command-recording local fake."""

import ast
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from bridge_sim import BridgeSim, make_client

from proton_mail_mcp.action_log import LogTransaction
from proton_mail_mcp.config import ConfigError
from proton_mail_mcp.create_folder import CreateFolderService
from proton_mail_mcp.folder_rules import load_rules
from proton_mail_mcp.guarded import TokenStore
from proton_mail_mcp.mail import MailError, MailValidationError, _decode_folder


class FolderBridge(BridgeSim):
    def handle(self, tag, op, uid_op, words, session, command):
        if op == "CREATE":
            name = _decode_folder(words[1].encode("ascii"))
            assert name.startswith("Folders/") and name not in self.mailbox_names()
            with self.lock:
                self.custom_folders.append(name)
                self.sync()
            return tag + b" OK created\r\n"
        return super().handle(tag, op, uid_op, words, session, command)


@pytest.fixture
def case(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Folder creation must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    sim = FolderBridge()
    client = make_client(sim, tmp_path)
    cleanup = client.cleanup
    result = SimpleNamespace(
        sim=sim,
        client=client,
        cleanup=cleanup,
        service=CreateFolderService(cleanup),
        log=cleanup.backend.log,
    )
    yield result
    sim.close()
    assert not sim.errors
    assert not sim.violations
    assert not any(
        command.split()[0] in {"DELETE", "RENAME", "STORE", "EXPUNGE", "UID", "APPEND"}
        for *_, command in sim.commands
    )


def creates(case):
    return [command for *_, command in case.sim.commands if command.startswith("CREATE ")]


def records(case):
    with case.log.transaction(readonly=True) as transaction:
        return transaction.records()


@pytest.mark.parametrize("name", ["Folders/GitHub", "Folders/Work/Project", "Folders/Café & News"])
def test_create_once_with_durable_intent_and_single_use_token(case, name):
    preview = case.service.preview_create_folder(name)
    assert preview["name"] == name and preview["exists"] is False
    assert preview["token"] and preview["expires_at"]
    assert not creates(case) and not case.log.path.exists()

    def before_create(sid, selected, op, command):
        if op == "CREATE":
            raw = [json.loads(line) for line in case.log.path.read_text().splitlines()]
            assert raw[0]["action"] == "create_folder" and raw[0]["status"] == "in_progress"
            assert raw[-1]["action"] == "execute_progress"
            assert raw[-1]["message"]["status"] == "uncertain"
            with pytest.raises(MailValidationError, match="already used"):
                case.cleanup.tokens.inspect(preview["token"], "create_folder")

    case.sim.hooks.append(before_create)
    result = case.service.create_folder(preview["token"])
    assert result["created"] and result["token_consumed"] and not result["partial"]
    assert name in case.sim.custom_folders and len(creates(case)) == 1
    logged = records(case)
    assert len(logged) == 1 and logged[0]["log_id"] == result["log_id"]
    assert logged[0]["params"] == {"name": name}
    assert logged[0]["messages"][0]["result"] == "created"
    assert logged[0]["status"] == "completed"
    assert preview["token"] not in case.log.path.read_text()
    assert case.client.list_actions(token=preview["token"])["actions"][0]["done"] == 1
    with pytest.raises(MailValidationError, match="already used"):
        case.service.create_folder(preview["token"])
    with pytest.raises(MailValidationError, match="cannot be undone"):
        case.client.undo_action(result["log_id"])
    assert name in case.sim.custom_folders and len(creates(case)) == 1


def test_second_preview_of_created_folder_issues_no_token(case, monkeypatch):
    token = case.service.preview_create_folder("Folders/New")["token"]
    assert case.service.create_folder(token)["created"]

    def unexpected(*args, **kwargs):
        pytest.fail("An existing folder must not issue or consume a token")

    monkeypatch.setattr(case.cleanup.tokens, "issue", unexpected)
    monkeypatch.setattr(case.cleanup.tokens, "claim", unexpected)
    preview = case.service.preview_create_folder("Folders/New")
    assert preview["exists"] is True and preview["token"] is None
    assert preview["expires_at"] is None and len(creates(case)) == 1


def test_folder_appears_after_preview_returns_logged_noop_without_consuming_token(case):
    token = case.service.preview_create_folder("Folders/Appeared")["token"]
    case.sim.custom_folders.append("Folders/Appeared")
    for _ in range(2):
        result = case.service.create_folder(token)
        assert result["exists"] is True and result["created"] is False
        assert result["log_id"] and "log_ids" not in result
        assert result["token_consumed"] is False and not result["partial"]
        assert case.cleanup.tokens.inspect(token, "create_folder").params == {
            "name": "Folders/Appeared"
        }
    assert not creates(case)
    assert all(record["messages"][0]["result"] == "exists" for record in records(case))


def test_nonselectable_existing_folder_is_also_noop(case, monkeypatch):
    handle = case.sim.handle

    def existing_parent(tag, op, uid_op, words, session, command):
        response = handle(tag, op, uid_op, words, session, command)
        if op == "LIST":
            response = b'* LIST (\\Noselect) "/" "Folders/Parent"\r\n' + response
        return response

    monkeypatch.setattr(case.sim, "handle", existing_parent)
    preview = case.service.preview_create_folder("Folders/Parent")
    assert preview["exists"] and preview["token"] is None and not creates(case)


INVALID_NAMES = [
    None,
    1,
    [],
    "",
    "GitHub",
    "Folders",
    "Folders/",
    "INBOX",
    "Trash",
    "Labels/News",
    "folders/News",
    " Folders/News",
    "Folders/ News",
    "Folders/News ",
    "Folders/a//b",
    "Folders/./b",
    "Folders/../b",
    "Folders/a/..",
    "Folders/a\nNOOP",
    "Folders/a\0b",
    "Folders/a\x7fb",
    "Folders/a\x85b",
    "Folders/*",
    "Folders/%",
    'Folders/a"b',
    "Folders/a'b",
    "Folders/a\\b",
    "Folders/" + "x" * 493,
    "Folders/\ud800",
]


@pytest.mark.parametrize("name", INVALID_NAMES)
def test_invalid_folder_names_refused_before_connection(case, name):
    with pytest.raises(MailValidationError):
        case.service.preview_create_folder(name)
    assert not case.sim.commands and not case.log.path.exists()
    assert not case.cleanup.tokens._grants


@pytest.mark.parametrize("name", ["Folders/*", "Folders/%", "Folders/" + "x" * 493])
def test_rules_share_strict_destination_validation(tmp_path, name):
    path = tmp_path / "rules.json"
    path.write_text(
        json.dumps(
            {"version": 1, "rules": [{"field": "subject", "contains": "News", "folder": name}]}
        )
    )
    with pytest.raises(ConfigError):
        load_rules(path)


def test_name_length_boundary_is_accepted(case):
    name = "Folders/" + "x" * 492
    assert case.service.preview_create_folder(name)["token"]


def test_mail_client_forwards_preview_and_creation_to_shared_token_store(case):
    preview = case.client.preview_create_folder("Folders/Client")
    assert case.cleanup.tokens.inspect(preview["token"], "create_folder")
    result = case.client.create_folder(preview["token"])
    assert result["created"] and "Folders/Client" in case.sim.custom_folders
    assert case.client.preview_create_folder("Folders/Client")["token"] is None


@pytest.mark.parametrize("token", [None, "", "forged"])
def test_invalid_token_refused_before_connection(case, token):
    with pytest.raises(MailValidationError):
        case.service.create_folder(token)
    assert not case.sim.commands and not case.log.path.exists()


def test_wrong_action_token_and_expired_token_cannot_create(case):
    now = [0.0]
    case.cleanup.tokens = TokenStore(ttl=1, clock=lambda: now[0])
    token = case.service.preview_create_folder("Folders/Expires")["token"]
    with pytest.raises(MailValidationError, match="action"):
        case.cleanup.tokens.inspect(token, "archive")
    now[0] = 2.0
    with pytest.raises(MailValidationError, match="expired"):
        case.service.create_folder(token)
    assert not creates(case) and not case.log.path.exists()


@pytest.mark.parametrize("params", [{}, {"name": "Trash"}, {"name": "Folders/A", "extra": "B"}])
def test_malformed_grant_cannot_create(case, params):
    token, _ = case.cleanup.tokens.issue("create_folder", params, {}, [])
    with pytest.raises(MailValidationError):
        case.service.create_folder(token)
    assert not case.sim.commands


def test_empty_message_grants_only_allowed_for_folder_creation():
    store = TokenStore()
    for action in ("archive", "move_to_folder", "apply_rules", "unsubscribe"):
        with pytest.raises(MailValidationError):
            store.issue(action, {}, {}, [])
    with pytest.raises(MailValidationError):
        store.issue("create_folder", {"name": "Folders/A"}, {}, [{"message_id": "id"}])


def test_journal_unwritable_refuses_create_and_preserves_token(case, monkeypatch):
    token = case.service.preview_create_folder("Folders/Unlogged")["token"]

    def unavailable(*args, **kwargs):
        raise MailError("Synthetic unavailable journal")

    monkeypatch.setattr(LogTransaction, "append", unavailable)
    with pytest.raises(MailError):
        case.service.create_folder(token)
    assert not creates(case)
    assert case.cleanup.tokens.inspect(token, "create_folder")


def test_create_failure_is_journaled_without_exception_text(case):
    token = case.service.preview_create_folder("Folders/Refused")["token"]
    case.sim.hooks.append(
        lambda sid, selected, op, command: ("NO", "private detail") if op == "CREATE" else None
    )
    result = case.service.create_folder(token)
    assert result["partial"] and result["token_consumed"] and not result["created"]
    assert len(creates(case)) == 1
    assert records(case)[0]["messages"][0]["status"] == "uncertain"
    assert "private detail" not in str(result) + case.log.path.read_text()


@pytest.mark.parametrize("failure", ["intent", "outcome"])
def test_journal_progress_failure_never_hides_creation_outcome(case, monkeypatch, failure):
    token = case.service.preview_create_folder("Folders/JournalFailure")["token"]
    append = LogTransaction.append

    def fail_progress(transaction, record):
        if record["action"] == "execute_progress" and record["message"]["status"] == (
            "uncertain" if failure == "intent" else "done"
        ):
            raise MailError("Synthetic journal failure")
        return append(transaction, record)

    monkeypatch.setattr(LogTransaction, "append", fail_progress)
    result = case.service.create_folder(token)
    assert result["partial"] and result["token_consumed"]
    assert result["created"] is (failure == "outcome")
    assert len(creates(case)) == (1 if failure == "outcome" else 0)
    assert records(case)[0]["messages"][0]["status"] == (
        "uncertain" if failure == "outcome" else "not_attempted"
    )


def test_package_has_no_mailbox_delete_or_rename_commands():
    package = Path(__file__).resolve().parents[1] / "src" / "proton_mail_mcp"
    for source in package.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute):
                assert node.attr.casefold() not in {"delete", "rename"}, source.name
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value.upper() not in {"DELETE", "RENAME"}, source.name
