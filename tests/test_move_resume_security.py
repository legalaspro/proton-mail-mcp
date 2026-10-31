"""Move approval retries keep only untouched source handles and a fixed resume deadline."""

import hashlib
import json
import threading

import pytest
from bridge_sim import Msg, hdr, mutations
from test_cleanup_resume import records
from test_cleanup_resume import slow_cleanup as slow_cleanup

from proton_mail_mcp.guarded import TokenStore
from proton_mail_mcp.mail import MailValidationError

ACTION = "move_to_folder"
PARAMS = {"folder": "Folders/Reviewed"}


def identity(row):
    return tuple(row[key] for key in ("folder", "uidvalidity", "uid", "message_id"))


def preview_move(case):
    # Explicit source certification binds every resumed UID to INBOX.
    preview = case.service.preview_action(
        ACTION, query={"folder": "INBOX", "from_": "notifications@example.invalid"}, params=PARAMS
    )
    assert preview["token"] and preview["count"] == len(case.items)
    assert not preview["blocked"] and not mutations(case.peer)
    return preview


def run_summary(case, token, run_id):
    result = case.client.list_actions(token=token)
    return next(run for run in result["runs"] if run["run_id"] == run_id)


def test_rotations_cover_exact_remaining_uids_and_identities_without_new_arrivals(
    slow_cleanup, monkeypatch
):
    case = slow_cleanup(count=65, timeout=24, mutation_seconds=1.5)
    preview = preview_move(case)
    approved = case.service.tokens.inspect(preview["token"], ACTION)
    original = {identity(row) for row in approved.messages}
    original_by_uid = {row["uid"]: row["message_id"] for row in approved.messages}
    resolved = case.service._resolve
    verified = []

    def only_bound(*args, **kwargs):
        bound = kwargs.get("bound_rows")
        assert bound, "Execute and resume must never rerun the preview selector"
        verified.extend(identity(row) for row in bound)
        assert {identity(row) for row in bound} <= original
        return resolved(*args, **kwargs)

    monkeypatch.setattr(case.service, "_resolve", only_bound)
    arrivals, moved = [], []

    def mutation_identity(session, folder, operation, command):
        if operation != "MOVE":
            return
        uid = command.split()[2]
        item = case.peer.messages[case.peer.uidmap[folder][int(uid)]]
        moved.append((folder, str(case.peer.uidvalidity[folder]), uid, item.message_id()))
        assert original_by_uid[uid] == item.message_id()
        if not arrivals:
            arrivals.append(
                case.peer.add(
                    Msg(hdr("<during@example.invalid>", from_="notifications@example.invalid"))
                )
            )

    case.peer.arrival_hook = mutation_identity
    token, results, secrets = preview["token"], [], [preview["token"]]
    while token:
        started = case.clock.elapsed
        result = case.service.execute(ACTION, token)
        assert case.clock.elapsed - started < 50
        assert {"partial", "run_id", "done", "remaining", "uncertain"} <= (result.keys())
        assert result["done"] > 0 and result["uncertain"] == 0
        results.append(result)
        assert len(results) <= preview["estimated_calls"]
        assert len(moved) == len(set(moved)) == sum(item["done"] for item in results)
        assert set(moved) <= original
        logged = [row for chunk in records(case) for row in chunk["messages"]]
        assert len(logged) == len(moved)
        assert all(row["status"] == "done" for row in logged)
        assert {(row["uid_before"], row["message_id"]) for row in logged} == {
            (row[2], row[3]) for row in moved
        }
        if result["resume"]:
            retry = case.service.tokens.inspect(token, ACTION)
            assert {identity(row) for row in retry.messages} == original - set(moved)
        else:
            before = list(mutations(case.peer))
            with pytest.raises(MailValidationError, match="already used"):
                case.service.execute(ACTION, token)
            assert mutations(case.peer) == before
        token = result["continuation_token"]
        run = run_summary(case, preview["token"], result["run_id"])
        endings = [
            row
            for row in map(json.loads, case.path.read_text().splitlines())
            if row["action"] == "run_end" and row["run_id"] == result["run_id"]
        ]
        assert len(endings) == len(results)
        final_event = endings[-1]
        complete = len(moved) == len(original)
        assert final_event["done"] == len(moved)
        assert final_event["remaining"] == len(original) - len(moved)
        assert final_event["status"] == ("completed" if complete else "partial")
        assert final_event["partial"] is not complete
        assert run["done"] == len(moved)
        assert run["remaining"] == len(original) - len(moved)
        if token:
            secrets.append(token)
            assert result["partial"] and result["resume"] and result["error"]
            grant = case.service.tokens.inspect(token, ACTION)
            assert {identity(row) for row in grant.messages} == original - set(moved)
            assert grant.action == approved.action and grant.params == approved.params
            assert grant.selector == approved.selector
            assert grant.expires > approved.expires
            assert grant.expires_at == results[0]["expires_at"]
            assert grant.run_id == results[0]["run_id"]
            assert run["status"] == "partial"
            assert run["continuation"] == {
                "available": True,
                "expires_at": results[0]["expires_at"],
                "remaining": len(grant.messages),
            }
            assert hashlib.sha256(token.encode("ascii")).digest() in case.service.tokens._grants
            if len(arrivals) == 1:
                arrivals.append(
                    case.peer.add(
                        Msg(hdr("<after@example.invalid>", from_="notifications@example.invalid"))
                    )
                )
        else:
            assert not result["partial"] and not result["resume"]
            assert run["status"] == "completed"
            assert run["continuation"] == {"available": False, "expires_at": None, "remaining": 0}
        public = json.dumps(case.client.list_actions()) + case.path.read_text()
        assert all(secret not in public for secret in secrets)
    assert len(results) > 1 and verified and set(moved) == original
    assert len({result["run_id"] for result in results}) == 1
    assert all(item.folder == PARAMS["folder"] for item in case.items)
    assert len(arrivals) == 2 and all(item.folder == "INBOX" for item in arrivals)


