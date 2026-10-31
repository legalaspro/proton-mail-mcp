"""Shipped timeout defaults leave enough time to undo each logged cleanup chunk."""

import json
from types import SimpleNamespace

import pytest
from action_records import execution_records as replay_records
from resume_assertions import ResumeAudit
from test_cleanup_mutations import MUTATIONS, MutatingImap
from test_mail import client, message, unquote
from test_preview_proton_shape import WorkClock

from proton_mail_mcp import mail
from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import MailValidationError


# The shipped 120-second defaults: one batched chunk moves all 184 messages in
# one call. Undo restores each message by its own logged handle, so a large
# chunk can need several undo calls; each retry restores only untouched entries.
@pytest.mark.parametrize("move_seconds,undo_calls", [(1, 2), (2, 4)])
def test_shipped_defaults_restore_184_after_chunked_cleanup(
    tmp_path, monkeypatch, move_seconds, undo_calls
):
    identifiers = [f"<default-undo-{index:03d}@example.test>" for index in range(1, 185)]
    items = {str(index): message(identifier) for index, identifier in enumerate(identifiers, 1)}
    original_messages = {id(item) for item in items.values()}
    fake = MutatingImap({"INBOX": items, "Trash": {}, "All Mail": dict(items)})
    clock = WorkClock()
    monkeypatch.setattr(mail, "time", clock)
    log = tmp_path / "synthetic-default-undo.jsonl"
    # Exercise the actual shipped settings independently of the chunk fixture.
    settings = Settings.from_env(
        {
            "PROTON_MCP_TOKEN": "offline-default-undo-" + "x" * 32,
            "PROTON_BRIDGE_USER": "reader@example.test",
            "PROTON_MCP_ACTION_LOG": str(log),
        }
    )
    assert settings.call_timeout == 120
    assert settings.execute_timeout == 120
    mailbox = client(settings, fake)
    service = mailbox.cleanup
    original_uid = fake.uid
    highwater = {folder: max(map(int, rows), default=100) for folder, rows in fake.folders.items()}

    def timed_move(command, *args):
        result = original_uid(command, *args)
        if command in {"MOVE", "COPY"} and fake.copyuid:
            # IMAP UIDs cannot rewind after a folder empties. Reusing UIDs in
            # the fake would mask which message a later undo actually restores.
            destination = unquote(args[1])
            validity, before, after = fake.copyuid.decode().split()
            renumbered = []
            for uid in after.split(","):
                assigned = max(int(uid), highwater[destination] + 1)
                if str(assigned) != uid:
                    fake.folders[destination][str(assigned)] = fake.folders[destination].pop(uid)
                highwater[destination] = assigned
                renumbered.append(str(assigned))
            fake.copyuid = f"{validity} {before} {','.join(renumbered)}".encode()
        if command == "MOVE":
            clock.elapsed += move_seconds
        return result

    monkeypatch.setattr(fake, "uid", timed_move)
    preview = service.preview_action("move_to_trash", ids=identifiers)
    assert preview["count"] == 184 and preview["token"]
    case = SimpleNamespace(service=service, fake=fake, clock=clock, log=log)
    audit = ResumeAudit(case, preview["token"])
    result = audit.execute(preview["token"])
    assert result["done"] == 184 and not result["partial"] and not result["resume"]
    assert clock.elapsed == move_seconds  # One batched MOVE command.
    executed = replay_records([json.loads(line) for line in log.read_text().splitlines()])
    assert [len(record["messages"]) for record in executed] == [184]
    assert result["log_ids"] == [record["log_id"] for record in executed]
    assert len(fake.folders["Trash"]) == 184

    restored, calls = 0, 0
    log_id = result["log_ids"][0]
    while restored < 184:
        calls += 1
        started = clock.elapsed
        undo = service.undo_action(log_id)
        # Each undo call stays within its own call budget (the response reserve
        # absorbs the last per-message MOVE that started before its I/O end).
        assert clock.elapsed - started < settings.execute_timeout
        assert undo["count"] > 0 and all(
            entry["status"] in {"done", "not_attempted"} for entry in undo["messages"]
        )
        restored += undo["count"]
        assert undo["partial"] is (restored < 184)
        assert calls <= undo_calls
    assert calls == undo_calls
    with pytest.raises(MailValidationError, match="already been undone"):
        service.undo_action(log_id)
    assert len(fake.folders["INBOX"]) == 184
    assert {id(item) for item in fake.folders["INBOX"].values()} == original_messages
    assert not fake.folders["Trash"]
    changes = [call for call in fake.calls if call[0] in MUTATIONS]
    assert len(changes) == 1 + 184
    assert all(call[0] == "MOVE" for call in changes)
    assert changes[0][2] == '"Trash"'
    assert all(call[2] == '"INBOX"' for call in changes[1:])
