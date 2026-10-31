"""Pin cleanup planning and conservative accounting with synthetic backends."""

import pytest
from test_cleanup_chunks import assert_progress, records
from test_cleanup_chunks import chunk_mailbox as chunk_mailbox

from proton_mail_mcp.action_log import new_record
from proton_mail_mcp.mail import MailValidationError


@pytest.mark.parametrize("action,first_chunk", [("move_to_trash", 30), ("remove_label", 20)])
def test_remove_label_recovery_allowance_changes_chunk_capacity(
    chunk_mailbox, monkeypatch, action, first_chunk
):
    case = chunk_mailbox(count=30, timeout=48)
    case.fake.folders["Labels/News"] = dict(case.fake.folders["INBOX"])
    params = {"label": "News"} if action == "remove_label" else {}
    preview = case.service.preview_action(action, ids=case.identifiers, params=params)
    assert preview["token"]
    original = case.service.backend.apply
    sizes = []

    def apply(connection, deadline, action, params, rows, **kwargs):
        sizes.append(len(rows))
        return original(connection, deadline, action, params, rows, **kwargs)

    monkeypatch.setattr(case.service.backend, "apply", apply)
    result = case.service.execute(action, preview["token"])
    # Reserve 4.8 s for the response and 1 s for logging: one batched MOVE (2 s
    # plus 0.05 s per further message) holds all 30, while per-message removals
    # keep their 2.05 s allowance: floor(42.2 / 2.05) = 20.
    assert sizes[0] == first_chunk
    assert_progress(result, done=30, remaining=0)
    assert case.claims == [True]


def test_flagged_backend_result_is_uncertain_not_unchanged(chunk_mailbox, monkeypatch):
    case = chunk_mailbox(count=3)
    case.fake.folders["Labels/News"] = dict(case.fake.folders["INBOX"])
    preview = case.service.preview_action(
        "remove_label", ids=case.identifiers, params={"label": "News"}
    )
    assert preview["token"]

    def apply(
        connection,
        deadline,
        action,
        params,
        rows,
        *,
        before_chunk,
        before_mutation,
        token_digest=None,
    ):
        before_chunk()
        before_mutation()
        # Model a backend interrupted after STORE succeeded but before EXPUNGE:
        # the flagged membership may have changed and must remain uncertain.
        record = new_record(action, params)
        record["partial"] = True
        record["messages"] = [
            {"message_id": row["message_id"], "status": status}
            for row, status in zip(rows, ("done", "flagged", "not_attempted"), strict=True)
        ]
        with case.service.backend.log.transaction() as transaction:
            transaction.append(record)
        return {**record, "count": 1, "error": "Synthetic interrupted label removal"}

    monkeypatch.setattr(case.service.backend, "apply", apply)
    result = case.service.execute("remove_label", preview["token"])
    assert_progress(result, done=1, remaining=1, uncertain=1)
    assert result["log_ids"] == [records(case)[0]["log_id"]]
    assert result["token_consumed"] is True and case.claims == [True]
    assert "token consumed" in result["error"]
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute("remove_label", preview["token"])
