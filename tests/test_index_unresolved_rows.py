"""Indexed discovery preserves unresolved rows and every later refusal boundary."""

from dataclasses import replace

import pytest
from bridge_sim import mutations
from test_identity_index_integration import (
    assert_no_mutations_or_header_search,
)
from test_identity_index_integration import (
    indexed_mailbox as indexed_mailbox,
)

from proton_mail_mcp.mail import MailValidationError

REAL_FOLDERS = {"INBOX", "Archive"}


def duplicate_fault(item):
    item.headers += f"X-Pad-Data: {item.message_id()}\r\n".encode()
    original = item.headers
    changed = original.replace(b"X-Pad-Data:", b"Message-ID:")
    assert len(original) == len(changed)
    return changed


def configure_index(client, enabled):
    client.settings = replace(client.settings, identity_index_enabled=enabled)


def raw_rules_rows(client, identifiers, folder="INBOX"):
    with client._session() as (connection, _):
        connection._cleanup_raw_identity = True
        return client.cleanup._folder_rows(
            connection, client._deadline(), folder, identifiers, stage="source"
        )


def test_rules_discovery_keeps_good_rows_and_one_unresolved_on_both_paths(indexed_mailbox):
    results = []
    for enabled in (False, True):
        sim, client, service, _, selected = indexed_mailbox(count=3, filler=2)
        configure_index(client, enabled)
        selected[-1].headers = duplicate_fault(selected[-1])
        identifiers = [item.message_id() for item in selected]
        # Exercise both a newly built inventory and its persisted warm reuse.
        for _ in range(2):
            rows = raw_rules_rows(client, identifiers)
            blocked = client.cleanup._blocked(rows, REAL_FOLDERS)
            assert [row["message_id"] for row in rows] == identifiers
            assert [bool(row.get("identity_invalid")) for row in rows] == [False, False, True]
            assert len(blocked) == 1 and blocked[0]["unresolved"] is True
            assert blocked[0]["message_id"] == identifiers[-1]
            results.append((rows, blocked))
        # Rules still refuse the complete batch when protected membership cannot
        # be resolved; discovery must not accidentally authorize the good subset.
        with pytest.raises(MailValidationError, match="unresolved"):
            service.preview_rules()
        assert not client.cleanup.tokens._grants
        assert_no_mutations_or_header_search(sim)
    for rows, blocked in results[1:]:
        assert rows == results[0][0]
        assert blocked == results[0][1]


def test_ordinary_preview_shows_good_rows_and_one_unresolved_on_both_paths(indexed_mailbox):
    results = []
    for enabled in (False, True):
        sim, client, _, _, selected = indexed_mailbox(count=3, filler=2)
        configure_index(client, enabled)
        selected[-1].headers = duplicate_fault(selected[-1])
        identifiers = [item.message_id() for item in selected]
        for _ in range(2):
            preview = client.preview_action("move_to_trash", ids=identifiers)
            assert preview["token"] and preview["count"] == 2
            assert [row["message_id"] for row in preview["samples"]] == identifiers[:-1]
            assert not preview["blocked"]
            assert preview["excluded_ids"] == identifiers[-1:]
            assert preview["excluded_reasons"] == {"invalid_identity": 1}
            grant = client.cleanup.tokens.inspect(preview["token"], "move_to_trash")
            assert [row["message_id"] for row in grant.messages] == identifiers[:-1]
            results.append(
                {
                    key: value
                    for key, value in preview.items()
                    if key not in {"token", "expires_at", "log_id"}
                }
            )
        assert len(client.cleanup.tokens._grants) == 2
        assert_no_mutations_or_header_search(sim)
    assert all(result == results[0] for result in results[1:])


@pytest.mark.parametrize("enabled", [False, True], ids=["full-inventory", "index"])
def test_rules_keep_hidden_second_identity_as_an_unresolved_row(indexed_mailbox, enabled):
    sim, client, service, _, selected = indexed_mailbox(count=1, filler=2)
    configure_index(client, enabled)
    unrelated = next(item for item in sim.messages.values() if item.folder == "Archive")
    identifier = selected[0].message_id()
    unrelated.headers += f"Message-ID: {identifier}\r\n".encode()
    for _ in range(2):
        rows = raw_rules_rows(client, [identifier], "Archive")
        assert len(rows) == 1 and rows[0]["identity_invalid"]
        assert rows[0]["message_id"] == unrelated.message_id() != identifier
        assert client.cleanup._blocked(rows, REAL_FOLDERS)[0]["unresolved"] is True
    with pytest.raises(MailValidationError, match="unresolved"):
        service.preview_rules()
    assert not client.cleanup.tokens._grants
    assert_no_mutations_or_header_search(sim)


def test_ordinary_index_refuses_hidden_identity_in_unrelated_envelope(indexed_mailbox):
    sim, client, _, _, selected = indexed_mailbox(count=1, filler=2)
    unrelated = next(item for item in sim.messages.values() if item.folder == "Archive")
    identifier = selected[0].message_id()
    unrelated.headers += f"Message-ID: {identifier}\r\n".encode()
    result = client.preview_action("move_to_trash", ids=[identifier])
    assert result["token"] is None and result["excluded_ids"] == [identifier]
    assert result["excluded_reasons"] == {"invalid_identity": 1}
    assert not client.cleanup.tokens._grants
    assert_no_mutations_or_header_search(sim)


@pytest.mark.parametrize("enabled", [False, True], ids=["full-inventory", "index"])
@pytest.mark.parametrize("rules", [False, True], ids=["ordinary", "rules"])
@pytest.mark.parametrize("phase", ["confirm", "preflight", "per_uid", "undo", "undo_per_uid"])
def test_unresolved_row_refuses_every_action_phase(
    indexed_mailbox, monkeypatch, enabled, rules, phase
):
    sim, client, service, _, selected = indexed_mailbox(count=1, filler=2, latency=0)
    configure_index(client, enabled)
    item = selected[0]
    changed = duplicate_fault(item)
    preview = (
        service.preview_rules()
        if rules
        else client.preview_action("move_to_trash", ids=[item.message_id()])
    )
    assert preview["token"]
    execute = service.apply_rules if rules else client.move_to_trash
    completed = execute(preview["token"]) if phase.startswith("undo") else None
    if completed:
        assert completed["count"] == 1 and not completed["partial"]
    before_mutations = mutations(sim)
    before_snapshot = sim.snapshot()
    before_flags = item.flags.copy()
    injected = []

    def corrupt():
        item.headers = changed
        injected.append(True)

    if phase in {"confirm", "undo"}:
        corrupt()
    elif phase == "preflight":
        preflight = client.cleanup.backend._preflight

        def corrupt_before_preflight(*args, **kwargs):
            corrupt()
            return preflight(*args, **kwargs)

        monkeypatch.setattr(client.cleanup.backend, "_preflight", corrupt_before_preflight)
    else:

        def corrupt_at_writable_select(sid, folder, operation, command):
            if operation == "SELECT":
                corrupt()

        sim.hooks.append(corrupt_at_writable_select)
    try:
        result = (
            client.undo_action(completed["log_ids"][0]) if completed else execute(preview["token"])
        )
    except MailValidationError as error:
        assert any(word in str(error) for word in ("Message-ID", "identity", "unresolved"))
    else:
        assert result["count"] == 0 and result["partial"]
        assert all(row["status"] == "not_attempted" for row in result["messages"])
    assert injected
    assert mutations(sim) == before_mutations
    assert sim.snapshot() == before_snapshot
    assert item.flags == before_flags
    assert not any("SEARCH" in command and "HEADER" in command for *_, command in sim.commands)
