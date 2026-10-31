"""Unsubscribe approvals use fake mail and an in-memory HTTP boundary only."""

import json
import re
import smtplib
import socket
from dataclasses import replace

import pytest
from test_mail import HTML, FakeImap, client, message

from proton_mail_mcp.action_log import LogTransaction, replay_records
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.guarded import approval_hash
from proton_mail_mcp.mail import MailError, MailValidationError

URL = "https://news.example.com/unsubscribe?secret=synthetic-query-secret"
IDENTIFIER = "<newsletter@example.test>"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        token="offline-test-token-at-least-thirty-two-characters",
        bridge_user="reader@example.test",
        action_log_path=str(tmp_path / "actions.jsonl"),
    )


def newsletter(identifier=IDENTIFIER, *, url=URL, one_click=True, html=None):
    item = message(identifier)
    if url is not None:
        item.headers = f"List-Unsubscribe: <{url}>\r\n".encode() + item.headers
    if one_click:
        item.headers = b"List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n" + item.headers
    if html is not None:
        item.structure = HTML
        item.sections = {"1": html.encode()}
    return item


def setup(settings, *, items=None):
    fake = FakeImap({"INBOX": items or {"1": newsletter()}})
    return client(settings, fake), fake


def forbid_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Refused unsubscribe must not open HTTP, SMTP, or sockets")

    monkeypatch.setattr("proton_mail_mcp.unsubscribe_http.perform", forbidden)
    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def assert_read_only(fake):
    assert {call[0] for call in fake.calls} <= {
        "STARTTLS",
        "LOGIN",
        "CAPABILITY",
        "LIST",
        "EXAMINE",
        "SEARCH",
        "FETCH",
        "LOGOUT",
    }
    assert not fake.appended
    assert all(call[2] is True for call in fake.calls if call[0] == "EXAMINE")
    assert not any("\\Seen" in str(call) for call in fake.calls)
    assert all("BODY[" not in call[2] for call in fake.calls if call[0] == "FETCH")


@pytest.mark.parametrize("url", [None, "mailto:leave@example.com"])
def test_absent_or_mailto_only_links_never_receive_approval(settings, monkeypatch, url):
    forbid_requests(monkeypatch)
    mailbox, fake = setup(settings, items={"1": newsletter(url=url, one_click=False)})

    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])

    assert preview["count"] == 1
    assert preview["token"] is None and preview["blocked"]
    with pytest.raises(MailValidationError):
        mailbox.unsubscribe(preview["token"])
    assert_read_only(fake)


def test_confirmation_is_single_use_logged_before_http_and_read_only(settings, monkeypatch):
    mailbox, fake = setup(settings)
    requests = []

    def perform(target, timeout=5.0):
        records = replay_records(
            [json.loads(line) for line in mailbox.cleanup.backend.log.path.read_text().splitlines()]
        )
        assert records[-1]["action"] == "unsubscribe"
        assert records[-1]["status"] == "in_progress"
        requests.append(target)
        assert 0 < timeout <= 5
        return 204

    monkeypatch.setattr("proton_mail_mcp.unsubscribe_http.perform", perform)
    preview = mailbox.preview_action("unsubscribe", query={})
    assert preview["token"] and not requests
    assert {"from", "subject", "date", "folder", "id", "message_id"} <= set(preview["samples"][0])

    result = mailbox.unsubscribe(preview["token"])

    assert result["count"] == 1 and result["partial"] is False
    assert result["messages"][0]["status"] == "done"
    assert requests[0]["url"] == URL and requests[0]["method"] == "POST"
    before = len(fake.calls)
    with pytest.raises(MailValidationError, match="already used"):
        mailbox.unsubscribe(preview["token"])
    assert len(fake.calls) == before and len(requests) == 1

    log = mailbox.cleanup.backend.log.path.read_text()
    for secret in (
        "sender@example.com",
        "synthetic-query-secret",
        URL,
        IDENTIFIER,
        preview["token"],
    ):
        assert secret not in log
    actions = mailbox.list_actions(token=preview["token"])["actions"]
    assert [row["log_id"] for row in actions] == [result["log_id"], preview["log_id"]]
    assert actions[0]["action"] == "unsubscribe" and actions[0]["done"] == 1
    with pytest.raises(MailValidationError):
        mailbox.undo_action(result["log_id"])
    assert_read_only(fake)


