"""Guarded changes and reversals use only in-memory command-recording IMAP fakes."""

import json
import os
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_mail import FakeImap, client, message, unquote

from proton_mail_mcp.action_log import ActionLog, replay_records
from proton_mail_mcp.cleanup_imap import CleanupMutator, _copyuid
from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import CallDeadlineExceeded, MailError, MailValidationError
from proton_mail_mcp.protected import ProtectedPolicy

MUTATIONS = {"MOVE", "COPY", "STORE", "EXPUNGE"}


def uid_values(spec):
    """Expand an explicit IMAP UID set ("7", "7,9" or "7:9") in ascending order."""
    if isinstance(spec, bytes):
        spec = spec.decode("ascii")
    values = []
    for part in spec.split(","):
        first, _, last = part.partition(":")
        assert first.isdigit() and (not last or last.isdigit()), "UID sets never use *"
        low, high = sorted((int(first), int(last or first)))
        values.extend(str(uid) for uid in range(low, high + 1))
    return values


class MutatingImap(FakeImap):
    capabilities = (b"IMAP4rev1", b"MOVE", b"UIDPLUS")

    def __init__(self, folders=None, *, strip_labels_on_trash=False):
        super().__init__(folders)
        self.copyuid = None
        self.deleted = set()
        self.strip_labels_on_trash = strip_labels_on_trash

    def select(self, folder, *, readonly):
        self.current = unquote(folder)
        self.calls.append(("EXAMINE" if readonly else "SELECT", folder, readonly))
        return "OK", [str(len(self.folders[self.current])).encode()]

    def response(self, name):
        if name == "COPYUID":
            result, self.copyuid = self.copyuid, None
            return name, [result]
        return super().response(name)

    def uid(self, command, *args):
        if command not in MUTATIONS:
            return super().uid(command, *args)
        self.calls.append((command, *args))
        # Each mutation names explicit token UIDs: one UID or an exact UID set
        # (batched STORE/MOVE), never "*" and never more than one 200-UID chunk.
        uids = uid_values(args[0])
        assert 1 <= len(uids) <= 200, "Mutations must address exact token UIDs"
        if command in {"MOVE", "COPY"}:
            target = unquote(args[1])
            moved, assigned = [], []
            for uid in uids:
                # UID commands silently skip UIDs that no longer exist.
                item = self.folders[self.current].get(uid)
                if item is None:
                    continue
                after = str(max(map(int, self.folders[target]), default=100) + 1)
                self.folders[target][after] = item
                if command == "MOVE":
                    del self.folders[self.current][uid]
                if target == "Trash" and self.strip_labels_on_trash:
                    for name, messages in self.folders.items():
                        if name.startswith("Labels/"):
                            for existing_uid, existing in list(messages.items()):
                                if existing is item:
                                    del messages[existing_uid]
                moved.append(uid)
                assigned.append(after)
            self.copyuid = (
                f"{self.uidvalidity} {','.join(moved)} {','.join(assigned)}".encode()
                if moved
                else None
            )
            return "OK", [b"operation completed"]
        assert self.current.startswith("Labels/"), "Flags/expunge forbidden in real folders"
        for uid in uids:
            if command == "STORE":
                assert args[1] in {"+FLAGS.SILENT", "-FLAGS.SILENT"}
                assert args[2] == r"(\Deleted)"
                if args[1].startswith("+"):
                    self.deleted.add((self.current, uid))
                else:
                    self.deleted.discard((self.current, uid))
            else:
                assert (self.current, uid) in self.deleted
                del self.folders[self.current][uid]
                self.deleted.remove((self.current, uid))
        return "OK", []


@pytest.fixture
def setup(tmp_path):
    item = message("<cleanup@example.test>")
    fake = MutatingImap(
        {
            "INBOX": {"1": item},
            "Trash": {},
            "Archive": {},
            "Folders/Review": {},
            "Labels/Keep": {},
            "All Mail": {"10": item},
            "Drafts": {},
        }
    )
    settings = Settings(
        token="test-token-at-least-thirty-two-characters",
        bridge_user="test@example.test",
        action_log_path=str(tmp_path / "actions.jsonl"),
    )
    mailbox = client(settings, fake)
    return mailbox, fake, CleanupMutator(mailbox, settings, ProtectedPolicy())


