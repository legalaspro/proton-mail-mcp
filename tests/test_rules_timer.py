"""Off-by-default rules scheduling is exercised against synthetic IMAP only."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations
from test_cleanup_mutations import MUTATIONS, MutatingImap, uid_values
from test_folder_rules import rule
from test_mail import client, message, unquote
from windows_skips import has_mode

from proton_mail_mcp import localfs
from proton_mail_mcp.action_log import ActionLog, LogTransaction, replay_records
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.guarded import MAX_BATCH
from proton_mail_mcp.mail import MailError, MailValidationError
from proton_mail_mcp.rules_timer import RulesTimer


class TimerImap(MutatingImap):
    def create(self, name):
        self.calls.append(("CREATE", name))
        self.folders[unquote(name)] = {}
        return "OK", []


@pytest.fixture
def timer_case(tmp_path):
    def build(*, count=2, rules=None):
        path = tmp_path / "synthetic-rules.json"
        path.write_text(json.dumps({"version": 1, "rules": [rule()] if rules is None else rules}))
        items = {str(i): message(f"<timer-{i}@example.test>") for i in range(1, count + 1)}
        fake = TimerImap(
            {
                "INBOX": items,
                "Folders/Review": {},
                "Folders/Other": {},
                "All Mail": dict(items),
                "Trash": {},
            }
        )
        settings = Settings(
            token="synthetic-timer-token-" + "x" * 32,
            bridge_user="reader@example.test",
            call_timeout=300,
            execute_timeout=300,
            action_log_path=str(tmp_path / "synthetic-actions.jsonl"),
        )
        cleanup = client(settings, fake).cleanup
        state = tmp_path / "synthetic-timer-state.json"
        timer = RulesTimer(cleanup, rules_path=path, state_path=state)
        return SimpleNamespace(timer=timer, cleanup=cleanup, fake=fake, rules=path, state=state)

    return build


def arrive(case, uid, *, folder="INBOX", sender=None, subject="Hello", seen=False):
    item = message(f"<arrival-{folder.replace('/', '-')}-{uid}@example.test>", subject=subject)
    if sender:
        item.headers = item.headers.replace(b"sender@example.com", sender.encode())
    item.flags = {r"\Seen"} if seen else set()
    case.fake.folders[folder][str(uid)] = item
    case.fake.folders["All Mail"][str(10000 + uid)] = item
    return item


def mark(case, folder="INBOX"):
    return json.loads(case.state.read_text())["mailboxes"][folder]


def baseline(case):
    """Represent a pre-existing installation that already acknowledged its backlog."""
    validity = case.fake.uidvalidity if hasattr(case, "fake") else case.sim.uidvalidity["INBOX"]
    highest = (
        max(map(int, case.fake.folders["INBOX"]), default=0)
        if hasattr(case, "fake")
        else case.sim.uid_of("INBOX", case.old.pid)
    )
    case.state.write_text(
        json.dumps(
            {
                "version": 1,
                "mailboxes": {
                    "INBOX": {
                        "uidvalidity": str(validity),
                        "uid": highest,
                        "pending": False,
                    }
                },
            }
        )
    )
    return case.timer.run()


def changed(case):
    return [
        call for call in case.fake.calls if call[0] in MUTATIONS | {"CREATE", "DELETE", "RENAME"}
    ]


def changed_uids(case):
    """Every UID named by a mutation command (batched commands name UID sets)."""
    return [uid for call in case.fake.calls if call[0] in MUTATIONS for uid in uid_values(call[1])]


def records(case):
    return replay_records(
        [json.loads(line) for line in case.cleanup.backend.log.path.read_text().splitlines()]
    )


def test_first_run_drains_backlog_and_records_completed_high_water(timer_case):
    case = timer_case(count=4)
    result = case.timer.run()
    assert result["status"] == "initialized" and result["count"] == 4
    assert mark(case)["uid"] == 4 and mark(case)["uidvalidity"] == "1"
    assert not case.fake.folders["INBOX"]
    assert len(case.fake.folders["Folders/Review"]) == len(changed_uids(case)) == 4
    assert len(changed(case)) == 1  # One batched MOVE for the timer's chunk.
    assert has_mode(case.state, 0o600)


def test_new_mail_moves_old_matching_mail_never_does(timer_case):
    case = timer_case()
    baseline(case)
    old = dict(case.fake.folders["INBOX"])
    arrive(case, 3)
    assert case.timer.run()["count"] == 1
    assert case.fake.folders["INBOX"] == old
    assert len(case.fake.folders["Folders/Review"]) == 1
    assert mark(case)["uid"] == 3
    assert case.timer.run()["count"] == 0
    assert len(changed(case)) == 1


def test_empty_mailbox_initializes_zero_then_accepts_first_arrival(timer_case):
    case = timer_case(count=0)
    assert case.timer.run()["count"] == 0 and mark(case)["uid"] == 0
    arrive(case, 1)
    assert case.timer.run()["count"] == 1 and mark(case)["uid"] == 1


def test_uidvalidity_reset_records_current_top_without_backlog_moves(timer_case):
    case = timer_case()
    baseline(case)
    arrive(case, 10)
    case.fake.uidvalidity = "2"
    before = deepcopy(case.fake.folders)
    result = case.timer.run()
    assert result["status"] == "reset" and result["count"] == 0
    assert mark(case)["uid"] == 10 and mark(case)["uidvalidity"] == "2"
    assert case.fake.folders == before and changed(case) == []
    assert any("reset" in json.dumps(record).lower() for record in records(case))
    arrive(case, 11)
    assert case.timer.run()["count"] == 1
    assert set(case.fake.folders["INBOX"]) == {"1", "2", "10"}


def test_51_arrivals_move_50_then_one_without_skipping_uid(timer_case):
    case = timer_case(count=1)
    baseline(case)
    for uid in range(2, 53):
        arrive(case, uid)
    first = case.timer.run()
    assert first["count"] == 50 and mark(case)["uid"] == 51
    assert set(case.fake.folders["INBOX"]) == {"1", "52"}
    assert case.timer.run()["count"] == 1
    assert mark(case)["uid"] == 52 and set(case.fake.folders["INBOX"]) == {"1"}


def test_configured_cap_is_count_of_moves_with_contiguous_skips(timer_case):
    case = timer_case(count=0)
    baseline(case)
    arrive(case, 1, sender="relative@gmail.com")
    arrive(case, 2, subject="Unrelated", sender="other@example.test")
    arrive(case, 3)
    arrive(case, 4)
    assert case.timer.run(cap=1)["count"] == 1
    assert mark(case)["uid"] == 3
    assert set(case.fake.folders["INBOX"]) == {"1", "2", "4"}
    assert case.timer.run(cap=1)["count"] == 1 and mark(case)["uid"] == 4


@pytest.mark.parametrize("cap", [0, -1, MAX_BATCH + 1, True, 1.5, "50"])
def test_invalid_caps_fail_before_mail_access(timer_case, cap):
    case = timer_case()
    with pytest.raises((ConfigError, MailValidationError, ValueError)):
        case.timer.run(cap=cap)
    assert case.fake.calls == [] and not case.state.exists()


def test_old_custom_folder_mark_is_retained_but_no_longer_an_allowed_source(timer_case):
    case = timer_case(count=1)
    baseline(case)
    state = json.loads(case.state.read_text())
    state["mailboxes"]["Folders/Other"] = {"uidvalidity": "1", "uid": 14, "pending": False}
    case.state.write_text(json.dumps(state))
    before = case.state.read_bytes()
    arrive(case, 15, folder="Folders/Other")
    case.fake.calls.clear()
    with pytest.raises(MailValidationError, match="source"):
        case.timer.run(folder="Folders/Other")
    assert case.state.read_bytes() == before and changed(case) == []
    assert not any(call[0] in {"SELECT", "EXAMINE"} for call in case.fake.calls)
    assert "15" in case.fake.folders["Folders/Other"]
    arrive(case, 2)
    assert case.timer.run()["count"] == 1
    assert mark(case, "Folders/Other") == state["mailboxes"]["Folders/Other"]


@pytest.mark.parametrize("empty_rules", [False, True])
def test_unmatched_arrivals_are_deliberate_skips(timer_case, empty_rules):
    case = timer_case(rules=[] if empty_rules else [rule("subject", "Wanted")])
    baseline(case)
    arrive(case, 4, subject="Unrelated")
    assert case.timer.run()["count"] == 0 and mark(case)["uid"] == 4
    assert changed(case) == []


@pytest.mark.parametrize("missing_matches", [False, True])
def test_missing_destination_fails_entire_run_even_for_later_rule(timer_case, missing_matches):
    case = timer_case(rules=[rule(), rule("subject", "Later", "Folders/Other")])
    baseline(case)
    arrive(case, 3, subject="Later" if missing_matches else "Hello")
    before = case.state.read_bytes()
    del case.fake.folders["Folders/Other"]
    with pytest.raises((MailError, MailValidationError, ConfigError)):
        case.timer.run()
    assert changed(case) == [] and case.state.read_bytes() == before
    assert any(
        record.get("partial") or "fail" in json.dumps(record).lower() for record in records(case)
    )


@pytest.mark.parametrize("invalid", ["{", '{"version":1,"rules":"bad"}', None])
def test_rules_parse_failure_does_not_connect_or_advance(timer_case, invalid):
    case = timer_case()
    baseline(case)
    arrive(case, 3)
    before = case.state.read_bytes()
    case.fake.calls.clear()
    if invalid is None:
        case.rules.unlink()
    else:
        case.rules.write_text(invalid)
    with pytest.raises(ConfigError):
        case.timer.run()
    assert case.fake.calls == [] and case.state.read_bytes() == before


@pytest.mark.parametrize("failure", ["open", "append"])
def test_unwritable_journal_prevents_every_move(timer_case, monkeypatch, tmp_path, failure):
    case = timer_case()
    baseline(case)
    arrive(case, 3)
    if failure == "open":
        case.cleanup.backend.log = ActionLog(tmp_path)
    else:

        def denied(*args, **kwargs):
            raise MailError("synthetic append failure")

        monkeypatch.setattr(LogTransaction, "append", denied)
    try:
        result = case.timer.run()
    except MailError:
        pass
    else:
        assert result["status"] == "partial"
    assert changed(case) == [] and mark(case)["uid"] == 2
    assert "3" in case.fake.folders["INBOX"]


def test_each_move_has_durable_intent_before_command_and_mark_advances_after(
    timer_case, monkeypatch
):
    case = timer_case()
    baseline(case)
    arrive(case, 3)
    arrive(case, 4)
    original = case.fake.uid
    observed = []

    def checked(command, *args):
        if command == "MOVE":
            intents = [row for record in records(case) for row in record.get("messages", [])]
            for uid in uid_values(args[0]):
                assert mark(case)["uid"] < int(uid)
                assert mark(case)["pending"] is True
                assert any(
                    row["uid_before"] == uid and row["status"] == "uncertain" for row in intents
                )
                observed.append(uid)
        return original(command, *args)

    monkeypatch.setattr(case.fake, "uid", checked)
    result = case.timer.run()
    assert result["count"] == 2 and observed == ["3", "4"]
    assert mark(case)["uid"] == 4 and mark(case)["pending"] is False
    assert sum(record["action"] == "move_to_folder" for record in records(case)) == len(
        result["log_ids"]
    )


def test_timer_moves_and_undo_preserve_read_and_unread_flags(timer_case):
    case = timer_case()
    baseline(case)
    read = arrive(case, 3, seen=True)
    unread = arrive(case, 4)
    before = {id(item): set(item.flags) for item in (read, unread)}
    result = case.timer.run()
    assert result["count"] == 2 and result["log_ids"]
    assert {id(item): item.flags for item in case.fake.folders["Folders/Review"].values()} == before
    for log_id in reversed(result["log_ids"]):
        assert case.cleanup.undo_action(log_id)["count"] > 0
    assert not case.fake.folders["Folders/Review"]
    assert {
        id(item): item.flags
        for item in case.fake.folders["INBOX"].values()
        if hasattr(item, "flags")
    } == before
    assert not any(
        call[0] in {"STORE", "EXPUNGE", "COPY", "CREATE", "DELETE", "RENAME"}
        for call in case.fake.calls
    )


def test_protected_source_and_protected_membership_never_move(timer_case):
    case = timer_case(rules=[rule("subject", "Hello"), rule(folder="Folders/Other")])
    baseline(case)
    arrive(case, 3, sender="relative@gmail.com")
    arrive(case, 4)
    peer = message("<arrival-INBOX-4@example.test>")
    peer.headers = peer.headers.replace(b"sender@example.com", b"relative@gmail.com")
    case.fake.folders["All Mail"]["10004"] = peer
    arrive(case, 5)
    assert case.timer.run()["count"] == 1
    assert set(case.fake.folders["INBOX"]) == {"1", "2", "3", "4"}
    assert not case.fake.folders["Folders/Other"] and mark(case)["uid"] == 5


def test_existing_lock_refuses_overlapping_run_without_mail_access(timer_case):
    case = timer_case()
    baseline(case)
    arrive(case, 3)
    before = case.state.read_bytes()
    case.fake.calls.clear()
    with case.state.with_suffix(".lock").open("a") as lock:
        localfs.lock(lock.fileno())
        result = case.timer.run()
    assert result["status"] == "busy" and result["count"] == 0
    assert case.fake.calls == [] and case.state.read_bytes() == before
    assert case.timer.run()["count"] == 1


def test_pending_crash_state_refuses_unattended_replay(timer_case):
    case = timer_case()
    baseline(case)
    arrive(case, 3)
    data = json.loads(case.state.read_text())
    data["mailboxes"]["INBOX"]["pending"] = True
    case.state.write_text(json.dumps(data))
    before = case.state.read_bytes()
    with pytest.raises(MailError):
        case.timer.run()
    assert changed(case) == [] and case.state.read_bytes() == before


@pytest.mark.parametrize("invalid", ["{", '{"version":1,"mailboxes":{"INBOX":{"uid":-1}}}'])
def test_corrupt_state_fails_closed_instead_of_reinitializing(timer_case, invalid):
    case = timer_case()
    case.state.write_text(invalid)
    with pytest.raises((MailError, ConfigError)):
        case.timer.run()
    assert changed(case) == [] and case.state.read_text() == invalid


def test_state_write_failure_prevents_the_first_move(timer_case, monkeypatch):
    case = timer_case()
    baseline(case)
    arrive(case, 3)
    before = case.state.read_bytes()

    def denied(*args, **kwargs):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("proton_mail_mcp.rules_timer.os.replace", denied)
    result = case.timer.run()
    assert result["status"] == "partial" and result["count"] == 0
    assert changed(case) == [] and case.state.read_bytes() == before


def test_failed_move_never_advances_past_unprocessed_uid(timer_case, monkeypatch):
    case = timer_case()
    baseline(case)
    arrive(case, 3)
    arrive(case, 4)
    original = case.fake.uid

    def fail_move(command, *args):
        if command == "MOVE":
            raise MailError("synthetic move failure")
        return original(command, *args)

    monkeypatch.setattr(case.fake, "uid", fail_move)
    result = case.timer.run()
    assert result["status"] == "partial" and result["count"] == 0
    assert mark(case)["uid"] == 2 and mark(case)["pending"] is True
    assert set(case.fake.folders["INBOX"]) == {"1", "2", "3", "4"}


def test_bridge_socket_peer_journal_and_undo_without_network(tmp_path):
    sim = BridgeSim(folders=("Folders/Review",))
    try:
        old = sim.add(Msg(hdr("<timer-old@example.test>")))
        cleanup = make_client(sim, tmp_path, call_timeout=300).cleanup
        path = tmp_path / "synthetic-rules.json"
        path.write_text(json.dumps({"version": 1, "rules": [rule()]}))
        timer = RulesTimer(cleanup, rules_path=path, state_path=tmp_path / "timer-state.json")
        assert (
            baseline(SimpleNamespace(timer=timer, state=timer.state_path, sim=sim, old=old))[
                "count"
            ]
            == 0
        )
        new = sim.add(Msg(hdr("<timer-new@example.test>")))
        result = timer.run()
        assert result["count"] == 1
        assert old.folder == "INBOX" and new.folder == "Folders/Review"
        for log_id in result["log_ids"]:
            assert cleanup.undo_action(log_id)["count"] == 1
        assert new.folder == "INBOX" and old.folder == "INBOX"
        assert all("UID MOVE " in item[3] for item in mutations(sim))
        assert not sim.violations
    finally:
        sim.close()


def test_timer_units_remain_optional_and_are_never_installed_or_enabled():
    root = Path(__file__).resolve().parents[1]
    service = (root / "examples/systemd/proton-mail-mcp-rules.service").read_text()
    timer = (root / "examples/systemd/proton-mail-mcp-rules.timer").read_text()
    assert "Type=oneshot" in service and "proton-mail-mcp-rules-run" in service
    assert "OnUnitActiveSec=5min" in timer or "OnCalendar=*:0/5" in timer
    assert "Persistent=true" not in timer
    for path in [root / "Makefile", *sorted((root / "scripts").rglob("*"))]:
        # test_operator_timing imports a script, which can cache its bytecode here.
        if path.is_file() and "__pycache__" not in path.parts:
            assert "proton-mail-mcp-rules" not in path.read_text(), path
    server = (root / "src/proton_mail_mcp/server.py").read_text()
    assert "RulesTimer" not in server and "rules_timer" not in server


def test_old_backlog_over_batch_cap_does_not_block_new_mail(timer_case):
    case = timer_case(count=MAX_BATCH + 1)
    baseline(case)
    arrive(case, MAX_BATCH + 2)
    assert case.timer.run()["count"] == 1
    assert len(case.fake.folders["INBOX"]) == MAX_BATCH + 1
    assert mark(case)["uid"] == MAX_BATCH + 2


@pytest.mark.parametrize("host", ["mail.example.test", "192.0.2.10", "127.0.0.1.example.test"])
def test_remote_endpoint_refused_before_credentials_or_connection(timer_case, monkeypatch, host):
    from dataclasses import replace

    case = timer_case()
    settings = replace(case.cleanup.settings, imap_host=host)
    case.cleanup.settings = case.cleanup.client.settings = settings

    def forbidden(*args, **kwargs):
        pytest.fail("Remote timer endpoint reached credentials or IMAP")

    monkeypatch.setattr(case.cleanup.client, "credentials_provider", forbidden)
    monkeypatch.setattr(case.cleanup.client, "imap_factory", forbidden)
    with pytest.raises(MailValidationError, match="loopback"):
        case.timer.run()
    assert case.fake.calls == [] and not case.state.exists()


def test_uidvalidity_change_before_checkpoint_cannot_acknowledge_new_mail(timer_case, monkeypatch):
    case = timer_case()
    baseline(case)
    arrive(case, 3)
    original = case.fake.uid

    def reset_after_move(command, *args):
        result = original(command, *args)
        if command == "MOVE":
            case.fake.uidvalidity = "99"
        return result

    monkeypatch.setattr(case.fake, "uid", reset_after_move)
    # The batched MOVE's own source check already sees the new UIDVALIDITY: the
    # chunk is interrupted and the run partial, so no checkpoint follows.
    result = case.timer.run()
    assert result["status"] == "partial" and len(result["log_ids"]) == 1
    chunk = next(row for row in records(case) if row.get("log_id") == result["log_ids"][0])
    assert chunk["partial"] and "move verification" in chunk["error"]
    assert mark(case)["uid"] == 2 and mark(case)["uidvalidity"] == "1"
    assert mark(case)["pending"] is True


@pytest.mark.parametrize("failure", ["rules", "missing-target", "journal"])
def test_cli_failures_are_nonzero_and_never_move(
    timer_case, monkeypatch, capsys, tmp_path, failure
):
    from proton_mail_mcp import rules_timer

    case = timer_case()
    baseline(case)
    arrive(case, 3)
    if failure == "rules":
        case.rules.write_text("{")
    elif failure == "missing-target":
        del case.fake.folders["Folders/Review"]
    else:
        case.cleanup.backend.log = ActionLog(tmp_path)
    monkeypatch.setenv("PROTON_BRIDGE_USER", "synthetic-cli@example.test")
    monkeypatch.setattr("sys.argv", ["proton-mail-mcp-rules-run"])
    monkeypatch.setattr(rules_timer, "MailClient", lambda settings: case.cleanup.client)
    monkeypatch.setattr(rules_timer, "RulesTimer", lambda cleanup: case.timer)
    with pytest.raises(SystemExit) as exit_info:
        rules_timer.main()
    assert exit_info.value.code == 1 and changed(case) == []
    output = capsys.readouterr()
    assert "failed" in output.err.lower()
    assert "synthetic-cli@example.test" not in output.err


def test_cli_first_run_and_overlapping_run_exit_successfully(timer_case, monkeypatch, capsys):
    from proton_mail_mcp import rules_timer

    case = timer_case(count=0)
    monkeypatch.setenv("PROTON_BRIDGE_USER", "synthetic-cli@example.test")
    monkeypatch.setattr("sys.argv", ["proton-mail-mcp-rules-run"])
    monkeypatch.setattr(rules_timer, "MailClient", lambda settings: case.cleanup.client)
    monkeypatch.setattr(rules_timer, "RulesTimer", lambda cleanup: case.timer)
    with pytest.raises(SystemExit) as exit_info:
        rules_timer.main()
    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "initialized"
    with case.state.with_suffix(".lock").open("a") as lock:
        localfs.lock(lock.fileno())
        with pytest.raises(SystemExit) as exit_info:
            rules_timer.main()
    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "busy"
    assert changed(case) == []


@pytest.mark.parametrize("collision", ["state", "lock", "symlink"])
def test_state_and_lock_must_not_alias_action_journal(timer_case, tmp_path, collision):
    case = timer_case()
    log = case.cleanup.backend.log.path
    log.write_text('{"preserved":true}\n')
    if collision == "state":
        state = log
    elif collision == "lock":
        case.cleanup.backend.log = ActionLog(tmp_path / "collision.lock")
        log = case.cleanup.backend.log.path
        log.write_text('{"preserved":true}\n')
        state = tmp_path / "collision.json"
    else:
        state = tmp_path / "link.json"
        state.symlink_to(log)
    with pytest.raises(MailValidationError, match="distinct"):
        RulesTimer(case.cleanup, rules_path=case.rules, state_path=state)
    assert log.read_text() == '{"preserved":true}\n' and case.fake.calls == []


def test_uidvalidity_change_during_planning_refuses_before_any_move(timer_case, monkeypatch):
    from proton_mail_mcp.folder_rules import FolderRulesService

    case = timer_case()
    baseline(case)
    arrive(case, 3)
    original = FolderRulesService._plan

    def changed_epoch(*args, **kwargs):
        case.fake.uidvalidity = "2"
        return original(*args, **kwargs)

    monkeypatch.setattr(FolderRulesService, "_plan", changed_epoch)
    with pytest.raises(MailError, match="UIDVALIDITY"):
        case.timer.run()
    assert changed(case) == []
    assert mark(case)["uid"] == 2 and mark(case)["uidvalidity"] == "1"


def test_failed_per_message_intent_append_prevents_move(timer_case, monkeypatch):
    case = timer_case()
    baseline(case)
    arrive(case, 3)
    original = LogTransaction.append

    def fail_intent(self, record):
        if record["action"] == "execute_progress" and record["message"]["status"] == "uncertain":
            raise MailError("synthetic intent append failure")
        return original(self, record)

    monkeypatch.setattr(LogTransaction, "append", fail_intent)
    try:
        result = case.timer.run()
    except MailError:
        pass
    else:
        assert result["status"] == "partial" and result["count"] == 0
    assert changed(case) == [] and mark(case)["uid"] == 2
    assert mark(case)["pending"] is True


def test_protected_membership_is_refreshed_between_timer_chunks(timer_case, monkeypatch):
    case = timer_case(count=0)
    baseline(case)
    for uid in range(1, 52):
        arrive(case, uid)
    original = case.cleanup.backend.apply
    chunks = []

    def protect_remaining_after_chunk(*args, **kwargs):
        result = original(*args, **kwargs)
        chunks.append(result["count"])
        peer = message("<arrival-INBOX-51@example.test>")
        peer.headers = peer.headers.replace(b"sender@example.com", b"relative@gmail.com")
        case.fake.folders["All Mail"]["10051"] = peer
        return result

    monkeypatch.setattr(case.cleanup.backend, "apply", protect_remaining_after_chunk)
    result = case.timer.run(cap=51)
    assert result["count"] == 50 and chunks == [50]
    assert set(case.fake.folders["INBOX"]) == {"51"}
    assert mark(case)["uid"] == 51 and mark(case)["pending"] is False
