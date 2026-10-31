"""Every mutation token is bound to one previewed action set: offline doubles only."""

import copy
import hashlib
import json
import socket

import pytest
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations
from test_create_folder import FolderBridge
from test_send import DRAFT_ID, Host, draft, local_certificate, records, redeem  # noqa: F401
from test_send import rig as _send_rig
from test_unsubscribe import newsletter
from test_unsubscribe import setup as unsubscribe_mailbox

from proton_mail_mcp.config import Settings
from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.guarded import TokenStore, approval_hash
from proton_mail_mcp.mail import MailError, MailValidationError

rig = _send_rig
ROWS = [
    {"folder": "INBOX", "uidvalidity": "7", "uid": "1", "message_id": "<a@example.test>"},
    {"folder": "INBOX", "uidvalidity": "7", "uid": "2", "message_id": "<b@example.test>"},
]
CLEANUP = [
    ("move_to_trash", "INBOX", None, (), ()),
    ("archive", "INBOX", None, (), ()),
    ("move_to_folder", "INBOX", {"folder": "Folders/Reviewed"}, (), ()),
    ("mark_read", "INBOX", None, (), ()),
    ("mark_unread", "INBOX", None, (), (r"\Seen",)),
    ("add_label", "INBOX", {"label": "Review"}, (), ()),
    ("remove_label", "Labels/Review", {"label": "Review"}, ("Labels/Review",), ()),
]
RULES = {
    "version": 1,
    "rules": [{"field": "subject", "contains": "Invoice", "folder": "Folders/Reviewed"}],
}


def store():
    tokens = TokenStore()
    arguments = {
        "action": "move_to_folder",
        "params": {"folder": "Folders/Reviewed", "mark_read": False},
        "selector": {"query": {"folder": "INBOX"}},
        "messages": copy.deepcopy(ROWS),
    }
    token, _ = tokens.issue(*arguments.values())
    grant = tokens._grants[hashlib.sha256(token.encode("ascii")).digest()]
    return tokens, token, grant, arguments


def test_approval_hash_is_canonical_and_covers_every_component():
    base = approval_hash("archive", {}, {"query": {"folder": "INBOX"}}, ROWS)
    assert base == approval_hash("archive", {}, {"query": {"folder": "INBOX"}}, ROWS[::-1])
    assert len(base) == 64 and int(base, 16) >= 0
    changed_row = copy.deepcopy(ROWS)
    changed_row[1]["seen"] = True
    reviewed = {"folder": "INBOX", "reviewed_rows": copy.deepcopy(ROWS)}
    reviewed_changed = copy.deepcopy(reviewed)
    reviewed_changed["reviewed_rows"][0]["subject"] = "edited"
    variants = [
        approval_hash("move_to_trash", {}, {"query": {"folder": "INBOX"}}, ROWS),
        approval_hash("archive", {"mark_read": True}, {"query": {"folder": "INBOX"}}, ROWS),
        approval_hash("archive", {}, {"query": {"folder": "Archive"}}, ROWS),
        approval_hash("archive", {}, {"query": {"folder": "INBOX"}, "limit": 1}, ROWS),
        approval_hash("archive", {}, {"query": {"folder": "INBOX"}}, ROWS[:1]),
        approval_hash("archive", {}, {"query": {"folder": "INBOX"}}, changed_row),
        approval_hash("apply_rules", {}, reviewed, ROWS),
        approval_hash("apply_rules", {}, reviewed_changed, ROWS),
    ]
    assert base not in variants and len(set(variants)) == len(variants)


@pytest.mark.parametrize(
    "target",
    [
        {"unsubscribe_target": {"method": "POST", "host": "a.test", "url": "https://a.test/1"}},
        {"draft_hash": "0" * 64},
        {"from": "Other <other@example.test>"},
        {"uid": "3"},
    ],
)
def test_approval_hash_changes_with_any_bound_row_detail(target):
    changed = copy.deepcopy(ROWS)
    changed[0].update(target)
    assert approval_hash("archive", {}, {}, changed) != approval_hash("archive", {}, {}, ROWS)


