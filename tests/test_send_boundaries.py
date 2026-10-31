"""Adversarial send boundaries with synthetic mail and in-process MCP clients."""

import asyncio
import hashlib
import json
import threading
from dataclasses import replace
from email import policy
from email.parser import BytesParser

import pytest
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import (
    CancelledNotification,
    CancelledNotificationParams,
    ClientNotification,
    ElicitResult,
)
from test_send import (
    DRAFT_ID,
    RECIPIENT,
    SENDER,
    Host,
    draft,
    local_certificate,  # noqa: F401
    records,
    redeem,
)
from test_send import rig as _send_rig
from test_send_http import _HTTP

from proton_mail_mcp.mail import MailError, MailValidationError
from proton_mail_mcp.server import create_app, create_mcp

rig = _send_rig

CHANGES = (
    "header",
    "to",
    "cc",
    "bcc",
    "from",
    "subject",
    "body",
    "attachment",
    "attachment_name",
    "uid",
    "uidvalidity",
    "gone",
)


def rich_draft():
    return draft(
        cc="carbon@example.test", bcc="blind@example.test", attachment=b"original attachment"
    ).replace(b"MIME-Version: 1.0\r\n", b"X-Reviewed: original\r\nMIME-Version: 1.0\r\n", 1)


def change_draft(rig, change):
    if change in {"uid", "uidvalidity"}:
        rig.client.identity[change] = "99"
    elif change == "gone":
        rig.client.raw = None
    else:
        before, after = {
            "header": (b"X-Reviewed: original", b"X-Reviewed: changed"),
            "to": (b"To: recipient@example.test", b"To: different@example.test"),
            "cc": (b"Cc: carbon@example.test", b"Cc: different@example.test"),
            "bcc": (b"Bcc: blind@example.test", b"Bcc: different@example.test"),
            "from": (b"From: sender@example.test", b"From: alternate@example.test"),
            "subject": (b"Subject: Synthetic", b"Subject: Changed"),
            "body": (b"Synthetic body", b"Changed body"),
            "attachment": (b"b3JpZ2luYWwgYXR0YWNobWVudA==", b"Y2hhbmdlZCBhdHRhY2htZW50"),
            "attachment_name": (b"report.bin", b"changed.bin"),
        }[change]
        assert before in rig.client.raw
        rig.client.raw = rig.client.raw.replace(before, after)


def prepare_rich_draft(rig):
    # The alternate sender is valid and allowlisted: its rejection must come
    # from payload binding, rather than an unrelated address policy.
    rig.service.settings = replace(
        rig.settings, send_from_allowlist=(SENDER, "alternate@example.test")
    )
    rig.client.raw = rich_draft()
    return rig.service.preview_send(DRAFT_ID)


def refuse_or_return_unsent(rig, token, host):
    try:
        result = redeem(rig, token, host)
    except (MailError, MailValidationError):
        pass
    else:
        assert result["sent"] is False


@pytest.mark.parametrize("change", CHANGES)
@pytest.mark.parametrize("stage", ["preview", "confirmation", "credentials", "login", "rcpt"])
def test_changed_draft_refuses_at_each_boundary_and_restoration_cannot_reuse_token(
    rig, monkeypatch, change, stage
):
    preview = prepare_rich_draft(rig)
    original_raw, original_identity = rig.client.raw, dict(rig.client.identity)
    host = Host()
    if stage == "preview":
        change_draft(rig, change)
    elif stage == "confirmation":
        host.during = lambda: change_draft(rig, change)
    else:
        target = rig.client if stage == "credentials" else rig.smtp
        attribute = "credentials_provider" if stage == "credentials" else stage
        original = getattr(target, attribute)
        changed = False

        def mutate(*args, **kwargs):
            nonlocal changed
            result = original(*args, **kwargs)
            if not changed:
                change_draft(rig, change)
                changed = True
            return result

        monkeypatch.setattr(target, attribute, mutate)
    refuse_or_return_unsent(rig, preview["token"], host)
    assert rig.smtp.sent == []
    assert not any(call[0] in {"data", "sendmail"} for call in rig.smtp.calls)
    if stage in {"preview", "confirmation"}:
        assert rig.smtp.calls == []
    else:
        assert sum(call[0] == "connect" for call in rig.smtp.calls) == 1
        assert rig.smtp.calls[-1][0] == "close"
    assert len(host.prompts) == (stage != "preview")
    rig.client.raw, rig.client.identity = original_raw, original_identity
    before = list(rig.smtp.calls)
    retry_host = Host()
    with pytest.raises((MailError, MailValidationError)):
        redeem(rig, preview["token"], retry_host)
    assert retry_host.prompts == []
    assert rig.smtp.calls == before
    assert any(row["draft_hash"] == preview["draft_hash"] for row in records(rig))


