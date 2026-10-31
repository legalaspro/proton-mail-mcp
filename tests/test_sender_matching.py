"""Read-only search and guarded previews share decoded sender query semantics."""

import re
from email import policy
from email.parser import BytesParser

import pytest
from test_cleanup_mutations import MutatingImap
from test_mail import client, message, unquote

from proton_mail_mcp.config import Settings
from proton_mail_mcp.sender_matching import sender_matches


@pytest.fixture
def acme(tmp_path):
    variants = [
        "Acme <info@acme.example.com>",
        '"Acme" <INFO@ACME.EXAMPLE.COM>',
        "=?UTF-8?Q?Acme_Espa=C3=B1a?= <info@acme.example.com>",
        "Acme España <info@acme.example.com>",
        "Acme <info (newsletter) @ acme.example.com>",
    ]
    items = {}
    for index in range(25):
        item = message(f"<acme-{index}@example.test>")
        sender = variants[index % len(variants)]
        item.headers = re.sub(
            rb"(?m)^From: [^\r\n]*",
            lambda match: b"From: " + sender.encode(),
            item.headers,
        )
        items[str(index + 1)] = item
    items["26"] = message("<unrelated@example.test>")
    fake = MutatingImap({"INBOX": items, "All Mail": dict(items), "Trash": {}})
    settings = Settings(
        token="synthetic-sender-parity-" + "x" * 32,
        bridge_user="reader@example.test",
        action_log_path=str(tmp_path / "actions.jsonl"),
    )
    return client(settings, fake)


@pytest.mark.parametrize(
    "query,count", [("info@acme.example.com", 25), ("ACME", 25), ("ESPAÑA", 10)]
)
def test_acme_sender_search_and_preview_have_the_same_count(acme, query, count):
    search = acme.search_mail(from_=query, include_snippets=False, limit=100)
    preview = acme.preview_action("move_to_trash", query={"folder": "INBOX", "from_": query})
    assert search["total"] == preview["count"] == count
    assert preview["token"] and not preview["blocked"]
    assert {row["message_id"] for row in search["messages"]} == {
        row["message_id"]
        for row in acme.cleanup.tokens.inspect(preview["token"], "move_to_trash").messages
    }


@pytest.mark.parametrize(
    "sender,query",
    [
        ('"Acme" <INFO@ACME.EXAMPLE.COM>', "acme <info@"),
        ("=?UTF-8?Q?Acme_Espa=C3=B1a?= <info@acme.example.com>", "ESPAÑA"),
        ("Acme <info (newsletter) @ acme.example.com>", "INFO@ACME.EXAMPLE.COM"),
        ("Straße <info@acme.example.com>", "STRASSE"),
    ],
)
def test_shared_sender_matcher_decodes_and_casefolds(sender, query):
    assert sender_matches(sender, query)


def test_raw_utf8_casefold_match_survives_lowercase_only_server(tmp_path, monkeypatch):
    item = message("<unicode-sender@example.test>")
    item.headers = item.headers.replace(
        b"Sender <sender@example.com>", "Straße <info@acme.example.com>".encode()
    )
    fake = MutatingImap({"INBOX": {"1": item}, "All Mail": {"1": item}, "Trash": {}})
    original = fake.uid

    def uid(command, *args):
        if command == "SEARCH" and "FROM" in args:
            fake.calls.append((command, *args))
            # Model Gluon's raw header lowercasing: strasse is not in straße.
            # The old FROM prefilter excluded this UID before local casefolding.
            value = unquote(args[args.index("FROM") + 1]).lower()
            matched = [
                number
                for number, entry in fake.folders[fake.current].items()
                if value
                in str(BytesParser(policy=policy.default).parsebytes(entry.headers)["From"]).lower()
            ]
            return "OK", [" ".join(matched).encode()]
        return original(command, *args)

    monkeypatch.setattr(fake, "uid", uid)
    settings = Settings(
        token="synthetic-sender-casefold-" + "x" * 32,
        bridge_user="reader@example.test",
        action_log_path=str(tmp_path / "actions.jsonl"),
    )
    mailbox = client(settings, fake)
    searched = mailbox.search_mail(from_="STRASSE", include_snippets=False)
    preview = mailbox.preview_action("move_to_trash", query={"from_": "STRASSE"})
    assert searched["total"] == preview["count"] == 1
    assert preview["token"] and not preview["blocked"]
    assert all("FROM" not in call for call in fake.calls if call[0] == "SEARCH")