@pytest.mark.parametrize(
    "change",
    [
        "missing_identity",
        "duplicate_header",
        "swapped_identity",
        "fingerprint",
        "protected",
        "moved",
        "uidvalidity",
    ],
)
@pytest.mark.parametrize("resume_kind", ["continuation", "original"])
def test_resume_refuses_changed_bound_rows(slow_cleanup, change, resume_kind):
    case = slow_cleanup(count=30, timeout=24, mutation_seconds=2)
    preview = preview_move(case)
    first = case.service.execute(ACTION, preview["token"])
    token = first["continuation_token"] if resume_kind == "continuation" else preview["token"]
    assert token and 0 < first["done"] < len(case.items)
    remaining = case.service.tokens.inspect(token, ACTION).messages
    approved_remainder = {identity(row) for row in remaining}
    changed_row = remaining[0]
    changed = case.peer.messages[case.peer.uidmap["INBOX"][int(changed_row["uid"])]]
    header = f"Message-ID: {changed_row['message_id']}\r\n".encode("ascii")
    if change == "missing_identity":
        changed.headers = changed.headers.replace(header, b"")
    elif change == "duplicate_header":
        changed.headers = header + changed.headers
    elif change == "swapped_identity":
        changed.headers = changed.headers.replace(
            header, b"Message-ID: <swapped@example.invalid>\r\n"
        )
    elif change == "fingerprint":
        changed.headers = changed.headers.replace(b"Subject: Hello", b"Subject: Changed")
    elif change == "protected":
        changed.headers = changed.headers.replace(
            b"notifications@example.invalid", b"synthetic-blocked@gmail.com"
        )
    elif change == "moved":
        changed.folder = "Archive"
        case.peer.sync()
    else:
        case.peer.reset_uidvalidity("INBOX")
    before = len(mutations(case.peer))
    results = []
    while token:
        result = case.service.execute(ACTION, token)
        results.append(result)
        assert len(results) < 10
        token = result["continuation_token"]
        if token:
            grant = case.service.tokens.inspect(token, ACTION)
            assert {identity(row) for row in grant.messages} <= approved_remainder
    final = results[-1]
    assert final["partial"] and not final["resume"] and final["continuation_token"] is None
    blocked = {identity(row) for row in final["blocked"]}
    assert identity(changed_row) in blocked
    if change == "uidvalidity":
        assert blocked == approved_remainder and mutations(case.peer)[before:] == []
        assert final["run_done"] == first["done"]
    else:
        assert final["run_done"] == len(case.items) - 1
        assert final["remaining"] == 1
        assert all(
            command.split()[2] != changed_row["uid"]
            for _, _, _, command in mutations(case.peer)[before:]
        )
        assert changed.folder == ("Archive" if change == "moved" else "INBOX")
    run = run_summary(case, preview["token"], first["run_id"])
    assert run["status"] == "partial"
    assert run["blocked"] == len(blocked)
    assert not run["continuation"]["available"]