def test_exact_wire_and_ordered_envelope_match_independent_hash_oracle_once(rig):
    preview = prepare_rich_draft(rig)
    # Derive canonical transport bytes from the input message, independently of
    # production snapshot/hash helpers. The binding format is versioned v1.
    message = BytesParser(policy=policy.SMTP).parsebytes(rig.client.raw)
    del message["Bcc"]
    expected_wire = message.as_bytes(policy=policy.SMTP)
    if not expected_wire.endswith(b"\r\n"):
        expected_wire += b"\r\n"
    recipients = [RECIPIENT, "carbon@example.test", "blind@example.test"]
    envelope = json.dumps(
        {"from": SENDER, "recipients": recipients},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    expected_hash = hashlib.sha256(
        b"proton-mail-mcp:send:v1\0" + len(envelope).to_bytes(8, "big") + envelope + expected_wire
    ).hexdigest()
    assert preview["draft_hash"] == expected_hash
    host = Host()
    result = redeem(rig, preview["token"], host)
    assert result["sent"] is True and result["draft_hash"] == expected_hash
    assert rig.smtp.sent == [(SENDER, tuple(recipients), expected_wire)]
    assert b"Bcc:" not in expected_wire
    assert sum(call[0] == "data" for call in rig.smtp.calls) == 1
    assert expected_hash in host.prompts[0]
    assert all(row["draft_hash"] == expected_hash for row in records(rig))
    before = list(rig.smtp.calls)
    with pytest.raises((MailError, MailValidationError)):
        redeem(rig, preview["token"])
    assert rig.smtp.calls == before


@pytest.mark.parametrize("action", ["decline", "cancel", "accept"])
def test_protocol_confirmation_is_single_use_and_session_survives(rig, action):
    async def run():
        prompts = []

        async def answer(ctx, params):
            prompts.append(params)
            assert rig.smtp.calls == []
            return ElicitResult(action=action, content={"confirm": True})

        server = create_mcp(rig.settings, rig.client)
        async with create_connected_server_and_client_session(
            server, elicitation_callback=answer
        ) as client:
            preview = await client.call_tool("preview_send", {"draft_id": DRAFT_ID})
            token = preview.structuredContent["token"]
            result = await client.call_tool("send_draft", {"token": token})
            assert result.isError is (action != "accept")
            retry = await client.call_tool("send_draft", {"token": token})
            assert retry.isError is True
            assert len(prompts) == 1
            assert (await client.list_tools()).tools

    asyncio.run(run())
    assert len(rig.smtp.sent) == (action == "accept")
    if action != "accept":
        assert rig.smtp.calls == []


@pytest.mark.parametrize("action", ["decline", "accept"])
def test_concurrent_redemption_cannot_obtain_a_second_confirmation(rig, action):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        host = Host(action=action)
        original = host.elicit

        async def blocked(**kwargs):
            entered.set()
            await release.wait()
            return await original(**kwargs)

        host.elicit = blocked
        token = rig.service.preview_send(DRAFT_ID)["token"]
        first = asyncio.create_task(rig.service.send_draft(token, host))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second_host = Host()
            with pytest.raises((MailError, MailValidationError)):
                await rig.service.send_draft(token, second_host)
            assert second_host.prompts == [] and rig.smtp.calls == []
        finally:
            release.set()
        if action == "accept":
            assert (await first)["sent"] is True
        else:
            with pytest.raises((MailError, MailValidationError)):
                await first
        with pytest.raises((MailError, MailValidationError)):
            await rig.service.send_draft(token, Host())

    asyncio.run(run())
    assert len(rig.smtp.sent) == (action == "accept")


async def wait_for_cancel_audit(rig, outcomes=("cancelled", "confirmation_cancelled")):
    # Read the synthetic fixture directly while the worker may hold the journal
    # lock. Do not release the session until its refusal has been persisted.
    async with asyncio.timeout(3):
        while True:
            if rig.log.path.exists():
                rows = [json.loads(line) for line in rig.log.path.read_text().splitlines()]
                if any(row.get("result") in outcomes for row in rows):
                    return
            await asyncio.sleep(0.005)


def test_protocol_cancellation_during_confirmation_preserves_session_and_burns_token(rig):
    async def run():
        prompted, release = asyncio.Event(), asyncio.Event()

        async def pending_confirmation(ctx, params):
            prompted.set()
            await release.wait()
            return ElicitResult(action="cancel")

        server = create_mcp(rig.settings, rig.client)
        async with create_connected_server_and_client_session(
            server, elicitation_callback=pending_confirmation
        ) as client:
            preview = await client.call_tool("preview_send", {"draft_id": DRAFT_ID})
            token = preview.structuredContent["token"]
            request_id = client._request_id
            pending = asyncio.create_task(client.call_tool("send_draft", {"token": token}))
            await asyncio.wait_for(prompted.wait(), 2)
            await client.send_notification(
                ClientNotification(
                    CancelledNotification(params=CancelledNotificationParams(requestId=request_id))
                )
            )
            await wait_for_cancel_audit(rig)
            # The SDK client awaits elicitation callbacks in its receive loop.
            # Let it read the cancellation acknowledgement only after the
            # server has recorded cancellation of the still-unanswered form.
            release.set()
            with pytest.raises(McpError):
                await asyncio.wait_for(pending, 2)
            assert (await client.list_tools()).tools
            assert (await client.call_tool("send_draft", {"token": token})).isError is True

    asyncio.run(run())
    assert rig.smtp.calls == []
    assert records(rig)[0]["result"] == "confirmation_cancelled"


@pytest.mark.parametrize("stage", ["credentials", "login", "rcpt", "data"])
def test_acknowledged_http_cancellation_respects_data_boundary_and_preserves_session(
    rig, monkeypatch, stage, caplog
):
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

    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            client = _HTTP(app, rig.settings.token)
            await client.initialize({"elicitation": {"form": {}}})
            preview = await client.rpc(
                "tools/call", {"name": "preview_send", "arguments": {"draft_id": DRAFT_ID}}
            )
            token = preview["result"]["structuredContent"]["token"]
            client.on_elicit = lambda prompt: {"action": "accept", "content": {"confirm": True}}
            request_id = client.request_id + 1
            pending = asyncio.create_task(
                client.rpc("tools/call", {"name": "send_draft", "arguments": {"token": token}})
            )
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                if stage == "credentials":
                    assert rig.smtp.calls == []
                status, _ = await client.post(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {
                            "requestId": request_id,
                            "reason": "Synthetic user cancellation",
                        },
                    }
                )
                assert status == 202
                cancelled_reply = await asyncio.wait_for(asyncio.shield(pending), 2)
                assert "error" in cancelled_reply
                assert len(rig.smtp.sent) == (stage == "data")
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)
            await wait_for_cancel_audit(rig, ("sent",) if stage == "data" else ("cancelled",))
            assert (await client.rpc("tools/list"))["result"]["tools"]
            replay = await client.rpc(
                "tools/call", {"name": "send_draft", "arguments": {"token": token}}
            )
            assert replay["result"]["isError"] is True
            assert len(client.elicitations) == 1

    asyncio.run(run())
    assert len(rig.smtp.sent) == (stage == "data")
    assert sum(call[0] in {"data", "sendmail"} for call in rig.smtp.calls) == (stage == "data")
    if stage == "credentials":
        assert rig.smtp.calls == []
    else:
        assert rig.smtp.calls[-1][0] == "close"
    assert "Request already responded to" not in caplog.text