@pytest.mark.parametrize("url", [URL, None], ids=["permitted", "blocked"])
def test_unsubscribe_preview_is_logged_without_content(settings, monkeypatch, url):
    forbid_requests(monkeypatch)
    mailbox, fake = setup(settings, items={"1": newsletter(url=url)})
    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])
    assert re.fullmatch(r"[0-9a-f]{32}", preview["log_id"])
    [entry] = mailbox.list_actions(action="unsubscribe")["actions"]
    assert entry["log_id"] == preview["log_id"]
    assert entry["action"] == "preview_action" and entry["preview_action"] == "unsubscribe"
    assert entry["status"] == ("previewed" if url else "blocked")
    assert entry["count"] == 1 and entry["blocked_count"] == (0 if url else 1)
    assert not entry["partial"] and entry["total_is_exact"]
    journal = mailbox.cleanup.backend.log.path.read_text()
    for private in (URL, IDENTIFIER, "sender@example.com", preview["token"] or "never-logged"):
        assert private not in journal
    with pytest.raises(MailValidationError, match="cannot be undone"):
        mailbox.undo_action(preview["log_id"])
    assert_read_only(fake)


def test_failed_preview_log_retires_approval(settings, monkeypatch):
    forbid_requests(monkeypatch)
    mailbox, fake = setup(settings)

    def failed_append(self, record):
        raise MailError("Cleanup action log is unavailable")

    monkeypatch.setattr(LogTransaction, "append", failed_append)
    with pytest.raises(MailError, match="unavailable"):
        mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])
    assert not mailbox.cleanup.tokens._grants
    assert_read_only(fake)


@pytest.mark.parametrize(
    "url,allowed",
    [("https://news.example.com/unsubscribe", True), ("https://foreign.test/unsubscribe", False)],
)
def test_get_preview_requires_sender_domain(settings, monkeypatch, url, allowed):
    forbid_requests(monkeypatch)
    mailbox, fake = setup(settings, items={"1": newsletter(url=url, one_click=False)})

    preview = mailbox.preview_action("unsubscribe", query={})

    assert bool(preview["token"]) is allowed
    assert bool(preview["blocked"]) is not allowed
    assert_read_only(fake)


@pytest.mark.parametrize("post", [False, True])
def test_encoded_comment_cannot_be_executed(settings, monkeypatch, post):
    forbid_requests(monkeypatch)
    comment_host = "foreign.test" if post else "example.com"
    item = newsletter(url=None, one_click=post)
    item.headers = (
        "List-Unsubscribe: "
        f"(=?utf-8?q?=29_=3Chttps=3A//{comment_host}/docs=3E_=28?=) "
        "<https://example.com/unsubscribe>\r\n"
    ).encode() + item.headers
    mailbox, fake = setup(settings, items={"1": item})
    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])
    assert preview["token"] and not preview["blocked"]
    requests = []

    def perform(target, **kwargs):
        requests.append(target)
        return 204

    monkeypatch.setattr("proton_mail_mcp.unsubscribe_http.perform", perform)
    result = mailbox.unsubscribe(preview["token"])

    assert result["count"] == 1 and result["partial"] is False
    assert requests == [
        {
            "method": "POST" if post else "GET",
            "host": "example.com",
            "url": "https://example.com/unsubscribe",
        }
    ]
    assert_read_only(fake)