@pytest.mark.parametrize("resume_kind", ["continuation", "original"])
def test_resume_wrong_action_folder_and_ids_preserve_capability_until_resume_expiry(
    slow_cleanup, resume_kind
):
    case = slow_cleanup(count=30, timeout=24, mutation_seconds=2)
    preview = preview_move(case)
    original = case.service.tokens.inspect(preview["token"], ACTION)
    first = case.service.execute(ACTION, preview["token"])
    token = first["continuation_token"] if resume_kind == "continuation" else preview["token"]
    assert token
    resumed = case.service.tokens.inspect(token, ACTION)
    assert resumed.expires > original.expires
    before = list(mutations(case.peer))
    for action, kwargs, error in [
        ("archive", {}, "action"),
        (ACTION, {"params": {"folder": "Archive"}}, "params"),
        (ACTION, {"ids": ["<outside@example.invalid>"]}, "message set"),
    ]:
        with pytest.raises(MailValidationError, match=error):
            case.service.execute(action, token, **kwargs)
        grant = case.service.tokens.inspect(token, ACTION)
        assert grant.expires == resumed.expires and grant.expires_at == resumed.expires_at
    with pytest.raises(MailValidationError, match="Invalid"):
        case.service.execute(ACTION, token + "tampered")
    case.clock.elapsed = resumed.expires
    expired_run = run_summary(case, preview["token"], first["run_id"])
    assert expired_run["remaining"] > 0 and not expired_run["continuation"]["available"]
    with pytest.raises(MailValidationError, match="expired"):
        case.service.execute(ACTION, token)
    assert mutations(case.peer) == before
    state = run_summary(case, preview["token"], first["run_id"])["continuation"]
    assert state == {"available": False, "expires_at": None, "remaining": 0}


def test_resume_atomic_claim_has_exactly_one_winner(slow_cleanup):
    case = slow_cleanup(count=30, timeout=24, mutation_seconds=2)
    preview = preview_move(case)
    first = case.service.execute(ACTION, preview["token"])
    token = first["continuation_token"]
    expected = case.service.tokens.inspect(token, ACTION)
    barrier = threading.Barrier(3)
    successes, failures = [], []

    def claim():
        barrier.wait(timeout=10)
        try:
            successes.append(case.service.tokens.claim(token, ACTION))
        except MailValidationError as error:
            failures.append(error)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=10)
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert len(successes) == len(failures) == 1
    assert "already used" in str(failures[0])
    assert {identity(row) for row in successes[0].messages} == {
        identity(row) for row in expected.messages
    }
    assert not run_summary(case, preview["token"], first["run_id"])["continuation"]["available"]


