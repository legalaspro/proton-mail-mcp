"""Thread expansion remains bounded with an offline, command-recording IMAP double."""

import pytest
from test_mail import ATTACHMENT, PLAIN, FakeImap, client, message

from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import CallDeadlineExceeded, MailError
from proton_mail_mcp.threading import (
    MAX_THREAD_BODY_BYTES,
    MAX_THREAD_BODY_PARTS,
    MAX_THREAD_HEADER_BYTES,
)


@pytest.fixture
def settings():
    return Settings(
        token="a-test-token-that-is-at-least-32-characters", bridge_user="reader@example.com"
    )


def test_thread_uses_all_mail_combined_search_and_batched_metadata(settings):
    fake = FakeImap(
        {
            "INBOX": {"1": message("<root@example.com>")},
            "Labels/One": {"2": message("<root@example.com>")},
            "All Mail": {
                "100": message("<root@example.com>"),
                "101": message("<child@example.com>", reply="<root@example.com>"),
                "102": message("<sibling@example.com>", reply="<root@example.com>"),
            },
        },
        folder_flags={"All Mail": r"\All"},
        uidnext=True,
    )
    found = client(settings, fake).read_thread("<root@example.com>")
    assert found["partial"] is False
    # Only the root has a real-folder copy; the others keep null canonical fields.
    assert {
        item["message_id"]: (item["folder"], item["uid"], item["alias"]["uid"])
        for item in found["messages"]
    } == {
        "<root@example.com>": ("INBOX", "1", "100"),
        "<child@example.com>": (None, None, "101"),
        "<sibling@example.com>": (None, None, "102"),
    }
    assert all(
        item["uidvalidity"] is None
        for item in found["messages"]
        if item["message_id"] != "<root@example.com>"
    )
    assert all(item["text"] == "Hello plain text" for item in found["messages"])
    assert all(item["alias"]["folder"] == "All Mail" for item in found["messages"])
    assert all(item["alias"]["uidvalidity"] == "1" for item in found["messages"])
    examined = [call[1] for call in fake.calls if call[0] == "EXAMINE"]
    assert examined[0] == '"All Mail"' and '"Labels/One"' not in examined
    canonical_searches = [
        call for call in fake.calls if call[0] == "SEARCH" and "HEADER" not in call
    ]
    assert canonical_searches
    assert all(set(call[3:]) <= {"OR", "ON", "14-Oct-2026"} for call in canonical_searches)
    searches = [call for call in fake.calls if call[0] == "SEARCH" and "HEADER" in call]
    assert len(searches) == 3
    assert searches[0][3:5] == ("HEADER", "Message-ID")
    assert all("OR" in call for call in searches[1:])
    assert all(
        all(field in call for field in ("Message-ID", "References", "In-Reply-To"))
        for call in searches[1:]
    )
    metadata = [call for call in fake.calls if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]]
    assert [call[1] for call in metadata] == ["100", "101,102"]
    assert all("BODYSTRUCTURE" not in call[2] for call in metadata)
    assert [
        call[1] for call in fake.calls if call[0] == "FETCH" and call[2] == "(BODYSTRUCTURE)"
    ] == ["100", "101", "102"]


def test_requested_message_body_survives_expansion_deadline(settings, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])
    fake = FakeImap(
        {
            "INBOX": {
                "1": message("<root@example.com>", sections={"1": b"Requested body"}),
                "2": message("<child@example.com>", reply="<root@example.com>"),
            }
        }
    )
    mailbox = client(settings, fake)
    deadline = mailbox._deadline()
    monkeypatch.setattr(mailbox, "_deadline", lambda: deadline)
    original_uid = fake.uid
    searches = 0

    def expire_during_expansion(command, *args):
        nonlocal searches
        result = original_uid(command, *args)
        if command == "SEARCH":
            searches += 1
            if searches == 3:
                clock[0] = deadline.io_end
        return result

    monkeypatch.setattr(fake, "uid", expire_during_expansion)
    found = mailbox.read_thread("<root@example.com>")
    assert found["partial"] is True
    assert found["partial_reason"] == "thread expansion limited"
    assert found["timed_out"] is True and "Thread read timed out" in found["error"]
    root = next(item for item in found["messages"] if item["message_id"] == "<root@example.com>")
    assert root["text"] == "Requested body"
    assert root["truncated"] is False
    child = next(item for item in found["messages"] if item["message_id"] == "<child@example.com>")
    assert child["truncated"] is True


