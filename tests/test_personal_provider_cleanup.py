"""Allowlist cleanup approvals and audit records use only in-memory mail fakes."""

import hashlib
import json
import os
from copy import deepcopy

import pytest
from action_records import execution_log, execution_records
from test_cleanup_mutations import MutatingImap, mutation_calls
from test_mail import client, message

from proton_mail_mcp.action_log import ActionLog
from proton_mail_mcp.config import Settings
from proton_mail_mcp.guarded import CleanupService
from proton_mail_mcp.mail import CallDeadlineExceeded, MailValidationError
from proton_mail_mcp.protected import ProtectedPolicy

APPROVED = "approved-sender@gmail.com"
OTHER_PERSONAL = "someone-else@gmail.com"
PRIVATE_SUBJECT = "Synthetic allowlist audit privacy sentinel"
ENTRY_DIGEST = hashlib.sha256(APPROVED.encode()).hexdigest()


def approving(config):
    # The committed allowlist is empty, so the local policy approves the sender.
    if config.get("mode") == "replace":
        return config
    entries = [APPROVED, *config.get("personal_provider_allowlist", [])]
    return {**config, "personal_provider_allowlist": entries}


def evidence(*headers):
    return [{"header": header, "entry_sha256": ENTRY_DIGEST} for header in headers]


# The fake faithfully models Gluon's ENVELOPE: absent Sender and Reply-To fall
# back to From, so the guarded path evaluates all three envelope address fields.
FALLBACK_EVIDENCE = evidence("From", "Sender", "Reply-To")


@pytest.fixture
def mailbox(tmp_path):
    def create(*, count=1, sender=APPROVED, extra_headers=b"", config=None):
        source = {}
        for index in range(1, count + 1):
            item = message(f"<allowlist-{index}@example.test>", subject=PRIVATE_SUBJECT)
            item.headers = extra_headers + item.headers.replace(
                b"From: Sender <sender@example.com>\r\n",
                f"From: Newsletter <{sender}>\r\n".encode(),
            )
            source[str(index)] = item
        fake = MutatingImap(
            {
                "INBOX": source,
                "Trash": {},
                "Archive": {},
                "Labels/Review": {},
                "All Mail": {str(1000 + int(uid)): item for uid, item in source.items()},
            }
        )
        path = tmp_path / "synthetic-policy.json"
        path.write_text(json.dumps(approving(config or {})))
        policy = ProtectedPolicy(str(path))
        settings = Settings(
            token="offline-allowlist-test-token-at-least-thirty-two-characters",
            bridge_user="reader@example.test",
            action_log_path=str(tmp_path / "synthetic-actions.jsonl"),
            call_timeout=120,
        )
        service = CleanupService(client(settings, fake), policy=policy)
        return service, fake

    return create


def records(service):
    with service.backend.log.transaction() as transaction:
        return execution_records(transaction.records())


def load_policy(service, tmp_path, config):
    path = tmp_path / "changed-synthetic-policy.json"
    path.write_text(json.dumps(approving(config)))
    policy = ProtectedPolicy(str(path))
    service.policy = service.backend.policy = policy


def test_preview_reports_bypasses_beyond_sample_limit_without_execution_log(mailbox):
    service, fake = mailbox(count=12)
    preview = service.preview_action("move_to_trash", query={})
    assert preview["token"] and not preview["blocked"]
    assert preview["count"] == preview["personal_provider_bypass_count"] == 12
    assert len(preview["samples"]) == 10
    assert all(row["personal_provider_bypasses"] == FALLBACK_EVIDENCE for row in preview["samples"])
    assert not mutation_calls(fake)
    assert not execution_log(service.backend.log.path)


