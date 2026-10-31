"""Rules revalidate source metadata even when no reviewed messages were skipped."""

import pytest
from bridge_sim import FOLDER_ATTRS, mutations
from resume_assertions import assert_bound_progress
from test_cleanup_mutations import uid_values
from test_duplicate_msgid import (
    DESTINATION,
    duplicates,
    logged,
    observe_sim_mutations,
    rules_service,
)
from test_duplicate_msgid import duplicate_mailbox as duplicate_mailbox
from test_rules_inbox_attributes import REFUSED_FLAGS

from proton_mail_mcp import guarded
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.rules_source import RulesSourceError


@pytest.mark.parametrize("flag", REFUSED_FLAGS)
@pytest.mark.parametrize("mark_read", [False, True])
def test_rules_source_conflict_stops_before_second_chunk(
    duplicate_mailbox, tmp_path, monkeypatch, flag, mark_read
):
    # One batched chunk would hold all 51 rows; keep a second chunk to refuse.
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 21)
    sim, client = duplicate_mailbox()
    items = duplicates(sim, count=51)
    service = rules_service(client, tmp_path)
    preview = service.preview_rules(mark_read=mark_read)
    assert preview["count"] == preview["matched_count"] == 51 and preview["token"]
    assert all(not rule["blocked"] for rule in preview["rules"])
    grant = client.cleanup.tokens.inspect(preview["token"], "apply_rules")
    actual = observe_sim_mutations(sim, grant, operation="MOVE")
    initial_uids = {str(sim.uid_of("INBOX", item.pid)): item for item in items}
    apply = client.cleanup.backend.apply
    chunks = []

    def change_source_after_chunk(*args, **kwargs):
        result = apply(*args, **kwargs)
        chunks.append(len(args[4]))
        monkeypatch.setitem(FOLDER_ATTRS, "INBOX", flag)
        return result

    monkeypatch.setattr(client.cleanup.backend, "apply", change_source_after_chunk)
    result = service.apply_rules(preview["token"])
    assert len(chunks) == 1 and 0 < chunks[0] <= 21
    first_size = chunks[0]
    assert result["count"] == result["done"] == first_size
    assert result["remaining"] == 51 - first_size and result["uncertain"] == 0
    assert result["partial"] and result["token_consumed"]
    assert "chunk preparation" in result["error"]
    assert len(result["log_ids"]) == len(logged(client)) == 1
    assert_bound_progress(client.cleanup, grant, [result], actual)
    moved_uids = {row[2] for row in actual}
    assert len(moved_uids) == first_size
    assert {uid for uid, item in initial_uids.items() if item.folder == DESTINATION} == moved_uids
    assert {str(sim.uid_of("INBOX", item.pid)) for item in items if item.folder == "INBOX"} == (
        set(initial_uids) - moved_uids
    )
    entries = logged(client)[0]["messages"]
    assert {row["uid_after"] for row in entries} == {
        str(sim.uid_of(DESTINATION, initial_uids[uid].pid)) for uid in moved_uids
    }
    assert all((r"\Seen" in initial_uids[uid].flags) is mark_read for uid in moved_uids)
    assert all(not initial_uids[uid].flags for uid in set(initial_uids) - moved_uids)
    commands = mutations(sim)
    moves = [call[3].split()[2] for call in commands if call[3].startswith("UID MOVE ")]
    stores = [call[3].split()[2] for call in commands if call[3].startswith("UID STORE ")]
    # One batched command per chunk names exactly the moved UIDs.
    assert len(moves) == 1 and set(uid_values(moves[0])) == moved_uids
    assert len(stores) == (1 if mark_read else 0)
    assert all(set(uid_values(spec)) == moved_uids for spec in stores)
    before_retry = mutations(sim)
    with pytest.raises(MailValidationError, match="already used"):
        service.apply_rules(preview["token"])
    assert mutations(sim) == before_retry
    continuation = result["continuation_token"]
    assert continuation
    with pytest.raises(RulesSourceError, match="Rules source refused"):
        service.apply_rules(continuation)
    assert mutations(sim) == before_retry