@pytest.mark.parametrize("folder", ["INBOX", "Drafts", "All Mail", "Labels/One"])
def test_include_thread_false_only_reads_requested_message(settings, monkeypatch, folder):
    fake = FakeImap(
        {
            folder: {
                "1": message("<root@example.com>"),
                "2": message("<child@example.com>", reply="<root@example.com>"),
            }
        }
    )

    def no_lookup(*args, **kwargs):
        pytest.fail("Direct reads must skip canonical lookup")

    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_views", no_lookup)
    found = client(settings, fake).read_thread("<root@example.com>", include_thread=False)
    assert found["partial"] is False
    assert [item["message_id"] for item in found["messages"]] == ["<root@example.com>"]
    [item] = found["messages"]
    assert (item["folder"], item["uid"], item["uidvalidity"]) == (None, None, None)
    assert item["alias"] == {"folder": folder, "uid": "1", "uidvalidity": "1"}
    searches = [call for call in fake.calls if call[0] == "SEARCH"]
    assert len(searches) == 1
    assert searches[0][3] == "UID"
    assert searches[0][5:7] == ("HEADER", "Message-ID")
    assert "References" not in searches[0]
    assert not any(call[0] == "FETCH" and call[1] == "2" for call in fake.calls)


def test_thread_body_has_explicit_byte_cap_and_truncated_flag(settings):
    payload = b"x" * (MAX_THREAD_BODY_BYTES + 100)
    structure = PLAIN.replace(b"24 2", str(len(payload)).encode() + b" 2")
    fake = FakeImap(
        {
            "INBOX": {
                "1": message("<root@example.com>", structure=structure, sections={"1": payload})
            }
        }
    )
    found = client(settings, fake).read_thread("<root@example.com>", include_thread=False)
    root = found["messages"][0]
    assert len(root["text"]) == MAX_THREAD_BODY_BYTES
    assert root["truncated"] is True
    assert root["body_size_bytes"] == len(payload)
    assert root["body_fetched_bytes"] == MAX_THREAD_BODY_BYTES
    assert root["body_limit_bytes"] == MAX_THREAD_BODY_BYTES
    assert root["size_bytes"] == len(fake.folders["INBOX"]["1"].headers) + len(payload)
    assert found["partial"] is False
    assert ("FETCH", "1", f"(BODY.PEEK[1]<0.{MAX_THREAD_BODY_BYTES}>)") in fake.calls


def test_thread_caps_text_part_round_trips_and_never_fetches_attachment(settings):
    count = MAX_THREAD_BODY_PARTS + 20
    structure = b"(" + b" ".join([PLAIN] * count + [ATTACHMENT]) + b' "MIXED" NIL NIL NIL)'
    # Empty text parts used to leave the byte budget untouched indefinitely.
    sections = {str(index): b"" for index in range(1, count + 1)}
    fake = FakeImap(
        {"INBOX": {"1": message("<many@example.com>", structure=structure, sections=sections)}}
    )
    result = client(settings, fake).read_thread("<many@example.com>", include_thread=False)
    item = result["messages"][0]
    assert item["truncated"] is True
    assert item["body_size_bytes"] == count * 24
    assert item["body_fetched_bytes"] == 0
    assert item["body_parts_fetched"] == MAX_THREAD_BODY_PARTS
    assert item["attachments"] == [
        {"filename": "invoice.pdf", "type": "application/pdf", "size": 400}
    ]
    body_requests = [
        call[2] for call in fake.calls if call[0] == "FETCH" and call[2].startswith("(BODY.PEEK[")
    ]
    assert body_requests == [
        f"(BODY.PEEK[{index}]<0.{MAX_THREAD_BODY_BYTES}>)"
        for index in range(1, MAX_THREAD_BODY_PARTS + 1)
    ]
    assert [call for call in fake.calls if call[0] == "FETCH" and call[2] == "(BODYSTRUCTURE)"] == [
        ("FETCH", "1", "(BODYSTRUCTURE)")
    ]


