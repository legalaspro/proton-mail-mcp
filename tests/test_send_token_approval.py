"""Own-recipient approval capabilities exercised with synthetic local doubles."""

import asyncio
import copy
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from test_send import (
    DRAFT_ID,
    RECIPIENT,
    SENDER,
    TOKEN,
    Host,
    draft,
    local_certificate,  # noqa: F401
    records,
    redeem,
)
from test_send import rig as _send_rig

from proton_mail_mcp import send as send_module
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.guarded import TokenStore
from proton_mail_mcp.mail import MailError, MailValidationError
from proton_mail_mcp.send import SendService

rig = _send_rig
BASE = {"PROTON_MCP_TOKEN": TOKEN, "PROTON_BRIDGE_USER": SENDER}
FAILURES = (MailError, MailValidationError)


@pytest.fixture
def own_rig(rig, monkeypatch):
    monkeypatch.setattr(
        send_module, "_OWN_ADDRESS_SHA256", hashlib.sha256(RECIPIENT.encode("ascii")).hexdigest()
    )
    rig.settings = replace(rig.settings, send_self_no_form=True)
    rig.service.settings = rig.client.settings = rig.settings
    return rig


def assert_no_data(rig):
    assert rig.smtp.sent == []
    assert not any(call[0] in {"data", "sendmail"} for call in rig.smtp.calls)


def assert_burned(rig, token):
    previous = list(rig.smtp.calls)
    with pytest.raises(FAILURES):
        redeem(rig, token, Host())
    assert rig.smtp.calls == previous


@pytest.mark.parametrize(
    "cc,bcc", [(None, None), (RECIPIENT, None), (None, RECIPIENT), (RECIPIENT, RECIPIENT)]
)
def test_own_recipients_require_single_to_for_unprompted_send(own_rig, monkeypatch, cc, bcc):
    rig = own_rig
    rig.client.raw = draft(cc=cc, bcc=bcc)
    host = Host(support=False)
    preview = rig.service.preview_send(DRAFT_ID)
    if cc or bcc:
        assert preview["approval"] == "host_confirmation"
        with pytest.raises(FAILURES):
            redeem(rig, preview["token"], host)
        assert rig.smtp.calls == [] and host.prompts == []
        assert_no_data(rig)
        assert records(rig)[-1]["result"] == "confirmation_unavailable"
        assert_burned(rig, preview["token"])
        return
    confirm = AsyncMock(side_effect=AssertionError("Unexpected confirmation request"))
    monkeypatch.setattr(send_module, "require_confirmation", confirm)
    assert preview["approval"] == "token"
    assert preview["to"] == [RECIPIENT]
    assert preview["cc"] == ([RECIPIENT] if cc else [])
    assert preview["bcc"] == ([RECIPIENT] if bcc else [])
    result = redeem(rig, preview["token"], host)
    assert result["sent"] is True and result["token_consumed"] is True
    assert len(rig.smtp.sent) == 1
    assert rig.smtp.sent[0][:2] == (SENDER, tuple([RECIPIENT] * (1 + bool(cc) + bool(bcc))))
    assert b"Bcc:" not in rig.smtp.sent[0][2]
    assert sum(call[0] == "data" for call in rig.smtp.calls) == 1
    assert host.prompts == []
    confirm.assert_not_awaited()
    assert_burned(rig, preview["token"])
    assert records(rig)[-1]["result"] == "token_invalid"


@pytest.mark.parametrize("field", ["cc", "bcc"])
def test_own_cc_or_bcc_without_to_requires_host_confirmation(own_rig, field):
    rig = own_rig
    rig.client.raw = draft(**{field: RECIPIENT}).replace(
        b"To: " + RECIPIENT.encode() + b"\r\n", b""
    )
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "host_confirmation"
    assert preview["to"] == [] and preview[field] == [RECIPIENT]
    host = Host(support=False)
    with pytest.raises(FAILURES):
        redeem(rig, preview["token"], host)
    assert rig.smtp.calls == [] and host.prompts == []
    assert_no_data(rig)
    assert records(rig)[-1]["result"] == "confirmation_unavailable"
    assert_burned(rig, preview["token"])


