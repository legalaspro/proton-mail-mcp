"""Optional reply lookup preserves a bounded, Drafts-only append path."""

from dataclasses import replace
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

import pytest
from test_mail import FakeImap, client, message

from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import MailClient
from proton_mail_mcp.security import Credentials


@pytest.fixture
def settings():
    return Settings(
        token="a-test-token-that-is-at-least-32-characters", bridge_user="reader@example.com"
    )


def test_reply_lookup_prefers_all_mail_even_with_more_than_100_labels(settings):
    folders = {f"Labels/{index}": {} for index in range(120)}
    folders["Sent"] = {"5": message("<parent@example.com>")}
    folders["All Mail"] = {
        "9": message("<parent@example.com>", references="<ancestor@example.com>")
    }
    fake = FakeImap(folders, folder_flags={"All Mail": r"\All", "Sent": r"\Sent"})
    client(settings, fake).create_draft(
        "other@example.com",
        subject="Re: Hello",
        body="Draft body",
        in_reply_to="<parent@example.com>",
    )
    assert [call[1] for call in fake.calls if call[0] == "EXAMINE"] == ['"All Mail"']
    assert len([call for call in fake.calls if call[0] == "SEARCH"]) == 1
    assert len(fake.appended) == 1
    folder, flags, internaldate, raw = fake.appended[0]
    assert (folder, flags, internaldate) == ('"Drafts"', r"(\Draft)", None)
    draft = BytesParser(policy=policy.default).parsebytes(raw)
    assert draft["References"] == "<ancestor@example.com> <parent@example.com>"


@pytest.mark.parametrize(
    "stage", ["starttls", "login", "capability", "select", "search", "headers"]
)
def test_reply_lookup_timeout_falls_back_and_appends_on_fresh_session(settings, monkeypatch, stage):
    lookup = FakeImap({"INBOX": {"1": message("<parent@example.com>")}})
    lookup.shutdown = Mock()
    append = FakeImap()
    connections = iter([lookup, append])
    timeouts = []

    def factory(host, port, *, timeout):
        timeouts.append(timeout)
        return next(connections)

    mailbox = MailClient(
        settings, lambda _: Credentials("reader@example.com", "fake-test-password"), factory
    )

    def stalled(*args, **kwargs):
        raise TimeoutError("synthetic diagnostic")

    if stage in ("starttls", "login", "capability", "select"):
        monkeypatch.setattr(lookup, stage, stalled)
    else:
        original_uid = lookup.uid

        def stall_lookup(command, *args):
            if command == ("SEARCH" if stage == "search" else "FETCH"):
                raise TimeoutError("synthetic diagnostic")
            return original_uid(command, *args)

        monkeypatch.setattr(lookup, "uid", stall_lookup)
    result = mailbox.create_draft(
        "other@example.com",
        subject="Re: Hello",
        body="Draft body",
        in_reply_to="<parent@example.com>",
    )
    assert not lookup.appended
    lookup.shutdown.assert_called_once_with()
    assert not any(call[0] == "LOGOUT" for call in lookup.calls)
    assert len(append.appended) == 1
    assert 0 < timeouts[0] <= settings.call_timeout / 2
    folder, flags, internaldate, raw = append.appended[0]
    assert (folder, flags, internaldate) == ('"Drafts"', r"(\Draft)", None)
    draft = BytesParser(policy=policy.default).parsebytes(raw)
    assert draft["Message-ID"] == result
    assert draft["In-Reply-To"] == "<parent@example.com>"
    assert draft["References"] == "<parent@example.com>"
    assert draft.get_content().strip() == "Draft body"
    assert [call[0] for call in append.calls] == [
        "STARTTLS",
        "LOGIN",
        "CAPABILITY",
        "APPEND",
        "LOGOUT",
    ]


def test_reply_lookup_reserves_half_the_call_budget_for_append(settings, monkeypatch):
    # Ten seconds of usable I/O plus the 10% response reserve keeps the
    # original five-second lookup and five-second APPEND boundary assertions.
    settings = replace(settings, call_timeout=100 / 9)
    now = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: now[0])
    lookup = FakeImap({"INBOX": {"1": message("<parent@example.com>")}})
    lookup.shutdown = Mock()
    append = FakeImap()
    connections = iter([lookup, append])
    timeouts = []

    def factory(host, port, *, timeout):
        timeouts.append(timeout)
        return next(connections)

    def stalled(*args):
        now[0] += 5
        raise TimeoutError("synthetic diagnostic")

    monkeypatch.setattr(lookup, "uid", stalled)
    mailbox = MailClient(
        settings, lambda _: Credentials("reader@example.com", "fake-test-password"), factory
    )
    mailbox.create_draft(
        "other@example.com",
        subject="Re: Hello",
        body="Draft body",
        in_reply_to="<parent@example.com>",
    )
    assert timeouts == [5, 5]
    assert len(append.appended) == 1