def test_issued_grant_stores_its_action_hash_and_binding():
    tokens, token, grant, arguments = store()
    assert grant.action_hash == grant.binding == approval_hash(*arguments.values())
    assert grant.binding not in repr(grant) and token not in repr(tokens._grants)
    assert tokens.inspect(token, "move_to_folder").action_hash == grant.action_hash


@pytest.mark.parametrize(
    "tamper",
    [
        lambda grant: grant.params.update(folder="Folders/Other"),
        lambda grant: grant.params.update(mark_read=True),
        lambda grant: grant.selector.update(query={"folder": "Archive"}),
        lambda grant: grant.messages.append(dict(ROWS[0], uid="9", message_id="<c@example.test>")),
        lambda grant: grant.messages[0].update(seen=True),
        lambda grant: setattr(grant, "action", "move_to_trash"),
    ],
    ids=["target", "param", "selector", "extra-row", "row-detail", "action"],
)
@pytest.mark.parametrize("operation", ["inspect", "claim"])
def test_any_unbound_change_to_a_stored_grant_refuses_redemption(tamper, operation):
    tokens, token, grant, _ = store()
    tamper(grant)
    action = grant.action
    with pytest.raises(MailValidationError) as error:
        getattr(tokens, operation)(token, action)
    assert "does not match" in str(error.value) and token not in str(error.value)
    with pytest.raises(MailValidationError, match="does not match|already used"):
        tokens.claim(token, action)


def test_continuation_keeps_action_hash_and_binds_its_exact_remainder():
    tokens, token, grant, _ = store()
    claimed = tokens.claim(token, "move_to_folder")
    continuation = tokens.continue_grant(claimed, claimed.messages[1:])
    remainder = tokens.inspect(continuation, "move_to_folder")
    assert remainder.action_hash == grant.action_hash
    assert remainder.binding == approval_hash(
        claimed.action, claimed.params, claimed.selector, claimed.messages[1:]
    )
    assert remainder.binding != remainder.action_hash
    stored = next(iter(tokens._grants.values()))
    stored.messages.append(copy.deepcopy(ROWS[0]))
    with pytest.raises(MailValidationError, match="does not match its approved action set"):
        tokens.claim(continuation, "move_to_folder")


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Approval binding tests must use in-process doubles")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    simulations = []

    def build(simulator=BridgeSim, **options):
        sim = simulator(**options)
        simulations.append(sim)
        return sim, make_client(sim, tmp_path, call_timeout=60)

    yield build
    for sim in simulations:
        sim.close()
        assert not sim.errors and not sim.violations


def cleanup_case(bridge, source, labels, flags, subject="Invoice"):
    sim, client = bridge()
    folder = "INBOX" if source.startswith("Labels/") else source
    items = [
        sim.add(
            Msg(
                hdr(f"<binding-{index}@example.test>", subject=f"{subject} {index}"),
                folder=folder,
                labels=labels,
                flags=flags,
            )
        )
        for index in range(2)
    ]
    return sim, client, items


def edit_subject(item):
    item.headers = item.headers.replace(b"Subject: ", b"Subject: Edited ")


@pytest.mark.parametrize("action,source,params,labels,flags", CLEANUP)
def test_cleanup_payload_change_after_preview_is_refused_before_mutation(
    bridge, action, source, params, labels, flags
):
    sim, client, items = cleanup_case(bridge, source, labels, flags)
    preview = client.preview_action(action, query={"folder": source}, params=params)
    assert preview["token"] and preview["count"] == 2, preview
    grant = client.cleanup.tokens.inspect(preview["token"], action)
    assert preview["action_hash"] == grant.action_hash == grant.binding
    edit_subject(items[1])
    with pytest.raises(MailValidationError):
        getattr(client, action)(preview["token"])
    assert not mutations(sim)