@pytest.mark.parametrize(
    "address",
    [
        "other@example.test",
        "recipient@example.test.evil.test",
        "prefixrecipient@example.test",
        "Recipient@example.test",
        "recipient@EXAMPLE.test",
        "recipient+tag@example.test",
        "recipient@xn--exmple-cua.test",
    ],
)
def test_noncanonical_or_nonown_recipient_requires_host(own_rig, address):
    rig = own_rig
    rig.client.raw = draft(to=address)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "host_confirmation"
    host = Host(support=False)
    with pytest.raises(FAILURES):
        redeem(rig, preview["token"], host)
    assert rig.smtp.calls == [] and host.prompts == []
    assert records(rig)[-1]["result"] == "confirmation_unavailable"
    assert_burned(rig, preview["token"])


@pytest.mark.parametrize("field", ["To", "Cc", "Bcc"])
@pytest.mark.parametrize(
    "address",
    [
        "rеcipient@example.test",
        "recipient@exаmple.test",
        "recipient@example.test (Recipient)",
        "Group: recipient@example.test;",
        "recipient@example.test\t",
    ],
)
def test_ambiguous_recipient_headers_cannot_receive_approval(own_rig, field, address):
    rig = own_rig
    raw = draft(cc=RECIPIENT, bcc=RECIPIENT)
    rig.client.raw = raw.replace(
        field.encode() + b": " + RECIPIENT.encode(),
        field.encode() + b": " + address.encode("utf-8"),
    )
    with pytest.raises(FAILURES):
        rig.service.preview_send(DRAFT_ID)
    assert rig.smtp.calls == []


def test_flag_off_still_uses_existing_host_approval(own_rig):
    rig = own_rig
    rig.service.settings = replace(rig.settings, send_self_no_form=False)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "host_confirmation"
    host = Host()
    assert redeem(rig, preview["token"], host)["sent"] is True
    assert len(host.prompts) == 1 and len(rig.smtp.sent) == 1


@pytest.mark.parametrize("support", [False, True])
def test_enabling_flag_after_preview_keeps_host_confirmation_path(own_rig, support):
    rig = own_rig
    rig.service.settings = replace(rig.settings, send_self_no_form=False)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "host_confirmation"
    rig.service.settings = replace(rig.settings, send_self_no_form=True)
    host = Host(support=support)
    if support:
        assert redeem(rig, preview["token"], host)["sent"] is True
        assert len(host.prompts) == 1 and len(rig.smtp.sent) == 1
    else:
        with pytest.raises(FAILURES):
            redeem(rig, preview["token"], host)
        assert rig.smtp.calls == [] and host.prompts == []
        assert records(rig)[-1]["result"] == "confirmation_unavailable"
    assert_burned(rig, preview["token"])


@pytest.mark.parametrize("kind", ["expired", "unknown"])
def test_invalid_token_refuses_before_smtp_and_is_audited(own_rig, kind):
    rig = own_rig
    token = rig.service.preview_send(DRAFT_ID)["token"]
    if kind == "expired":
        rig.now[0] += rig.settings.send_token_ttl + 1
    else:
        token = "unknown-synthetic-preview-token"
    with pytest.raises(FAILURES):
        redeem(rig, token, Host(support=False))
    assert rig.smtp.calls == []
    assert records(rig)[-1]["result"] == ("token_expired" if kind == "expired" else "token_invalid")
    assert_burned(rig, token)


