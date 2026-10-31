"""Provider-issued opaque IDs round-trip without weakening identity boundaries."""

from dataclasses import replace
from email import policy
from email.parser import BytesParser

import pytest
from bridge_sim import Msg, hdr, mutations
from test_cleanup_raw_predicate import raw_mailbox as raw_mailbox
from test_mail import FakeImap, client, message
from test_mail import settings as settings
from test_unsubscribe import newsletter

from proton_mail_mcp.cleanup_lookup import raw_message_id
from proton_mail_mcp.mail import MAX_MESSAGE_ID_BYTES, MailValidationError, _message_id


@pytest.mark.parametrize(
    "identifier",
    ["abcdef01-2345-4678-9abc-def012345678-0", "<Provider-ID>", "<a@b@c>"],
)
def test_search_ids_round_trip_through_exact_thread_lookup(settings, identifier):
    item = message("<placeholder@example.invalid>")
    item.headers = item.headers.replace(b"<placeholder@example.invalid>", identifier.encode())
    # HEADER SEARCH is case-insensitive and a substring match. Neither another
    # spelling nor a longer token may be mistaken for the selected identity.
    other = message("<placeholder@example.invalid>")
    other.headers = other.headers.replace(
        b"<placeholder@example.invalid>", (identifier + "-suffix").encode()
    )
    different_case = message("<placeholder@example.invalid>")
    different_case.headers = different_case.headers.replace(
        b"<placeholder@example.invalid>", identifier.swapcase().encode()
    )
    fake = FakeImap({"INBOX": {"1": other, "2": different_case, "9": item}})
    mailbox = client(settings, fake)
    row = next(row for row in mailbox.search_mail()["messages"] if row["uid"] == "9")
    assert row["message_id"] == identifier
    found = mailbox.read_thread(row["message_id"], include_thread=False)
    assert [row["message_id"] for row in found["messages"]] == [identifier]
    assert found["messages"][0]["alias"]["uid"] == "9"
    searches = [call for call in fake.calls if call[0] == "SEARCH" and "HEADER" in call]
    assert searches[-1][3:7] == ("UID", "1:9", "HEADER", "Message-ID")


def test_message_id_tokens_reject_injection_and_resource_abuse_before_connecting(settings):
    fake = FakeImap()
    mailbox = client(settings, fake)
    for identifier in (
        "",
        " id",
        "id ",
        "id id",
        "id\r\n",
        "id\t",
        'id"quoted',
        "id\x00",
        "id\x7f",
        "id\x85",
        "id\u200b",
        "id\udcff",
        "<one><two>",
        "one@example.invalid,two@example.invalid",
        "one@example.invalid(comment)",
        "x" * (MAX_MESSAGE_ID_BYTES + 1),
    ):
        with pytest.raises(MailValidationError):
            mailbox.read_thread(identifier)
    assert not fake.calls
    assert _message_id("Provider-ID") == "Provider-ID"
    boundary = "x" * (MAX_MESSAGE_ID_BYTES - 2) + "@y"
    assert _message_id(_message_id(boundary)) == boundary


def test_bare_provider_id_round_trips_from_search_to_all_previews(settings, tmp_path):
    identifier = "abcdef01-2345-4678-9abc-def012345678-0"
    item = newsletter(identifier)
    item.headers = item.headers.replace(
        b"Sender <sender@example.com>", b'"Example Sales" <info@sales.example.com>'
    )
    item.headers = (
        b"Return-Path: <info@sales.example.com>\r\n"
        b"Authentication-Results: example; dmarc=pass header.from=sales.example.com;\r\n"
        b" spf=pass smtp.mailfrom=sales.example.com; dkim=pass header.d=example.com\r\n"
        + item.headers
    )
    mailbox = client(
        replace(settings, action_log_path=str(tmp_path / "actions.jsonl")),
        FakeImap({"INBOX": {"1": item}}),
    )
    returned = mailbox.search_mail()["messages"][0]["message_id"]
    assert returned == identifier
    assert mailbox.read_thread(returned)["messages"][0]["message_id"] == identifier
    for action, selector in (
        ("unsubscribe", {"ids": [returned]}),
        ("mark_read", {"query": {"folder": "INBOX"}}),
    ):
        preview = mailbox.preview_action(action, **selector)
        assert preview["count"] == 1 and preview["token"]
        assert preview["excluded_count"] == 0
        assert preview["samples"][0]["message_id"] == identifier


def test_opaque_identity_remains_bound_through_cleanup_and_undo(raw_mailbox):
    sim, mailbox = raw_mailbox
    identifier = "Provider-ID-without-domain"
    item = sim.add(Msg(hdr(identifier)))
    preview = mailbox.preview_action("mark_read", ids=[identifier])
    assert preview["count"] == 1 and preview["token"]
    completed = mailbox.mark_read(preview["token"])
    assert completed["count"] == 1 and not completed["partial"]
    undone = mailbox.undo_action(completed["log_ids"][0])
    assert undone["count"] == 1 and not undone["partial"]
    assert item.folder == "INBOX" and not item.flags


def test_opaque_identity_cannot_hide_duplicate_fields_or_controls(raw_mailbox):
    sim, mailbox = raw_mailbox
    identifier = "Provider-ID-without-domain"
    sim.add(Msg(hdr(identifier)))
    # A second identity hidden by ENVELOPE must still exclude the requested ID.
    sim.add(Msg(hdr("Other-ID", extra=f"Message-ID: {identifier}\r\n".encode())))
    preview = mailbox.preview_action("mark_read", ids=[identifier])
    assert not preview["token"]
    assert preview["excluded_reasons"] == {"invalid_identity": 1}
    assert not mutations(sim)
    for suffix in ("\t", "\r\n ", "\x00"):
        headers = BytesParser(policy=policy.default).parsebytes(hdr(identifier + suffix))
        with pytest.raises(MailValidationError):
            raw_message_id(headers)
