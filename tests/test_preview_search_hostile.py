"""Server SEARCH only narrows candidates; local selector semantics remain authoritative."""

import re
from email import policy
from email.parser import BytesParser

import pytest
from test_cleanup_mutations import MutatingImap
from test_mail import client, message, unquote

from proton_mail_mcp.config import Settings


@pytest.fixture
def search_case(tmp_path, monkeypatch):
    def build(items, *, widen=True):
        fake = MutatingImap({"INBOX": items, "All Mail": dict(items), "Trash": {}})
        original = fake.uid

        def uid(command, *args):
            if command == "SEARCH" and widen:
                fake.calls.append((command, *args))
                return "OK", [" ".join(fake.folders[fake.current]).encode()]
            return original(command, *args)

        monkeypatch.setattr(fake, "uid", uid)
        settings = Settings(
            token="synthetic-search-token-" + "x" * 32,
            bridge_user="reader@example.test",
            action_log_path=str(tmp_path / "synthetic-actions.jsonl"),
        )
        return client(settings, fake), fake

    return build


@pytest.mark.parametrize(
    "field,needle", [("from_", "ENDER@EXAMPLE"), ("to", "READER@"), ("subject", "ELLo")]
)
def test_widened_search_preserves_casefold_substring_filter(search_case, field, needle):
    wanted = message("<wanted@example.test>")
    other = message("<other@example.test>", subject="Unrelated")
    other.headers = other.headers.replace(b"sender@example.com", b"elsewhere@example.test")
    other.headers = other.headers.replace(b"reader@example.com", b"nobody@example.test")
    mailbox, fake = search_case({"1": wanted, "2": other})
    preview = mailbox.preview_action("move_to_trash", query={"folder": "INBOX", field: needle})
    assert preview["token"] and preview["count"] == 1
    grant = mailbox.cleanup.tokens.inspect(preview["token"], "move_to_trash")
    assert [row["uid"] for row in grant.messages] == ["1"]
    searches = [call for call in fake.calls if call[0] == "SEARCH"]
    if field == "from_":
        assert all("FROM" not in call for call in searches)
    else:
        assert any(field.upper() in call[3:] for call in searches)


@pytest.mark.parametrize("needle", ['Hello ") ALL (" \\ path', "café Straße"])
def test_selector_search_quoting_and_unicode_keep_exact_client_semantics(search_case, needle):
    wanted = message("<quoted@example.test>", subject=needle)
    other = message("<other@example.test>", subject="Unrelated")
    mailbox, _ = search_case({"1": wanted, "2": other}, widen=False)
    preview = mailbox.preview_action("move_to_trash", query={"folder": "INBOX", "subject": needle})
    assert preview["token"] and preview["count"] == 1
    assert [
        row["uid"]
        for row in mailbox.cleanup.tokens.inspect(preview["token"], "move_to_trash").messages
    ] == ["1"]


@pytest.mark.parametrize(
    "subject,needle", [("Straße", "STRASSE"), ("oﬃce", "OFFICE"), ("ος", "οσ")]
)
def test_server_lowercase_search_can_safely_omit_client_casefold_matches(
    search_case, monkeypatch, subject, needle
):
    wanted = message("<unicode-fold@example.test>", subject=subject)
    other = message("<other@example.test>", subject="Unrelated")
    mailbox, fake = search_case({"1": wanted, "2": other}, widen=False)
    original = fake.uid

    def uid(command, *args):
        if command == "SEARCH" and "SUBJECT" in args:
            fake.calls.append((command, *args))
            if fake.literal is not None:
                term = fake.literal.decode("utf-8").lower()
                fake.literal = None
            else:
                term = unquote(args[args.index("SUBJECT") + 1]).lower()
            matches = []
            for number, item in fake.folders[fake.current].items():
                headers = BytesParser(policy=policy.default).parsebytes(item.headers)
                if term in str(headers["Subject"]).lower():
                    matches.append(number)
            return "OK", [" ".join(matches).encode()]
        return original(command, *args)

    monkeypatch.setattr(fake, "uid", uid)
    preview = mailbox.preview_action("move_to_trash", query={"folder": "INBOX", "subject": needle})
    # A server's weaker case matching may omit a client-equivalent candidate.
    # Refuse an empty plan instead of falling back to a whole-folder inventory.
    assert preview["token"] is None and preview["count"] == 0
    searches = [call for call in fake.calls if call[0] == "SEARCH"]
    assert searches and all("SUBJECT" in call for call in searches)
    assert not any(call[0] == "FETCH" for call in fake.calls)