def test_thread_selected_headers_have_a_separate_partial_fetch_cap(settings):
    item = message("<headers@example.com>")
    item.headers += b"References: " + b"x" * (MAX_THREAD_HEADER_BYTES + 100) + b"\r\n\r\n"
    fake = FakeImap({"INBOX": {"1": item}})
    result = client(settings, fake).read_thread("<headers@example.com>", include_thread=False)
    found = result["messages"][0]
    assert result["partial"] is True
    assert found["headers_truncated"] is True
    assert found["headers_fetched_bytes"] == MAX_THREAD_HEADER_BYTES
    assert found["truncated"] is True
    metadata = next(
        call[2] for call in fake.calls if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]
    )
    assert f"]<0.{MAX_THREAD_HEADER_BYTES}>" in metadata


@pytest.mark.parametrize(
    "parameters,disposition",
    [
        (b"NIL", b'("INLINE" ("FILENAME" "attached.mime"))'),
        (b'("NAME" "attached.mime")', b"NIL"),
    ],
)
def test_thread_never_fetches_text_in_a_named_multipart_attachment(
    settings, parameters, disposition
):
    attached = b"(" + PLAIN + b' "MIXED" ' + parameters + b" " + disposition + b" NIL)"
    structure = b"(" + PLAIN + b" " + attached + b' "MIXED" NIL NIL NIL)'
    fake = FakeImap(
        {
            "INBOX": {
                "1": message(
                    "<attached@example.com>",
                    structure=structure,
                    sections={"1": b"Visible narrative"},
                )
            }
        }
    )
    result = client(settings, fake).read_thread("<attached@example.com>", include_thread=False)
    assert result["messages"][0]["text"] == "Visible narrative"
    assert not any(call[0] == "FETCH" and "BODY.PEEK[2" in call[2] for call in fake.calls)


def test_thread_keeps_successful_text_prefix_if_a_later_mime_part_times_out(settings, monkeypatch):
    structure = b"(" + PLAIN + b" " + PLAIN + b' "MIXED" NIL NIL NIL)'
    prefix = b"Successfully fetched prefix"
    fake = FakeImap(
        {
            "INBOX": {
                "1": message(
                    "<prefix@example.com>",
                    structure=structure,
                    sections={"1": prefix, "2": b"Unavailable part"},
                )
            }
        }
    )
    original_uid = fake.uid

    def stalled_second_part(command, *args):
        if command == "FETCH" and args[1].startswith("(BODY.PEEK[2]"):
            raise CallDeadlineExceeded("Synthetic timed-out part")
        return original_uid(command, *args)

    monkeypatch.setattr(fake, "uid", stalled_second_part)
    result = client(settings, fake).read_thread("<prefix@example.com>", include_thread=False)
    item = result["messages"][0]
    assert result["partial"] is True and "timed out" in result["partial_reason"]
    assert result["timed_out"] is True and result["error"] == result["partial_reason"]
    assert item["truncated"] is True
    assert item["text"] == prefix.decode()
    assert item["body_fetched_bytes"] == len(prefix)
    assert item["body_parts_fetched"] == 1


def test_thread_depth_cap_returns_available_messages_as_partial(settings, monkeypatch):
    monkeypatch.setattr("proton_mail_mcp.threading.MAX_THREAD_DEPTH", 2)
    fake = FakeImap(
        {
            "INBOX": {
                str(index + 1): message(
                    f"<{index}@example.com>",
                    reply=f"<{index - 1}@example.com>" if index else None,
                )
                for index in range(6)
            }
        }
    )
    found = client(settings, fake).read_thread("<0@example.com>")
    assert found["partial"] is True
    assert {item["message_id"] for item in found["messages"]} == {
        "<0@example.com>",
        "<1@example.com>",
        "<2@example.com>",
    }
    assert len([call for call in fake.calls if call[0] == "SEARCH"]) == 3


def test_include_thread_requires_a_boolean_before_connecting(settings):
    fake = FakeImap()
    with pytest.raises(ValueError, match="boolean"):
        client(settings, fake).read_thread("<root@example.com>", include_thread="false")
    assert not fake.calls