@pytest.mark.parametrize("post", [False, True])
@pytest.mark.parametrize(
    "value",
    [
        "(=?utf-8?q?=29_=3Chttps=3A//example.com/docs=3E_=28?=)",
        "=?utf-8?q?=3Chttps://example.com/unsubscribe=3E?=",
    ],
)
def test_encoded_comment_or_word_only_never_receives_approval(settings, monkeypatch, post, value):
    forbid_requests(monkeypatch)
    item = newsletter(url=None, one_click=post)
    item.headers = f"List-Unsubscribe: {value}\r\n".encode() + item.headers
    mailbox, fake = setup(settings, items={"1": item})

    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])

    assert preview["token"] is None and preview["blocked"]
    with pytest.raises(MailValidationError):
        mailbox.unsubscribe(preview["token"])
    assert_read_only(fake)


def test_protected_sender_refused_at_preview_and_rechecked_on_confirmation(settings, monkeypatch):
    forbid_requests(monkeypatch)
    mailbox, fake = setup(settings)
    item = fake.folders["INBOX"]["1"]
    original = item.headers
    item.headers = original.replace(b"sender@example.com", b"alerts@broker.example.org")
    preview = mailbox.preview_action("unsubscribe", query={})
    assert preview["token"] is None and preview["blocked"]

    item.headers = original
    preview = mailbox.preview_action("unsubscribe", query={})
    item.headers = b"Reply-To: private@gmail.com\r\n" + item.headers
    with pytest.raises(MailValidationError):
        mailbox.unsubscribe(preview["token"])
    assert_read_only(fake)


@pytest.mark.parametrize("cap", [2, 50])
def test_unsubscribe_batch_cap_can_only_be_lowered(settings, monkeypatch, cap):
    forbid_requests(monkeypatch)
    mailbox, fake = setup(
        replace(settings, unsubscribe_max_batch=cap),
        items={str(i): newsletter(f"<{i}@example.test>") for i in range(1, cap + 2)},
    )
    preview = mailbox.preview_action("unsubscribe", query={})
    assert preview["count"] == cap + 1
    assert preview["token"] is None and str(cap) in preview["reason"]
    assert_read_only(fake)


@pytest.mark.parametrize("value", [0, 51, True, 1.5])
def test_invalid_unsubscribe_cap_fails_closed(settings, value):
    with pytest.raises(ConfigError):
        replace(settings, unsubscribe_max_batch=value)


def test_unsubscribe_cap_loads_from_environment_mapping():
    settings = Settings.from_env(
        {
            "PROTON_MCP_TOKEN": "offline-test-token-at-least-thirty-two-characters",
            "PROTON_BRIDGE_USER": "reader@example.test",
            "PROTON_MCP_UNSUBSCRIBE_MAX_BATCH": "7",
        }
    )
    assert settings.unsubscribe_max_batch == 7


def test_changed_url_after_preview_cannot_execute(settings, monkeypatch):
    forbid_requests(monkeypatch)
    mailbox, fake = setup(settings)
    preview = mailbox.preview_action("unsubscribe", query={})
    item = fake.folders["INBOX"]["1"]
    item.headers = item.headers.replace(b"synthetic-query-secret", b"different-secret")

    with pytest.raises(MailValidationError, match="changed"):
        mailbox.unsubscribe(preview["token"])
    assert_read_only(fake)


@pytest.mark.parametrize(
    "change", ["post", "extra-header", "comment", "comment-encoding", "folding"]
)
def test_token_rejects_changes_to_unselected_details(settings, monkeypatch, change):
    forbid_requests(monkeypatch)
    item = newsletter(html='<a href="https://example.com/old">Unsubscribe</a>')
    item.headers = item.headers.replace(
        b"List-Unsubscribe: ", b"List-Unsubscribe: (Original comment) "
    )
    mailbox, fake = setup(settings, items={"1": item})
    token = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])["token"]
    assert token
    if change == "post":
        item.headers = item.headers.replace(
            b"List-Unsubscribe=One-Click", b"List-Unsubscribe=Other"
        )
    elif change == "extra-header":
        item.headers = b"List-Unsubscribe: <https://example.com/extra>\r\n" + item.headers
    elif change == "comment-encoding":
        item.headers = item.headers.replace(b"Original comment", b"=?utf-8?q?Original_comment?=")
    elif change == "folding":
        item.headers = item.headers.replace(b") <", b")\r\n <")
    else:
        item.headers = item.headers.replace(b"Original comment", b"Modified comment")
    with pytest.raises(MailValidationError, match="changed"):
        mailbox.unsubscribe(token)
    assert_read_only(fake)


