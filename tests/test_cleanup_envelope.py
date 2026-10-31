"""Conservative DB sender evidence, supplemented by raw headers during cleanup."""

import pytest

from proton_mail_mcp.cleanup_lookup import envelope_row, matching_rows
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.protected import ProtectedPolicy


def envelope(*, sender=None, identifier=b"<one@example.invalid>"):
    return {
        "envelope": [
            b"Sat, 17 Oct 2026 09:00:00 +0000",
            b"Synthetic message",
            sender,
            sender,
            sender,
            [[None, None, b"reader", b"example.invalid"]],
            None,
            None,
            None,
            identifier,
        ]
    }


@pytest.mark.parametrize(
    "sender",
    [
        None,
        [],
        b"garbled",
        [[None, None, None, None]],
        [[None, None, b"group", None]],
        [[None, b"route", b"sender", b"example.invalid"]],
        [[b"=?UTF-8?B?TmV3cw==?=", None, b"sender", b"example.invalid"]],
        [[None, None, b"=?encoded?=", b"example.invalid"]],
        [[None, None, b"sender", b"bad host"]],
        [[None, None, b"sender", b"example.invalid", b"extra"]],
        [[b"bad\x00name", None, b"sender", b"example.invalid"]],
    ],
)
def test_unknown_envelope_sender_is_protected(sender):
    row = envelope_row(envelope(sender=sender), "7", "All Mail", "12")
    assert row["sender_status"] == "unknown"
    assert row["sender_invalid"] is True
    assert ProtectedPolicy().check(row)


def test_each_known_envelope_sender_is_checked():
    item = envelope(
        sender=[
            [None, None, b"news", b"example.invalid"],
            [None, None, b"person", b"gmail.com"],
        ]
    )
    item["envelope"][3] = [item["envelope"][2][0]]
    row = envelope_row(item, "7", "All Mail", "12")
    assert row.get("sender_invalid")
    assert ProtectedPolicy().check(row)


def test_multiple_from_addresses_without_sender_fail_closed():
    # An absent Sender is copied from From, so two responsible senders are
    # conservatively unsupported even when every address is otherwise valid.
    item = envelope(
        sender=[
            [None, None, b"news", b"example.invalid"],
            [None, None, b"other", b"example.invalid"],
        ]
    )
    row = envelope_row(item, "7", "All Mail", "12")
    assert row["sender_invalid"]
    assert ProtectedPolicy().check(row)


def test_valid_utf8_display_name_is_not_ambiguous_sender():
    item = envelope(sender=[["Bänk Support".encode(), None, b"news", b"example.invalid"]])
    row = envelope_row(item, "7", "All Mail", "12")
    assert not row.get("sender_invalid")
    assert row["from"] == "Bänk Support <news@example.invalid>"
    assert not ProtectedPolicy().check(row)


@pytest.mark.parametrize(
    "identifier",
    [None, b"", b"garbled id", b"<bad\xff@example.invalid>", b"<one@example.invalid> extra"],
)
def test_unavailable_envelope_identity_blocks_only_matching_candidates(identifier):
    row = envelope_row(envelope(identifier=identifier), "7", "All Mail", "12")
    assert row["identity_invalid"]
    if identifier and b"<one@example.invalid>" in identifier:
        assert matching_rows([row], ["<one@example.invalid>"])[0]["identity_invalid"]
    else:
        assert matching_rows([row], ["<one@example.invalid>"]) == []


def test_one_envelope_containing_two_requested_ids_is_ambiguous():
    row = envelope_row(
        envelope(identifier=b"<one@example.invalid> <two@example.invalid>"),
        "7",
        "All Mail",
        "12",
    )
    with pytest.raises(MailValidationError, match="ambiguous"):
        matching_rows([row], ["<one@example.invalid>", "<two@example.invalid>"])


def test_envelope_identity_matching_is_case_sensitive():
    row = envelope_row(envelope(identifier=b"<Case@example.invalid>"), "7", "All Mail", "12")
    assert not matching_rows([row], ["<case@example.invalid>"])
    assert matching_rows([row], ["<Case@example.invalid>"])