def test_large_reply_set_cannot_displace_requested_high_uid_message(settings):
    messages = {
        str(uid): message(f"<reply-{uid}@example.com>", reply="<root@example.com>")
        for uid in range(1, 151)
    }
    messages["1000"] = message("<root@example.com>", sections={"1": b"Requested body"})
    fake = FakeImap({"INBOX": messages})
    found = client(settings, fake).read_thread("<root@example.com>")
    assert found["partial"] is True
    assert len(found["messages"]) == 100
    root = next(item for item in found["messages"] if item["message_id"] == "<root@example.com>")
    assert root["text"] == "Requested body"
    assert root["uid"] == "1000"
    assert [call[1] for call in fake.calls if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]][
        0
    ] == "1000"
    retained_uids = {item["uid"] for item in found["messages"]}
    assert all(
        call[1] in retained_uids
        for call in fake.calls
        if call[0] == "FETCH" and "HEADER.FIELDS" not in call[2]
    )
    assert fake.calls[-1] == ("LOGOUT",)


def test_unicode_thread_links_use_targeted_literal_searches(settings):
    fake = FakeImap(
        {
            "INBOX": {
                "1": message("<café@example.com>"),
                "2": message("<child@example.com>", reply="<café@example.com>"),
                "3": message("<unrelated@example.com>"),
            }
        }
    )
    found = client(settings, fake).read_thread("<café@example.com>")
    assert found["partial"] is False
    assert {item["message_id"] for item in found["messages"]} == {
        "<café@example.com>",
        "<child@example.com>",
    }
    assert fake.search_literals == ["café@example.com".encode()] * 4
    searches = [call for call in fake.calls if call[0] == "SEARCH"]
    assert searches[0][3:] == ("UID", "1:3", "HEADER", "Message-ID")
    assert [call[3:] for call in searches[1:4]] == [
        ("HEADER", field) for field in ("Message-ID", "References", "In-Reply-To")
    ]
    assert not any(call[0] == "SEARCH" and "ALL" in call for call in fake.calls)
    assert all(
        argument.isascii()
        for call in fake.calls
        if call[0] == "SEARCH"
        for argument in call[1:]
        if isinstance(argument, bytes)
    )
    assert not any(
        call[0] == "FETCH" and call[1] == "3" and "HEADER.FIELDS" not in call[2]
        for call in fake.calls
    )


@pytest.mark.parametrize("unicode_seed", [True, False])
def test_later_frontier_reuses_previously_nonmatching_cached_headers(
    settings, monkeypatch, unicode_seed
):
    seed_id = "<café@example.com>" if unicode_seed else "<child@example.com>"
    fake = FakeImap(
        {
            "INBOX": {
                "1": message("<root@example.com>"),
                "2": message("<parent@example.com>", reply="<root@example.com>"),
                "3": message("<sibling@example.com>", reply="<root@example.com>"),
                "4": message(seed_id, reply="<parent@example.com>"),
            }
        }
    )
    original_uid = fake.uid
    searches = 0

    def include_initial_false_positives(command, *args):
        nonlocal searches
        response = original_uid(command, *args)
        if command == "SEARCH":
            searches += 1
            if searches == 2 and not unicode_seed:
                # Header SEARCH is a substring filter; exact client matching can
                # reject candidates that connect to a later discovered ancestor.
                return "OK", [b"1 2 3 4"]
        return response

    monkeypatch.setattr(fake, "uid", include_initial_false_positives)
    found = client(settings, fake).read_thread(seed_id)
    assert found["partial"] is False
    assert {item["message_id"] for item in found["messages"]} == {
        "<root@example.com>",
        "<parent@example.com>",
        "<sibling@example.com>",
        seed_id,
    }
    metadata_uids = [
        uid
        for call in fake.calls
        if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]
        for uid in call[1].split(",")
    ]
    assert sorted(metadata_uids) == ["1", "2", "3", "4"]
    body_uids = [
        call[1] for call in fake.calls if call[0] == "FETCH" and call[2].startswith("(BODY.PEEK[")
    ]
    assert sorted(body_uids) == ["1", "2", "3", "4"]
    structure_uids = [
        call[1] for call in fake.calls if call[0] == "FETCH" and call[2] == "(BODYSTRUCTURE)"
    ]
    assert sorted(structure_uids) == ["1", "2", "3", "4"]


@pytest.mark.parametrize("status", ["NO", "BAD"])
def test_requested_body_survives_failed_expansion_search(settings, monkeypatch, status):
    fake = FakeImap(
        {"INBOX": {"1": message("<root@example.com>", sections={"1": b"Requested body"})}}
    )
    original_uid = fake.uid

    def fail_expansion(command, *args):
        if command == "SEARCH" and "OR" in args:
            fake.calls.append((command, *args))
            return status, [b"synthetic server error"]
        return original_uid(command, *args)

    monkeypatch.setattr(fake, "uid", fail_expansion)
    found = client(settings, fake).read_thread("<root@example.com>")
    assert found["partial"] is True
    assert found["messages"][0]["text"] == "Requested body"
    assert found["messages"][0]["truncated"] is False
    assert len([call for call in fake.calls if call[0] == "SEARCH"]) == 2


