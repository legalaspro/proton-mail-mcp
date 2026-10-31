"""Present but unusable optional senders are NIL in Gluon's DB envelope."""

import bridge_costs
import pytest
from test_cleanup_mutations import mutation_calls
from test_cleanup_mutations import setup as mutation_setup

from proton_mail_mcp.cleanup_lookup import envelope_row
from proton_mail_mcp.mail import MailError, _ImapValues
from proton_mail_mcp.protected import ProtectedPolicy


@pytest.fixture
def sender_mailbox(tmp_path):
    return mutation_setup.__wrapped__(tmp_path)


@pytest.mark.parametrize(
    "header,index",
    [
        ("Sender: <<<garbled", 3),
        ("Sender:", 3),
        ("Reply-To: <<<garbled", 4),
        ("Reply-To: =?x-unknown?Q?Bank?= <security@bank.example.com>", 4),
    ],
)
def test_present_unusable_sender_blocks_cleanup(sender_mailbox, monkeypatch, header, index):
    mailbox, fake, _ = sender_mailbox
    raw = (
        "Message-ID: <cleanup@example.test>\r\n"
        "From: offers@updates.example.invalid\r\n"
        f"{header}\r\n\r\n"
    ).encode()
    fake.folders["INBOX"]["1"].headers = raw
    # IMAP INTERNALDATE exists independently of this message's missing Date header.
    fake.folders["INBOX"]["1"].internaldate = "14-Oct-2026 10:00:00 +0000"
    address = b'((NIL NIL "offers" "updates.example.invalid"))'
    fields = [
        b"NIL",
        b'"probe"',
        address,
        address,
        address,
        b"NIL",
        b"NIL",
        b"NIL",
        b"NIL",
        b'"<cleanup@example.test>"',
    ]
    fields[index] = b"NIL"
    wire = b"(" + b" ".join(fields) + b")"
    original = bridge_costs.envelope_from_headers
    monkeypatch.setattr(
        bridge_costs,
        "envelope_from_headers",
        lambda value: wire if value == raw else original(value),
    )

    row = envelope_row({"envelope": _ImapValues(wire).value()}, "1", "INBOX", "1")
    assert row["sender_invalid"] is True
    assert ProtectedPolicy().check(row)
    result = mailbox.preview_action("move_to_trash", ids=["<cleanup@example.test>"])
    if result["token"] is not None or not result["blocked"]:
        pytest.fail("Unusable sender received cleanup approval", pytrace=False)
    assert not mutation_calls(fake)


@pytest.mark.parametrize("escape", [rb"\n", rb"\x00", rb"\u2028", rb"\U000e0001", rb"\a", rb"\t"])
def test_control_escaped_sender_cannot_become_ordinary_sender(escape):
    address = b'(("News' + escape + b'letter" NIL "offers" "updates.example.invalid"))'
    wire = b"(NIL NIL " + b" ".join([address] * 3) + b' NIL NIL NIL NIL "<cleanup@example.test>")'
    try:
        envelope = _ImapValues(wire).value()
    except MailError:
        return  # Refusing a malformed IMAP quoted string is fail closed too.
    row = envelope_row({"envelope": envelope}, "1", "INBOX", "1")
    assert row["sender_invalid"] is True
    assert ProtectedPolicy().check(row)


@pytest.mark.parametrize("name", [rb"News\"letter", rb"News\\letter"])
def test_valid_imap_name_escapes_remain_usable(name):
    address = b'(("' + name + b'" NIL "offers" "updates.example.invalid"))'
    wire = b"(NIL NIL " + b" ".join([address] * 3) + b' NIL NIL NIL NIL "<cleanup@example.test>")'
    row = envelope_row({"envelope": _ImapValues(wire).value()}, "1", "INBOX", "1")
    assert not row.get("sender_invalid")
    assert not ProtectedPolicy().check(row)
