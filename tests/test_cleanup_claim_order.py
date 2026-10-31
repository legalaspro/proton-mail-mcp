"""A confirmed approval is consumed before the first mailbox mutation."""

import pytest
from test_preview_proton_shape import SYNTHETIC_BATCH, headers
from test_preview_proton_shape import shape_mailbox as shape_mailbox

from proton_mail_mcp.mail import MailValidationError

IDS = SYNTHETIC_BATCH[:2]


def token_valid(client, token, action):
    try:
        client.cleanup.tokens.inspect(token, action)
        return True
    except MailValidationError:
        return False


def test_claim_failure_leaves_mailbox_unchanged(shape_mailbox, monkeypatch):
    client, peer, _ = shape_mailbox(mailbox_size=80)
    preview = client.preview_action("move_to_trash", ids=IDS)
    if not preview["token"]:
        pytest.fail("control preview has no token", pytrace=False)

    def refuse(*args, **kwargs):
        raise MailValidationError("Invalid or already used preview token")

    monkeypatch.setattr(client.cleanup.tokens, "claim", refuse)
    client.move_to_trash(preview["token"])
    if peer.mutations:
        pytest.fail(f"{peer.mutations} mutation(s) despite a refused claim", pytrace=False)


@pytest.mark.parametrize("action", ["move_to_trash", "remove_label"])
def test_token_is_consumed_before_the_first_mutation_command(shape_mailbox, action):
    client, peer, _ = shape_mailbox(mailbox_size=80)
    kwargs = {"ids": IDS}
    if action == "remove_label":
        peer.folders["Labels/Review"] = {
            str(index + 1): headers(identifier) for index, identifier in enumerate(IDS)
        }
        kwargs = {"query": {"folder": "Labels/Review"}, "params": {"label": "Review"}}
    preview = client.preview_action(action, **kwargs)
    if not preview["token"]:
        pytest.fail("control preview has no token", pytrace=False)
    seen = []
    original_hook = peer.command_hook

    def hook(folder, operation, command):
        if operation in ("MOVE", "COPY", "STORE", "EXPUNGE") and not seen:
            seen.append(token_valid(client, preview["token"], action))
        if original_hook is not None:
            original_hook(folder, operation, command)

    peer.command_hook = hook
    executed = getattr(client, action)(preview["token"])
    if not executed["count"]:
        pytest.fail("execute changed nothing", pytrace=False)
    if seen != [False]:
        pytest.fail("approval still valid when the first mutation command arrived", pytrace=False)
    if token_valid(client, preview["token"], action):
        pytest.fail("approval still valid after execute", pytrace=False)