def snapshots(mailbox, mutator, fake, folder="INBOX"):
    with mailbox._session() as (connection, _):
        rows = mutator._fetch(connection, mailbox._deadline(), folder, list(fake.folders[folder]))
    for row in rows:
        row["personal_provider_allowlist_digest"] = mutator.policy.allowlist_digest()
        _, row["personal_provider_bypasses"] = mutator.policy.evaluate(row)
        row["folders"] = [folder, "All Mail"]
        row["memberships"] = [
            {key: row[key] for key in ("folder", "uidvalidity", "uid")},
            {"folder": "All Mail", "uidvalidity": "1", "uid": "10"},
        ]
    return rows


def apply(setup, action, params=None, rows=None):
    mailbox, fake, mutator = setup
    if rows is None:
        folder = "Labels/Keep" if action == "remove_label" else "INBOX"
        rows = snapshots(mailbox, mutator, fake, folder)
    with mailbox._session() as (connection, _):
        return mutator.apply(connection, mailbox._deadline(), action, params or {}, rows)


def mutation_calls(fake):
    return [call for call in fake.calls if call[0] in MUTATIONS]


@pytest.mark.parametrize(
    "missing",
    [
        ("personal_provider_allowlist_digest",),
        ("personal_provider_bypasses",),
        ("personal_provider_allowlist_digest", "personal_provider_bypasses"),
    ],
)
def test_execute_requires_allowlist_binding_before_claiming_or_mutating(setup, missing):
    mailbox, fake, mutator = setup
    rows = snapshots(mailbox, mutator, fake)
    for field in missing:
        del rows[0][field]
    claims = []
    with mailbox._session() as (connection, _):
        result = mutator.apply(
            connection,
            mailbox._deadline(),
            "move_to_trash",
            {},
            rows,
            before_mutation=lambda: claims.append(True),
        )
    assert result["partial"] is True
    assert result["count"] == 0
    assert result["messages"][0]["status"] == "not_attempted"
    assert result["messages"][0]["personal_provider_bypasses"] == []
    assert not claims
    assert not mutation_calls(fake)
    assert fake.folders["INBOX"] and not fake.folders["Trash"]


@pytest.mark.parametrize(
    "action,params,commands,target",
    [
        ("move_to_trash", {}, ["MOVE"], "Trash"),
        ("archive", {}, ["MOVE"], "Archive"),
        ("move_to_folder", {"folder": "Folders/Review"}, ["MOVE"], "Folders/Review"),
        ("add_label", {"label": "Keep"}, ["COPY"], "Labels/Keep"),
        ("remove_label", {"label": "Keep"}, ["STORE", "EXPUNGE"], "All Mail"),
    ],
)
def test_mutating_path_only_issues_allowed_scoped_commands(setup, action, params, commands, target):
    mailbox, fake, mutator = setup
    if action == "remove_label":
        fake.folders["Labels/Keep"]["20"] = fake.folders["INBOX"]["1"]
    result = apply(setup, action, params)
    assert result["partial"] is False
    assert result["count"] == 1
    assert [call[0] for call in mutation_calls(fake)] == commands
    record = result["messages"][0]
    assert record["destination_folder"] == target
    assert record["uid_after"] is not None
    assert record["memberships_before"]
    assert fake.folders["All Mail"]
    if action == "remove_label":
        assert not fake.folders["Labels/Keep"]
        assert fake.folders["INBOX"]
    else:
        assert fake.folders[target]
    undo = mutator.undo_action(result["log_id"])
    assert undo["partial"] is False
    assert undo["count"] == 1
    assert fake.folders["INBOX"]
    if action == "add_label":
        assert not fake.folders["Labels/Keep"]
    if action == "remove_label":
        assert fake.folders["Labels/Keep"]