def test_execute_logs_each_header_digest_privately_and_appends_undo(mailbox, monkeypatch):
    service, fake = mailbox(
        count=2,
        sender=APPROVED.upper(),
        extra_headers=(f"Sender: {APPROVED}\r\nReply-To: {APPROVED}\r\n").encode(),
    )
    opened = []
    original_open = os.open

    def track_open(path, flags, mode=0o777, *, dir_fd=None):
        if os.fspath(path) == os.fspath(service.backend.log.path):
            opened.append(flags)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", track_open)
    preview = service.preview_action("move_to_trash", query={})
    expected = evidence("From", "Sender", "Reply-To")
    assert preview["token"] and preview["personal_provider_bypass_count"] == 2
    assert preview["samples"][0]["personal_provider_bypasses"] == expected
    result = service.execute("move_to_trash", preview["token"])
    assert result["count"] == 2 and not result["partial"]
    logged = records(service)
    assert len(logged) == 1 and logged[0]["action"] == "move_to_trash"
    assert [row["personal_provider_bypasses"] for row in logged[0]["messages"]] == [
        expected,
        expected,
    ]
    before = service.backend.log.path.read_bytes()
    assert ENTRY_DIGEST.encode() in before
    for private in (APPROVED, PRIVATE_SUBJECT, service.settings.token):
        assert private.casefold().encode() not in before.lower()
    assert mutation_calls(fake) == [("MOVE", "1:2", '"Trash"')]  # One batched MOVE.
    with pytest.raises(MailValidationError, match="already used"):
        service.execute("move_to_trash", preview["token"])
    undo = service.undo_action(result["log_ids"][0])
    assert undo["count"] == 2 and not undo["partial"]
    after = service.backend.log.path.read_bytes()
    assert after.startswith(before) and len(records(service)) == 3
    assert opened
    assert all(flags & os.O_APPEND and not flags & os.O_TRUNC for flags in opened)
    assert service.backend.log.path.stat().st_mode & 0o777 == 0o600
    assert all(
        row["personal_provider_bypasses"] == expected for row in records(service)[-1]["messages"]
    )


@pytest.mark.parametrize(
    "sender,extra_headers,config,reason",
    [
        (OTHER_PERSONAL, b"", None, "Personal mail provider in From"),
        (
            APPROVED,
            f"Reply-To: {OTHER_PERSONAL}\r\n".encode(),
            None,
            "Personal mail provider in Reply-To",
        ),
        (
            APPROVED,
            f"Sender: {OTHER_PERSONAL}\r\n".encode(),
            None,
            "Personal mail provider in Sender",
        ),
        (APPROVED, b"", {"addresses": [APPROVED]}, "Protected personal address"),
        (APPROVED, b"", {"domains": ["gmail.com"]}, "Protected sender domain"),
        (APPROVED, b"", {"domain_patterns": ["gmail.*"]}, "Protected financial"),
        (APPROVED, b"", {"keywords": ["newsletter"]}, "Protected sender display name"),
    ],
)
def test_allowlist_never_approves_other_personal_addresses_or_protections(
    mailbox, sender, extra_headers, config, reason
):
    service, fake = mailbox(sender=sender, extra_headers=extra_headers, config=config)
    preview = service.preview_action("move_to_trash", query={})
    assert preview["token"] is None
    assert reason in preview["blocked"][0]["reason"]
    assert not mutation_calls(fake)
    assert not execution_log(service.backend.log.path)


def test_preview_blocks_unparseable_from_after_approved_address(mailbox):
    service, fake = mailbox(extra_headers=f"Sender: {APPROVED}\r\n".encode())
    item = fake.folders["INBOX"]["1"]
    # Gluon's ENVELOPE admits this domain; policy IDNA validation then fails
    # after the first address has already recorded its personal-provider bypass.
    item.headers = item.headers.replace(
        f"From: Newsletter <{APPROVED}>\r\n".encode(),
        f"From: {APPROVED}, x@{'a' * 64}.example\r\n".encode(),
    )
    preview = service.preview_action("move_to_trash", query={})
    assert preview["count"] == 1
    assert preview["token"] is None
    assert "Unparseable From sender" in preview["blocked"][0]["reason"]
    assert not mutation_calls(fake)
    assert not execution_log(service.backend.log.path)