@pytest.mark.parametrize("change", ["body", "subject", "attachment", "draft_id", "from"])
@pytest.mark.parametrize("stage", ["before_send", "rcpt"])
def test_changed_draft_refuses_before_data_and_requires_new_preview(
    own_rig, monkeypatch, change, stage
):
    rig = own_rig
    token = rig.service.preview_send(DRAFT_ID)["token"]
    original_raw = rig.client.raw

    def mutate():
        changes = {
            "body": {"body": "Changed synthetic body"},
            "subject": {"subject": "Changed synthetic subject"},
            "attachment": {"attachment": b"changed synthetic attachment"},
            "draft_id": {"identifier": "<another-draft@example.test>"},
            "from": {"sender": "other@example.test"},
        }
        rig.client.raw = draft(**changes[change])

    if stage == "before_send":
        mutate()
    else:
        original = rig.smtp.rcpt

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            mutate()
            return result

        monkeypatch.setattr(rig.smtp, "rcpt", changed)
    with pytest.raises(FAILURES):
        redeem(rig, token, Host(support=False))
    assert_no_data(rig)
    assert records(rig)[-1]["result"] == "draft_changed"
    rig.client.raw = original_raw
    assert_burned(rig, token)


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
def test_recipient_changed_after_preview_refuses(own_rig, field):
    rig = own_rig
    token = rig.service.preview_send(DRAFT_ID)["token"]
    rig.client.raw = draft(**{field: "other@example.test"})
    with pytest.raises(FAILURES):
        redeem(rig, token, Host())
    assert rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "draft_changed"
    assert_burned(rig, token)


@pytest.mark.parametrize("field", ["to", "cc", "bcc", "recipients"])
def test_token_binds_each_recipient_list_independently_of_draft_hash(own_rig, monkeypatch, field):
    rig = own_rig
    rig.client.raw = draft(cc=RECIPIENT, bcc=RECIPIENT)
    token = rig.service.preview_send(DRAFT_ID)["token"]
    original = rig.service._read

    def altered(identifier):
        snapshot = copy.deepcopy(original(identifier))
        snapshot[field].append(RECIPIENT)
        return snapshot

    monkeypatch.setattr(rig.service, "_read", altered)
    with pytest.raises(FAILURES):
        redeem(rig, token, Host())
    assert rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "draft_changed"
    assert_burned(rig, token)


def test_token_binds_recipient_header_roles_even_when_envelope_and_hash_match(own_rig, monkeypatch):
    rig = own_rig
    token = rig.service.preview_send(DRAFT_ID)["token"]
    original = rig.service._read

    def moved(identifier):
        snapshot = original(identifier)
        snapshot["to"], snapshot["cc"] = [], [RECIPIENT]
        snapshot["recipient_headers"].update(to=None, cc=" " + RECIPIENT)
        assert not send_module._own_recipients(snapshot)
        return snapshot

    monkeypatch.setattr(rig.service, "_read", moved)
    with pytest.raises(FAILURES):
        redeem(rig, token, Host(support=False))
    assert rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "draft_changed"
    assert_burned(rig, token)


@pytest.mark.parametrize("edited", ["Bcc:  {}", "Bcc:{}", 'Bcc: "Blind" <{}>'])
def test_bcc_spelling_edit_alone_voids_the_token(rig, edited):
    blind = "blind@example.test"
    rig.client.raw = draft(bcc=blind)
    before = rig.service._read(DRAFT_ID)
    token = rig.service.preview_send(DRAFT_ID)["token"]
    rig.client.raw = rig.client.raw.replace(f"Bcc: {blind}".encode(), edited.format(blind).encode())
    after = rig.service._read(DRAFT_ID)
    assert after["identity"] == before["identity"] and after["bcc"] == before["bcc"]
    with pytest.raises(FAILURES):
        redeem(rig, token, Host())
    assert rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "draft_changed"