def test_concurrent_resume_executes_each_bound_identity_once(slow_cleanup):
    case = slow_cleanup(count=30, timeout=24, mutation_seconds=2)
    preview = preview_move(case)
    first = case.service.execute(ACTION, preview["token"])
    token = first["continuation_token"]
    approved = case.service.tokens.inspect(token, ACTION)
    expected = {identity(row) for row in approved.messages}
    entered, release = threading.Event(), threading.Event()
    outcome, moved = {}, []

    def hold_first_move(session, folder, operation, command):
        if operation != "MOVE":
            return
        uid = command.split()[2]
        item = case.peer.messages[case.peer.uidmap[folder][int(uid)]]
        moved.append((folder, str(case.peer.uidvalidity[folder]), uid, item.message_id()))
        if not entered.is_set():
            entered.set()
            assert release.wait(10)

    case.peer.arrival_hook = hold_first_move

    def execute():
        try:
            outcome["result"] = case.service.execute(ACTION, token)
        except BaseException as error:
            outcome["error"] = error

    worker = threading.Thread(target=execute)
    worker.start()
    try:
        assert entered.wait(10)
        before = list(mutations(case.peer))
        with pytest.raises(MailValidationError, match="Another cleanup operation"):
            case.service.execute(ACTION, token)
        assert mutations(case.peer) == before
    finally:
        release.set()
        worker.join(timeout=20)
    assert not worker.is_alive() and "error" not in outcome
    result = outcome["result"]
    assert len(moved) == len(set(moved)) == result["done"] > 0
    assert set(moved) <= expected
    if result["continuation_token"]:
        remaining = case.service.tokens.inspect(result["continuation_token"], ACTION)
        assert {identity(row) for row in remaining.messages} == expected - set(moved)
        retry = case.service.tokens.inspect(token, ACTION)
        assert {identity(row) for row in retry.messages} == expected - set(moved)
    else:
        before = list(mutations(case.peer))
        with pytest.raises(MailValidationError, match="already used"):
            case.service.execute(ACTION, token)
        assert mutations(case.peer) == before


def test_restart_cannot_recover_a_resume_secret_from_run_journal(slow_cleanup):
    case = slow_cleanup(count=30, timeout=24, mutation_seconds=2)
    preview = preview_move(case)
    approved = {
        identity(row) for row in case.service.tokens.inspect(preview["token"], ACTION).messages
    }
    first = case.service.execute(ACTION, preview["token"])
    token = first["continuation_token"]
    assert token and token not in case.path.read_text()
    case.service.tokens = TokenStore(clock=case.clock.monotonic)
    run = run_summary(case, preview["token"], first["run_id"])
    assert run["status"] == "partial" and run["remaining"] == 30 - first["done"]
    assert run["continuation"] == {"available": False, "expires_at": None, "remaining": 0}
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute(ACTION, token)
    # Recovery requires a fresh, separately reviewed approval of current rows.
    fresh = case.service.preview_action(
        ACTION, query={"folder": "INBOX", "from_": "notifications@example.invalid"}, params=PARAMS
    )
    grant = case.service.tokens.inspect(fresh["token"], ACTION)
    done = {row["message_id"] for chunk in records(case) for row in chunk["messages"]}
    assert {row["message_id"] for row in grant.messages} == {
        item.message_id() for item in case.items
    } - done
    completed = {
        tuple(
            row[key] for key in ("source_folder", "uidvalidity_before", "uid_before", "message_id")
        )
        for chunk in records(case)
        for row in chunk["messages"]
    }
    assert {identity(row) for row in grant.messages} == approved - completed


def test_run_lookup_after_restart_does_not_initialize_mutation_policy(slow_cleanup, monkeypatch):
    from proton_mail_mcp import guarded
    from proton_mail_mcp.mail import MailClient

    case = slow_cleanup(count=30, timeout=24, mutation_seconds=2)
    preview = preview_move(case)
    first = case.service.execute(ACTION, preview["token"])
    assert first["partial"] and first["resume"]
    restarted = MailClient(case.client.settings)
    monkeypatch.setattr(
        guarded,
        "CleanupService",
        lambda *a, **kw: pytest.fail("Lookup initialized mutation policy"),
    )
    before = list(mutations(case.peer))
    result = restarted.list_actions(token=preview["token"])
    assert restarted._cleanup_service is None
    assert len(result["runs"]) == 1
    run = result["runs"][0]
    assert run["run_id"] == first["run_id"] and run["done"] == first["done"]
    assert run["continuation"] == {"available": False, "expires_at": None, "remaining": 0}
    assert mutations(case.peer) == before