@pytest.mark.parametrize("fail_at", [1, 2])
def test_partial_execute_logs_bypasses_for_done_and_uncertain_only(mailbox, monkeypatch, fail_at):
    service, fake = mailbox(count=3)
    # One batched chunk with three source folders sends one MOVE per folder.
    fake.folders["Archive"]["2"] = fake.folders["INBOX"].pop("2")
    fake.folders["Folders/Later"] = {"3": fake.folders["INBOX"].pop("3")}
    preview = service.preview_action(
        "move_to_trash", ids=[f"<allowlist-{index}@example.test>" for index in (1, 2, 3)]
    )
    assert preview["token"] and preview["count"] == 3
    original_uid = fake.uid
    attempted = 0

    def fail(command, *args):
        nonlocal attempted
        if command == "MOVE":
            attempted += 1
            if attempted == fail_at:
                raise CallDeadlineExceeded("synthetic interruption")
        return original_uid(command, *args)

    monkeypatch.setattr(fake, "uid", fail)
    result = service.execute("move_to_trash", preview["token"])
    assert result["partial"] and result["count"] == fail_at - 1
    logged = records(service)[0]
    assert logged["partial"]
    assert [row["status"] for row in logged["messages"]] == (
        ["done"] * (fail_at - 1) + ["uncertain"] + ["not_attempted"] * (3 - fail_at)
    )
    assert [row["personal_provider_bypasses"] for row in logged["messages"]] == (
        [FALLBACK_EVIDENCE] * fail_at + [[]] * (3 - fail_at)
    )
    assert APPROVED.encode() not in service.backend.log.path.read_bytes()


@pytest.mark.parametrize("change", ["remove", "add_unrelated", "remove_unrelated"])
def test_effective_allowlist_change_refuses_existing_token(mailbox, tmp_path, change):
    initial = (
        {"personal_provider_allowlist": [OTHER_PERSONAL]} if change == "remove_unrelated" else None
    )
    service, fake = mailbox(config=initial)
    preview = service.preview_action("move_to_trash", query={})
    assert preview["token"]
    changed = {
        "remove": {"mode": "replace", "personal_domains": ["gmail.com"]},
        "add_unrelated": {"personal_provider_allowlist": [OTHER_PERSONAL]},
        "remove_unrelated": {},
    }[change]
    load_policy(service, tmp_path, changed)
    with pytest.raises(MailValidationError, match="protected|changed"):
        service.execute("move_to_trash", preview["token"])
    assert not mutation_calls(fake)
    assert not execution_log(service.backend.log.path)
    service.tokens.inspect(preview["token"], "move_to_trash")


@pytest.mark.parametrize("change", ["policy", "bypass_entry"])
def test_allowlist_change_after_writable_selection_refuses_before_mutation(
    mailbox, tmp_path, monkeypatch, change
):
    service, fake = mailbox(config={"personal_provider_allowlist": [OTHER_PERSONAL]})
    preview = service.preview_action("move_to_trash", query={})
    original_select = fake.select
    writable_selects = 0

    def change_policy(folder, *, readonly):
        nonlocal writable_selects
        result = original_select(folder, readonly=readonly)
        if not readonly:
            writable_selects += 1
            if writable_selects == 2:
                if change == "policy":
                    load_policy(service, tmp_path, {})
                else:
                    item = fake.folders["INBOX"]["1"]
                    item.headers = item.headers.replace(APPROVED.encode(), OTHER_PERSONAL.encode())
        return result

    monkeypatch.setattr(fake, "select", change_policy)
    result = service.execute("move_to_trash", preview["token"])
    assert result["partial"] and result["count"] == 0
    assert not mutation_calls(fake)
    entry = records(service)[0]["messages"][0]
    assert entry["status"] == "not_attempted" and entry["personal_provider_bypasses"] == []


