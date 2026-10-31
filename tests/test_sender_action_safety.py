"""Search can display unsafe senders; destructive approvals must still refuse them."""

import json
from types import SimpleNamespace

import pytest
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations

from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.mail import MailValidationError

FROM = b"From: Sender <sender@example.com>\r\n"
UNSAFE_SENDERS = [
    pytest.param(
        b"From: Sender <sender@example.com>, Other <other@example.invalid>\r\n",
        id="multiple-from-mailboxes",
    ),
    pytest.param(
        b"From: Sender <sender@example.com>, Other <other@example.invalid>\r\n"
        b"Sender: sender@example.com\r\n",
        id="multiple-from-with-single-sender",
    ),
    pytest.param(FROM + FROM, id="duplicate-identical-from-fields"),
    pytest.param(FROM + b"From: Other <other@example.invalid>\r\n", id="duplicate-from-fields"),
    pytest.param(
        FROM + b"Sender: sender@example.com\r\nSender: other@example.invalid\r\n",
        id="duplicate-sender-fields",
    ),
    pytest.param(
        FROM + b"Reply-To: sender@example.com\r\nReply-To: other@example.invalid\r\n",
        id="duplicate-reply-to-fields",
    ),
    pytest.param(
        b'From: "billing@trusted.example" <sender@example.com>\r\n',
        id="address-spoofing-display-name",
    ),
    pytest.param(
        b"From: =?utf-8?q?billing=40trusted.example?= <sender@example.com>\r\n",
        id="encoded-address-spoofing-display-name",
    ),
]


@pytest.fixture
def mailbox(tmp_path):
    sim = BridgeSim()
    client = make_client(sim, tmp_path, call_timeout=300)
    item = sim.add(Msg(hdr("<sender-safety@example.invalid>")))
    rules_path = tmp_path / "sender-rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [{"field": "subject", "contains": "Hello", "folder": "Folders/Reviewed"}],
            }
        )
    )
    yield SimpleNamespace(
        sim=sim,
        client=client,
        item=item,
        rules=FolderRulesService(client.cleanup, rules_path),
    )
    sim.close()
    assert not sim.errors and not sim.violations
    assert all(not worker.is_alive() for _, _, worker in sim.peers)


@pytest.mark.parametrize("sender_headers", UNSAFE_SENDERS)
@pytest.mark.parametrize("action", ["move_to_folder", "move_to_trash"])
def test_unsafe_sender_remains_searchable_but_cannot_receive_move_token(
    mailbox, sender_headers, action
):
    case = mailbox
    case.item.headers = case.item.headers.replace(FROM, sender_headers)
    searched = case.client.search_mail(from_="sender@example.com", include_snippets=False)
    assert searched["total"] == 1

    # Matching is separate from approval safety: a selected ambiguous sender
    # must be reported as blocked, never silently disappear from the preview.
    preview = case.client.preview_action(
        action,
        query={"folder": "INBOX", "from_": "sender@example.com"},
        params={"folder": "Folders/Reviewed"} if action == "move_to_folder" else None,
    )
    assert preview["count"] == searched["total"]
    assert preview["token"] is None
    assert len(preview["blocked"]) == 1 and preview["blocked"][0]["unresolved"]
    assert case.item.folder == "INBOX" and not mutations(case.sim)


@pytest.mark.parametrize("sender_headers", UNSAFE_SENDERS)
def test_sender_ambiguity_added_after_move_preview_refuses_execution(mailbox, sender_headers):
    case = mailbox
    preview = case.client.preview_action(
        "move_to_folder",
        query={"folder": "INBOX", "subject": "Hello"},
        params={"folder": "Folders/Reviewed"},
    )
    assert preview["token"]
    case.item.headers = case.item.headers.replace(FROM, sender_headers)

    with pytest.raises(MailValidationError):
        case.client.move_to_folder(preview["token"])

    assert case.item.folder == "INBOX" and not mutations(case.sim)


