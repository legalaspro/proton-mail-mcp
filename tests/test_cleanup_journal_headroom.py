"""A rounded chunk size must still leave the durable journal its own budget."""

from test_cleanup_chunks import approve, moved, planned, records
from test_cleanup_chunks import chunk_mailbox as chunk_mailbox

from proton_mail_mcp import guarded


def test_safe_small_tail_is_processed_before_handing_off_remainder(chunk_mailbox, monkeypatch):
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 10)
    case = chunk_mailbox(count=25, timeout=4.8, move_seconds=1)
    token = approve(case)
    original = case.service.backend.apply
    sizes = []

    def apply(connection, deadline, action, params, rows, **kwargs):
        assert planned(rows) <= deadline.io_end - case.clock.elapsed
        sizes.append(len(rows))
        return original(connection, deadline, action, params, rows, **kwargs)

    monkeypatch.setattr(case.service.backend, "apply", apply)
    first = case.service.execute("move_to_trash", token)
    # After ten rows, seven still fit their own complete command, identity,
    # journal and response allowances. A continuation receives only the last eight.
    assert sizes == [10, 7]
    assert first["done"] == 17 and first["remaining"] == 8 and first["resume"]
    continuation = case.service.tokens.inspect(first["continuation_token"], "move_to_trash")
    assert [row["uid"] for row in continuation.messages] == [str(uid) for uid in range(18, 26)]
    assert [len(record["messages"]) for record in records(case)] == sizes
    second = case.service.execute("move_to_trash", first["continuation_token"])
    assert second["done"] == 8 and not second["partial"] and not second["resume"]
    assert sizes == [10, 7, 8]
    assert moved(case) == [str(uid) for uid in range(1, 26)]
    assert all(row["status"] == "done" for record in records(case) for row in record["messages"])


def test_chunk_never_spends_the_journal_reserve(chunk_mailbox, monkeypatch):
    case = chunk_mailbox(count=15, timeout=3.8, move_seconds=1)
    token = approve(case)
    original = case.service.backend.apply
    started = []

    def apply(connection, deadline, action, params, rows, **kwargs):
        # A 3.8 second call retains a 0.38 second response margin. With 3.42
        # seconds of I/O, a batched chunk of ten (2 s for its command and first
        # message, 0.05 s for each further one) leaves too little time for the
        # required 1 second durable checkpoint.
        available = deadline.io_end - case.clock.elapsed
        assert planned(rows) <= available
        started.append(len(rows))
        return original(connection, deadline, action, params, rows, **kwargs)

    monkeypatch.setattr(case.service.backend, "apply", apply)
    result = case.service.execute("move_to_trash", token)
    assert result["done"] == 9 and result["partial"] and result["resume"]
    assert started == [9]
    second = case.service.execute("move_to_trash", result["continuation_token"])
    assert second["done"] == 6 and not second["partial"]
    assert started == [9, 6]