@pytest.mark.parametrize(
    "change",
    [
        "unsubscribe-first-encoding",
        "unsubscribe-second-encoding",
        "post-first-encoding",
        "post-second-encoding",
        "unsubscribe-order",
        "post-order",
        "cross-field-order",
        "unsubscribe-name-case",
        "post-name-case",
        "unsubscribe-leading-whitespace",
        "post-leading-whitespace",
    ],
)
def test_equal_size_raw_header_change_invalidates_token(settings, monkeypatch, change):
    forbid_requests(monkeypatch)
    item = newsletter(url=None, one_click=False)
    original_headers = item.headers
    fields = [
        b"List-Unsubscribe: (=?utf-8?q?First_comment?=) <https://example.com/unsubscribe>\r\n",
        b"List-Unsubscribe-Post: (=?utf-8?q?First_comment?=)\r\n",
        b"List-Unsubscribe: (=?utf-8?q?Second_comment?=) <https://example.com/second>\r\n",
        b"List-Unsubscribe-Post: (=?utf-8?q?Second_comment?=)\r\n",
    ]
    item.headers = b"".join(fields) + original_headers
    mailbox, fake = setup(settings, items={"1": item})
    token = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])["token"]
    assert token
    original = item.headers

    if change.endswith("encoding"):
        index = int(change.startswith("post")) + 2 * int("second" in change)
        fields[index] = fields[index].replace(b"utf-8?q?", b"UTF-8?Q?")
    elif change.endswith("order"):
        first, second = {
            "unsubscribe-order": (0, 2),
            "post-order": (1, 3),
            "cross-field-order": (0, 1),
        }[change]
        fields[first], fields[second] = fields[second], fields[first]
    else:
        index = int(change.startswith("post"))
        if change.endswith("name-case"):
            fields[index] = fields[index].replace(b"List-Unsubscribe", b"list-unsubscribe", 1)
        else:
            fields[index] = fields[index].replace(b": ", b":\t", 1)
    item.headers = b"".join(fields) + original_headers
    assert item.headers != original and len(item.headers) == len(original)

    with pytest.raises(MailValidationError, match="changed"):
        mailbox.unsubscribe(token)
    assert_read_only(fake)


@pytest.mark.parametrize("sender", [b"attacker@foreign.test", b"alerts@broker.example.org"])
def test_fetched_headers_cannot_change_sender_after_envelope_validation(
    settings, monkeypatch, sender
):
    forbid_requests(monkeypatch)
    mailbox, fake = setup(settings, items={"1": newsletter(one_click=False)})
    token = mailbox.preview_action("unsubscribe", query={})["token"]
    original = mailbox._raw_headers

    def changed_headers(connection, uid):
        item = fake.folders["INBOX"]["1"]
        item.headers = item.headers.replace(b"sender@example.com", sender)
        return original(connection, uid)

    monkeypatch.setattr(mailbox, "_raw_headers", changed_headers)
    with pytest.raises(MailValidationError):
        mailbox.unsubscribe(token)
    assert_read_only(fake)


@pytest.mark.parametrize("token", [None, "", "forged-token", "é" * 43])
def test_invalid_token_refuses_before_mail_or_network(settings, monkeypatch, token):
    forbid_requests(monkeypatch)
    mailbox, fake = setup(settings)
    with pytest.raises(MailValidationError):
        mailbox.unsubscribe(token)
    assert fake.calls == []