@pytest.mark.parametrize("stage,code", [("mail", 550), ("rcpt", 550), ("rcpt", 421)])
def test_smtp_envelope_rejection_never_reaches_data(rig, monkeypatch, stage, code):
    original = getattr(rig.smtp, stage)

    def reject(*args, **kwargs):
        original(*args, **kwargs)
        return code, b"synthetic refusal"

    monkeypatch.setattr(rig.smtp, stage, reject)
    token = rig.service.preview_send(DRAFT_ID)["token"]
    result = redeem(rig, token)
    assert result["sent"] is False
    assert rig.smtp.sent == []
    assert not any(call[0] in {"data", "sendmail"} for call in rig.smtp.calls)
    assert rig.smtp.calls[-1][0] == "close"


def test_partial_rcpt_refusal_sends_once_and_retains_partial_outcome(rig, monkeypatch):
    rig.client.raw = draft(cc="carbon@example.test")
    original = rig.smtp.rcpt

    def reject_one(recipient, options=()):
        if recipient == RECIPIENT:
            rig.smtp.calls.append(("rcpt", recipient, tuple(options)))
            return 550, b"synthetic refusal"
        return original(recipient, options)

    monkeypatch.setattr(rig.smtp, "rcpt", reject_one)
    token = rig.service.preview_send(DRAFT_ID)["token"]
    result = redeem(rig, token)
    assert result["result"] == "partial" and result["sent"] is False
    assert len(rig.smtp.sent) == 1
    assert rig.smtp.sent[0][1] == ("carbon@example.test",)
    assert sum(call[0] == "data" for call in rig.smtp.calls) == 1
    assert records(rig)[-1]["result"] == "partial"


