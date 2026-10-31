"""Identity spelling is canonical across discovery, approval and execution."""

from dataclasses import replace
from email import policy
from email.parser import BytesParser

import bridge_sim
import pytest
from bridge_sim import Msg, hdr, make_client, mutations
from certified_index_peer import CertifiedIndexPeer
from test_cleanup_raw_identity_hardening import rules_service
from test_mail import FakeImap, client, message
from test_mail import settings as settings
from test_unsubscribe import assert_read_only, newsletter, setup

from proton_mail_mcp.cleanup_lookup import raw_message_id
from proton_mail_mcp.guarded import _cleanup_message_id, _selector
from proton_mail_mcp.mail import MailValidationError, _draft_message_id, _message_id

CANONICAL = "<Sales.18@example.invalid>"
SPELLINGS = [CANONICAL, CANONICAL[1:-1]]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_message_id_spelling_has_one_canonical_identity(spelling):
    assert _message_id(spelling) == CANONICAL
    assert _cleanup_message_id(spelling) == CANONICAL
    assert _selector(None, [spelling]) == {"ids": [CANONICAL]}
    headers = BytesParser(policy=policy.default).parsebytes(hdr(spelling))
    assert raw_message_id(headers) == CANONICAL


@pytest.mark.parametrize(
    "identifier",
    [
        "",
        " leading-space",
        "trailing-space ",
        "<one@example.invalid",
        "one@example.invalid>",
        "<<one@example.invalid>>",
        "one @example.invalid",
        "one@example.invalid two@example.invalid",
        "<one@example.invalid> <two@example.invalid>",
        "one@example.invalid\r\nInjected: value",
        "one@example.invalid\x00",
    ],
)
def test_malformed_cleanup_ids_remain_invalid(identifier):
    with pytest.raises(MailValidationError):
        _cleanup_message_id(identifier)
    with pytest.raises(MailValidationError):
        _message_id(identifier)


def test_draft_control_rejection_is_preserved():
    with pytest.raises(MailValidationError, match="control"):
        _draft_message_id("\r\n" + CANONICAL)


@pytest.fixture(params=[False, True], ids=["full-inventory", "certified-index"])
def mailbox(tmp_path, request):
    sim = CertifiedIndexPeer()
    mailbox = make_client(sim, tmp_path)
    mailbox.settings = replace(
        mailbox.settings,
        identity_index_enabled=request.param,
        identity_index_dir=str(tmp_path / "identity-index"),
    )
    try:
        yield sim, mailbox
    finally:
        sim.close()
        assert not sim.errors and not sim.violations
        assert all(not worker.is_alive() for _, _, worker in sim.peers)


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("selector", ["ids", "query"])
@pytest.mark.parametrize("action", ["mark_read", "move_to_trash"])
def test_canonical_preview_token_execute_and_undo_roundtrip(mailbox, spelling, selector, action):
    sim, mailbox = mailbox
    item = sim.add(Msg(hdr(spelling)))
    before = sim.snapshot()
    arguments = {"ids": [CANONICAL[1:-1]]} if selector == "ids" else {"query": {"folder": "INBOX"}}
    preview = mailbox.preview_action(action, **arguments)
    assert preview["count"] == 1 and preview["token"]
    assert preview["excluded_count"] == 0
    assert preview["samples"][0]["message_id"] == CANONICAL
    grant = mailbox.cleanup.tokens.inspect(preview["token"], action)
    assert grant.messages[0]["message_id"] == CANONICAL
    assert all(row["message_id"] == CANONICAL for row in grant.messages[0]["memberships"])
    assert not mutations(sim)
    completed = getattr(mailbox, action)(preview["token"])
    assert completed["count"] == 1 and not completed["partial"]
    assert completed["messages"][0]["message_id"] == CANONICAL
    undone = mailbox.undo_action(completed["log_ids"][0])
    assert undone["count"] == 1 and not undone["partial"]
    assert sim.snapshot() == before and item.folder == "INBOX" and not item.flags


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_optional_execute_ids_match_the_canonical_preview_token(mailbox, spelling):
    sim, mailbox = mailbox
    sim.add(Msg(hdr(CANONICAL[1:-1])))
    preview = mailbox.preview_action("mark_read", ids=[CANONICAL])
    result = mailbox.cleanup.execute("mark_read", preview["token"], ids=[spelling])
    assert result["count"] == 1 and not result["partial"]


