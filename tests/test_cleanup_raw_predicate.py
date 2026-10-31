"""Ordinary cleanup retains main's raw HEADER predicate without HEADER SEARCH."""

import socket

import bridge_sim
import pytest
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations

from proton_mail_mcp.mail import MailValidationError

WANTED = "<requested@example.invalid>"
UNRELATED = "<unrelated@example.invalid>"


@pytest.fixture
def raw_mailbox(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Raw predicate tests must use the socketpair simulator")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    sim = BridgeSim()
    try:
        yield sim, make_client(sim, tmp_path)
    finally:
        sim.close()
        assert not sim.errors and not sim.violations
        assert all(not worker.is_alive() for _, _, worker in sim.peers)


@pytest.mark.parametrize("selector", ["ids", "query"])
@pytest.mark.parametrize("raw_kind", ["missing", "unrelated"])
@pytest.mark.parametrize("confirm", [False, True], ids=["preview", "confirm"])
def test_missing_or_unrelated_raw_identity_refuses_without_mutation(
    raw_mailbox, monkeypatch, selector, raw_kind, confirm
):
    sim, client = raw_mailbox
    original = hdr(WANTED)
    item = sim.add(Msg(original))
    expected_envelope = bridge_sim.envelope_from_headers(original)
    # Keep ENVELOPE stable while FETCH exposes the changed raw field. This is
    # the disagreement main's HEADER membership predicate used to reject.
    monkeypatch.setattr(bridge_sim, "envelope_from_headers", lambda raw: expected_envelope)
    kwargs = {"ids": [WANTED]} if selector == "ids" else {"query": {"folder": "INBOX"}}
    token = client.preview_action("mark_read", **kwargs)["token"] if confirm else None
    if confirm:
        assert token
    item.headers = (
        original.replace(b"Message-ID:", b"X-Trace-Id:")
        if raw_kind == "missing"
        else original.replace(WANTED.encode(), UNRELATED.encode())
    )
    # A confirm refusal must come from raw evidence, not size/date drift.
    assert len(item.headers) == len(original)
    if confirm:
        with pytest.raises(MailValidationError):
            client.mark_read(token)
    else:
        try:
            result = client.preview_action("mark_read", **kwargs)
        except MailValidationError:
            pass
        else:
            assert result["token"] is None
    assert r"\Seen" not in item.flags
    assert mutations(sim) == []


@pytest.mark.parametrize("selector", ["ids", "query"])
def test_non_ascii_raw_identity_keeps_preview_and_confirm_working(raw_mailbox, selector):
    sim, client = raw_mailbox
    identifier = "<café@example.invalid>"
    item = sim.add(Msg(hdr(identifier)))
    kwargs = {"ids": [identifier]} if selector == "ids" else {"query": {"folder": "INBOX"}}
    preview = client.preview_action("mark_read", **kwargs)
    assert preview["token"] and preview["count"] == 1
    assert mutations(sim) == []
    completed = client.mark_read(preview["token"])
    assert completed["count"] == 1 and not completed["partial"]
    assert r"\Seen" in item.flags
    assert not any("SEARCH" in command and "HEADER" in command for _, _, _, command in sim.commands)