def test_smtp_size_option_counts_exact_canonical_wire_bytes(rig, monkeypatch):
    rig.smtp.does_esmtp = True
    monkeypatch.setattr(rig.smtp, "has_extn", lambda name: name == "size", raising=False)
    token = rig.service.preview_send(DRAFT_ID)["token"]
    assert redeem(rig, token)["sent"] is True
    options = next(call[2] for call in rig.smtp.calls if call[0] == "mail")
    assert options == (f"size={len(rig.smtp.sent[0][2])}",)


@pytest.mark.parametrize("fail_audit", [False, True])
def test_http_cancellation_during_refusal_audit_survives_success_or_failure(
    rig, monkeypatch, fail_audit, caplog
):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = rig.service._audit
    monkeypatch.setattr("proton_mail_mcp.server.SendService", lambda *args: rig.service)

    def blocked(draft, result):
        if entered.is_set():
            return original(draft, result)
        entered.set()
        try:
            assert release.wait(timeout=5), "Synthetic audit hook was never released"
            if fail_audit:
                raise MailError("Synthetic audit failure")
            return original(draft, result)
        finally:
            finished.set()

    monkeypatch.setattr(rig.service, "_audit", blocked)

    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            client = _HTTP(app, rig.settings.token)
            await client.initialize({"elicitation": {"form": {}}})
            preview = await client.rpc(
                "tools/call", {"name": "preview_send", "arguments": {"draft_id": DRAFT_ID}}
            )
            token = preview["result"]["structuredContent"]["token"]
            client.on_elicit = lambda prompt: {"action": "decline"}
            request_id = client.request_id + 1
            pending = asyncio.create_task(
                client.rpc("tools/call", {"name": "send_draft", "arguments": {"token": token}})
            )
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                status, _ = await client.post(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": request_id},
                    }
                )
                assert status == 202
                assert "error" in await asyncio.wait_for(asyncio.shield(pending), 2)
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)
            assert await asyncio.to_thread(finished.wait, 3)
            assert (await client.rpc("tools/list"))["result"]["tools"]
            retry = await client.rpc(
                "tools/call", {"name": "send_draft", "arguments": {"token": token}}
            )
            assert retry["result"]["isError"] is True
            assert len(client.elicitations) == 1

    asyncio.run(run())
    assert rig.smtp.calls == []
    assert "Request already responded to" not in caplog.text


@pytest.mark.parametrize("stage", ["credentials", "login", "rcpt"])
def test_raw_task_cancellation_joins_worker_and_prevents_later_data(rig, monkeypatch, stage):
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

    async def run():
        token = rig.service.preview_send(DRAFT_ID)["token"]
        task = asyncio.create_task(rig.service.send_draft(token, Host()))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0.01)
            # The cancelled caller must keep ownership of its worker until the
            # worker acknowledges cancellation; an abandoned worker could send.
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        with pytest.raises((MailError, MailValidationError)):
            await rig.service.send_draft(token, Host())

    asyncio.run(run())
    assert rig.smtp.sent == []
    assert not any(call[0] in {"data", "sendmail"} for call in rig.smtp.calls)
    if stage == "credentials":
        assert rig.smtp.calls == []
    else:
        assert rig.smtp.calls[-1][0] == "close"