@pytest.mark.parametrize(
    "action,params",
    [
        ("move_to_folder", {"folder": "Trash"}),
        ("move_to_folder", {"folder": "Spam"}),
        ("move_to_folder", {"folder": "Sent"}),
        ("move_to_folder", {"folder": "Drafts"}),
        ("move_to_folder", {"folder": "All Mail"}),
        ("move_to_folder", {"folder": "Folders/Missing"}),
        ("add_label", {"label": "Missing"}),
        ("add_label", {"label": "../Trash"}),
        ("move_to_trash", {"folder": "Archive"}),
    ],
)
def test_targets_must_exist_and_special_folder_moves_need_dedicated_tool(setup, action, params):
    with pytest.raises(MailValidationError):
        apply(setup, action, params)
    assert not mutation_calls(setup[1])


def test_missing_move_capability_refuses_without_copy_delete_fallback(setup):
    setup[1].capabilities = (b"IMAP4rev1", b"UIDPLUS")
    with pytest.raises(MailValidationError, match="Server lacks MOVE capability"):
        apply(setup, "move_to_trash")
    assert not mutation_calls(setup[1])


def test_label_removal_requires_uidplus_and_all_mail_copy(setup):
    mailbox, fake, mutator = setup
    fake.folders["Labels/Keep"]["20"] = fake.folders["INBOX"]["1"]
    fake.capabilities = (b"IMAP4rev1", b"MOVE")
    with pytest.raises(MailValidationError, match="UIDPLUS"):
        apply(setup, "remove_label", {"label": "Keep"})
    fake.capabilities = MutatingImap.capabilities
    fake.folders["All Mail"].clear()
    with pytest.raises(MailValidationError, match="All Mail"):
        apply(setup, "remove_label", {"label": "Keep"})
    assert not mutation_calls(fake)


def test_remove_label_cannot_use_real_folder_source(setup):
    mailbox, fake, mutator = setup
    rows = snapshots(mailbox, mutator, fake)
    with pytest.raises(MailValidationError, match="exactly its label"):
        apply(setup, "remove_label", {"label": "Keep"}, rows)
    assert not mutation_calls(fake)


def test_execution_defensively_checks_cap_identity_and_current_protection(setup):
    mailbox, fake, mutator = setup
    rows = snapshots(mailbox, mutator, fake)
    with pytest.raises(MailValidationError, match="200"):
        apply(setup, "archive", rows=rows * 201)
    changed = deepcopy(rows)
    changed[0]["message_id"] = "<changed@example.test>"
    with pytest.raises(MailValidationError, match="Message set changed"):
        apply(setup, "archive", rows=changed)
    fake.folders["INBOX"]["1"].headers = fake.folders["INBOX"]["1"].headers.replace(
        b"sender@example.com", b"person@gmail.com"
    )
    with pytest.raises(MailValidationError, match="Protected"):
        apply(setup, "archive", rows=rows)
    assert not mutation_calls(fake)


def test_readwrite_select_uidvalidity_reset_refuses_before_any_mutation(setup, monkeypatch):
    mailbox, fake, mutator = setup
    rows = snapshots(mailbox, mutator, fake)
    original = fake.select

    def reset(folder, *, readonly):
        if not readonly:
            fake.uidvalidity = "99"
        return original(folder, readonly=readonly)

    monkeypatch.setattr(fake, "select", reset)
    with pytest.raises(MailValidationError, match="UIDVALIDITY"):
        apply(setup, "archive", rows=rows)
    assert not mutation_calls(fake)


def test_deadline_after_first_move_logs_done_and_uncertain(setup, monkeypatch):
    # A batched chunk sends one MOVE per source folder: a lost second
    # acknowledgement leaves the first folder's UID done and the next uncertain.
    mailbox, fake, mutator = setup
    fake.folders["Folders/Review"]["2"] = message("<second@example.test>")
    rows = snapshots(mailbox, mutator, fake) + snapshots(mailbox, mutator, fake, "Folders/Review")
    original = fake.uid
    moved = 0

    def expire(command, *args):
        nonlocal moved
        if command == "MOVE":
            moved += 1
            if moved == 2:
                raise CallDeadlineExceeded("synthetic deadline")
        return original(command, *args)

    monkeypatch.setattr(fake, "uid", expire)
    result = apply(setup, "archive", rows=rows)
    assert result["partial"] is True
    assert [row["status"] for row in result["messages"]] == ["done", "uncertain"]
    assert [call[1] for call in mutation_calls(fake)] == ["1"]
    lines = mutator.log.path.read_text().splitlines()
    records = replay_records([json.loads(line) for line in lines])
    assert len(records) == 1
    assert records[0]["partial"] is True
    assert json.loads(lines[0])["status"] == "in_progress"


