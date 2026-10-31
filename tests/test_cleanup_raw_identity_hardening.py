"""Every authorization phase requires one exact raw identity on the actual UID."""

import json
from email import policy
from email.parser import BytesParser
from pathlib import Path

import bridge_sim
import pytest
from bridge_sim import Msg, hdr, mutations
from test_cleanup_raw_predicate import raw_mailbox as raw_mailbox

from proton_mail_mcp.cleanup_lookup import (
    folder_inventory,
    raw_identity_fields,
    raw_message_id,
)
from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.mail import MailValidationError

WANTED = "<requested@example.invalid>"
OTHER = "<unrelated@example.invalid>"
BAD_FIELDS = (
    "missing",
    "unrelated",
    "duplicate_same",
    "duplicate_other",
    "unparseable",
    "non_utf8",
    "case_mismatch",
)


def raw_fixture(kind):
    original = hdr(WANTED, extra=f"X-Pad-Data: {WANTED}\r\n".encode())
    field = f"Message-ID: {WANTED}".encode()
    if kind == "missing":
        changed = original.replace(b"Message-ID:", b"X-Missing-:")
    elif kind == "unrelated":
        changed = original.replace(field, f"Message-ID: {OTHER}".encode())
    elif kind.startswith("duplicate"):
        changed = original.replace(b"X-Pad-Data:", b"Message-ID:")
        if kind == "duplicate_other":
            changed = changed.replace(WANTED.encode(), OTHER.encode(), 1)
    elif kind == "unparseable":
        changed = original.replace(field, field[:-1] + b"[")
    elif kind == "non_utf8":
        changed = original.replace(field, field.replace(b"requested", b"requeste\xff"))
    else:
        assert kind == "case_mismatch"
        changed = original.replace(field, field.replace(b"requested", b"REQUESTED"))
    # No other fingerprint change can explain a refusal.
    assert len(changed) == len(original)
    return original, changed


@pytest.mark.parametrize("selector", ["ids", "query"])
@pytest.mark.parametrize("action", ["mark_read", "move_to_trash"])
@pytest.mark.parametrize("raw_kind", BAD_FIELDS)
@pytest.mark.parametrize(
    "phase", ["preview", "confirm", "preflight", "per_uid", "undo", "undo_per_uid"]
)
def test_raw_identity_refuses_every_authorization_phase(
    raw_mailbox, monkeypatch, selector, action, raw_kind, phase
):
    sim, client = raw_mailbox
    original, changed = raw_fixture(raw_kind)
    item = sim.add(Msg(original))
    expected_envelope = bridge_sim.envelope_from_headers(original)
    # The fake peer's envelope remains canonical while the raw field changes.
    monkeypatch.setattr(bridge_sim, "envelope_from_headers", lambda raw: expected_envelope)
    kwargs = {"ids": [WANTED]} if selector == "ids" else {"query": {"folder": "INBOX"}}
    token = None
    completed = None
    if phase != "preview":
        token = client.preview_action(action, **kwargs)["token"]
        assert token
    if phase.startswith("undo"):
        completed = getattr(client, action)(token)
        assert completed["count"] == 1 and not completed["partial"]
    before = mutations(sim)
    state_before = sim.snapshot()
    flags_before = set(item.flags)
    injected = []

    def corrupt():
        item.headers = changed
        injected.append(True)

    if phase in {"preview", "confirm", "undo"}:
        corrupt()
    elif phase == "preflight":
        preflight = client.cleanup.backend._preflight

        def corrupt_before_preflight(*args, **kwargs):
            corrupt()
            return preflight(*args, **kwargs)

        monkeypatch.setattr(client.cleanup.backend, "_preflight", corrupt_before_preflight)
    else:
        # Corrupt only after all ordinary/preflight reads, at the writable
        # selection immediately preceding the final per-UID FETCH.
        def corrupt_at_writable_select(sid, folder, operation, command):
            if operation == "SELECT":
                corrupt()

        sim.hooks.append(corrupt_at_writable_select)

    try:
        if phase == "preview":
            result = client.preview_action(action, **kwargs)
            assert result["token"] is None
            assert result["excluded_count"] == 1
            assert result["excluded_reasons"] == {"invalid_identity": 1}
            assert not client.cleanup.tokens._grants
        else:
            result = (
                client.undo_action(completed["log_ids"][0])
                if completed
                else getattr(client, action)(token)
            )
            assert result["count"] == 0 and result["partial"]
            assert "verification" in result["error"]
            assert all(row["status"] == "not_attempted" for row in result["messages"])
    except MailValidationError as error:
        assert "Message-ID" in str(error) or "identity" in str(error)

    assert injected
    if phase == "preview":
        assert not client.cleanup.tokens._grants
    assert mutations(sim) == before
    assert sim.snapshot() == state_before
    assert item.flags == flags_before
    assert not any("SEARCH" in command and "HEADER" in command for _, _, _, command in sim.commands)


