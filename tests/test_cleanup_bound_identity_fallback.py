"""Prior identity evidence outside approved memberships retains full verification."""

import pytest
from action_records import execution_log
from test_cleanup_mutations import uid_values
from test_cleanup_socket import mutations
from test_cleanup_socket import socket_mailbox as socket_mailbox

from proton_mail_mcp.mail import MailValidationError


def preview_with_other_identity_copy(client, peer, folder):
    # The source query approves two UIDs. An older same-identity copy is
    # inspected for protection, but its different fingerprint is not membership
    # evidence for either approved row. It must not disappear from verification.
    other = (
        peer.folders["INBOX"]["7"]
        .replace(b"October newsletter", b"Earlier copy")
        .replace(b"Oct", b"Nov")
    )
    peer.folders[folder]["500"] = other
    preview = client.preview_action(
        "move_to_folder",
        query={"folder": "INBOX", "subject": "October newsletter"},
        params={"folder": "Folders/Reviewed"},
    )
    assert preview["count"] == 2 and preview["token"] and not preview["blocked"]
    grant = client.cleanup.tokens.inspect(preview["token"], "move_to_folder")
    assert all(
        (membership["folder"], membership["uid"]) != (folder, "500")
        for row in grant.messages
        for membership in row["memberships"]
    )
    return preview, other


@pytest.mark.parametrize(
    "folder,change",
    [
        ("Archive", "protected"),
        ("All Mail", "protected"),
        ("INBOX", "protected"),
        ("Archive", "inconsistent_sender"),
        ("All Mail", "inconsistent_sender"),
    ],
)
def test_prior_identity_copy_outside_memberships_still_checked(
    socket_mailbox, monkeypatch, folder, change
):
    client, peer, log_path = socket_mailbox()
    preview, other = preview_with_other_identity_copy(client, peer, folder)
    sender = b"person@gmail.com" if change == "protected" else b"other@example.com"
    peer.folders[folder]["500"] = other.replace(b"sender@example.com", sender)
    monkeypatch.setattr(
        client.cleanup.tokens,
        "claim",
        lambda *args, **kwargs: pytest.fail("Changed identity evidence was claimed"),
    )
    before = len(peer.commands)
    with pytest.raises(MailValidationError):
        client.move_to_folder(preview["token"])
    commands = peer.commands[before:]
    assert any(command.startswith(b"UID SEARCH") for _, command in commands)
    assert not mutations(commands) and not execution_log(log_path)
    assert set(peer.folders["INBOX"]) == ({"7", "8", "500"} if folder == "INBOX" else {"7", "8"})
    client.cleanup.tokens.inspect(preview["token"], "move_to_folder")


@pytest.mark.parametrize("folder", ["Archive", "All Mail", "INBOX"])
def test_unchanged_identity_copy_fallback_does_not_widen_approval(socket_mailbox, folder):
    client, peer, _ = socket_mailbox()
    preview, other = preview_with_other_identity_copy(client, peer, folder)
    before = len(peer.commands)
    result = client.move_to_folder(preview["token"])
    assert result["done"] == 2 and not result["partial"]
    assert peer.folders[folder]["500"] == other
    assert {
        (source, uid)
        for source, command in mutations(peer.commands[before:])
        for uid in uid_values(command.split()[2])
    } == {("INBOX", "7"), ("INBOX", "8")}