@pytest.mark.parametrize(
    "raw_spelling,envelope_spelling", [(CANONICAL, CANONICAL[1:-1]), (CANONICAL[1:-1], CANONICAL)]
)
def test_raw_and_envelope_identity_spellings_can_differ(
    mailbox, monkeypatch, raw_spelling, envelope_spelling
):
    sim, mailbox = mailbox
    sim.add(Msg(hdr(raw_spelling)))
    expected_envelope = bridge_sim.envelope_from_headers(hdr(envelope_spelling))
    monkeypatch.setattr(bridge_sim, "envelope_from_headers", lambda raw: expected_envelope)
    preview = mailbox.preview_action("mark_read", ids=[CANONICAL])
    assert preview["count"] == 1 and preview["token"]
    assert mailbox.mark_read(preview["token"])["count"] == 1


def test_duplicate_bracketed_and_bare_identity_fields_stay_invalid(mailbox):
    sim, mailbox = mailbox
    sim.add(Msg(hdr(CANONICAL, extra=f"Message-ID: {CANONICAL[1:-1]}\r\n".encode())))
    preview = mailbox.preview_action("move_to_trash", ids=[CANONICAL])
    assert preview["token"] is None
    assert preview["excluded_reasons"] == {"invalid_identity": 1}
    assert not mutations(sim)


def test_rules_cannot_ignore_hidden_bare_duplicate_identity(mailbox):
    sim, mailbox = mailbox
    sim.add(Msg(hdr(CANONICAL)))
    sim.add(Msg(hdr("<other@example.invalid>") + f"Message-ID: {CANONICAL[1:-1]}\r\n".encode()))
    with pytest.raises(MailValidationError):
        rules_service(mailbox).preview_rules()
    assert not mutations(sim)


def test_rules_accept_bracketless_identity_through_execution(mailbox):
    sim, mailbox = mailbox
    item = sim.add(Msg(hdr(CANONICAL[1:-1])))
    rules = rules_service(mailbox)
    preview = rules.preview_rules()
    assert preview["count"] == 1 and preview["token"]
    completed = rules.apply_rules(preview["token"])
    assert completed["count"] == 1 and not completed["partial"]
    assert item.folder == "Folders/Reviewed"


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_search_and_thread_return_canonical_ids_for_bracketless_headers(settings, spelling):
    fake = FakeImap(
        {"INBOX": {"1": message(spelling), "2": message("<prefix-Sales.18@example.invalid>")}}
    )
    mailbox = client(settings, fake)
    found = mailbox.search_mail(folder="INBOX")
    assert CANONICAL in {row["message_id"] for row in found["messages"]}
    thread = mailbox.read_thread(CANONICAL[1:-1], include_thread=False)
    assert [row["message_id"] for row in thread["messages"]] == [CANONICAL]


def test_unsubscribe_accepts_bare_message_id_through_preview_and_execute(settings, monkeypatch):
    mailbox, fake = setup(settings, items={"1": newsletter(CANONICAL[1:-1])})
    performed = []
    monkeypatch.setattr(
        "proton_mail_mcp.unsubscribe_http.perform",
        lambda target, **kwargs: performed.append(target) or 204,
    )
    preview = mailbox.preview_action("unsubscribe", ids=[CANONICAL[1:-1]])
    assert preview["count"] == 1 and preview["token"]
    assert preview["samples"][0]["message_id"] == CANONICAL
    completed = mailbox.unsubscribe(preview["token"])
    assert completed["count"] == 1 and not completed["partial"]
    assert len(performed) == 1
    assert_read_only(fake)
