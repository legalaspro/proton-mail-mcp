"""Real IMAP selection semantics and hostile local state for the optional timer."""

import json
import os
import socket
from types import SimpleNamespace

import pytest
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations
from test_folder_rules import rule
from test_rules_timer import baseline

from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.mail import MailError
from proton_mail_mcp.rules_timer import RulesTimer
from proton_mail_mcp.search import _uidvalidity


@pytest.fixture
def socket_timer(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Timer safety tests must use the local socketpair peer")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    sim = BridgeSim(folders=("Folders/Review",))
    old = sim.add(Msg(hdr("<old-safety@example.test>")))
    client = make_client(sim, tmp_path, call_timeout=300)
    rules = tmp_path / "synthetic-rules.json"
    rules.write_text(json.dumps({"version": 1, "rules": [rule()]}))
    state = tmp_path / "synthetic-state.json"
    timer = RulesTimer(client.cleanup, rules_path=rules, state_path=state)
    yield SimpleNamespace(sim=sim, old=old, client=client, timer=timer, state=state)
    sim.close()
    assert not sim.errors
    assert not sim.violations


def test_uidvalidity_reads_do_not_consume_selected_mailbox_response(socket_timer):
    case = socket_timer
    with case.client._session() as (connection, _):
        for folder in ("INBOX", "Folders/Review", "INBOX"):
            case.client._select(connection, folder)
            expected = str(case.sim.uidvalidity[folder])
            assert _uidvalidity(connection) == _uidvalidity(connection) == expected
            assert connection.untagged_responses["UIDVALIDITY"] == [expected.encode()]
        case.sim.reset_uidvalidity("INBOX")
        case.client._select(connection, "INBOX")
        expected = str(case.sim.uidvalidity["INBOX"])
        assert _uidvalidity(connection) == _uidvalidity(connection) == expected


def test_missing_current_uidvalidity_never_falls_back_to_stale_response():
    def stale_response(*args):
        pytest.fail("Missing current selection identity must fail closed")

    connection = SimpleNamespace(untagged_responses={}, response=stale_response)
    with pytest.raises(MailError, match="UIDVALIDITY"):
        _uidvalidity(connection)


def test_epoch_change_during_plan_cannot_move_reused_old_uid(socket_timer, monkeypatch):
    case = socket_timer
    baseline(case)
    before = case.state.read_bytes()
    new = case.sim.add(Msg(hdr("<new-safety@example.test>")))
    original = FolderRulesService._plan

    def reset_before_plan(*args, **kwargs):
        # Renumbering gives the old backlog message the candidate's previous UID.
        case.sim.reset_uidvalidity("INBOX")
        return original(*args, **kwargs)

    monkeypatch.setattr(FolderRulesService, "_plan", reset_before_plan)
    with pytest.raises(MailError, match="UIDVALIDITY"):
        case.timer.run()
    assert case.old.folder == new.folder == "INBOX"
    assert not mutations(case.sim) and case.state.read_bytes() == before


def test_epoch_change_after_move_keeps_pending_old_mark(socket_timer, monkeypatch):
    case = socket_timer
    baseline(case)
    before = json.loads(case.state.read_text())["mailboxes"]["INBOX"]
    new = case.sim.add(Msg(hdr("<new-safety@example.test>")))
    backend = case.client.cleanup.backend
    original = backend.apply

    def reset_after_apply(*args, **kwargs):
        result = original(*args, **kwargs)
        case.sim.reset_uidvalidity("INBOX")
        return result

    monkeypatch.setattr(backend, "apply", reset_after_apply)
    with pytest.raises(MailError, match="UIDVALIDITY"):
        case.timer.run()
    mark = json.loads(case.state.read_text())["mailboxes"]["INBOX"]
    assert mark == {**before, "pending": True}
    assert case.old.folder == "INBOX" and new.folder == "Folders/Review"
    assert len(mutations(case.sim)) == 1
    commands = len(case.sim.commands)
    with pytest.raises(MailError, match="inspection"):
        case.timer.run()
    assert len(case.sim.commands) == commands


@pytest.mark.parametrize("name", ["state_path", "lock_path"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_unsafe_state_or_lock_never_touches_mail(socket_timer, tmp_path, name, kind):
    case = socket_timer
    path = getattr(case.timer, name)
    target = tmp_path / "preserved-synthetic-data"
    target.write_bytes(b"preserve this data\n")
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        os.mkfifo(path, 0o600)
    with pytest.raises((MailError, OSError)):
        case.timer.run()
    assert not case.sim.commands and not mutations(case.sim)
    assert target.read_bytes() == b"preserve this data\n"
    assert path.is_symlink() if kind == "symlink" else path.exists()


@pytest.mark.parametrize(
    "raw",
    [
        '{"version":1,"version":1,"mailboxes":{}}',
        '{"version":true,"mailboxes":{}}',
        '{"version":1,"mailboxes":{"INBOX":{"uidvalidity":"1","uid":true,"pending":false}}}',
        '{"version":1,"mailboxes":{"INBOX":{"uidvalidity":"1","uid":2,"pending":0}}}',
    ],
)
def test_ambiguous_state_is_not_replaced_with_fresh_baseline(socket_timer, raw):
    case = socket_timer
    case.state.write_text(raw)
    with pytest.raises(MailError, match="state is invalid"):
        case.timer.run()
    assert not case.sim.commands and case.state.read_text() == raw