def test_failed_journal_prevents_http(settings, monkeypatch):
    forbid_requests(monkeypatch)
    mailbox, fake = setup(settings)
    preview = mailbox.preview_action("unsubscribe", query={})
    mailbox.cleanup.backend.log.path.write_text("invalid complete record\n")

    with pytest.raises(MailError):
        mailbox.unsubscribe(preview["token"])
    assert_read_only(fake)


def test_torn_journal_tail_is_sealed_before_http(settings, monkeypatch):
    mailbox, fake = setup(settings)
    preview = mailbox.preview_action("unsubscribe", query={})
    log = mailbox.cleanup.backend.log
    tail = b'{"interrupted":'
    log.path.write_bytes(tail)

    def perform(target, timeout=5.0):
        sealed, *records = log.path.read_bytes().splitlines()
        assert sealed.startswith(tail + b"\0")
        record = replay_records([json.loads(line) for line in records])[-1]
        assert record["action"] == "unsubscribe" and record["status"] == "in_progress"
        return 204

    monkeypatch.setattr("proton_mail_mcp.unsubscribe_http.perform", perform)
    result = mailbox.unsubscribe(preview["token"])
    assert result["count"] == 1 and not result["partial"]
    assert mailbox.list_actions(token=preview["token"])["actions"][0]["done"] == 1
    assert_read_only(fake)


def test_expired_preview_returns_no_token_on_transport_error(settings, monkeypatch):
    mailbox, _ = setup(settings)

    def timeout(connection, deadline, selector, **kwargs):
        deadline.end = 0
        raise MailError("synthetic private transport diagnostic")

    monkeypatch.setattr(
        "proton_mail_mcp.unsubscribe.UnsubscribeService._rows", staticmethod(timeout)
    )
    result = mailbox.preview_action("unsubscribe", query={})
    assert result["token"] is None and result["partial"]
    assert result["total_is_exact"] is False
    [entry] = mailbox.list_actions(action="preview_action")["actions"]
    assert entry["log_id"] == result["log_id"] and entry["status"] == "partial"
    assert entry["partial"] and not entry["total_is_exact"]
    assert "private" not in json.dumps(result)


def test_http_failure_is_uncertain_logged_and_never_retried(settings, monkeypatch):
    from proton_mail_mcp.unsubscribe_http import UnsubscribeHTTPError

    mailbox, fake = setup(
        settings,
        items={"1": newsletter(), "2": newsletter("<second@example.test>")},
    )
    requests = []

    def fail(target, timeout=5.0):
        requests.append(target)
        raise UnsubscribeHTTPError("private server body synthetic-query-secret")

    monkeypatch.setattr("proton_mail_mcp.unsubscribe_http.perform", fail)
    token = mailbox.preview_action("unsubscribe", query={})["token"]

    result = mailbox.unsubscribe(token)

    assert result["count"] == 0 and result["partial"] is True
    assert [entry["status"] for entry in result["messages"]] == ["uncertain", "not_attempted"]
    assert len(requests) == 1
    assert "private server body" not in json.dumps(result)
    assert "synthetic-query-secret" not in mailbox.cleanup.backend.log.path.read_text()
    actions = mailbox.list_actions(token=token)["actions"]
    assert actions[0]["uncertain"] == 1 and actions[0]["not_attempted"] == 1
    with pytest.raises(MailValidationError, match="already used"):
        mailbox.unsubscribe(token)
    assert len(requests) == 1
    assert_read_only(fake)


def test_execution_rechecks_unsubscribe_specific_cap(settings, monkeypatch):
    forbid_requests(monkeypatch)
    mailbox, fake = setup(replace(settings, unsubscribe_max_batch=1))
    token = mailbox.preview_action("unsubscribe", query={})["token"]
    grant = next(iter(mailbox.cleanup.tokens._grants.values()))
    grant.messages *= 2
    grant.binding = approval_hash(grant.action, grant.params, grant.selector, grant.messages)
    before = len(fake.calls)

    with pytest.raises(MailValidationError, match="cap"):
        mailbox.unsubscribe(token)
    assert len(fake.calls) == before