@pytest.mark.parametrize("selector", ["ids", "query"])
@pytest.mark.parametrize("action", ["mark_read", "move_to_trash"])
def test_utf8_raw_identity_round_trips_through_confirmation_and_undo(raw_mailbox, selector, action):
    sim, client = raw_mailbox
    identifier = "<café@exämple.invalid>"
    item = sim.add(Msg(hdr(identifier)))
    before = sim.snapshot()
    kwargs = {"ids": [identifier]} if selector == "ids" else {"query": {"folder": "INBOX"}}
    preview = client.preview_action(action, **kwargs)
    assert preview["token"] and preview["count"] == 1
    assert preview["samples"][0]["message_id"] == identifier
    completed = getattr(client, action)(preview["token"])
    assert completed["count"] == 1 and not completed["partial"]
    undone = client.undo_action(completed["log_ids"][0])
    assert undone["count"] == 1 and not undone["partial"]
    assert sim.snapshot() == before
    assert item.folder == "INBOX" and not item.flags


def test_raw_field_inventory_keeps_every_occurrence_and_recovers_utf8():
    identifier = "<café@exämple.invalid>"
    headers = BytesParser(policy=policy.default).parsebytes(
        f"Message-ID: {identifier}\r\nMessage-ID: {OTHER}\r\n\r\n".encode()
    )
    assert raw_identity_fields(headers) == [identifier, OTHER]
    with pytest.raises(MailValidationError, match="ambiguous"):
        raw_message_id(headers)


def test_raw_field_parser_rejects_invalid_utf8_instead_of_replacement_text():
    headers = BytesParser(policy=policy.default).parsebytes(
        b"Message-ID: <invalid\xff@example.invalid>\r\n\r\n"
    )
    with pytest.raises(MailValidationError, match="UTF-8"):
        raw_message_id(headers)


@pytest.mark.parametrize("raw_kind", BAD_FIELDS)
def test_identity_lookup_marks_invalid_raw_evidence(raw_mailbox, monkeypatch, raw_kind):
    sim, client = raw_mailbox
    original, changed = raw_fixture(raw_kind)
    sim.add(Msg(changed))
    expected_envelope = bridge_sim.envelope_from_headers(original)
    monkeypatch.setattr(bridge_sim, "envelope_from_headers", lambda raw: expected_envelope)
    with client._session() as (connection, _):
        rows = folder_inventory(
            client, connection, client._deadline(), "INBOX", identifiers=[WANTED]
        )
        assert len(rows) == 1 and rows[0]["identity_invalid"]
    assert mutations(sim) == []


def rules_service(client):
    path = Path(client.settings.action_log_path).parent / "synthetic-rules.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [
                    {
                        "field": "from",
                        "contains": "sender@example.com",
                        "folder": "Folders/Reviewed",
                    }
                ],
            }
        )
    )
    return FolderRulesService(client.cleanup, path)


@pytest.mark.parametrize("raw_kind", BAD_FIELDS)
@pytest.mark.parametrize("confirm", [False, True])
def test_rules_share_exact_raw_identity_refusal(raw_mailbox, monkeypatch, raw_kind, confirm):
    sim, client = raw_mailbox
    original, changed = raw_fixture(raw_kind)
    item = sim.add(Msg(original))
    expected_envelope = bridge_sim.envelope_from_headers(original)
    monkeypatch.setattr(bridge_sim, "envelope_from_headers", lambda raw: expected_envelope)
    rules = rules_service(client)
    token = rules.preview_rules()["token"] if confirm else None
    if confirm:
        assert token
    item.headers = changed
    with pytest.raises(MailValidationError):
        rules.apply_rules(token) if confirm else rules.preview_rules()
    assert not mutations(sim)
    assert item.folder == "INBOX" and not item.flags
    if not confirm:
        assert not client.cleanup.tokens._grants


def test_rules_accept_utf8_identity_and_preserve_it_through_undo(raw_mailbox):
    sim, client = raw_mailbox
    identifier = "<café@exämple.invalid>"
    item = sim.add(Msg(hdr(identifier)))
    rules = rules_service(client)
    preview = rules.preview_rules()
    assert preview["token"] and preview["count"] == 1
    completed = rules.apply_rules(preview["token"])
    assert completed["count"] == 1 and not completed["partial"]
    assert item.folder == "Folders/Reviewed"
    restored = client.undo_action(completed["log_ids"][0])
    assert restored["count"] == 1 and not restored["partial"]
    assert item.folder == "INBOX" and not item.flags