@pytest.mark.parametrize("own,final_read", [(True, 2), (False, 3)], ids=["own-address", "host"])
def test_cancel_during_the_final_read_cannot_reach_data(own_rig, monkeypatch, own, final_read):
    rig = own_rig
    rig.service.settings = replace(rig.settings, send_self_no_form=own)
    token = rig.service.preview_send(DRAFT_ID)["token"]
    rig.client.reads.clear()
    entered, release = threading.Event(), threading.Event()
    original = rig.client.read_draft

    def read(draft_id):
        result = original(draft_id)
        if len(rig.client.reads) == final_read:
            entered.set()
            assert release.wait(timeout=5), "Synthetic read was never released"
        return result

    monkeypatch.setattr(rig.client, "read_draft", read)

    async def run():
        task = asyncio.create_task(rig.service.send_draft(token, Host()))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            # Reaching RCPT shows this is the read just before DATA.
            assert any(call[0] == "rcpt" for call in rig.smtp.calls)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert_no_data(rig)
    outcomes = [row for row in records(rig) if row["action"] == "send_result"]
    assert [(row["result"], row["uncertain"]) for row in outcomes] == [("cancelled", False)]


def test_unknown_approval_path_refuses_without_skipping_the_form_unchecked(own_rig):
    rig = own_rig
    snapshot = rig.service._read(DRAFT_ID)
    token, _ = rig.tokens.issue(
        "send_draft",
        {"approval": "unknown"},
        {"draft_id": DRAFT_ID},
        [send_module._binding(snapshot)],
    )
    with pytest.raises(FAILURES, match="Invalid approval path"):
        redeem(rig, token, Host(support=False))
    assert rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "token_invalid"


@pytest.mark.parametrize("flag,reads", [(True, 2), (False, 3)])
def test_send_rereads_the_draft_only_where_it_decides_something(own_rig, flag, reads):
    rig = own_rig
    rig.service.settings = replace(rig.settings, send_self_no_form=flag)
    token = rig.service.preview_send(DRAFT_ID)["token"]
    rig.client.reads.clear()
    assert redeem(rig, token, Host())["sent"] is True
    assert len(rig.client.reads) == reads


def test_extra_actual_envelope_address_cannot_reach_data(own_rig, monkeypatch):
    rig = own_rig
    original = rig.service._read

    def extra_recipient(identifier):
        snapshot = original(identifier)
        snapshot["recipients"].append("other@example.test")
        return snapshot

    monkeypatch.setattr(rig.service, "_read", extra_recipient)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "host_confirmation"
    with pytest.raises(FAILURES):
        redeem(rig, preview["token"], Host(support=False))
    assert rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "confirmation_unavailable"


def test_envelope_mutation_during_rcpt_refuses_before_data(own_rig, monkeypatch):
    rig = own_rig
    token = rig.service.preview_send(DRAFT_ID)["token"]
    original_read = rig.service._read
    changed = False

    def capture(identifier):
        snapshot = original_read(identifier)
        if changed:
            snapshot["recipients"].append("other@example.test")
        return snapshot

    original_rcpt = rig.smtp.rcpt

    def mutate(address, options=()):
        nonlocal changed
        result = original_rcpt(address, options)
        changed = True
        return result

    monkeypatch.setattr(rig.service, "_read", capture)
    monkeypatch.setattr(rig.smtp, "rcpt", mutate)
    with pytest.raises(FAILURES):
        redeem(rig, token, Host(support=False))
    assert_no_data(rig)
    assert records(rig)[-1]["result"] == "draft_changed"
    assert_burned(rig, token)


def test_expiry_during_smtp_preparation_refuses_data(own_rig, monkeypatch):
    rig = own_rig
    token = rig.service.preview_send(DRAFT_ID)["token"]
    original = rig.smtp.rcpt

    def expire(*args, **kwargs):
        result = original(*args, **kwargs)
        rig.now[0] += 601
        return result

    monkeypatch.setattr(rig.smtp, "rcpt", expire)
    with pytest.raises(FAILURES):
        redeem(rig, token, Host(support=False))
    assert_no_data(rig)
    assert records(rig)[-1]["result"] == "token_expired"
    assert_burned(rig, token)