@pytest.mark.parametrize("malformed", ["structure", "base64"])
def test_requested_body_survives_malformed_related_message(settings, malformed):
    structure = b'("TEXT")' if malformed == "structure" else PLAIN.replace(b'"7BIT"', b'"BASE64"')
    fake = FakeImap(
        {
            "INBOX": {
                "1": message("<root@example.com>", sections={"1": b"Requested body"}),
                "2": message(
                    "<child@example.com>",
                    reply="<root@example.com>",
                    structure=structure,
                    sections={"1": b"a!!!"},
                ),
                "3": message("<later@example.com>", reply="<root@example.com>"),
            }
        }
    )
    found = client(settings, fake).read_thread("<root@example.com>")
    assert found["partial"] is True
    assert found["messages"][0]["text"] == "Requested body"
    assert found["messages"][0]["truncated"] is False
    assert all(item["truncated"] is True for item in found["messages"][1:])
    assert not any(
        call[0] == "FETCH" and call[1] == "3" and "HEADER.FIELDS" not in call[2]
        for call in fake.calls
    )


def test_thread_failure_before_seed_is_an_error(settings, monkeypatch):
    fake = FakeImap({"INBOX": {}})
    original_uid = fake.uid

    def fail_search(command, *args):
        if command == "SEARCH":
            return "NO", [b"synthetic server error"]
        return original_uid(command, *args)

    monkeypatch.setattr(fake, "uid", fail_search)
    with pytest.raises(MailError):
        client(settings, fake).read_thread("<root@example.com>")


@pytest.mark.parametrize("unicode_seed", [True, False])
def test_unicode_frontier_does_not_scan_large_unrelated_mailbox(settings, unicode_seed):
    seed = "<café@example.com>" if unicode_seed else "<root@example.com>"
    unrelated = {str(uid): message(f"<filler-{uid}@example.com>") for uid in range(1, 1002)}
    unrelated.update(
        {
            "2000": message(seed, references=None if unicode_seed else "<café@example.com>"),
            "2001": message("<child@example.com>", reply=seed),
        }
    )
    fake = FakeImap({"All Mail": unrelated}, folder_flags={"All Mail": r"\All"})
    found = client(settings, fake).read_thread(seed)
    assert found["partial"] is False
    assert {item["message_id"] for item in found["messages"]} == {seed, "<child@example.com>"}
    assert not any(call[0] == "SEARCH" and "ALL" in call for call in fake.calls)
    metadata_uids = {
        uid
        for call in fake.calls
        if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]
        for uid in call[1].split(",")
    }
    assert metadata_uids == {"2000", "2001"}


def test_long_reference_frontier_splits_ascii_or_commands(settings):
    references = " ".join(f"<ancestor-{index}@example.com>" for index in range(61))
    fake = FakeImap({"INBOX": {"1": message("<root@example.com>", references=references)}})
    found = client(settings, fake).read_thread("<root@example.com>")
    assert found["partial"] is False
    searches = [call for call in fake.calls if call[0] == "SEARCH" and "OR" in call]
    assert len(searches) == 4
    assert all(call.count("OR") <= 59 for call in searches)
    assert sum(call.count("HEADER") for call in searches) == 62 * 3


def test_invalid_header_date_uses_internaldate_for_thread_order(settings):
    root = message("<root@example.com>", internaldate="14-Oct-2026 10:00:00 +0000")
    child = message(
        "<child@example.com>",
        reply="<root@example.com>",
        internaldate="14-Oct-2026 11:00:00 +0000",
    )
    child.headers = child.headers.replace(
        b"Date: Wed, 14 Oct 2026 10:00:00 +0000", b"Date: invalid date"
    )
    fake = FakeImap({"INBOX": {"1": root, "2": child}})
    found = client(settings, fake).read_thread("<root@example.com>")
    assert [item["message_id"] for item in found["messages"]] == [
        "<root@example.com>",
        "<child@example.com>",
    ]
