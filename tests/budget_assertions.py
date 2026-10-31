"""Independent socket mutation and durable progress proofs for bounded calls."""

from collections import Counter
from email import policy
from email.parser import BytesParser

from action_records import execution_records
from resume_assertions import identity, journal_identity


class SocketBudgetAudit:
    def __init__(self, client, peer, clock, token, action, monkeypatch):
        self.service, self.peer, self.clock = client.cleanup, peer, clock
        self.action = action
        self.grant = self.service.tokens.inspect(token, action)
        self.resume_expiry = None
        self.approved = {identity(row) for row in self.grant.messages}
        self.actual, self.results, self.durations = [], [], []
        self.initial = self.contents()
        self.original_hook = peer.command_hook

        def observe(folder, operation, command):
            if self.original_hook is not None:
                self.original_hook(folder, operation, command)
            if operation in {"MOVE", "STORE", "EXPUNGE"}:
                # A batched command names a UID set; account for every UID in it.
                spec = command.split()[2].decode("ascii")
                for uid in peer.uids(spec, peer.folders[folder]):
                    raw = peer.folders[folder][uid]
                    item = (
                        folder,
                        str(peer.uidvalidities[folder]),
                        uid,
                        str(BytesParser(policy=policy.default).parsebytes(raw)["Message-ID"]),
                    )
                    self.actual.append((operation, item))

        monkeypatch.setattr(peer, "command_hook", observe)

    def contents(self):
        return {
            folder: {
                uid: str(BytesParser(policy=policy.default).parsebytes(raw)["Message-ID"])
                for uid, raw in rows.items()
            }
            for folder, rows in self.peer.folders.items()
        }

    def execute(self, token):
        started = self.clock.elapsed
        result = self.service.execute(self.action, token)
        self.durations.append(self.clock.elapsed - started)
        assert self.durations[-1] < 50, self.durations
        self.results.append(result)
        assert result["run_id"] == self.results[0]["run_id"]
        assert result["expires_at"] == self.results[0]["expires_at"]
        assert {"partial", "run_id", "done", "remaining", "uncertain", "stop_reason"} <= (
            result.keys()
        )
        assert not result["uncertain"] and not result["blocked"]
        assert len(self.actual) == len(set(self.actual)), "No UID mutation may be repeated"
        actual = {item for _, item in self.actual}
        assert actual <= self.approved
        operations = Counter(operation for operation, _ in self.actual)
        if self.action == "remove_label":
            assert operations == Counter({"STORE": len(actual), "EXPUNGE": len(actual)})
        else:
            assert operations == Counter({"MOVE": len(actual)})
        claimed = [row for call in self.results for row in call["messages"]]
        assert all(row["status"] == "done" for row in claimed)
        assert len(claimed) == sum(call["done"] for call in self.results) == len(actual)
        assert {journal_identity(row) for row in claimed} == actual
        with self.service.backend.log.transaction(readonly=True) as transaction:
            records = execution_records(transaction.records())
        journaled = [row for chunk in records for row in chunk["messages"]]
        assert all(row["status"] == "done" for row in journaled)
        assert len(journaled) == len(actual)
        assert {journal_identity(row) for row in journaled} == actual
        assert [record["log_id"] for record in records] == [
            log_id for call in self.results for log_id in call["log_ids"]
        ]
        assert result["run_done"] == len(actual)
        assert result["remaining"] == len(self.approved - actual)
        run = self.service.backend.log.get_run(result["run_id"])
        assert run["done"] == len(actual)
        assert (run["status"] == "completed") == (actual == self.approved)
        contents = self.contents()
        for folder in {item[0] for item in self.approved}:
            removed = {item[2] for item in actual if item[0] == folder}
            assert contents[folder] == {
                uid: identifier
                for uid, identifier in self.initial[folder].items()
                if uid not in removed
            }
        if self.action == "remove_label":
            assert contents["All Mail"] == self.initial["All Mail"]
        else:
            target = self.grant.params["folder"] if self.action == "move_to_folder" else "Trash"
            assert set(contents[target].values()) == set(self.initial[target].values()) | {
                item[3] for item in actual
            }
            assert len(contents[target]) == len(self.initial[target]) + len(actual)
            assert set(contents[target].items()) == set(self.initial[target].items()) | {
                (row["uid_after"], row["message_id"]) for row in journaled
            }
            assert all(
                row["uidvalidity_after"] == str(self.peer.uidvalidities[target])
                for row in journaled
            )
        continuation = result["continuation_token"]
        if continuation:
            resumed = self.service.tokens.inspect(continuation, self.action)
            assert result["partial"] and result["resume"] and result["stop_reason"]
            assert {identity(row) for row in resumed.messages} == self.approved - actual
            if self.resume_expiry is None:
                self.resume_expiry = resumed.expires
                assert self.resume_expiry > self.grant.expires
            assert resumed.expires == self.resume_expiry
            assert resumed.expires_at == result["expires_at"]
        else:
            assert not result["partial"] and not result["resume"]
            assert actual == self.approved
        return result

    def finish(self, token):
        for _ in range(len(self.approved) + 2):
            result = self.execute(token)
            token = result["continuation_token"]
            if token is None:
                return self.results
        raise AssertionError("Bounded calls did not complete their exact approved remainder")