@pytest.mark.parametrize("sender_headers", UNSAFE_SENDERS)
@pytest.mark.parametrize("confirm", [False, True], ids=["preview", "confirmation"])
def test_rules_refuse_unsafe_sender_before_any_move(mailbox, sender_headers, confirm):
    case = mailbox
    token = case.rules.preview_rules()["token"] if confirm else None
    if confirm:
        assert token
    case.item.headers = case.item.headers.replace(FROM, sender_headers)

    # A subject rule deliberately selects the row regardless of its From:
    # matching another field must never bypass the sender approval guard.
    with pytest.raises(MailValidationError):
        if confirm:
            case.rules.apply_rules(token)
        else:
            case.rules.preview_rules()

    assert case.item.folder == "INBOX" and not mutations(case.sim)


@pytest.mark.parametrize(
    "sender,query",
    [
        (
            "=?UTF-8?Q?=3D=3FUTF-8=3FQ=3Finfo=3D40acme.example.com=3F=3D?= <evil@example.invalid>",
            "info@acme.example.com",
        ),
        (
            "=?UTF-8?Q?=3D=3FUTF-8=3FQ=3FB=3DC3=3DA4nk=3F=3D?= <offers@example.invalid>",
            "Bänk",
        ),
    ],
)
def test_preview_does_not_decode_sender_display_name_a_second_time(mailbox, sender, query):
    case = mailbox
    case.item.headers = case.item.headers.replace(FROM, f"From: {sender}\r\n".encode())
    searched = case.client.search_mail(from_=query, include_snippets=False)
    preview = case.client.preview_action("move_to_trash", query={"folder": "INBOX", "from_": query})
    # Decoding a display name can itself produce literal encoded-word-looking
    # text. A second parse must not turn that text into a different sender match.
    assert searched["total"] == preview["count"] == 0
    assert preview["token"] is None and not mutations(case.sim)


@pytest.mark.parametrize(
    "sender_headers",
    [
        b"From: Acme \xff <info@acme.example.com>\r\n",
        b"From: Acme <inf\xffo@acme.example.com>\r\n",
    ],
    ids=["invalid-display-name", "invalid-mailbox"],
)
def test_invalid_utf8_sender_remains_discoverable_but_cannot_receive_token(mailbox, sender_headers):
    case = mailbox
    case.item.headers = case.item.headers.replace(FROM, sender_headers)
    searched = case.client.search_mail(from_="acme", include_snippets=False)
    preview = case.client.preview_action(
        "move_to_trash", query={"folder": "INBOX", "from_": "acme"}
    )
    assert searched["total"] == preview["count"] == 1
    assert preview["token"] is None
    assert len(preview["blocked"]) == 1 and preview["blocked"][0]["unresolved"]
    assert case.item.folder == "INBOX" and not mutations(case.sim)


@pytest.mark.parametrize("field", [2, 3, 4], ids=["from", "sender", "reply-to"])
@pytest.mark.parametrize("kind", ["address", "display-name"])
@pytest.mark.parametrize("confirm", [False, True], ids=["preview", "confirmation"])
def test_raw_sender_cannot_hide_protected_envelope_evidence(
    mailbox, monkeypatch, field, kind, confirm
):
    case = mailbox
    preview = (
        case.client.preview_action("move_to_trash", query={"folder": "INBOX"}) if confirm else None
    )
    if confirm:
        assert preview["token"]
    metadata = case.client._metadata

    def disagreeing_metadata(*args, **kwargs):
        result = metadata(*args, **kwargs)
        for row in result.values():
            envelope = row.get("envelope")
            if envelope is None:
                continue
            envelope[field] = (
                [[None, None, b"alerts", b"bank.example.com"]]
                if kind == "address"
                else [[b"Bank Security", None, b"sender", b"example.com"]]
            )
        return result

    monkeypatch.setattr(case.client, "_metadata", disagreeing_metadata)
    if confirm:
        with pytest.raises(MailValidationError):
            case.client.move_to_trash(preview["token"])
    else:
        preview = case.client.preview_action("move_to_trash", query={"folder": "INBOX"})
        assert preview["count"] == 1 and preview["token"] is None
        assert len(preview["blocked"]) == 1 and preview["blocked"][0]["unresolved"]
    assert case.item.folder == "INBOX" and not mutations(case.sim)
