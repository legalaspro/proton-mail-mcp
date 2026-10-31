"""Continuation source certificates inspect bounded appends and fail closed."""

import pytest
from bridge_sim import Msg, hdr, mutations
from test_cleanup_resume import slow_cleanup as slow_cleanup

ACTION = "move_to_folder"
TARGET = "Folders/Reviewed"


def partial_run(case):
    preview = case.service.preview_action(
        ACTION,
        query={"from_": "notifications@example.invalid"},
        params={"folder": TARGET},
    )
    # A per-call limit leaves a continuation; its full remainder fits one chunk.
    result = case.service.execute(ACTION, preview["token"], limit=5)
    assert result["done"] == 5 and result["continuation_token"]
    return result


def test_unrelated_append_is_checked_once_without_inventory(slow_cleanup):
    case = slow_cleanup(count=30, timeout=24, mutation_seconds=2, batch_size=200)
    first = partial_run(case)
    arrival = case.peer.add(Msg(hdr("<unapproved@example.invalid>"), folder="Archive"))
    arrival_uid = str(case.peer.uid_of("Archive", arrival.pid))
    start = len(case.peer.commands)
    fetch_start = len(case.peer.fetch_batches)
    result = case.service.execute(ACTION, first["continuation_token"], limit=5)
    assert result["done"] > 0 and result["available"]
    assert arrival.folder == "Archive"
    assert not any(
        command.startswith("UID SEARCH") for _, _, _, command in case.peer.commands[start:]
    )
    assert [
        fetched for folder, fetched in case.peer.fetch_batches[fetch_start:] if folder == "Archive"
    ] == [((arrival_uid, arrival.message_id()),)]
    resumed = case.service.tokens.inspect(result["continuation_token"], ACTION)
    assert resumed.implicit_source_progress["Archive"] == str(case.peer.next_uid["Archive"])

    fetch_start = len(case.peer.fetch_batches)
    case.service.execute(ACTION, result["continuation_token"])
    assert not any(folder == "Archive" for folder, _ in case.peer.fetch_batches[fetch_start:])


@pytest.mark.parametrize(
    "change",
    [
        "same_identity",
        "raw_identity",
        "uidvalidity",
        "large_gap",
        "allocation_gap",
        "missing_metadata",
    ],
)
def test_uncertifiable_append_refuses_without_inventory_or_mutation(slow_cleanup, change):
    case = slow_cleanup(count=18, timeout=24, mutation_seconds=2, batch_size=200)
    first = partial_run(case)
    pending = next(item for item in case.items if item.folder == "INBOX")
    if change == "same_identity":
        case.peer.add(Msg(pending.headers, folder="Archive"))
    elif change == "raw_identity":
        case.peer.add(
            Msg(
                b"Message-ID: <contradictory@example.invalid>\r\n"
                + hdr("<unapproved@example.invalid>"),
                folder="Archive",
            )
        )
    elif change == "uidvalidity":
        case.peer.uidvalidity["Archive"] += 1
    elif change == "missing_metadata":
        arrival = case.peer.add(Msg(pending.headers, folder="Archive"))
        case.peer.fetch_omit.add(("Archive", arrival.pid))
    elif change == "allocation_gap":
        case.peer.next_uid["Archive"] += 1
    else:
        case.peer.next_uid["Archive"] += 200
    before = list(mutations(case.peer))
    start = len(case.peer.commands)
    result = case.service.execute(ACTION, first["continuation_token"])
    assert result["done"] == result["uncertain"] == 0
    assert result["remaining"] == first["remaining"] == len(result["blocked"])
    assert not result["available"] and result["continuation_token"] is None
    assert result["continuation_reason"] and result["stop_reason"] and result["error"]
    assert mutations(case.peer) == before
    assert not any(
        command.startswith("UID SEARCH") for _, _, _, command in case.peer.commands[start:]
    )
