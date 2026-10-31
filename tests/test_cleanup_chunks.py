"""Chunked confirmation uses the real mutator and log against in-memory mail only."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from action_records import execution_log
from action_records import execution_records as replay_records
from resume_assertions import ResumeAudit, identity, preserve_failure_boundary
from test_cleanup_mutations import MUTATIONS, MutatingImap, uid_values
from test_mail import client, message, unquote
from test_preview_proton_shape import WorkClock

from proton_mail_mcp import guarded, mail
from proton_mail_mcp.action_log import LogTransaction
from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import CallDeadlineExceeded, MailError, MailValidationError


@pytest.fixture
def chunk_mailbox(tmp_path, monkeypatch):
    def build(*, count=184, timeout=300, move_seconds=1):
        identifiers = [f"<chunk-{index:03d}@example.test>" for index in range(1, count + 1)]
        items = {str(index): message(identifier) for index, identifier in enumerate(identifiers, 1)}
        fake = MutatingImap(
            {
                "INBOX": items,
                "Trash": {},
                "Archive": {},
                "Labels/News": {},
                "All Mail": dict(items),
            }
        )
        clock = WorkClock()
        monkeypatch.setattr(mail, "time", clock)
        settings = Settings(
            token="offline-chunk-test-" + "x" * 32,
            bridge_user="reader@example.test",
            call_timeout=timeout,
            execute_timeout=timeout,
            action_log_path=str(tmp_path / "synthetic-actions.jsonl"),
        )
        mailbox = client(settings, fake)
        service = mailbox.cleanup
        original_uid = fake.uid
        # Real IMAP UIDs never rewind within one UIDVALIDITY, even when a MOVE
        # empties a folder. Preserve that guarantee while later undo fills it.
        highwater = {
            folder: max(map(int, rows), default=100) for folder, rows in fake.folders.items()
        }

        def slow_move(command, *args):
            result = original_uid(command, *args)
            if command in {"MOVE", "COPY"} and fake.copyuid:
                destination = unquote(args[1])
                validity, before, after = fake.copyuid.decode().split()
                renumbered = []
                for uid in after.split(","):
                    assigned = max(int(uid), highwater[destination] + 1)
                    if str(assigned) != uid:
                        fake.folders[destination][str(assigned)] = fake.folders[destination].pop(
                            uid
                        )
                    highwater[destination] = assigned
                    renumbered.append(str(assigned))
                fake.copyuid = f"{validity} {before} {','.join(renumbered)}".encode()
            if command == "MOVE":
                clock.elapsed += move_seconds
            return result

        monkeypatch.setattr(fake, "uid", slow_move)
        claims = []
        original_claim = service.tokens.claim

        def claim(*args, **kwargs):
            claims.append(True)
            return original_claim(*args, **kwargs)

        monkeypatch.setattr(service.tokens, "claim", claim)
        return SimpleNamespace(
            service=service,
            mailbox=mailbox,
            fake=fake,
            clock=clock,
            identifiers=identifiers,
            claims=claims,
            log=service.backend.log.path,
        )

    return build


def records(case):
    # The cleanup journal; per-call audit records in the same log have their own tests.
    lines = case.log.read_text().splitlines() if case.log.exists() else []
    return [
        record
        for record in replay_records([json.loads(line) for line in lines])
        if record.get("action") != "audit"
    ]


def mutations(case):
    return [call for call in case.fake.calls if call[0] in MUTATIONS]


def moved(case):
    """Every UID named by a batched mutation command, in command order."""
    return [uid for call in mutations(case) for uid in uid_values(call[1])]


def planned(rows):
    """Seconds a batched one-folder chunk is planned to need, with its journal reserve."""
    return (
        guarded.EXECUTE_BATCH_SECONDS
        + (len(rows) - 1) * guarded.EXECUTE_BATCH_SECONDS_PER_MESSAGE
        + guarded.EXECUTE_LOG_RESERVE_SECONDS
    )


def approve(case, *, query=False):
    selector = {"query": {}} if query else {"ids": case.identifiers}
    count = len(case.identifiers)
    preview = case.service.preview_action(
        "move_to_trash", **selector, **({"limit": count} if count > 200 else {})
    )
    assert preview["count"] == count and preview["blocked"] == []
    assert preview["token"]
    return preview["token"]


def assert_progress(result, *, done, remaining, uncertain=0, error_done=None):
    assert result["count"] == result["done"] == done
    assert result["remaining"] == remaining
    assert result["uncertain"] == uncertain
    assert result["partial"] is bool(remaining or uncertain)
    if result["partial"]:
        assert str(done if error_done is None else error_done) in result["error"]
        assert str(remaining) in result["error"]
        assert "unchanged" in result["error"]


@pytest.mark.parametrize("query", [False, True])
def test_450_slow_moves_chunk_log_before_next_chunk_and_undo_one_token(
    chunk_mailbox, monkeypatch, query
):
    # One bounded approval of 450: batched chunks of at most 200, one MOVE each.
    case = chunk_mailbox(count=450)
    token = approve(case, query=query)
    original_apply = case.service.backend.apply
    sizes, deadlines, durable_prefixes = [], [], []

    def apply(connection, deadline, action, params, rows, **kwargs):
        # A subsequent mutation must never begin before prior chunks are durable.
        prior = records(case)
        assert len(prior) == len(sizes)
        assert sum(len(record["messages"]) for record in prior) == sum(sizes)
        if durable_prefixes:
            assert case.log.read_bytes().startswith(durable_prefixes[-1])
        sizes.append(len(rows))
        deadlines.append(deadline)
        assert planned(rows) <= deadline.end - case.clock.elapsed
        assert planned(rows) <= deadline.io_end - case.clock.elapsed
        result = original_apply(connection, deadline, action, params, rows, **kwargs)
        assert len(records(case)) == len(sizes)
        durable_prefixes.append(case.log.read_bytes())
        return result

    monkeypatch.setattr(case.service.backend, "apply", apply)
    monkeypatch.setattr(
        case.service.tokens, "issue", lambda *a, **kw: pytest.fail("Execute issued another token")
    )
    audit = ResumeAudit(case, token)
    audit.finish(token)
    result = audit.combined()
    assert_progress(result, done=450, remaining=0)
    assert sizes == [200, 200, 50]
    assert len({id(deadline) for deadline in deadlines}) == len(audit.results)
    assert case.clock.elapsed == len(sizes)  # One slow MOVE command per chunk.
    assert case.claims == [True] * len(audit.results)
    assert not case.fake.folders["INBOX"] and len(case.fake.folders["Trash"]) == 450
    assert all(call[0] == "MOVE" and call[2] == '"Trash"' for call in mutations(case))
    assert [len(uid_values(call[1])) for call in mutations(case)] == sizes
    assert result["log_ids"] == [record["log_id"] for record in records(case)]
    assert len(set(result["log_ids"])) == len(sizes)
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute("move_to_trash", token)
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute(
            "move_to_trash", token, ids=[*case.identifiers, "<extra@example.test>"]
        )
    for log_id, size in zip(result["log_ids"], sizes, strict=True):
        chunk = next(record for record in records(case) if record["log_id"] == log_id)
        expected_ids = {row["message_id"] for row in chunk["messages"]}
        undo = case.service.undo_action(log_id)
        assert undo["count"] == size and not undo["partial"]
        assert {row["message_id"] for row in undo["messages"]} == expected_ids
        with pytest.raises(MailValidationError, match="already been undone"):
            case.service.undo_action(log_id)
    assert case.log.read_bytes().startswith(durable_prefixes[-1])
    assert len(case.fake.folders["INBOX"]) == 450 and not case.fake.folders["Trash"]


def test_chunk_size_shrinks_to_available_headroom(chunk_mailbox, monkeypatch):
    # 2.42 s of I/O remain after the response and journal reserves: one command
    # (2 s, including its first message) plus 0.05 s for each of eight more.
    case = chunk_mailbox(count=15, timeout=3.8)
    token = approve(case)
    original = case.service.backend.apply
    sizes = []

    def apply(connection, deadline, action, params, rows, **kwargs):
        sizes.append(len(rows))
        assert planned(rows) <= deadline.end - case.clock.elapsed
        assert planned(rows) <= deadline.io_end - case.clock.elapsed
        return original(connection, deadline, action, params, rows, **kwargs)

    monkeypatch.setattr(case.service.backend, "apply", apply)
    first = case.service.execute("move_to_trash", token)
    assert_progress(first, done=9, remaining=6)
    assert first["resume"] and "headroom" in first["error"]
    second = case.service.execute("move_to_trash", first["continuation_token"])
    assert_progress(second, done=6, remaining=0)
    assert sizes == [9, 6]
    assert case.claims == [True, True]


def test_later_chunk_failure_keeps_completed_log_undoable(chunk_mailbox, monkeypatch):
    case = chunk_mailbox()
    token = approve(case)
    preserve_failure_boundary(case, monkeypatch)
    original = case.service.backend.apply
    attempts = []

    def fail_after_fifty(*args, **kwargs):
        attempts.append(True)
        if len(case.fake.folders["Trash"]) == 50:
            assert sum(len(record["messages"]) for record in records(case)) == 50
            raise MailError("Synthetic second chunk preflight failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(case.service.backend, "apply", fail_after_fifty)
    audit = ResumeAudit(case, token)
    audit.finish(token, stop=lambda result: "chunk preparation" in result.get("error", ""))
    result = audit.combined()
    assert_progress(result, done=50, remaining=134, error_done=audit.results[-1]["done"])
    assert len(moved(case)) == 50
    assert set(case.fake.folders["INBOX"]) == {str(index) for index in range(51, 185)}
    assert len(case.fake.folders["Trash"]) == 50
    assert case.claims == [True] * len(audit.results)
    assert result["log_ids"] == [record["log_id"] for record in records(case)]
    assert "preview" in result["error"].lower()
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute("move_to_trash", token, ids=case.identifiers[50:])
    prior = case.log.read_bytes()
    undone = 0
    for log_id in result["log_ids"]:
        logged = next(record for record in records(case) if record["log_id"] == log_id)
        undo = case.service.undo_action(log_id)
        assert undo["count"] == len(logged["messages"]) and not undo["partial"]
        assert {row["message_id"] for row in undo["messages"]} == {
            row["message_id"] for row in logged["messages"]
        }
        undone += undo["count"]
    assert undone == 50 and case.log.read_bytes().startswith(prior)
    assert len(case.fake.folders["INBOX"]) == 184 and not case.fake.folders["Trash"]


def test_failure_within_chunk_logs_done_uncertain_and_untouched_separately(
    chunk_mailbox, monkeypatch
):
    # Chunks of 50: the second batched MOVE loses its acknowledgement. Its whole
    # UID set is uncertain; the first chunk stays done and later rows untouched.
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 50)
    case = chunk_mailbox()
    token = approve(case)
    original = case.fake.uid
    attempted = []

    def fail_move(command, *args):
        if command == "MOVE":
            attempted.append(args[0])
            if len(attempted) == 2:
                raise CallDeadlineExceeded("Synthetic lost MOVE acknowledgement")
        return original(command, *args)

    monkeypatch.setattr(case.fake, "uid", fail_move)
    audit = ResumeAudit(case, token)
    audit.finish(token, stop=lambda result: result["uncertain"] > 0)
    result = audit.combined()
    assert_progress(
        result, done=50, remaining=84, uncertain=50, error_done=audit.results[-1]["done"]
    )
    assert attempted == ["1:50", "51:100"] and moved(case) == [str(uid) for uid in range(1, 51)]
    assert len(records(case)) == 2
    assert result["log_ids"] == [record["log_id"] for record in records(case)]
    assert [row["status"] for row in records(case)[0]["messages"]] == ["done"] * 50
    assert [row["status"] for row in records(case)[-1]["messages"]] == ["uncertain"] * 50
    assert set(case.fake.folders["INBOX"]) == {str(index) for index in range(51, 185)}
    assert "uncertain" in result["error"]
    assert case.claims == [True] * len(audit.results)
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute("move_to_trash", token)
    assert audit.uncertain == {row for row in audit.approved if 51 <= int(row[2]) <= 100}
    continuation = case.service.tokens.inspect(result["continuation_token"], "move_to_trash")
    assert {row["uid"] for row in continuation.messages} == {
        str(index) for index in range(101, 185)
    }

    assert audit.undo_done() == 50
    assert len(case.fake.folders["INBOX"]) == 184 and not case.fake.folders["Trash"]


def test_later_chunk_root_log_failure_prevents_that_chunk_from_moving(chunk_mailbox, monkeypatch):
    case = chunk_mailbox()
    token = approve(case)
    preserve_failure_boundary(case, monkeypatch)
    original = LogTransaction.append
    roots = []

    def fail_second_root(transaction, record):
        if record["action"] == "move_to_trash":
            roots.append(record)
            if len(case.fake.folders["Trash"]) == 50:
                raise OSError("Synthetic journal write failure")
        return original(transaction, record)

    monkeypatch.setattr(LogTransaction, "append", fail_second_root)
    audit = ResumeAudit(case, token)
    audit.finish(token, stop=lambda result: "chunk preparation" in result.get("error", ""))
    result = audit.combined()
    assert_progress(result, done=50, remaining=134, error_done=audit.results[-1]["done"])
    assert "unlogged" not in result
    assert sum(len(record["messages"]) for record in records(case)) == 50
    assert len(moved(case)) == 50
    assert result["log_ids"] == [record["log_id"] for record in records(case)]
    assert set(case.fake.folders["INBOX"]) == {str(index) for index in range(51, 185)}
    assert "chunk preparation" in result["error"]
    assert case.claims == [True] * len(audit.results)

    assert audit.undo_done() == 50
    assert len(case.fake.folders["INBOX"]) == 184 and not case.fake.folders["Trash"]


def test_headroom_exhaustion_stops_before_next_chunk_and_consumes_token(chunk_mailbox, monkeypatch):
    # Chunks of ten; the first MOVE takes 12 s, so the observed command cost
    # leaves no safe headroom for a second chunk in the 21.6 s I/O window.
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 10)
    case = chunk_mailbox(count=25, timeout=24, move_seconds=12)
    token = approve(case)
    result = case.service.execute("move_to_trash", token)
    assert_progress(result, done=10, remaining=15)
    assert len(records(case)) == 1 and len(moved(case)) == 10 and len(mutations(case)) == 1
    assert case.clock.elapsed == 12
    assert set(case.fake.folders["INBOX"]) == {str(index) for index in range(11, 26)}
    assert "headroom" in result["error"]
    assert "retry when Bridge is idle" not in result["error"]
    assert "token consumed" in result["error"]
    assert "request a new preview" in result["error"]
    assert case.claims == [True]
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute("move_to_trash", token)


def test_no_headroom_for_one_chunk_keeps_token_without_mutation_or_log(chunk_mailbox):
    case = chunk_mailbox(count=2, timeout=30)
    token = approve(case)
    # Preview now checks the minimum execute reserve too. Lower the budget
    # after approval to isolate the execution guard and its retry behavior.
    case.mailbox.settings = replace(case.mailbox.settings, call_timeout=2.9, execute_timeout=2.9)
    case.service.settings = case.mailbox.settings
    with pytest.raises(MailValidationError, match="execute headroom") as error:
        case.service.execute("move_to_trash", token)
    assert "no messages were changed" in str(error.value)
    assert "PROTON_MCP_EXECUTE_TIMEOUT" in str(error.value)
    assert not case.claims and not mutations(case) and not execution_log(case.log)
    case.service.tokens.inspect(token, "move_to_trash")
    case.mailbox.settings = replace(case.mailbox.settings, call_timeout=30, execute_timeout=30)
    case.service.settings = case.mailbox.settings
    assert_progress(case.service.execute("move_to_trash", token), done=2, remaining=0)


@pytest.mark.parametrize("phase", ["setup", "verification"])
@pytest.mark.parametrize("elapsed", [36.01, 37.49])
def test_execute_uses_safe_headroom_after_eighty_percent(
    chunk_mailbox, monkeypatch, phase, elapsed
):
    case = chunk_mailbox(count=1, timeout=45)
    token = approve(case)
    owner, method = (
        (case.service.backend, "validate_target")
        if phase == "setup"
        else (case.service, "_resolve")
    )
    original = getattr(owner, method)
    delayed = False

    def slow_read(*args, **kwargs):
        nonlocal delayed
        result = original(*args, **kwargs)
        if not delayed:
            case.clock.elapsed += elapsed
            delayed = True
        return result

    monkeypatch.setattr(owner, method, slow_read)
    assert elapsed > 45 * 0.8
    assert 45 * 0.9 - elapsed >= planned([None])
    result = case.service.execute("move_to_trash", token)
    assert_progress(result, done=1, remaining=0)
    assert case.clock.elapsed == elapsed + 1
    assert moved(case) == ["1"] and case.claims == [True]
    assert records(case)[0]["messages"][0]["status"] == "done"


@pytest.mark.parametrize("elapsed", [80, 80.01])
def test_response_budget_yields_before_claim_and_preserves_approval(
    chunk_mailbox, monkeypatch, elapsed
):
    case = chunk_mailbox(count=1, timeout=100)
    token = approve(case)
    original = case.service._resolve

    work_left = elapsed

    def slow_resolution(connection, deadline, *args, **kwargs):
        nonlocal work_left
        rows = original(connection, deadline, *args, **kwargs)
        # Retain the original read-only work across retries while yielding at
        # the actual I/O deadline, independent of elapsed-budget percentages.
        work = min(work_left, max(0, deadline.io_end - case.clock.elapsed))
        work_left -= work
        case.clock.elapsed += work
        if work_left:
            deadline.check(connection)
        return rows

    monkeypatch.setattr(case.service, "_resolve", slow_resolution)
    first = case.service.execute("move_to_trash", token)
    assert first["partial"] and first["done"] == 0 and first["remaining"] == 1
    assert first["error_code"] == "first_chunk_unavailable"
    assert not first["token_consumed"] and not first["resume"]
    assert first["continuation_token"] is None
    assert not mutations(case) and not case.claims and not execution_log(case.log)
    case.service.tokens.inspect(token, "move_to_trash")
    audit = ResumeAudit(case, token)
    audit.finish(token)
    assert_progress(audit.combined(), done=1, remaining=0)
    assert work_left == 0 and case.clock.elapsed == elapsed + 1


def test_chunk_headroom_rechecked_after_preflight_before_claim(chunk_mailbox, monkeypatch):
    case = chunk_mailbox(count=20, timeout=3.8)
    token = approve(case)
    original = case.service.backend._preflight

    def slow_preflight(*args, **kwargs):
        original(*args, **kwargs)
        # The I/O budget leaves 2.42 s after the response and journal reserves:
        # nine messages fit one command. Slow preflight leaves room for five.
        case.clock.elapsed += 0.2

    monkeypatch.setattr(case.service.backend, "_preflight", slow_preflight)
    result = case.service.execute("move_to_trash", token)
    assert_progress(result, done=5, remaining=15)
    assert case.claims == [True] and moved(case) == ["1", "2", "3", "4", "5"]
    chunks = records(case)
    assert [len(record["messages"]) for record in chunks] == [5]
    assert [row["uid_before"] for record in chunks for row in record["messages"]] == [
        "1",
        "2",
        "3",
        "4",
        "5",
    ]
    assert all(row["status"] == "done" for record in chunks for row in record["messages"])
    assert set(case.fake.folders["INBOX"]) == {str(index) for index in range(6, 21)}
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute("move_to_trash", token)


def test_preflight_exhaustion_keeps_token_unclaimed_without_journal(chunk_mailbox, monkeypatch):
    case = chunk_mailbox(count=2, timeout=6)
    token = approve(case)
    original = case.service.backend._preflight

    def exhausted_preflight(*args, **kwargs):
        original(*args, **kwargs)
        # Less than one mutation plus its journal reserve remains.
        case.clock.elapsed += 2.41

    monkeypatch.setattr(case.service.backend, "_preflight", exhausted_preflight)
    with pytest.raises(MailValidationError, match="execute headroom"):
        case.service.execute("move_to_trash", token)
    assert not case.claims and not mutations(case) and not records(case)
    assert set(case.fake.folders["INBOX"]) == {"1", "2"}
    case.service.tokens.inspect(token, "move_to_trash")


def test_preflight_prefix_frozen_before_journal_and_final_claim(chunk_mailbox, monkeypatch):
    case = chunk_mailbox(count=20, timeout=3.8)
    token = approve(case)
    original_preflight = case.service.backend._preflight
    original_fetch = case.fake.uid
    original_record = case.service.backend._row_record
    built_rows = []

    def slow_preflight(*args, **kwargs):
        original_preflight(*args, **kwargs)
        case.clock.elapsed += 0.2

    def row_record(row, destination):
        built_rows.append(row["uid"])
        return original_record(row, destination)

    def exhausted_final_fetch(command, *args):
        result = original_fetch(command, *args)
        if command == "FETCH" and built_rows:
            # Preparation already froze five rows. A final check that loses its
            # headroom must leave that exact journal prefix unattempted.
            case.clock.elapsed += 2.0
        return result

    monkeypatch.setattr(case.service.backend, "_preflight", slow_preflight)
    monkeypatch.setattr(case.service.backend, "_row_record", row_record)
    monkeypatch.setattr(case.fake, "uid", exhausted_final_fetch)
    result = case.service.execute("move_to_trash", token)
    assert_progress(result, done=0, remaining=20)
    assert "headroom" in result["error"]
    assert built_rows == ["1", "2", "3", "4", "5"]
    assert not case.claims and not mutations(case)
    chunks = records(case)
    assert len(chunks) == 1
    assert [(row["uid_before"], row["status"]) for row in chunks[0]["messages"]] == [
        (uid, "not_attempted") for uid in ("1", "2", "3", "4", "5")
    ]
    assert set(case.fake.folders["INBOX"]) == {str(index) for index in range(1, 21)}
    case.service.tokens.inspect(token, "move_to_trash")


def test_chunk_headroom_rechecked_after_final_uid_fetch_before_claim(chunk_mailbox, monkeypatch):
    # 2.24 s of I/O after the reserves fit a two-message command (2.05 s) until
    # the final per-UID FETCH spends 0.41 s.
    case = chunk_mailbox(count=2, timeout=3.6)
    token = approve(case)
    original_select, original_uid = case.fake.select, case.fake.uid
    writable_selects = []

    def select(folder, *, readonly):
        if not readonly:
            writable_selects.append(True)
        return original_select(folder, readonly=readonly)

    def slow_final_fetch(command, *args):
        result = original_uid(command, *args)
        if command == "FETCH" and len(writable_selects) == 2:
            case.clock.elapsed += 0.41
        return result

    monkeypatch.setattr(case.fake, "select", select)
    monkeypatch.setattr(case.fake, "uid", slow_final_fetch)
    result = case.service.execute("move_to_trash", token)
    assert_progress(result, done=0, remaining=2)
    assert "headroom" in result["error"]
    assert not case.claims and not mutations(case)
    assert len(records(case)) == 1
    assert all(row["status"] == "not_attempted" for row in records(case)[0]["messages"])
    case.service.tokens.inspect(token, "move_to_trash")


@pytest.mark.parametrize("change", ["fingerprint", "protected"])
def test_between_chunk_revalidation_refuses_changed_remaining_set(
    chunk_mailbox, monkeypatch, change
):
    # One batched chunk would hold all 184 rows; keep a chunk boundary.
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 50)
    case = chunk_mailbox()
    token = approve(case)
    original = case.service.backend.apply

    first_size = []

    def change_after_first(*args, **kwargs):
        result = original(*args, **kwargs)
        assert len(records(case)) == 1, "Another chunk reached mutation after identity changed"
        first_size.append(result["count"])
        next_uid = str(first_size[0] + 1)
        if change == "fingerprint":
            case.fake.folders["INBOX"]["1001"] = case.fake.folders["INBOX"].pop(next_uid)
        else:
            item = case.fake.folders["INBOX"][next_uid]
            item.headers = item.headers.replace(b"sender@example.com", b"person@gmail.com")
        return result

    monkeypatch.setattr(case.service.backend, "apply", change_after_first)
    audit = ResumeAudit(case, token)
    result = audit.execute(token)
    assert_progress(result, done=first_size[0], remaining=184 - first_size[0])
    assert len(moved(case)) == first_size[0] and len(records(case)) == 1
    assert set(moved(case)) == {str(index) for index in range(1, first_size[0] + 1)}
    assert case.claims == [True]
    assert "preview" in result["error"].lower()
    assert result["stop_reason"] == "Cleanup interrupted during chunk preparation"
    continuation = case.service.tokens.inspect(result["continuation_token"], "move_to_trash")
    assert {identity(row) for row in continuation.messages} == audit.approved - audit.done
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute("move_to_trash", token)

    # The rotating continuation must refuse the changed row as well, while
    # retaining only the original approved identities for any later work.
    monkeypatch.setattr(case.service.backend, "apply", original)
    final = audit.finish(result["continuation_token"])
    blocked_uid = str(first_size[0] + 1)
    assert {row["uid"] for row in final["blocked"]} == {blocked_uid}
    assert final["remaining"] == 1 and len(audit.done) == 183
    assert blocked_uid not in set(moved(case))
    assert set(case.fake.folders["INBOX"]) == {"1001" if change == "fingerprint" else blocked_uid}


def test_explicit_confirmation_ids_cannot_widen_chunked_grant(chunk_mailbox):
    case = chunk_mailbox()
    token = approve(case)
    with pytest.raises(MailValidationError, match="message set"):
        case.service.execute(
            "move_to_trash", token, ids=[*case.identifiers, "<extra@example.test>"]
        )
    assert not mutations(case) and not case.claims and not execution_log(case.log)
    case.service.tokens.inspect(token, "move_to_trash")


def test_query_arrival_between_chunks_stays_outside_bound_set(chunk_mailbox, monkeypatch):
    case = chunk_mailbox()
    token = approve(case, query=True)
    original = case.service.backend.apply
    arrival = message("<unapproved@example.test>")

    def arrive_after_chunk(*args, **kwargs):
        result = original(*args, **kwargs)
        case.fake.folders["INBOX"]["1001"] = arrival
        return result

    monkeypatch.setattr(case.service.backend, "apply", arrive_after_chunk)
    audit = ResumeAudit(case, token)
    audit.finish(token)
    result = audit.combined()
    assert_progress(result, done=184, remaining=0)
    assert case.fake.folders["INBOX"] == {"1001": arrival}
    assert len(moved(case)) == 184 and case.claims == [True] * len(audit.results)


@pytest.mark.parametrize("arrival", [False, True])
def test_query_add_label_skips_completed_memberships_and_leaves_arrivals_untouched(
    chunk_mailbox, monkeypatch, arrival
):
    case = chunk_mailbox(count=101)
    preview = case.service.preview_action("add_label", query={}, params={"label": "News"})
    assert preview["token"] and preview["count"] == 101
    original = case.service.backend.apply
    sizes = []

    def apply(*args, **kwargs):
        result = original(*args, **kwargs)
        sizes.append(result["count"])
        if arrival and len(sizes) == 1:
            case.fake.folders["INBOX"]["1001"] = message("<unapproved@example.test>")
        return result

    monkeypatch.setattr(case.service.backend, "apply", apply)
    audit = ResumeAudit(case, preview["token"], action="add_label")
    audit.finish(preview["token"])
    result = audit.combined()
    assert_progress(result, done=101, remaining=0)
    assert sum(sizes) == 101 and all(0 < size <= 50 for size in sizes)
    assert len(case.fake.folders["Labels/News"]) == 101
    if arrival:
        assert "1001" in case.fake.folders["INBOX"]
        assert case.fake.folders["INBOX"]["1001"] not in case.fake.folders["Labels/News"].values()
    assert case.claims == [True] * len(audit.results)
    assert all(call[0] == "COPY" for call in mutations(case))


def test_chunking_does_not_raise_batch_cap_or_enable_permanent_delete(chunk_mailbox):
    case = chunk_mailbox(count=201)
    preview = case.service.preview_action("move_to_trash", ids=case.identifiers)
    assert preview["count"] == 201 and preview["token"] is None
    with pytest.raises(MailValidationError, match="Unsupported cleanup action"):
        case.service.preview_action("permanent_delete", ids=case.identifiers[:1])
    assert not mutations(case) and not execution_log(case.log)


def test_long_run_cleans_up_other_expired_previews_without_another_lookup(
    chunk_mailbox, monkeypatch
):
    # One-message chunks keep the run long enough (one slow MOVE per message)
    # to outlast the 60 s preview lifetime.
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 1)
    case = chunk_mailbox()
    extra = message("<unrelated-preview@example.test>")
    case.fake.folders["INBOX"]["185"] = extra
    case.fake.folders["All Mail"]["185"] = extra
    case.service.tokens.clock = lambda: case.clock.elapsed
    case.service.tokens.ttl = 60
    token = approve(case)
    other = case.service.preview_action("archive", ids=["<unrelated-preview@example.test>"])
    assert other["token"] and len(case.service.tokens._grants) == 2
    original = {
        identity(row) for row in case.service.tokens.inspect(token, "move_to_trash").messages
    }
    monkeypatch.setattr(
        case.service, "preview_action", lambda *a, **kw: pytest.fail("Run requested a new preview")
    )
    audit = ResumeAudit(case, token)
    pruned_other = False
    while audit.done != original:
        result = audit.execute(token)
        if not result["partial"]:
            break
        token = result["continuation_token"]
        assert token and result["resume"]
        if case.clock.elapsed >= audit.grant.expires:
            # Prune unrelated expired previews without a lookup of their token,
            # while retaining the same run's fixed-lifetime remaining subset.
            grants = list(case.service.tokens._grants.values())
            assert len(grants) == 1 and grants[0].action == "move_to_trash"
            assert {identity(row) for row in grants[0].messages} == original - audit.done
            assert grants[0].expires == audit.resume_expiry
            pruned_other = True
    assert pruned_other and not case.service.tokens._grants
    assert audit.done == original and len(moved(case)) == 184
    assert len(set(moved(case))) == 184
    assert case.clock.elapsed == 184
    assert set(case.fake.folders["INBOX"]) == {"185"}