@pytest.mark.parametrize("field", ["from_", "to"])
@pytest.mark.parametrize(
    "raw,needle",
    [
        ('"Reviewer" <{address}>', "Reviewer <"),
        ("Reviewer <{local}(comment)@example.com>", "{address}"),
        ("Reviewer <{local} (comment) @ example.com>", "{address}"),
        ('Reviewer <"{local}"@example.com>', "{address}"),
        (r'"Re\viewer" <{address}>', "Reviewer"),
        ("=?utf-8?b?UmV2aWV3ZXI=?= <{address}>", "Reviewer"),
    ],
    ids=[
        "quoted-display",
        "local-comment",
        "address-spaces",
        "quoted-local",
        "quoted-pair",
        "encoded-display",
    ],
)
def test_raw_address_search_preserves_reconstructed_envelope_matches(
    search_case, monkeypatch, field, raw, needle
):
    local = "sender" if field == "from_" else "reader"
    values = {"local": local, "address": local + "@example.com"}
    raw, needle = raw.format(**values), needle.format(**values)
    wanted = message("<formatted-address@example.test>")
    header = b"From" if field == "from_" else b"To"
    wanted.headers = re.sub(
        rb"(?m)^" + header + rb": [^\r\n]*",
        lambda match: header + b": " + raw.encode(),
        wanted.headers,
    )
    other = message("<other@example.test>")
    other.headers = other.headers.replace(b"sender@example.com", b"outside@example.invalid")
    other.headers = other.headers.replace(b"reader@example.com", b"elsewhere@example.invalid")
    mailbox, fake = search_case({"1": wanted, "2": other}, widen=False)
    original = fake.uid

    def uid(command, *args):
        if command != "SEARCH" or not any(key in args for key in ("FROM", "TO")):
            return original(command, *args)
        fake.calls.append((command, *args))
        matches = []
        for number, item in fake.folders[fake.current].items():
            headers = BytesParser(policy=policy.compat32).parsebytes(item.headers)
            terms = list(args[2:])

            def criterion():
                key = terms.pop(0)
                if key == "OR":
                    left, right = criterion(), criterion()
                    return left or right
                assert key in {"FROM", "TO"}
                term = unquote(terms.pop(0)).lower()
                return term in str(headers[key]).lower()

            results = []
            while terms:
                results.append(criterion())
            if all(results):
                matches.append(number)
        return "OK", [" ".join(matches).encode()]

    monkeypatch.setattr(fake, "uid", uid)
    preview = mailbox.preview_action("move_to_trash", query={"folder": "INBOX", field: needle})
    assert preview["token"] and preview["count"] == 1
    grant = mailbox.cleanup.tokens.inspect(preview["token"], "move_to_trash")
    assert [row["uid"] for row in grant.messages] == ["1"]
    searches = [call for call in fake.calls if call[0] == "SEARCH"]
    if field == "from_":
        assert all("FROM" not in call for call in searches)
    else:
        assert any("TO" in call for call in searches)


def test_widened_search_refilters_internaldate_boundaries_and_all_fields(search_case):
    dates = [
        "11-Oct-2026 23:59:59 -1200",
        "12-Oct-2026 00:00:00 +1400",
        "17-Oct-2026 23:59:59 -1200",
        "18-Oct-2026 00:00:00 +1400",
    ]
    items = {
        str(i): message(f"<day-{i}@example.test>", internaldate=stamp)
        for i, stamp in enumerate(dates, 1)
    }
    mailbox, _ = search_case(items)
    preview = mailbox.preview_action(
        "move_to_trash",
        query={
            "folder": "INBOX",
            "since": "2026-10-12",
            "before": "2026-10-18",
            "from_": "SENDER@",
            "to": "reader@",
            "subject": "hello",
        },
    )
    assert preview["token"] and preview["count"] == 2
    grant = mailbox.cleanup.tokens.inspect(preview["token"], "move_to_trash")
    assert {row["uid"] for row in grant.messages} == {"2", "3"}


def test_preview_fetch_requires_header_peek_and_readonly_commands(search_case, monkeypatch):
    mailbox, fake = search_case({"1": message("<peek@example.test>")})
    original = fake.uid
    checked = []

    def uid(command, *args):
        if command == "FETCH":
            checked.append(args[1])
        return original(command, *args)

    monkeypatch.setattr(fake, "uid", uid)
    try:
        preview = mailbox.preview_action("move_to_trash", query={"folder": "INBOX"})
    finally:
        # A failed preview must obey the same read-only protocol boundary.
        for query in checked:
            assert query == "(UID ENVELOPE)" or "BODY.PEEK[HEADER.FIELDS (" in query, (
                "Preview must use DB envelopes or PEEK headers"
            )
            assert "BODY[" not in query and "BODY.PEEK[]" not in query
            if query != "(UID ENVELOPE)":
                assert "RFC822.SIZE" in query and "INTERNALDATE" in query
        assert not any(call[0] in {"STORE", "MOVE", "COPY", "EXPUNGE"} for call in fake.calls)
    assert preview["token"] and preview["count"] == 1 and checked
    assert not any(call[0] == "SELECT" for call in fake.calls)