@pytest.mark.parametrize("held_token", ["original", "latest_continuation"])
def test_lost_response_retries_same_run_exact_remainder(slow_cleanup, monkeypatch, held_token):
    case = slow_cleanup(count=45, timeout=24, mutation_seconds=2)
    preview = preview_move(case)
    original = case.service.tokens.inspect(preview["token"], ACTION)
    approved = {identity(row) for row in original.messages}
    observed, results = [], []
    resolve = case.service._resolve
    expected = approved.copy()

    def bound_only(*args, **kwargs):
        bound = kwargs.get("bound_rows")
        assert bound and {identity(row) for row in bound} <= expected
        return resolve(*args, **kwargs)

    monkeypatch.setattr(case.service, "_resolve", bound_only)

    def observe(session, folder, operation, command):
        if operation == "MOVE":
            uid = command.split()[2]
            item = case.peer.messages[case.peer.uidmap[folder][int(uid)]]
            observed.append((folder, str(case.peer.uidvalidity[folder]), uid, item.message_id()))

    case.peer.arrival_hook = observe
    client_token = preview["token"]
    first = case.service.execute(ACTION, client_token)
    results.append(first)
    assert first["partial"] and first["continuation_token"]
    if held_token == "latest_continuation":
        # This response arrived. The next response, issued for its continuation,
        # is lost, leaving this as the client's latest known token.
        client_token = first["continuation_token"]
        expected = approved - set(observed)
        lost = case.service.execute(ACTION, client_token)
        results.append(lost)
        assert lost["partial"] and lost["continuation_token"]
    # The response is discarded by the client. It knows only client_token, never
    # any subsequent secret. A matching new arrival must remain outside the run.
    newcomer = case.peer.add(
        Msg(hdr("<lost-response-arrival@example.invalid>", from_="notifications@example.invalid"))
    )
    while results[-1]["partial"]:
        expected = approved - set(observed)
        retry = case.service.tokens.inspect(client_token, ACTION)
        assert {identity(row) for row in retry.messages} == expected
        assert retry.expires > original.expires and retry.params == original.params
        assert retry.expires_at == first["expires_at"]
        assert retry.run_id == first["run_id"]
        before = set(observed)
        result = case.service.execute(ACTION, client_token)
        results.append(result)
        assert len(results) < 15 and result["done"] > 0
        assert set(observed) - before <= expected
        assert len(observed) == len(set(observed)) == sum(row["done"] for row in results)
        assert set(observed) <= approved
        journaled = {
            (row["source_folder"], row["uidvalidity_before"], row["uid_before"], row["message_id"])
            for record in records(case)
            for row in record["messages"]
            if row["status"] == "done"
        }
        assert journaled == set(observed)
        assert result["run_done"] == len(journaled)
        assert result["remaining"] == len(approved - journaled)
        assert result["uncertain"] == 0
        if result["continuation_token"]:
            continuation = case.service.tokens.inspect(result["continuation_token"], ACTION)
            assert {identity(row) for row in continuation.messages} == approved - journaled
    assert {row["run_id"] for row in results} == {first["run_id"]}
    assert len(observed) == len(approved) == 45 and newcomer.folder == "INBOX"
    assert not results[-1]["resume"] and results[-1]["continuation_token"] is None
    for token in [preview["token"], client_token, *[r["continuation_token"] for r in results[:-1]]]:
        before = list(mutations(case.peer))
        for operation in (case.service.tokens.inspect, case.service.tokens.claim):
            with pytest.raises(MailValidationError, match="already used"):
                operation(token, ACTION)
        with pytest.raises(MailValidationError, match="already used"):
            case.service.execute(ACTION, token)
        assert mutations(case.peer) == before
    public = json.dumps(case.client.list_actions()) + case.path.read_text()
    assert preview["token"] not in public and client_token not in public
    assert not case.service.tokens._grants