def test_label_flag_partial_undo_clears_only_label_deleted_flag(setup, monkeypatch):
    mailbox, fake, mutator = setup
    fake.folders["Labels/Keep"]["20"] = fake.folders["INBOX"]["1"]
    original = fake.uid

    def expire(command, *args):
        if command == "EXPUNGE":
            raise CallDeadlineExceeded("synthetic deadline")
        return original(command, *args)

    monkeypatch.setattr(fake, "uid", expire)
    result = apply(setup, "remove_label", {"label": "Keep"})
    assert result["partial"] is True
    assert ("Labels/Keep", "20") in fake.deleted
    monkeypatch.setattr(fake, "uid", original)
    undo = mutator.undo_action(result["log_id"])
    assert undo["partial"] is False
    assert not fake.deleted
    assert fake.folders["All Mail"]
    assert fake.folders["INBOX"]


def test_undo_missing_messages_are_reported_and_held_for_inspection(setup):
    mailbox, fake, mutator = setup
    result = apply(setup, "archive")
    fake.folders["Archive"].clear()
    undo = mutator.undo_action(result["log_id"])
    assert undo["count"] == 0
    assert undo["partial"] is True
    assert undo["missing"] == [
        {
            "message_id": "<cleanup@example.test>",
            "folder": "Archive",
            "reason": 'not found during undo in "Archive"',
        }
    ]
    with pytest.raises(MailValidationError, match="unresolved"):
        mutator.undo_action(result["log_id"])
    records = replay_records(
        [json.loads(line) for line in mutator.log.path.read_text().splitlines()]
    )
    assert [record["action"] for record in records] == ["archive", "undo_claim", "undo_action"]


def test_undo_protected_sender_refuses_entire_batch(setup):
    mailbox, fake, mutator = setup
    result = apply(setup, "archive")
    moved = next(iter(fake.folders["Archive"].values()))
    moved.headers = moved.headers.replace(b"sender@example.com", b"person@gmail.com")
    before = mutation_calls(fake)
    with pytest.raises(MailValidationError, match="Protected"):
        mutator.undo_action(result["log_id"])
    assert mutation_calls(fake) == before


def test_undo_batch_cap_and_durable_claim_prevent_replay(setup):
    mailbox, fake, mutator = setup
    result = apply(setup, "archive")
    with mutator.log.transaction() as transaction:
        transaction.append({"log_id": "a" * 32, "action": "archive", "messages": [{}] * 201})
    with pytest.raises(MailValidationError, match="200"):
        mutator.undo_action("a" * 32)
    with mutator.log.transaction() as transaction:
        transaction.append({"action": "undo_claim", "undo_of": result["log_id"]})
    with pytest.raises(MailValidationError, match="unresolved"):
        mutator.undo_action(result["log_id"])


def test_undo_move_restores_labels_stripped_by_trash(setup):
    mailbox, fake, mutator = setup
    fake.strip_labels_on_trash = True
    fake.folders["Labels/Keep"]["20"] = fake.folders["INBOX"]["1"]
    rows = snapshots(mailbox, mutator, fake)
    rows[0]["folders"].append("Labels/Keep")
    result = apply(setup, "move_to_trash", rows=rows)
    assert not fake.folders["Labels/Keep"]
    undo = mutator.undo_action(result["log_id"])
    assert undo["partial"] is False
    assert fake.folders["INBOX"]
    assert fake.folders["Labels/Keep"]
    assert undo["messages"][0]["restored_labels"][0]["status"] == "done"


