"""All Mail walk checkpoint cost does not grow with its completed batch count."""

from test_folder_rules import mailbox as mailbox
from test_preview_rules_bounded import assert_read_only
from test_rules_all_mail_walk_resume import BATCH, ROWS, assert_like_the_retest, audit, walk_case

from proton_mail_mcp import cleanup_lookup


class PublishedSnapshots(dict):
    def __init__(self):
        super().__init__()
        self.all_mail_writes = 0

    def __setitem__(self, scope, snapshot):
        if snapshot["inventory"]["snapshot"]["folder"] == "All Mail":
            self.all_mail_writes += 1
        super().__setitem__(scope, snapshot)


def test_partial_walk_checkpoint_count_is_independent_of_completed_batches(mailbox, monkeypatch):
    completed_counts, checkpoint_counts = [], []
    for batch_size in (BATCH, BATCH // 2):
        with monkeypatch.context() as patch:
            case = walk_case(mailbox, patch)
            patch.setattr(cleanup_lookup, "_IDENTITY_BATCH_SIZE", batch_size)
            snapshots = PublishedSnapshots()
            case.cleanup.client._rules_raw_identity_snapshots = snapshots

            result = case.rules.preview_rules()

            assert_like_the_retest(result)
            saved = audit(case, result["next_cursor"])
            assert 1 < saved["next_uid"] < ROWS + 1
            completed = (saved["next_uid"] - 1) // batch_size
            completed_counts.append(completed)
            checkpoint_counts.append(snapshots.all_mail_writes)
            # Only completed FETCHes survive the timed-out batch. Both cursor
            # and cursorless continuations must receive valid final checksums.
            expected = {str(uid) for uid in range(1, saved["next_uid"])}
            assert set(saved["live_uids"]) == expected
            assert {row["uid"] for row in saved["rows"]} == expected
            assert len(case.raw) == completed + 1
            cached = next(
                item["inventory"]
                for item in snapshots.values()
                if item["inventory"]["snapshot"]["folder"] == "All Mail"
            )
            assert cached == saved
            for inventory in (saved, cached):
                assert cleanup_lookup._valid_preview_range_inventory(
                    inventory, saved["options"], "All Mail", saved["scope"]
                )
            assert_read_only(case.fake)

    assert completed_counts == [4, 8]
    assert checkpoint_counts[0] > 0
    assert checkpoint_counts[0] == checkpoint_counts[1], (
        "Publishing the whole inventory must not happen for every completed batch"
    )
