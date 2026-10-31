"""Identity and durable-progress assertions shared by bounded-call regressions."""

import json

import pytest
from action_records import execution_records as replay_records
from test_cleanup_mutations import uid_values

from proton_mail_mcp.mail import MailValidationError


def identity(row):
    return (row["folder"], row["uidvalidity"], row["uid"], row["message_id"])


def journal_identity(row):
    return (
        row["source_folder"],
        row["uidvalidity_before"],
        row["uid_before"],
        row["message_id"],
    )


class ResumeAudit:
    """Check every returned boundary against the original approved UID identities."""

    def __init__(self, case, token, *, action="move_to_trash", durable_intent=False):
        self.case, self.action = case, action
        self.grant = case.service.tokens.inspect(token, action)
        self.approved = {identity(row) for row in self.grant.messages}
        self.results, self.tokens = [], []
        self.resume_expiry = self.grant.expires if self.grant.resume_started else None
        self.durable_intent = durable_intent
        self.done, self.uncertain, self.blocked = set(), set(), set()
        self.calls_start = len(case.fake.calls)
        self.initial_targets = {
            folder: {id(item) for item in rows.values()}
            for folder, rows in case.fake.folders.items()
        }
        self.initial_items = {
            identity(row): case.fake.folders[row["folder"]][row["uid"]]
            for row in self.grant.messages
        }

    def check(self, token, result, started):
        case = self.case
        assert case.clock.elapsed - started < 50
        assert result["run_id"] and result["uncertain"] >= 0
        assert result["approved_total"] == len(self.approved)
        if self.results:
            assert result["run_id"] == self.results[0]["run_id"]
        current_done = {
            journal_identity(row) for row in result["messages"] if row["status"] == "done"
        }
        current_uncertain = {
            journal_identity(row)
            for row in result["messages"]
            if row["status"] not in {"done", "not_attempted"}
        }
        assert len(current_done) == sum(row["status"] == "done" for row in result["messages"])
        assert current_done <= self.approved and not (current_done & self.done)
        assert len(current_done) == result["done"]
        assert len(current_uncertain) == result["uncertain"]
        assert current_uncertain <= self.approved
        assert not (current_uncertain & (self.done | self.uncertain | current_done))
        self.done |= current_done
        self.uncertain |= current_uncertain
        for blocked in result["blocked"]:
            self.blocked |= {row for row in self.approved if row[-1] == blocked["message_id"]}
        assert result["run_done"] == len(self.done)
        assert result["run_uncertain"] == len(self.uncertain)
        assert result["remaining"] == len(self.approved - self.done - self.uncertain)
        if not result["partial"]:
            assert self.done == self.approved and not self.uncertain
            assert result["continuation_token"] is None and not result["resume"]
        else:
            assert result["stop_reason"]
        if result["continuation_token"]:
            continuation = case.service.tokens.inspect(result["continuation_token"], self.action)
            assert {identity(row) for row in continuation.messages} == (
                self.approved - self.done - self.uncertain - self.blocked
            )
            if self.resume_expiry is None:
                assert continuation.expires > self.grant.expires
                self.resume_expiry = continuation.expires
            assert continuation.expires == self.resume_expiry
            assert continuation.params == self.grant.params
            assert continuation.selector == self.grant.selector
        assert result["resume"] is bool(result["continuation_token"])
        self.results.append(result)
        self.tokens.append(token)
        self.assert_journal(durable_intent=self.durable_intent)
        self.assert_actual()

    def assert_journal(self, *, durable_intent=False):
        case = self.case
        raw = (
            [json.loads(line) for line in case.log.read_text().splitlines()]
            if case.log.exists()
            else []
        )
        chunks = [
            row
            for row in replay_records(raw)
            if row["action"] == self.action and row.get("run_id") == self.results[0]["run_id"]
        ]
        journal_done = {
            journal_identity(row)
            for chunk in chunks
            for row in chunk["messages"]
            if row["status"] == "done"
        }
        if durable_intent:
            # A failed outcome append retains the exact pre-MOVE reservation.
            intents = {
                journal_identity(row)
                for chunk in chunks
                for row in chunk["messages"]
                if row["status"] not in {"done", "not_attempted"}
            }
            assert journal_done <= self.done
            assert journal_done | intents == self.done | self.uncertain
        else:
            assert journal_done == self.done
        asserted = [
            journal_identity(row)
            for chunk in chunks
            for row in chunk["messages"]
            if row["status"] != "not_attempted"
        ]
        assert len(asserted) == len(set(asserted))
        assert set(asserted) <= self.approved
        runs = case.service.backend.log.list_actions()["runs"]
        if runs:
            run = next(row for row in runs if row["run_id"] == self.results[0]["run_id"])
            assert (run["status"] == "completed") == (len(self.done) == len(self.approved))

    def assert_actual(self):
        """The fake's successful commands independently prove UID and object identity."""
        calls = self.case.fake.calls[self.calls_start :]
        writes = [call for call in calls if call[0] in {"MOVE", "COPY"}]
        # Batched commands name UID sets; every done UID was named exactly once.
        named = [uid for call in writes for uid in uid_values(call[1])]
        assert len(named) == len(self.done)
        assert len(set(named)) == len(named)
        assert set(named) == {row[2] for row in self.done}
        target = self.grant.params.get("folder")
        if self.action == "move_to_trash":
            target = "Trash"
        elif self.action == "add_label":
            target = "Labels/" + self.grant.params["label"]
        if target:
            actual = {id(item) for item in self.case.fake.folders[target].values()}
            assert actual - self.initial_targets.get(target, set()) == {
                id(self.initial_items[row]) for row in self.done
            }
        for folder in {row[0] for row in self.approved}:
            moved = {id(self.initial_items[row]) for row in self.done if row[0] == folder}
            remaining = {id(item) for item in self.case.fake.folders[folder].values()}
            expected = self.initial_targets[folder]
            if self.action in {"move_to_trash", "move_to_folder", "archive"}:
                expected = expected - moved
            # Newly arrived rows may remain, but every approved source object is exact.
            approved_objects = {id(item) for item in self.initial_items.values()}
            assert remaining & approved_objects == expected & approved_objects

    def execute(self, token, *, invoke=None):
        started = self.case.clock.elapsed
        result = self.case.service.execute(self.action, token) if invoke is None else invoke(token)
        self.check(token, result, started)
        return result

    def finish(self, token, *, invoke=None, stop=None):
        for _ in range(40):
            result = self.execute(token, invoke=invoke)
            if not result["resume"] or (stop is not None and stop(result)):
                return result
            token = result["continuation_token"]
        pytest.fail("Bounded continuation did not finish")

    def undo_done(self):
        """Each physical log restores exactly its actually-done identities once."""
        chunks = replay_records(
            [json.loads(line) for line in self.case.log.read_text().splitlines()]
        )
        restored = set()
        for log_id in self.combined()["log_ids"]:
            chunk = next(row for row in chunks if row.get("log_id") == log_id)
            expected = {journal_identity(row) for row in chunk["messages"]} & self.done
            calls_before = len(self.case.fake.calls)
            source_before = {id(item) for item in self.case.fake.folders["INBOX"].values()}
            destination_before = {id(item) for item in self.case.fake.folders["Trash"].values()}
            restored_objects = {id(self.initial_items[row]) for row in expected}
            undo_uids = {
                uid
                for uid, item in self.case.fake.folders["Trash"].items()
                if id(item) in restored_objects
            }
            result = self.case.service.undo_action(log_id)
            undo_writes = [
                call
                for call in self.case.fake.calls[calls_before:]
                if call[0] in {"MOVE", "COPY", "STORE", "EXPUNGE"}
            ]
            assert len(undo_writes) == len(undo_uids) == len(expected)
            assert {(call[0], call[1], call[2]) for call in undo_writes} == {
                ("MOVE", uid, '"INBOX"') for uid in undo_uids
            }
            assert {id(item) for item in self.case.fake.folders["INBOX"].values()} == (
                source_before | restored_objects
            )
            assert {id(item) for item in self.case.fake.folders["Trash"].values()} == (
                destination_before - restored_objects
            )
            actual_ids = {
                row["message_id"] for row in result["messages"] if row["status"] == "done"
            }
            assert result["count"] == len(expected)
            assert actual_ids == {row[-1] for row in expected}
            assert not (restored & expected)
            restored |= expected
            writes_before = len(
                [
                    call
                    for call in self.case.fake.calls
                    if call[0] in {"MOVE", "COPY", "STORE", "EXPUNGE"}
                ]
            )
            refusal = "unresolved messages" if result["partial"] else "already been undone"
            with pytest.raises(MailValidationError, match=refusal):
                self.case.service.undo_action(log_id)
            assert (
                len(
                    [
                        call
                        for call in self.case.fake.calls
                        if call[0] in {"MOVE", "COPY", "STORE", "EXPUNGE"}
                    ]
                )
                == writes_before
            )
        assert restored == self.done
        return len(restored)

    def combined(self):
        result = dict(self.results[-1])
        result["done"] = result["count"] = len(self.done)
        result["uncertain"] = len(self.uncertain)
        result["messages"] = [row for call in self.results for row in call["messages"]]
        result["log_ids"] = [log_id for result in self.results for log_id in result["log_ids"]]
        return result


