"""Bridge raw identity audits resume safely as synthetic mailbox membership changes."""

from types import SimpleNamespace

import pytest

from proton_mail_mcp import cleanup_lookup
from proton_mail_mcp.mail import CallDeadlineExceeded, MailValidationError


class Deadline:
    expired = False

    def check(self, connection=None):
        if self.expired:
            raise CallDeadlineExceeded("Synthetic preview deadline reached")


def client():
    return SimpleNamespace(
        settings=SimpleNamespace(
            bridge_user="synthetic@example.invalid", imap_host="127.0.0.1", imap_port=1143
        )
    )


def raw_row(uid):
    identifier = f"<synthetic-{uid}@example.invalid>"
    return {
        "uid": uid,
        "message_id": identifier,
        "_raw_identity_fields": [identifier],
        "subject": "not retained in the completed audit",
    }


def audit(owner, peer, deadline=None, *, folder="INBOX", validity="1"):
    return cleanup_lookup._preview_raw_inventory(
        owner, peer, deadline or Deadline(), folder, validity, "membership"
    )


def test_raw_bridge_audit_resumes_survivors_and_fetches_only_new_uids(monkeypatch):
    owner = client()
    peer = SimpleNamespace(capabilities=(b"IMAP4rev1",), _rules_preview_state={})
    deadline = Deadline()
    uids = [str(uid) for uid in range(1, 451)]
    reads = []
    monkeypatch.setattr(cleanup_lookup, "enumerate_uids", lambda *args: list(uids))

    def fetch(*args, **kwargs):
        reads.append(list(args[4]))
        if len(reads) == 1:
            deadline.expired = True
        return [raw_row(uid) for uid in args[4]]

    monkeypatch.setattr(cleanup_lookup, "fetch_rows", fetch)
    with pytest.raises(CallDeadlineExceeded):
        audit(owner, peer, deadline)
    assert peer._rules_preview_state["progress"] == 200
    (snapshot,) = owner._rules_raw_identity_snapshots.values()
    assert len(snapshot["inventory"]["rows"]) == 200

    # One already audited UID and one pending UID disappear; two arrivals need
    # raw fields, while all surviving completed reads remain valid.
    uids = [uid for uid in uids if uid not in {"1", "201"}] + ["451", "452"]
    deadline.expired = False
    rows = audit(owner, peer, deadline)

    assert [row["uid"] for row in rows] == uids
    assert reads[1] == [str(uid) for uid in range(202, 402)]
    assert reads[2] == [str(uid) for uid in range(402, 453)]
    assert peer._rules_preview_state["inventories"]["INBOX"]["uids"] == uids
    assert len(owner._rules_raw_identity_snapshots) == 1
    assert all("subject" not in row for row in rows)


def test_raw_audit_checkpoints_once_after_multiple_batches_hit_deadline(monkeypatch):
    owner = client()
    peer = SimpleNamespace(_rules_preview_state={})
    deadline = Deadline()
    uids = [str(uid) for uid in range(1, 6)]
    reads, checkpoints = [], []
    checksum = cleanup_lookup._preview_inventory_checksum
    monkeypatch.setattr(cleanup_lookup, "_IDENTITY_BATCH_SIZE", 2)
    monkeypatch.setattr(cleanup_lookup, "enumerate_uids", lambda *args: list(uids))

    def fetch(*args, **kwargs):
        reads.append(list(args[4]))
        if len(reads) == 2:
            deadline.expired = True
        return [raw_row(uid) for uid in args[4]]

    def checkpoint(saved):
        checkpoints.append([row["uid"] for row in saved["rows"]])
        return checksum(saved)

    monkeypatch.setattr(cleanup_lookup, "fetch_rows", fetch)
    monkeypatch.setattr(cleanup_lookup, "_preview_inventory_checksum", checkpoint)
    with pytest.raises(CallDeadlineExceeded):
        audit(owner, peer, deadline)

    assert reads == [uids[:2], uids[2:4]]
    assert checkpoints == [uids[:4]]
    assert peer._rules_preview_state["progress"] == 4
    saved = peer._rules_preview_state["inventories"]["INBOX"]
    (snapshot,) = owner._rules_raw_identity_snapshots.values()
    assert snapshot["inventory"] == saved
    assert [row["uid"] for row in saved["rows"]] == uids[:4]
    assert saved["checksum"] == checksum(saved)


def test_completed_raw_audit_is_reused_and_reconciled_across_fresh_previews(monkeypatch):
    owner = client()
    peer = SimpleNamespace(capabilities=(b"IMAP4rev1",), _rules_preview_state={})
    uids = ["1", "2"]
    reads = []
    monkeypatch.setattr(cleanup_lookup, "enumerate_uids", lambda *args: list(uids))

    def fetch(*args, **kwargs):
        reads.append(list(args[4]))
        return [raw_row(uid) for uid in args[4]]

    monkeypatch.setattr(cleanup_lookup, "fetch_rows", fetch)
    audit(owner, peer)
    peer._rules_preview_state = {}
    assert [row["uid"] for row in audit(owner, peer)] == ["1", "2"]
    assert reads == [["1", "2"]]

    uids = ["2", "3"]
    peer._rules_preview_state = {}
    assert [row["uid"] for row in audit(owner, peer)] == uids
    assert reads == [["1", "2"], ["3"]]

    # An identity reset refuses an active preview, including an empty mailbox.
    uids = []
    with pytest.raises(MailValidationError, match="Rules preview mailbox changed"):
        audit(owner, peer, validity="2")
    peer._rules_preview_state = {}
    assert audit(owner, peer, validity="2") == []
    assert reads == [["1", "2"], ["3"]]


