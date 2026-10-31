"""Complete identity discovery preserves malformed raw-field refusal semantics."""

from copy import deepcopy

import pytest
from test_folder_rules import mailbox as mailbox
from test_folder_rules import matching_membership_case
from test_mail import message

from proton_mail_mcp.cleanup_lookup import folder_inventory, matching_rows
from proton_mail_mcp.mail import MailValidationError


@pytest.mark.parametrize("confirm", [False, True])
def test_rules_hidden_case_variant_raw_identity_still_refuses(mailbox, confirm):
    case = matching_membership_case(mailbox, same_rule=False)
    peer = message("<independent@example.test>", date="Sat, 01 Jan 2000 09:00:00 +0000")
    case.fake.folders["Archive"] = {"3": peer}
    case.fake.folders["All Mail"]["3"] = peer
    peer.headers = peer.headers[:-2] + b"X-Pad-Data: <RULE-2@EXAMPLE.TEST>\r\n\r\n"
    token = case.rules.preview_rules()["token"] if confirm else None
    if confirm:
        assert token
    peer.headers = peer.headers.replace(b"X-Pad-Data:", b"Message-ID:")
    before = deepcopy(case.fake.folders)
    with pytest.raises(MailValidationError):
        case.rules.apply_rules(token) if confirm else case.rules.preview_rules()
    assert case.fake.folders == before
    assert not case.cleanup.backend.log.path.exists()


def test_identity_lookup_never_reuses_another_folder(mailbox):
    case = mailbox()
    case.fake.folders["Archive"] = {"2": case.fake.folders["INBOX"]["1"]}
    client = case.cleanup.client
    deadline = client._deadline()
    identifier = "<rule-1@example.test>"
    with client._session(deadline=deadline) as (connection, _):
        rows = folder_inventory(client, connection, deadline, "INBOX", identifiers=[identifier])
        assert {row["uid"]: row["message_id"] for row in matching_rows(rows, [identifier])} == {
            "1": identifier
        }
        # The fixture deliberately shares UIDVALIDITY across mailbox scopes.
        rows = folder_inventory(client, connection, deadline, "Archive", identifiers=[identifier])
        assert {row["uid"]: row["message_id"] for row in matching_rows(rows, [identifier])} == {
            "2": identifier
        }
