"""One exact draft has one live token and is delivered at most once."""

import smtplib
from dataclasses import replace

import pytest
from test_send import (
    DRAFT_ID,
    OTHER_DRAFT_ID,
    RECIPIENT,
    Host,
    draft,
    local_certificate,  # noqa: F401
    records,
    redeem,
    use_draft,
)
from test_send import rig as _send_rig
from test_send_token_approval import FAILURES
from test_send_token_approval import own_rig as _own_rig

from proton_mail_mcp.action_log import LogTransaction
from proton_mail_mcp.guarded import approval_hash

rig = _send_rig
own_rig = _own_rig
OTHER = "other@example.test"
PATHS = [pytest.param(False, id="host"), pytest.param(True, id="own-address")]


def path_rig(rig, own):
    rig.service.settings = replace(rig.settings, send_self_no_form=own)
    return rig


def data_commands(rig):
    return sum(call[0] == "data" for call in rig.smtp.calls)


def reservations(rig):
    return sum(record["budget_reserved"] for record in records(rig))


@pytest.mark.parametrize("own", PATHS)
def test_new_preview_retires_the_earlier_token(own_rig, own):
    rig = path_rig(own_rig, own)
    first = rig.service.preview_send(DRAFT_ID)
    second = rig.service.preview_send(DRAFT_ID)
    assert first["approval"] == second["approval"] == ("token" if own else "host_confirmation")
    host = Host()
    with pytest.raises(FAILURES):
        redeem(rig, first["token"], host)
    assert host.prompts == [] and rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "token_invalid"
    assert redeem(rig, second["token"], host)["sent"] is True
    assert data_commands(rig) == 1


def test_retiring_leaves_other_drafts_and_actions_live(own_rig):
    rig = own_rig
    selector = {"draft_id": DRAFT_ID}
    message = {"message_id": DRAFT_ID, "folder": "Drafts", "uidvalidity": "1", "uid": "7"}
    archive, _ = rig.tokens.issue("archive", {}, selector, [message])
    live = rig.service.preview_send(DRAFT_ID)
    assert rig.tokens.inspect(archive, "archive").selector == selector
    use_draft(rig, OTHER_DRAFT_ID)
    rig.service.preview_send(OTHER_DRAFT_ID)
    use_draft(rig, DRAFT_ID)
    rig.tokens.issue("archive", {}, selector, [message], sole=True)
    assert redeem(rig, live["token"], Host())["sent"] is True


@pytest.mark.parametrize("own", PATHS)
def test_sent_draft_cannot_be_sent_again(own_rig, own):
    rig = path_rig(own_rig, own)
    token = rig.service.preview_send(DRAFT_ID)["token"]
    assert redeem(rig, token, Host())["sent"] is True
    fresh = rig.service.preview_send(DRAFT_ID)
    host = Host()
    with pytest.raises(FAILURES, match="already sent"):
        redeem(rig, fresh["token"], host)
    assert host.prompts == []
    assert data_commands(rig) == 1 and len(rig.smtp.sent) == 1
    assert records(rig)[-1]["result"] == "already_sent"
    assert reservations(rig) == 1


def test_partially_sent_draft_cannot_be_sent_again(rig, monkeypatch):
    rig.client.raw = draft(to=f"{RECIPIENT}, {OTHER}")
    original = rig.smtp.rcpt
    monkeypatch.setattr(
        rig.smtp,
        "rcpt",
        lambda address, options=(): (
            (550, b"synthetic") if address == OTHER else original(address, options)
        ),
    )
    token = rig.service.preview_send(DRAFT_ID)["token"]
    assert redeem(rig, token, Host())["result"] == "partial"
    with pytest.raises(FAILURES, match="already sent"):
        redeem(rig, rig.service.preview_send(DRAFT_ID)["token"], Host())
    assert data_commands(rig) == 1
    assert records(rig)[-1]["result"] == "already_sent"


def test_token_claimed_before_a_newer_preview_cannot_double_send(rig):
    newer = []
    host = Host(during=lambda: newer.append(rig.service.preview_send(DRAFT_ID)["token"]))
    token = rig.service.preview_send(DRAFT_ID)["token"]
    assert redeem(rig, token, host)["sent"] is True
    with pytest.raises(FAILURES, match="already sent"):
        redeem(rig, newer[0], Host())
    assert data_commands(rig) == 1


def lost_reply(rig, monkeypatch, outcome):
    """Make DATA accept the message, then answer with `outcome`."""
    original = rig.smtp.data

    def accepted(raw):
        original(raw)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(rig.smtp, "data", accepted)
    return original


def assert_refused_as_possibly_sent(rig):
    fresh = rig.service.preview_send(DRAFT_ID)
    host = Host()
    with pytest.raises(FAILURES, match="may already have been sent"):
        redeem(rig, fresh["token"], host)
    assert host.prompts == []
    assert records(rig)[-1]["result"] == "possibly_sent"