@pytest.mark.parametrize("action,source,params,labels,flags", CLEANUP)
def test_cleanup_token_is_single_use(bridge, action, source, params, labels, flags):
    sim, client, _ = cleanup_case(bridge, source, labels, flags)
    token = client.preview_action(action, query={"folder": source}, params=params)["token"]
    result = getattr(client, action)(token)
    assert result["done"] == 2 and result["token_consumed"] is True
    applied = list(mutations(sim))
    with pytest.raises(MailValidationError, match="already used"):
        getattr(client, action)(token)
    assert mutations(sim) == applied


@pytest.mark.parametrize("action,source,params,labels,flags", CLEANUP)
def test_cleanup_token_expires(bridge, action, source, params, labels, flags):
    sim, client, _ = cleanup_case(bridge, source, labels, flags)
    now = [1000.0]
    client.cleanup.tokens.clock = lambda: now[0]
    token = client.preview_action(action, query={"folder": source}, params=params)["token"]
    now[0] += client.cleanup.tokens.ttl
    with pytest.raises(MailValidationError, match="expired"):
        getattr(client, action)(token)
    assert not mutations(sim)


def rules_case(bridge, tmp_path):
    sim, client, items = cleanup_case(bridge, "INBOX", (), ())
    path = tmp_path / "binding-rules.json"
    path.write_text(json.dumps(RULES))
    return sim, client, items, FolderRulesService(client.cleanup, path)


def test_rules_payload_or_rules_change_after_preview_is_refused(bridge, tmp_path):
    sim, client, items, rules = rules_case(bridge, tmp_path)
    preview = rules.preview_rules("INBOX")
    grant = client.cleanup.tokens.inspect(preview["token"], "apply_rules")
    assert preview["action_hash"] == grant.action_hash
    edit_subject(items[0])
    with pytest.raises(MailValidationError):
        rules.apply_rules(preview["token"])
    assert not mutations(sim)

    sim, client, items, rules = rules_case(bridge, tmp_path)
    token = rules.preview_rules("INBOX")["token"]
    changed = copy.deepcopy(RULES)
    changed["rules"][0]["folder"] = "Folders/Keep"
    rules.path.write_text(json.dumps(changed))
    with pytest.raises(MailValidationError, match="rules changed"):
        rules.apply_rules(token)
    assert not mutations(sim)


def test_rules_token_is_single_use_and_expires(bridge, tmp_path):
    sim, client, _, rules = rules_case(bridge, tmp_path)
    token = rules.preview_rules("INBOX")["token"]
    assert rules.apply_rules(token)["done"] == 2
    applied = list(mutations(sim))
    with pytest.raises(MailValidationError, match="already used"):
        rules.apply_rules(token)
    assert mutations(sim) == applied

    sim, client, _, rules = rules_case(bridge, tmp_path)
    now = [1000.0]
    client.cleanup.tokens.clock = lambda: now[0]
    token = rules.preview_rules("INBOX")["token"]
    now[0] += client.cleanup.tokens.ttl
    with pytest.raises(MailValidationError, match="expired"):
        rules.apply_rules(token)
    assert not mutations(sim)


@pytest.fixture
def unsubscribe_case(tmp_path, monkeypatch):
    settings = Settings(
        token="offline-binding-token-at-least-thirty-two-characters",
        bridge_user="reader@example.test",
        action_log_path=str(tmp_path / "actions.jsonl"),
    )
    mailbox, fake = unsubscribe_mailbox(settings)
    requests = []
    monkeypatch.setattr(
        "proton_mail_mcp.unsubscribe_http.perform",
        lambda target, *, timeout: requests.append(target["url"]) or 200,
    )
    return mailbox, fake, requests


