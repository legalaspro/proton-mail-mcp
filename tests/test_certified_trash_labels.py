"""Trash that removes labels cannot invalidate untouched approved rows itself."""

from bridge_sim import Msg, hdr, mutations
from resume_assertions import assert_bound_progress
from test_duplicate_msgid import duplicate_mailbox as duplicate_mailbox
from test_duplicate_msgid import logged, observe_sim_mutations

from proton_mail_mcp import guarded


def test_trash_own_label_removal_keeps_exact_untouched_remainder(duplicate_mailbox, monkeypatch):
    # One batched chunk would hold all 30; keep a remainder after the first chunk.
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 10)
    sim, client = duplicate_mailbox()
    items = [
        sim.add(Msg(hdr(f"<trash-label-{index}@example.test>"), labels={"Labels/Review"}))
        for index in range(30)
    ]
    original_label = sim.label

    def strip_labels(pid, name):
        original_label(pid, name)
        if name == "Trash":
            sim.messages[pid].labels.clear()

    monkeypatch.setattr(sim, "label", strip_labels)
    preview = client.preview_action("move_to_trash", query={"folder": "INBOX"})
    grant = client.cleanup.tokens.inspect(preview["token"], "move_to_trash")
    # Every chunk keeps full discovery when our own MOVE can change the label
    # counts. An old global label certificate cannot certify its later remainder.
    assert not grant.bounded_recovery
    assert all("label_states" not in row for row in grant.messages)
    actual = observe_sim_mutations(sim, grant, operation="MOVE")
    token, results = preview["token"], []
    while token:
        result = client.move_to_trash(token)
        results.append(result)
        assert_bound_progress(client.cleanup, grant, results, actual)
        assert not result["blocked"]
        assert len(results) <= preview["estimated_calls"]
        token = result["continuation_token"]
    assert sum(result["done"] for result in results) == 30 and not result["partial"]
    assert len(mutations(sim)) == 3  # One batched MOVE per ten-message chunk.
    assert len(actual) == 30
    assert all(item.folder == "Trash" and not item.labels for item in items)
    chunks = logged(client)
    assert len(chunks) > 1
    sim.hooks.remove(actual.hook)
    restored = set()
    for chunk in reversed(chunks):
        expected = {row["message_id"] for row in chunk["messages"]}
        undone = client.undo_action(chunk["log_id"])
        assert undone["count"] == len(expected) and not undone["partial"]
        assert {row["message_id"] for row in undone["messages"]} == expected
        assert not restored.intersection(expected)
        restored.update(expected)
    assert len(restored) == 30
    assert all(item.folder == "INBOX" and item.labels == {"Labels/Review"} for item in items)
