"""Decision 11 applies before preview approval and before any writable selection."""

from copy import deepcopy

import pytest
from action_records import execution_log
from bridge_sim import FOLDER_ATTRS, Msg, hdr, mutations
from test_duplicate_msgid import DESTINATION, rules_service
from test_duplicate_msgid import duplicate_mailbox as duplicate_mailbox

from proton_mail_mcp.mail import MailValidationError

SOURCES = [
    ("Drafts", None),
    ("Sent", None),
    ("drafts", None),
    ("sEnT", None),
    (" Drafts ", None),
    (" Sent ", None),
    ("Folders/Drafts", None),
    ("Folders/Sent", None),
    ("Folders/ drafts ", None),
    ("folders/ SENT ", None),
    ("Folders/Brouillons", r"\Drafts"),
    ("Folders/Envoyes", r"\sEnT"),
    ("Folders/Renamed drafts", r"\dRaFtS"),
    ("Folders/Renamed sent", r"\Sent"),
]
ACTIONS = ["mark_read", "mark_unread", "move_to_folder", "apply_rules"]


def source_message(sim, folder):
    return sim.add(
        Msg(
            hdr("<source-private@example.test>", from_="notifications@github.example"),
            folder=folder,
            body=b"synthetic-body-secret-never-log",
            flags={r"\Answered", "private-keyword"},
        )
    )


def action_calls(client, tmp_path, action, folder):
    if action == "apply_rules":
        service = rules_service(client, tmp_path)
        return lambda: service.preview_rules(folder=folder, mark_read=True), service.apply_rules
    params = {"folder": DESTINATION, "mark_read": True} if action == "move_to_folder" else None
    return (
        lambda: client.preview_action(action, query={"folder": folder}, params=params),
        getattr(client, action),
    )


def assert_untouched(sim, item, flags, folder):
    assert not mutations(sim)
    assert item.flags == flags and item.folder == folder
    assert not any(command.startswith("SELECT ") for _, _, _, command in sim.commands)


@pytest.mark.parametrize("source,attributes", SOURCES)
@pytest.mark.parametrize("action", ACTIONS)
def test_read_state_source_preview_refuses_without_token(
    duplicate_mailbox, tmp_path, monkeypatch, caplog, source, attributes, action
):
    sim, client = duplicate_mailbox(folders=(DESTINATION, source))
    if attributes:
        monkeypatch.setitem(FOLDER_ATTRS, source, attributes)
    item = source_message(sim, source)
    before = set(item.flags)
    preview, _ = action_calls(client, tmp_path, action, source)
    result = preview()
    assert result["token"] is None and result["blocked"]
    assert "source" in str(result).lower()
    assert "source refused" in caplog.text.lower()
    assert_untouched(sim, item, before, source)
    for secret in (item.message_id(), item.body.decode(), "notifications@github.example"):
        assert secret not in caplog.text
    assert not execution_log(client.settings.action_log_path)


@pytest.mark.parametrize("source,attributes", SOURCES)
@pytest.mark.parametrize("action", ACTIONS)
def test_read_state_source_stored_grant_refuses_before_write_select(
    duplicate_mailbox, tmp_path, monkeypatch, caplog, source, attributes, action
):
    sim, client = duplicate_mailbox(folders=(DESTINATION, source))
    item = source_message(sim, "INBOX")
    preview, execute = action_calls(client, tmp_path, action, "INBOX")
    token = preview()["token"]
    grant = client.cleanup.tokens.inspect(token, action)
    item.folder = source
    sim.sync()
    rows = deepcopy(grant.messages)
    for row in rows:
        row["folder"] = source
        row["uid"] = str(sim.uid_of(source, item.pid))
        row["uidvalidity"] = str(sim.uidvalidity[source])
        for membership in row["memberships"]:
            if membership["folder"] == "INBOX":
                membership.update({key: row[key] for key in ("folder", "uid", "uidvalidity")})
        row["folders"] = [source if name == "INBOX" else name for name in row["folders"]]
    selector = {"folder": source} if action == "apply_rules" else {"query": {"folder": source}}
    token, _ = client.cleanup.tokens.issue(action, grant.params, selector, rows)
    if attributes:
        monkeypatch.setitem(FOLDER_ATTRS, source, attributes)
    sim.commands.clear()
    before = set(item.flags)
    with pytest.raises(MailValidationError, match="source"):
        execute(token)
    assert_untouched(sim, item, before, source)
    assert client.cleanup.tokens.inspect(token, action)
    refused_preview, _ = action_calls(client, tmp_path, action, source)
    refused = refused_preview()
    assert refused["token"] is None and refused["blocked"]
    assert "source refused" in caplog.text.lower()
    assert_untouched(sim, item, before, source)
    for secret in (token, item.message_id(), item.body.decode(), "notifications@github.example"):
        assert secret not in caplog.text
    assert not execution_log(client.settings.action_log_path)


@pytest.mark.parametrize("attributes", [r"\Drafts", r"\Sent"])
@pytest.mark.parametrize("action", ACTIONS)
def test_stored_read_state_grant_rechecks_current_special_use(
    duplicate_mailbox, tmp_path, monkeypatch, action, attributes
):
    sim, client = duplicate_mailbox()
    item = source_message(sim, "INBOX")
    preview, execute = action_calls(client, tmp_path, action, "INBOX")
    token = preview()["token"]
    monkeypatch.setitem(FOLDER_ATTRS, "INBOX", attributes)
    sim.commands.clear()
    before = set(item.flags)
    with pytest.raises(MailValidationError, match="source"):
        execute(token)
    assert_untouched(sim, item, before, "INBOX")