def test_concurrent_token_redemption_reaches_data_at_most_once(own_rig, monkeypatch):
    rig = own_rig
    token = rig.service.preview_send(DRAFT_ID)["token"]
    barrier = threading.Barrier(4)
    release = threading.Event()
    failure_lock = threading.Lock()
    failures = 0
    original = rig.service._read

    def blocked(identifier):
        # Let all losing claims finish their refusal journals before the winner
        # reserves its budget, so journal contention cannot obscure the claim.
        assert release.wait(timeout=5)
        return original(identifier)

    monkeypatch.setattr(rig.service, "_read", blocked)

    def attempt():
        nonlocal failures
        barrier.wait(timeout=5)
        try:
            return redeem(rig, token, Host(support=False))["sent"]
        except FAILURES:
            with failure_lock:
                failures += 1
                if failures == 3:
                    release.set()
            return False

    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: attempt(), range(4)))
    assert results.count(True) == 1
    assert len(rig.smtp.sent) == 1
    assert sum(call[0] == "data" for call in rig.smtp.calls) == 1


def test_claim_checks_expiry_under_the_atomic_store_lock(own_rig, monkeypatch):
    store = own_rig.tokens
    token = own_rig.service.preview_send(DRAFT_ID)["token"]
    original = store.clock
    checks = []

    def locked_clock():
        checks.append(store._lock.locked())
        return original()

    monkeypatch.setattr(store, "clock", locked_clock)
    store.claim(token, "send_draft")
    assert checks and all(checks)
    with pytest.raises(MailValidationError):
        store.claim(token, "send_draft")


def test_approval_tokens_are_stored_only_as_digests(own_rig):
    rig = own_rig
    token = rig.service.preview_send(DRAFT_ID)["token"]
    assert list(rig.tokens._grants) == [hashlib.sha256(token.encode("ascii")).digest()]
    for grant in rig.tokens._grants.values():
        assert token not in repr(vars(grant))
    assert token not in repr(vars(rig.tokens))


def test_grant_binds_from_draft_and_all_recipient_fields(own_rig):
    rig = own_rig
    rig.client.raw = draft(cc=RECIPIENT, bcc=RECIPIENT)
    preview = rig.service.preview_send(DRAFT_ID)
    grant = rig.tokens.inspect(preview["token"], "send_draft")
    assert grant.selector == {"draft_id": DRAFT_ID}
    bound = grant.messages[0]
    assert bound["message_id"] == DRAFT_ID
    assert bound["draft_hash"] == preview["draft_hash"]
    assert bound["from"] == SENDER
    assert bound["to"] == bound["cc"] == bound["bcc"] == [RECIPIENT]
    assert bound["recipients"] == [RECIPIENT] * 3


def test_ttl_is_capped_even_with_an_injected_long_lived_store(own_rig):
    rig = own_rig
    store = TokenStore(1800, clock=lambda: rig.now[0])
    service = SendService(
        rig.client, rig.settings, tokens=store, log=rig.log, clock=lambda: rig.now[0]
    )
    token = service.preview_send(DRAFT_ID)["token"]
    grant = store.inspect(token, "send_draft")
    assert grant.expires - rig.now[0] <= min(rig.settings.send_token_ttl, 600)
    rig.now[0] += 601
    with pytest.raises(FAILURES):
        asyncio.run(service.send_draft(token, Host(support=False)))
    assert rig.smtp.calls == []
    with pytest.raises(ConfigError):
        Settings.from_env({**BASE, "PROTON_MCP_SEND_TOKEN_TTL": "601"})
    with pytest.raises(ConfigError):
        replace(rig.settings, send_token_ttl=601)


def test_empty_recipient_envelope_cannot_receive_token_approval(own_rig):
    own_rig.client.raw = draft().replace(b"To: " + RECIPIENT.encode() + b"\r\n", b"")
    with pytest.raises(FAILURES):
        own_rig.service.preview_send(DRAFT_ID)
    assert own_rig.smtp.calls == []
