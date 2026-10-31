"""Cold singleton reviews reuse current-call audit reads without stale flags."""

from types import SimpleNamespace

import pytest
from test_identity_index_integration import indexed_mailbox as indexed_mailbox
from test_mail import FakeImap, client, message

from proton_mail_mcp.cleanup_lookup import _preview_raw_inventory, folder_inventory
from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import CallDeadlineExceeded, _Deadline


def test_raw_audit_avoids_duplicate_candidate_fetch_and_next_call_refreshes_flags():
    item = message("<audit@example.test>", flags=set())
    connection = FakeImap({"INBOX": {"1": item}})
    settings = Settings(
        token="offline-audit-cost-token-" + "x" * 32,
        bridge_user="reader@example.test",
    )
    backend = client(settings, connection)
    connection._cleanup_raw_identity = True
    connection._cleanup_seen = True
    connection._rules_preview_state = {}

    def review():
        # Each real preview call starts with a fresh connection cache, retaining
        # only immutable identity evidence in its resumable chain state.
        connection._cleanup_inventory = {}
        return folder_inventory(
            backend,
            connection,
            backend._deadline(),
            "INBOX",
            identifiers=["<audit@example.test>"],
            stage="membership",
        )

    first = review()
    assert len(first) == 1 and first[0]["seen"] is False
    first_fetches = [call for call in connection.calls if call[0] == "FETCH"]
    # The raw audit already includes the full current-call candidate evidence.
    assert len(first_fetches) == 1
    assert "FLAGS" in first_fetches[0][2]
    saved = connection._rules_preview_state["inventories"]["INBOX"]["rows"][0]
    assert set(saved) == {"uid", "message_id", "_raw_identity_fields", "identity_invalid"}

    item.flags = {r"\Seen"}
    connection.calls.clear()
    second = review()
    assert len(second) == 1 and second[0]["seen"] is True
    # Warm immutable identities still require fresh sender/fingerprint/flags.
    assert len([call for call in connection.calls if call[0] == "FETCH"]) == 1


def test_singleton_raw_audit_keeps_response_reserve_and_completed_checkpoint(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: now[0])
    deadline = _Deadline(20, 20)
    deadline.rules_response_reserve = deadline.response_reserve
    deadline.response_reserve = 0
    connection = SimpleNamespace(_rules_preview_state={}, _cleanup_inventory={})
    monkeypatch.setattr("proton_mail_mcp.cleanup_lookup.enumerate_uids", lambda *args: ["1"])

    def fetched(*args, **kwargs):
        assert deadline.response_reserve == 2
        now[0] = 18.5
        return [{"uid": "1", "message_id": "<audit@example.test>", "_raw_identity_fields": []}]

    monkeypatch.setattr("proton_mail_mcp.cleanup_lookup.fetch_rows", fetched)
    now[0] = 17.5
    with pytest.raises(CallDeadlineExceeded):
        _preview_raw_inventory(SimpleNamespace(), connection, deadline, "INBOX", "1", "membership")
    assert deadline.response_reserve == 0
    saved = connection._rules_preview_state["inventories"]["INBOX"]
    assert [row["uid"] for row in saved["rows"]] == ["1"]
    assert connection._rules_preview_state["progress"] == 1


def test_certified_raw_audit_avoids_duplicate_candidate_fetch(indexed_mailbox):
    sim, backend, _, _, _ = indexed_mailbox(count=1, filler=0)
    state = {}
    with backend._session() as (connection, _):
        object.__setattr__(connection, "_rules_preview_state", state)
        object.__setattr__(connection, "_cleanup_inventory", {})
        connection._cleanup_raw_identity = True
        connection._cleanup_seen = True
        rows = folder_inventory(
            backend,
            connection,
            backend._deadline(),
            "INBOX",
            identifiers=["<index-wanted-0@example.invalid>"],
            stage="membership",
        )
    assert len(rows) == 1 and rows[0]["seen"] is False
    fetches = [command for _, folder, _, command in sim.commands if folder == "INBOX"]
    assert len([command for command in fetches if command.startswith("UID FETCH")]) == 1
    saved = next(iter(state["certified_inventories"].values()))["rows"]["1"]
    assert set(saved) == {"uid", "message_id", "_raw_identity_fields", "identity_invalid"}