def test_action_log_is_append_only_private_and_never_stores_token(setup, monkeypatch):
    mailbox, fake, mutator = setup
    calls = []
    original = os.open

    def track(path, flags, mode=0o777, **kwargs):
        if path == mutator.log.path:
            calls.append(flags)
        return original(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", track)
    first = apply(setup, "archive")
    before = mutator.log.path.read_bytes()
    mutator.undo_action(first["log_id"])
    after = mutator.log.path.read_bytes()
    assert after.startswith(before)
    assert calls and all(flags & os.O_APPEND and not flags & os.O_TRUNC for flags in calls)
    assert mutator.log.path.stat().st_mode & 0o777 == 0o600
    assert mailbox.settings.token.encode() not in after
    assert len(replay_records([json.loads(line) for line in after.splitlines()])) == 3


def test_action_log_unavailable_or_corrupt_prevents_mail_changes(setup, tmp_path):
    mailbox, fake, mutator = setup
    mutator.log.path.write_text("invalid record\n")
    with pytest.raises(MailError, match="invalid"):
        apply(setup, "archive")
    mutator.log = ActionLog(tmp_path)
    with pytest.raises(MailError, match="unavailable"):
        apply(setup, "archive")
    assert not mutation_calls(fake)


def test_action_log_refuses_symlink_and_busy_transaction(tmp_path):
    target = tmp_path / "other"
    target.write_text("preserve")
    symlink = tmp_path / "link"
    symlink.symlink_to(target)
    with pytest.raises(MailError):
        with ActionLog(symlink).transaction():
            pass
    assert target.read_text() == "preserve"
    log = ActionLog(tmp_path / "private")
    with log.transaction():
        with pytest.raises(MailError, match="busy"):
            with log.transaction():
                pass


def test_action_log_permission_is_restricted_without_rewriting_existing_content(tmp_path):
    path = tmp_path / "actions"
    path.write_text('{"previous":true}\n')
    path.chmod(0o644)
    with ActionLog(path).transaction() as transaction:
        assert transaction.records() == [{"previous": True}]
        transaction.append({"next": True})
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text().startswith('{"previous":true}\n')


@pytest.mark.parametrize("action", ["archive", "move_to_trash"])
def test_undo_without_copyuid_requires_unique_trash_match(setup, monkeypatch, action):
    mailbox, fake, mutator = setup
    original = fake.response

    def without_copyuid(name):
        return (name, [None]) if name == "COPYUID" else original(name)

    monkeypatch.setattr(fake, "response", without_copyuid)
    # Nor is the moved copy confirmed in its destination: the move stays
    # uncertain without a destination UID, so undo needs a unique Trash match.
    monkeypatch.setattr(mutator, "_pin_destinations", lambda *args: None)
    result = apply(setup, action)
    assert result["messages"][0]["status"] == "uncertain"
    assert result["messages"][0]["uid_after"] is None
    before = [call for call in fake.calls if call[0] in MUTATIONS]
    undo = mutator.undo_action(result["log_id"])
    if action == "move_to_trash":
        assert undo["count"] == 1 and not undo["partial"]
        assert fake.folders["INBOX"] and not fake.folders["Trash"]
    else:
        assert undo["count"] == 0 and undo["partial"]
        assert "outside the required Trash destination" in undo["missing"][0]["reason"]
        assert "manual handling required" in undo["missing"][0]["reason"]
        assert [call for call in fake.calls if call[0] in MUTATIONS] == before
        assert not fake.folders["INBOX"] and fake.folders["Archive"]


@pytest.mark.parametrize(
    "header",
    [
        b"Sender: alerts@secure.broker.example.org\r\n",
        b"Reply-To: tax@steueramt.example.org\r\n",
        b"Sender: first@newsletter.test, second@newsletter.test\r\n",
        b"Reply-To: broken-address\r\n",
    ],
)
def test_undo_rechecks_optional_and_multiple_sender_protection(setup, header):
    mailbox, fake, mutator = setup
    result = apply(setup, "archive")
    moved = next(iter(fake.folders["Archive"].values()))
    moved.headers = header + moved.headers
    before = mutation_calls(fake)
    with pytest.raises(MailValidationError, match="Protected"):
        mutator.undo_action(result["log_id"])
    assert mutation_calls(fake) == before


def test_label_scoped_expunge_never_removes_unrelated_deleted_uid(setup):
    mailbox, fake, mutator = setup
    fake.folders["Labels/Keep"] = {
        "20": fake.folders["INBOX"]["1"],
        "21": message("<unrelated@example.test>"),
    }
    fake.deleted.add(("Labels/Keep", "21"))
    rows = snapshots(mailbox, mutator, fake, "Labels/Keep")[:1]
    result = apply(setup, "remove_label", {"label": "Keep"}, rows)
    assert result["partial"] is False
    assert set(fake.folders["Labels/Keep"]) == {"21"}
    assert ("Labels/Keep", "21") in fake.deleted
    assert [call for call in fake.calls if call[0] == "EXPUNGE"] == [("EXPUNGE", "20")]


def test_action_log_open_denied_prevents_all_mutations(setup, monkeypatch):
    mailbox, fake, mutator = setup

    def denied(*args, **kwargs):
        raise PermissionError("synthetic private diagnostic")

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(MailError, match="unavailable"):
        apply(setup, "archive")
    assert not mutation_calls(fake)


def test_missing_from_after_writable_select_cannot_reuse_preview_sender(setup, monkeypatch):
    mailbox, fake, mutator = setup
    rows = snapshots(mailbox, mutator, fake)
    original = fake.select

    def remove_sender(folder, *, readonly):
        result = original(folder, readonly=readonly)
        if not readonly:
            item = fake.folders["INBOX"]["1"]
            item.headers = item.headers.replace(b"From: Sender <sender@example.com>\r\n", b"")
        return result

    monkeypatch.setattr(fake, "select", remove_sender)
    result = apply(setup, "archive", rows=rows)
    assert result["partial"] is True
    assert result["count"] == 0
    assert not mutation_calls(fake)
    assert fake.folders["INBOX"] and not fake.folders["Archive"]


def test_undo_rejects_message_id_header_normalized_from_malformed_value(setup):
    mailbox, fake, mutator = setup
    result = apply(setup, "archive")
    moved = next(iter(fake.folders["Archive"].values()))
    # Headerregistry adds a missing closing bracket to its string value. This
    # must remain blocked even though the normalized ID matches the log entry.
    moved.headers = moved.headers.replace(
        b"Message-ID: <cleanup@example.test>\r\n", b"Message-ID: <cleanup@example.test\r\n"
    )
    before = mutation_calls(fake)
    with pytest.raises(MailValidationError, match="Protected"):
        mutator.undo_action(result["log_id"])
    assert mutation_calls(fake) == before


@pytest.mark.parametrize(
    "mapping,expected",
    [
        (b"7 42:42 81:81", ("7", "81")),
        (b"7 42 81:81", ("7", "81")),
        (b"7 42:42 81", ("7", "81")),
        (b"7 43:43 81:81", (None, None)),
        (b"7 42:43 81:82", (None, None)),
        (b"7 42,43 81,82", (None, None)),
        (b"7 42 81:82", (None, None)),
    ],
)
@pytest.mark.parametrize("untagged", [False, True])
def test_copyuid_accepts_singleton_ranges_and_ignores_ambiguous_mappings(
    mapping, expected, untagged
):
    connection = SimpleNamespace(response=lambda _: ("COPYUID", [mapping if untagged else None]))
    response = [] if untagged else [b"[COPYUID " + mapping + b"] operation completed"]
    assert _copyuid(connection, response, "42") == expected


@pytest.mark.parametrize("change", ["missing", "protected", "identity", "validity"])
def test_remove_label_refreshes_cached_all_mail_recovery_before_mutation(setup, change):
    from proton_mail_mcp.cleanup_lookup import folder_inventory

    mailbox, fake, mutator = setup
    fake.folders["Labels/Keep"]["20"] = fake.folders["INBOX"]["1"]
    rows = snapshots(mailbox, mutator, fake, "Labels/Keep")
    deadline = mailbox._deadline()
    with mailbox._session(deadline=deadline) as (connection, _):
        folder_inventory(mailbox, connection, deadline, "All Mail")
        if change == "missing":
            fake.folders["All Mail"].clear()
        elif change == "protected":
            changed = deepcopy(fake.folders["All Mail"]["10"])
            changed.headers = changed.headers.replace(b"sender@example.com", b"person@gmail.com")
            fake.folders["All Mail"]["10"] = changed
        elif change == "identity":
            fake.folders["All Mail"]["10"] = message("<different@example.invalid>")
        else:
            fake.uidvalidity = "2"
        with pytest.raises(MailValidationError):
            mutator._preflight(connection, deadline, "remove_label", "Labels/Keep", rows)
    assert not mutation_calls(fake)