@pytest.mark.parametrize("scope", ["client", "account", "endpoint", "folder"])
def test_raw_audit_cache_does_not_cross_client_or_mailbox_scope(monkeypatch, scope):
    owner = client()
    peer = SimpleNamespace(_rules_preview_state={})
    reads = []
    monkeypatch.setattr(cleanup_lookup, "enumerate_uids", lambda *args: ["1"])

    def fetch(*args, **kwargs):
        reads.append(list(args[4]))
        return [raw_row(uid) for uid in args[4]]

    monkeypatch.setattr(cleanup_lookup, "fetch_rows", fetch)
    audit(owner, peer)
    if scope == "client":
        owner = client()
    elif scope == "account":
        owner.settings.bridge_user = "other@example.invalid"
    elif scope == "endpoint":
        owner.settings.imap_host = "127.0.0.2"
    peer._rules_preview_state = {}
    audit(owner, peer, folder="Archive" if scope == "folder" else "INBOX")
    assert reads == [["1"], ["1"]]


def test_raw_audit_completed_cache_expires_and_is_bounded(monkeypatch):
    owner = client()
    peer = SimpleNamespace(_rules_preview_state={})
    now = [0]
    reads = []
    monkeypatch.setattr(cleanup_lookup.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(cleanup_lookup, "_PREVIEW_RAW_MAX_SNAPSHOTS", 2)
    monkeypatch.setattr(cleanup_lookup, "enumerate_uids", lambda *args: ["1"])

    def fetch(*args, **kwargs):
        reads.append(args[3])
        return [raw_row(uid) for uid in args[4]]

    monkeypatch.setattr(cleanup_lookup, "fetch_rows", fetch)
    for folder in ["INBOX", "Archive", "All Mail"]:
        peer._rules_preview_state = {}
        audit(owner, peer, folder=folder)
    assert len(owner._rules_raw_identity_snapshots) == 2
    now[0] = cleanup_lookup._PREVIEW_RAW_SNAPSHOT_SECONDS + 1
    peer._rules_preview_state = {}
    audit(owner, peer, folder="All Mail")
    assert reads == ["INBOX", "Archive", "All Mail", "All Mail"]
    assert len(owner._rules_raw_identity_snapshots) == 1


@pytest.mark.parametrize("scale, expected_size", [(1, 200), (2, 100), (256, 1)])
def test_raw_audit_reduces_batch_size_and_saves_progress_before_deadline(
    monkeypatch, scale, expected_size
):
    owner = client()
    peer = SimpleNamespace(_rules_preview_state={"batch_scale": scale})
    deadline = Deadline()
    reads = []
    monkeypatch.setattr(
        cleanup_lookup, "enumerate_uids", lambda *args: [str(uid) for uid in range(1, 301)]
    )

    def fetch(*args, **kwargs):
        reads.append(list(args[4]))
        deadline.expired = True
        return [raw_row(uid) for uid in args[4]]

    monkeypatch.setattr(cleanup_lookup, "fetch_rows", fetch)
    with pytest.raises(CallDeadlineExceeded):
        audit(owner, peer, deadline)
    assert len(reads[0]) == expected_size
    assert peer._rules_preview_state["progress"] == expected_size
    assert len(peer._rules_preview_state["inventories"]["INBOX"]["rows"]) == expected_size


@pytest.mark.parametrize("validity", ["1", "2"])
def test_empty_bridge_membership_reconciles_or_refuses_changed_uidvalidity(monkeypatch, validity):
    saved = {
        "validity": "1",
        "uids": ["1"],
        "rows": [],
        "snapshot": {"folder": "INBOX", "exists": 1, "uidnext": "2"},
        "options": None,
    }
    saved["checksum"] = cleanup_lookup._preview_inventory_checksum(saved)
    peer = SimpleNamespace(
        capabilities=(b"IMAP4rev1",),
        _rules_preview_state={"inventories": {"INBOX": saved}},
        _cleanup_raw_identity=True,
        _cleanup_inventory={},
    )
    owner = client()

    def select(connection, folder):
        connection._selected_inventory = {"folder": folder, "exists": 0, "uidnext": "2"}
        return connection._selected_inventory

    owner._select = select
    monkeypatch.setattr(cleanup_lookup, "_uidvalidity", lambda connection: validity)
    monkeypatch.setattr(
        cleanup_lookup,
        "indexed_identity_uids",
        lambda *args, **kwargs: pytest.fail("Empty selected mailbox consulted another snapshot"),
    )
    if validity == "2":
        with pytest.raises(MailValidationError, match="Rules preview mailbox changed"):
            cleanup_lookup.folder_inventory(
                owner, peer, Deadline(), "INBOX", identifiers=["<synthetic-1@example.invalid>"]
            )
    else:
        assert not cleanup_lookup.folder_inventory(
            owner, peer, Deadline(), "INBOX", identifiers=["<synthetic-1@example.invalid>"]
        )
        assert saved["rows"] == saved["uids"] == []