@pytest.mark.parametrize("folder", ["INBOX", "Folders/Reviewed"])
@pytest.mark.parametrize("action", ["mark_read", "mark_unread"])
def test_read_state_ordinary_source_remains_allowed(duplicate_mailbox, folder, action):
    sim, client = duplicate_mailbox()
    item = source_message(sim, folder)
    item.flags.add(r"\Seen")
    before = set(item.flags)
    preview = client.preview_action(action, query={"folder": folder})
    assert preview["token"] and not preview["blocked"]
    result = getattr(client, action)(preview["token"])
    assert result["count"] == 1 and item.folder == folder
    assert item.flags == (before | {r"\Seen"} if action == "mark_read" else before - {r"\Seen"})
    assert client.undo_action(result["log_ids"][0])["count"] == 1
    assert item.flags == before


@pytest.mark.parametrize("action", ["mark_read", "mark_unread"])
def test_read_state_refusal_log_never_echoes_untrusted_source(
    duplicate_mailbox, tmp_path, monkeypatch, caplog, action
):
    source = "Folders/synthetic-private-source-secret"
    sim, client = duplicate_mailbox(folders=(source, DESTINATION))
    item = source_message(sim, source)
    monkeypatch.setitem(FOLDER_ATTRS, source, r"\Drafts")
    preview, execute = action_calls(client, tmp_path, action, source)
    assert preview()["token"] is None
    with pytest.raises(MailValidationError):
        execute("synthetic-forged-token-secret")
    for secret in (source, "synthetic-forged-token-secret", item.message_id(), item.body.decode()):
        assert secret not in caplog.text
    assert_untouched(sim, item, set(item.flags), source)


@pytest.mark.parametrize("alias", ["INBOX", "inbox", "InBoX"])
@pytest.mark.parametrize("attributes", [r"\dRaFtS", r"\sEnT"])
@pytest.mark.parametrize("phase", ["preview", "confirm"])
@pytest.mark.parametrize("action", ["mark_read", "mark_unread"])
def test_read_state_conflicting_list_alias_cannot_hide_special_use(
    duplicate_mailbox, tmp_path, monkeypatch, alias, attributes, phase, action
):
    sim, client = duplicate_mailbox()
    item = source_message(sim, "INBOX")
    preview, execute = action_calls(client, tmp_path, action, "INBOX")
    token = preview()["token"] if phase == "confirm" else None
    sim.custom_folders.append(alias)
    monkeypatch.setitem(FOLDER_ATTRS, alias, attributes)
    sim.commands.clear()
    before = set(item.flags)
    if phase == "preview":
        result = preview()
        assert result["token"] is None and result["blocked"]
    else:
        with pytest.raises(MailValidationError, match="source"):
            execute(token)
    assert_untouched(sim, item, before, "INBOX")


@pytest.mark.parametrize("source", ["Drafts", "Sent", "Folders/Brouillons"])
@pytest.mark.parametrize("action", ["mark_read", "mark_unread", "move_to_folder"])
def test_read_state_id_selector_refuses_protected_source(
    duplicate_mailbox, monkeypatch, source, action
):
    sim, client = duplicate_mailbox(folders=(DESTINATION, source))
    if source.startswith("Folders/"):
        monkeypatch.setitem(FOLDER_ATTRS, source, r"\Drafts")
    item = source_message(sim, source)
    before = set(item.flags)
    params = {"folder": DESTINATION, "mark_read": True} if action == "move_to_folder" else None
    preview = client.preview_action(action, ids=[item.message_id()], params=params)
    assert preview["token"] is None and preview["blocked"]
    assert_untouched(sim, item, before, source)


@pytest.mark.parametrize("attributes", [r"\Drafts", r"\Sent"])
@pytest.mark.parametrize("phase", ["preflight", "perform"])
@pytest.mark.parametrize("action", ["mark_read", "mark_unread", "move_to_folder"])
def test_backend_rechecks_read_state_source_before_write_select(
    duplicate_mailbox, tmp_path, monkeypatch, attributes, phase, action
):
    sim, client = duplicate_mailbox()
    item = source_message(sim, "INBOX")
    preview, _ = action_calls(client, tmp_path, action, "INBOX")
    grant = client.cleanup.tokens.inspect(preview()["token"], action)
    rows = grant.messages
    if action == "move_to_folder":
        rows[0]["mark_read"] = True
    monkeypatch.setitem(FOLDER_ATTRS, "INBOX", attributes)
    sim.commands.clear()
    before = set(item.flags)
    with client._session() as (connection, _):
        deadline = client._deadline()
        connection._cleanup_seen = True
        connection._cleanup_moved_handles = set()
        with pytest.raises(MailValidationError, match="source"):
            if phase == "preflight":
                client.cleanup.backend._preflight(connection, deadline, action, DESTINATION, rows)
            else:
                client.cleanup.backend._perform(
                    connection, deadline, action, DESTINATION, rows[0], {}
                )
    assert_untouched(sim, item, before, "INBOX")


@pytest.mark.parametrize("source", ["Drafts", "Sent"])
@pytest.mark.parametrize("action", ["move_to_folder", "move_to_trash", "archive"])
def test_plain_move_source_policy_remains_unchanged_until_h4(duplicate_mailbox, source, action):
    sim, client = duplicate_mailbox()
    item = source_message(sim, source)
    params = {"folder": DESTINATION} if action == "move_to_folder" else None
    preview = client.preview_action(action, query={"folder": source}, params=params)
    assert preview["token"] and not preview["blocked"]
    result = getattr(client, action)(preview["token"])
    assert result["count"] == 1
    assert all("UID MOVE " in command for _, _, _, command in mutations(sim))
    assert client.undo_action(result["log_ids"][0])["count"] == 1
    assert item.folder == source