def test_original_and_continuation_overlap_before_claim_is_single_flight(slow_cleanup, monkeypatch):
    case = slow_cleanup(count=30, timeout=24, mutation_seconds=2)
    preview = preview_move(case)
    first = case.service.execute(ACTION, preview["token"])
    token = first["continuation_token"]
    approved = case.service.tokens.inspect(token, ACTION)
    expected = {identity(row) for row in approved.messages}
    entered, release = threading.Event(), threading.Event()
    resolve = case.service._resolve
    outcome = {}

    def hold_verification(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return resolve(*args, **kwargs)

    monkeypatch.setattr(case.service, "_resolve", hold_verification)

    def execute():
        try:
            outcome["result"] = case.service.execute(ACTION, token)
        except BaseException as error:
            outcome["error"] = error

    worker = threading.Thread(target=execute)
    worker.start()
    try:
        assert entered.wait(10)
        before = list(mutations(case.peer))
        # Both names of this approval must be refused even before the first
        # operation has reached the atomic mutation claim.
        for retry in [preview["token"], token]:
            with pytest.raises(MailValidationError, match="Another cleanup operation"):
                case.service.execute(ACTION, retry)
        assert mutations(case.peer) == before
    finally:
        release.set()
        worker.join(timeout=20)
    assert not worker.is_alive() and "error" not in outcome
    result = outcome["result"]
    done = {
        (row["source_folder"], row["uidvalidity_before"], row["uid_before"], row["message_id"])
        for record in records(case)
        if record["log_id"] in result["log_ids"]
        for row in record["messages"]
        if row["status"] == "done"
    }
    assert len(done) == result["done"] > 0 and done <= expected
    assert len(mutations(case.peer)) == first["done"] + result["done"]
    retry = case.service.tokens.inspect(preview["token"], ACTION)
    assert {identity(row) for row in retry.messages} == expected - done


def test_lost_response_aliases_not_restored_without_durable_progress(slow_cleanup, monkeypatch):
    case = slow_cleanup(count=30, timeout=24, mutation_seconds=2)
    preview = preview_move(case)
    first = case.service.execute(ACTION, preview["token"])
    token = first["continuation_token"]
    assert token

    def fail_finish(*args, **kwargs):
        raise OSError("Synthetic final journal failure")

    monkeypatch.setattr(case.service.backend.log, "finish_run", fail_finish)
    result = case.service.execute(ACTION, token)
    assert result["partial"] and not result["resume"] and result["continuation_token"] is None
    before = list(mutations(case.peer))
    for retry in (preview["token"], token):
        with pytest.raises(MailValidationError, match="already used"):
            case.service.execute(ACTION, retry)
    assert mutations(case.peer) == before


def test_lost_response_retry_never_retries_uncertain_row(slow_cleanup):
    case = slow_cleanup(count=30, timeout=24, mutation_seconds=2)
    preview = preview_move(case)
    approved = {
        identity(row) for row in case.service.tokens.inspect(preview["token"], ACTION).messages
    }
    attempts = []

    def fail_third(session, folder, operation, command):
        if operation == "MOVE":
            uid = command.split()[2]
            item = case.peer.messages[case.peer.uidmap[folder][int(uid)]]
            attempts.append((folder, str(case.peer.uidvalidity[folder]), uid, item.message_id()))
            if len(attempts) == 3:
                return "NO", "Synthetic uncertain move acknowledgement"
        return None

    case.peer.arrival_hook = fail_third
    first = case.service.execute(ACTION, preview["token"])
    assert first["done"] == 2 and first["uncertain"] == 1 and first["resume"]
    uncertain = attempts[-1]
    results = [first]
    while results[-1]["resume"]:
        retry = case.service.tokens.inspect(preview["token"], ACTION)
        assert {identity(row) for row in retry.messages} == approved - set(attempts)
        results.append(case.service.execute(ACTION, preview["token"]))
        assert len(results) < 15
    assert len(attempts) == len(set(attempts)) == len(approved)
    assert set(attempts) == approved
    assert sum(row["done"] for row in results) == len(approved) - 1
    final = results[-1]
    assert final["partial"] and final["remaining"] == 0 and final["run_uncertain"] == 1
    assert final["continuation_token"] is None
    journaled = [row for record in records(case) for row in record["messages"]]
    done = {row["message_id"] for row in journaled if row["status"] == "done"}
    assert done == {row[3] for row in approved - {uncertain}}
    before = list(mutations(case.peer))
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute(ACTION, preview["token"])
    assert mutations(case.peer) == before
