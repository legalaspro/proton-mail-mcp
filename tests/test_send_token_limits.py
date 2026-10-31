"""Own-recipient approval preserves send budgets, transport guards and cancellation."""

import asyncio
import threading
from dataclasses import replace

import pytest
from test_send import DRAFT_ID, OTHER_DRAFT_ID, RECIPIENT, Host, draft, records, redeem, use_draft
from test_send_token_approval import (
    FAILURES,
    assert_burned,
    assert_no_data,
)
from test_send_token_approval import (
    local_certificate as local_certificate,
)
from test_send_token_approval import (
    own_rig as own_rig,
)
from test_send_token_approval import (
    rig as rig,
)


@pytest.mark.parametrize("uncertain", [False, True])
def test_token_path_daily_cap_retains_successful_and_uncertain_reservations(own_rig, uncertain):
    rig = own_rig
    rig.service.settings = replace(rig.settings, send_daily_cap=1)
    preview = rig.service.preview_send(DRAFT_ID)
    use_draft(rig, OTHER_DRAFT_ID)
    other_preview = rig.service.preview_send(OTHER_DRAFT_ID)
    use_draft(rig, DRAFT_ID)
    assert preview["approval"] == other_preview["approval"] == "token"
    if uncertain:
        rig.smtp.failure = "send"
    host = Host(support=False)
    result = redeem(rig, preview["token"], host)
    assert result["result"] == ("smtp_error" if uncertain else "sent")
    assert result["sent"] is (not uncertain)
    assert result["remaining_daily_sends"] == 0
    assert sum(call[0] == "data" for call in rig.smtp.calls) == 1
    assert len(rig.smtp.sent) == (0 if uncertain else 1)
    assert host.prompts == []
    assert sum(record["budget_reserved"] for record in records(rig)) == 1
    assert records(rig)[-1]["action"] == "send_result"
    assert records(rig)[-1]["result"] == result["result"]
    before = list(rig.smtp.calls)
    use_draft(rig, OTHER_DRAFT_ID)
    with pytest.raises(FAILURES, match="budget is exhausted"):
        redeem(rig, other_preview["token"], host)
    use_draft(rig, DRAFT_ID)
    with pytest.raises(FAILURES, match="budget is exhausted"):
        rig.service.preview_send(DRAFT_ID)
    assert rig.smtp.calls == before
    assert_burned(rig, preview["token"])
    assert_burned(rig, other_preview["token"])


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
def test_own_recipient_repetition_does_not_bypass_recipient_cap(own_rig, field):
    rig = own_rig
    rig.service.settings = replace(rig.settings, send_max_recipients=1)
    address = f"{RECIPIENT}, {RECIPIENT}" if field == "to" else RECIPIENT
    rig.client.raw = draft(**{field: address})
    with pytest.raises(FAILURES, match="recipient count"):
        rig.service.preview_send(DRAFT_ID)
    assert rig.tokens._grants == {}
    assert rig.smtp.calls == []


@pytest.mark.parametrize("failure", ["missing_pin", "starttls", "certificate_mismatch"])
def test_token_path_still_requires_tls_and_certificate_verification(own_rig, failure):
    rig = own_rig
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "token"
    host = Host(support=False)
    if failure == "missing_pin":
        rig.service.settings = replace(rig.settings, imap_cert_path=None)
        with pytest.raises(FAILURES, match="certificate pin"):
            redeem(rig, preview["token"], host)
        assert rig.smtp.calls == []
    else:
        if failure == "starttls":
            rig.smtp.failure = "tls"
        else:
            rig.smtp.der = b"synthetic mismatched certificate"
        result = redeem(rig, preview["token"], host)
        assert result["sent"] is False and result["result"] == "smtp_error"
        assert not any(call[0] == "login" for call in rig.smtp.calls)
    assert_no_data(rig)
    assert host.prompts == []
    assert_burned(rig, preview["token"])


@pytest.mark.parametrize("stage", ["credentials", "login", "rcpt"])
def test_token_path_cancellation_joins_worker_and_burns_approval(own_rig, monkeypatch, stage):
    rig = own_rig
    entered, release = threading.Event(), threading.Event()
    target = rig.client if stage == "credentials" else rig.smtp
    attribute = "credentials_provider" if stage == "credentials" else stage
    original = getattr(target, attribute)

    def blocked(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=5), "Synthetic transport hook was never released"
        return result

    monkeypatch.setattr(target, attribute, blocked)
    host = Host(support=False)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "token"

    async def run():
        task = asyncio.create_task(rig.service.send_draft(preview["token"], host))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert_no_data(rig)
    assert host.prompts == []
    assert any(record["result"] == "cancelled" for record in records(rig))
    assert sum(record["budget_reserved"] for record in records(rig)) == (stage != "credentials")
    if stage == "credentials":
        assert rig.smtp.calls == []
    else:
        assert rig.smtp.calls[-1][0] == "close"
    assert_burned(rig, preview["token"])