def test_unsubscribe_payload_change_after_preview_is_refused(unsubscribe_case):
    mailbox, fake, requests = unsubscribe_case
    preview = mailbox.preview_action("unsubscribe", query={})
    grant = mailbox.cleanup.tokens.inspect(preview["token"], "unsubscribe")
    assert preview["action_hash"] == grant.action_hash
    fake.folders["INBOX"]["1"] = newsletter(url="https://news.example.com/unsubscribe?other")
    with pytest.raises(MailValidationError):
        mailbox.unsubscribe(preview["token"])
    assert requests == []


def test_unsubscribe_token_is_single_use_and_expires(unsubscribe_case):
    mailbox, _, requests = unsubscribe_case
    token = mailbox.preview_action("unsubscribe", query={})["token"]
    assert mailbox.unsubscribe(token)["count"] == 1 and len(requests) == 1
    with pytest.raises(MailValidationError, match="already used"):
        mailbox.unsubscribe(token)
    now = [1000.0]
    mailbox.cleanup.tokens.clock = lambda: now[0]
    token = mailbox.preview_action("unsubscribe", query={})["token"]
    now[0] += mailbox.cleanup.tokens.ttl
    with pytest.raises(MailValidationError, match="expired"):
        mailbox.unsubscribe(token)
    assert len(requests) == 1


def creates(sim):
    return [command for *_, command in sim.commands if command.startswith("CREATE ")]


def test_create_folder_binding_replay_and_expiry(bridge):
    sim, client = bridge(FolderBridge)
    preview = client.preview_create_folder("Folders/Bound")
    grant = client.cleanup.tokens.inspect(preview["token"], "create_folder")
    assert preview["action_hash"] == grant.action_hash
    stored = next(iter(client.cleanup.tokens._grants.values()))
    stored.params["name"] = "Folders/Other"
    with pytest.raises(MailValidationError, match="does not match its approved action set"):
        client.create_folder(preview["token"])
    assert not creates(sim)

    token = client.preview_create_folder("Folders/Bound")["token"]
    assert client.create_folder(token)["created"] is True
    with pytest.raises(MailValidationError, match="already used"):
        client.create_folder(token)
    assert len(creates(sim)) == 1

    now = [1000.0]
    client.cleanup.tokens.clock = lambda: now[0]
    token = client.preview_create_folder("Folders/Later")["token"]
    now[0] += client.cleanup.tokens.ttl
    with pytest.raises(MailValidationError, match="expired"):
        client.create_folder(token)
    assert len(creates(sim)) == 1


def test_send_payload_change_replay_and_expiry(rig):
    preview = rig.service.preview_send(DRAFT_ID)
    grant = rig.tokens.inspect(preview["token"], "send_draft")
    assert preview["action_hash"] == grant.action_hash == grant.binding
    rig.client.raw = draft(body="A different body after the preview.")
    with pytest.raises((MailError, MailValidationError)):
        redeem(rig, preview["token"], Host())
    assert rig.smtp.sent == [] and records(rig)[-1]["result"] == "draft_changed"

    rig.client.raw = draft()
    token = rig.service.preview_send(DRAFT_ID)["token"]
    assert redeem(rig, token, Host())["sent"] is True
    with pytest.raises(MailValidationError, match="invalid, expired or used"):
        redeem(rig, token, Host())
    assert len(rig.smtp.sent) == 1

    token = rig.service.preview_send(DRAFT_ID)["token"]
    rig.now[0] += rig.tokens.ttl
    with pytest.raises(MailValidationError):
        redeem(rig, token, Host())
    assert len(rig.smtp.sent) == 1 and records(rig)[-1]["result"] == "token_expired"


def test_send_store_refuses_an_unbound_grant_change(rig):
    token = rig.service.preview_send(DRAFT_ID)["token"]
    grant = rig.tokens._grants[hashlib.sha256(token.encode("ascii")).digest()]
    grant.params = {**grant.params, "approval": "token"}
    with pytest.raises(MailValidationError):
        redeem(rig, token, Host())
    assert rig.smtp.calls == [] and records(rig)[-1]["result"] == "token_invalid"