def test_legacy_record_without_bypass_field_loads_and_can_be_undone(mailbox, tmp_path):
    service, fake = mailbox()
    preview = service.preview_action("move_to_trash", query={})
    result = service.execute("move_to_trash", preview["token"])
    legacy = deepcopy(records(service)[0])
    for row in legacy["messages"]:
        del row["personal_provider_bypasses"]
    legacy_log = ActionLog(tmp_path / "synthetic-legacy-actions.jsonl")
    with legacy_log.transaction() as transaction:
        transaction.append(legacy)
        assert transaction.records() == [legacy]
    before = legacy_log.path.read_bytes()
    service.backend.log = legacy_log
    undo = service.undo_action(result["log_ids"][0])
    assert undo["count"] == 1 and not undo["partial"]
    assert fake.folders["INBOX"] and not fake.folders["Trash"]
    assert legacy_log.path.read_bytes().startswith(before)
    assert records(service)[-1]["messages"][0]["personal_provider_bypasses"] == FALLBACK_EVIDENCE


@pytest.mark.parametrize("count", [200, 201])
def test_allowlist_preserves_batch_cap_and_trash_only_mutations(mailbox, monkeypatch, count):
    # The chunk of 200 flushes a journal line per row before its MOVE, and Windows keeps fsync
    # real: a loaded runner spent the execute budget on the disk and moved nothing. Hold the
    # clock still; which rows move is not a question of time here.
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: 1000.0)
    service, fake = mailbox(count=count)
    preview = service.preview_action("move_to_trash", query={})
    assert preview["count"] == count
    if count == 201:
        assert preview["token"] is None and "200" in preview["reason"]
        assert not mutation_calls(fake)
        return
    assert preview["token"]
    result = service.execute("move_to_trash", preview["token"])
    assert result["count"] == 200 and not result["partial"]
    assert len(fake.folders["Trash"]) == 200 and not fake.folders["INBOX"]
    assert [call[1] for call in mutation_calls(fake)] == ["1:200"]  # One batched MOVE.
    assert all(call[0] == "MOVE" and call[2] == '"Trash"' for call in mutation_calls(fake))
    assert all(
        row["personal_provider_bypasses"] == FALLBACK_EVIDENCE
        for row in records(service)[0]["messages"]
    )


def test_allowlist_cannot_add_permanent_delete_or_override_trash_target(mailbox):
    service, fake = mailbox()
    with pytest.raises(MailValidationError, match="Unsupported cleanup action"):
        service.preview_action("delete_permanently", query={})
    with pytest.raises(MailValidationError, match="params"):
        service.preview_action("move_to_trash", query={}, params={"folder": "Archive"})
    assert fake.calls == []


def test_allowlist_does_not_relax_all_mail_requirement_for_label_removal(mailbox):
    service, fake = mailbox()
    original = fake.folders["INBOX"]["1"]
    fake.folders["Labels/Review"]["20"] = original
    fake.folders["All Mail"].clear()
    before = deepcopy(fake.folders)
    preview = service.preview_action("remove_label", query={}, params={"label": "Review"})
    assert preview["count"] == 0 and preview["token"] is None
    assert preview["expires_at"] is None and not service.tokens._grants
    assert preview["excluded_count"] == 1
    assert preview["excluded_ids"] == ["<allowlist-1@example.test>"]
    assert preview["excluded_reasons"] == {"missing_all_mail_copy": 1}
    assert not mutation_calls(fake)
    assert not service.backend.log.path.exists()
    assert fake.folders == before
    assert fake.folders["Labels/Review"]["20"] is original


def test_disabled_personal_protection_needs_and_records_no_bypass(mailbox):
    service, fake = mailbox(config={"protect_personal_domains": False})
    preview = service.preview_action("move_to_trash", query={})
    assert preview["token"] and preview["personal_provider_bypass_count"] == 0
    assert "personal_provider_bypasses" not in preview["samples"][0]
    result = service.execute("move_to_trash", preview["token"])
    assert result["count"] == 1 and not result["partial"]
    assert records(service)[0]["messages"][0]["personal_provider_bypasses"] == []