@pytest.mark.parametrize("own", PATHS)
@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(smtplib.SMTPServerDisconnected("synthetic"), id="disconnected"),
        pytest.param(TimeoutError("synthetic"), id="timeout"),
        pytest.param((-1, b"synthetic"), id="malformed-reply"),
        pytest.param((251, b"synthetic"), id="other-success-reply"),
        pytest.param(smtplib.SMTPDataError(-1, b"synthetic"), id="malformed-reply-to-command"),
    ],
)
def test_data_without_an_explicit_refusal_blocks_a_resend(own_rig, monkeypatch, own, outcome):
    rig = path_rig(own_rig, own)
    original = lost_reply(rig, monkeypatch, outcome)
    result = redeem(rig, rig.service.preview_send(DRAFT_ID)["token"], Host())
    assert result["result"] == "smtp_error" and result["sent"] is False
    assert result["uncertain"] is True
    assert records(rig)[-1]["action"] == "send_result" and records(rig)[-1]["uncertain"] is True
    monkeypatch.setattr(rig.smtp, "data", original)
    before = reservations(rig)
    assert_refused_as_possibly_sent(rig)
    assert reservations(rig) == before
    assert data_commands(rig) == 1 and len(rig.smtp.sent) == 1


@pytest.mark.parametrize("own", PATHS)
def test_lost_journal_write_after_data_blocks_a_resend(own_rig, monkeypatch, own):
    rig = path_rig(own_rig, own)
    original = LogTransaction.append

    def failing(self, record):
        if record.get("action") == "send_result":
            raise OSError("synthetic")
        return original(self, record)

    monkeypatch.setattr(LogTransaction, "append", failing)
    with pytest.raises(FAILURES):
        redeem(rig, rig.service.preview_send(DRAFT_ID)["token"], Host())
    monkeypatch.setattr(LogTransaction, "append", original)
    assert len(rig.smtp.sent) == 1
    assert_refused_as_possibly_sent(rig)
    assert data_commands(rig) == 1 and len(rig.smtp.sent) == 1


def test_reservation_without_a_result_blocks_a_resend_even_before_data(rig):
    snapshot = rig.service._read(DRAFT_ID)
    with rig.log.transaction() as transaction:
        reservation = rig.service._record(snapshot, "reserved")
        reservation["budget_reserved"] = True
        transaction.append(reservation)
    assert_refused_as_possibly_sent(rig)
    assert rig.smtp.calls == []


def test_uncertain_outcome_does_not_block_an_edited_draft(rig, monkeypatch):
    original = lost_reply(rig, monkeypatch, smtplib.SMTPServerDisconnected("synthetic"))
    redeem(rig, rig.service.preview_send(DRAFT_ID)["token"], Host())
    monkeypatch.setattr(rig.smtp, "data", original)
    rig.client.raw = draft(body="Edited synthetic body")
    assert redeem(rig, rig.service.preview_send(DRAFT_ID)["token"], Host())["sent"] is True


def refuse_data(rig, monkeypatch, outcome):
    def refused(raw):
        rig.smtp.calls.append(("data",))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(rig.smtp, "data", refused)


def refuse_rcpt(rig, monkeypatch):
    monkeypatch.setattr(rig.smtp, "rcpt", lambda address, options=(): (550, b"synthetic"))


def fail_connect(rig, monkeypatch):
    rig.smtp.failure = "connect"


@pytest.mark.parametrize("own", PATHS)
@pytest.mark.parametrize(
    "fail",
    [
        pytest.param(fail_connect, id="connect"),
        pytest.param(refuse_rcpt, id="rcpt-refused"),
        pytest.param(
            lambda rig, monkeypatch: refuse_data(rig, monkeypatch, (554, b"synthetic")),
            id="data-reply-554",
        ),
        pytest.param(
            lambda rig, monkeypatch: refuse_data(rig, monkeypatch, (451, b"synthetic")),
            id="data-reply-451",
        ),
        pytest.param(
            lambda rig, monkeypatch: refuse_data(
                rig, monkeypatch, smtplib.SMTPDataError(503, b"synthetic")
            ),
            id="data-command-503",
        ),
    ],
)
def test_failure_known_to_precede_acceptance_stays_retryable(own_rig, monkeypatch, own, fail):
    rig = path_rig(own_rig, own)
    with monkeypatch.context() as failing:
        fail(rig, failing)
        result = redeem(rig, rig.service.preview_send(DRAFT_ID)["token"], Host())
    rig.smtp.failure = None
    assert result["result"] == "smtp_error" and result["uncertain"] is False
    assert records(rig)[-1]["uncertain"] is False
    assert redeem(rig, rig.service.preview_send(DRAFT_ID)["token"], Host())["sent"] is True
    assert reservations(rig) == 2 and len(rig.smtp.sent) == 1


def test_edited_draft_after_send_is_a_different_draft(rig):
    token = rig.service.preview_send(DRAFT_ID)["token"]
    assert redeem(rig, token, Host())["sent"] is True
    rig.client.raw = draft(body="Edited synthetic body")
    token = rig.service.preview_send(DRAFT_ID)["token"]
    assert redeem(rig, token, Host())["sent"] is True
    assert data_commands(rig) == 2


def test_draft_id_without_brackets_yields_a_working_token(rig):
    bare = DRAFT_ID.strip("<>")
    preview = rig.service.preview_send(bare)
    assert preview["draft_id"] == DRAFT_ID
    grant = rig.tokens.inspect(preview["token"], "send_draft")
    assert grant.selector == {"draft_id": DRAFT_ID}
    assert preview["action_hash"] == approval_hash(
        grant.action, grant.params, grant.selector, grant.messages
    )
    assert preview["action_hash"] == rig.service.preview_send(DRAFT_ID)["action_hash"]
    token = rig.service.preview_send(bare)["token"]
    assert redeem(rig, token, Host())["sent"] is True
    assert records(rig)[-1]["result"] == "sent"