def preserve_failure_boundary(case, monkeypatch, *, boundary=50):
    """Retain an original injected row boundary while allowing smaller timed chunks."""
    original = case.service.backend.apply

    def apply(connection, deadline, action, params, rows, **kwargs):
        done = len(case.fake.folders["Trash"])
        until_boundary = boundary - done % boundary
        rows[:] = rows[:until_boundary]
        return original(connection, deadline, action, params, rows, **kwargs)

    monkeypatch.setattr(case.service.backend, "apply", apply)


def assert_bound_progress(service, approved, results, actual):
    """Compare every response boundary with independent UID/identity observations."""
    original = {identity(row) for row in approved.messages}
    assert len(original) == len(approved.messages)
    assert len(actual) == len(set(actual)) and set(actual) <= original
    claimed = [
        journal_identity(row)
        for result in results
        for row in result["messages"]
        if row["status"] == "done"
    ]
    assert len(claimed) == len(set(claimed)) == len(actual)
    assert set(claimed) == set(actual)
    assert sum(result["done"] for result in results) == len(actual)
    assert all(result["count"] == result["done"] for result in results)
    assert all(result["uncertain"] == 0 for result in results)
    records = replay_records(
        [json.loads(line) for line in service.backend.log.path.read_text().splitlines()]
    )
    journaled = [
        journal_identity(row)
        for record in records
        for row in record["messages"]
        if row["status"] == "done"
    ]
    assert len(journaled) == len(set(journaled)) == len(actual)
    assert set(journaled) == set(actual)
    assert {record["log_id"] for record in records} == {
        log_id for result in results for log_id in result["log_ids"]
    }
    result = results[-1]
    assert len({call["run_id"] for call in results}) == 1
    assert result["run_done"] == len(actual)
    assert result["remaining"] == len(original - set(actual))
    assert result["approved_total"] == len(original)
    blocked = {identity(row) for row in result["blocked"]}
    token = result["continuation_token"]
    assert result["resume"] is bool(token)
    if token:
        assert result["partial"]
        continuation = service.tokens.inspect(token, approved.action)
        assert {identity(row) for row in continuation.messages} == original - set(actual) - blocked
        assert continuation.expires > approved.expires
        assert continuation.expires_at == results[0]["expires_at"]
        assert continuation.params == approved.params
        assert continuation.run_id == result["run_id"]
    if not result["partial"]:
        assert not token and set(actual) == original
    run = service.backend.log.get_run(result["run_id"])
    assert run["done"] == len(actual)
    assert (run["status"] == "completed") is (set(actual) == original)
