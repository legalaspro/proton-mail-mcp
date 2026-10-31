"""Unsubscribe discovery is independent of displayed or bounded message bodies."""

import base64
import re
from dataclasses import replace

import pytest
from test_mail import HTML
from test_unsubscribe import IDENTIFIER, URL, assert_read_only, forbid_requests, newsletter, setup
from test_unsubscribe import settings as settings

from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.unsubscribe import MAX_HTML_BYTES

BODY_URL = "https://example.com/unsubscribe?token=body-secret"
LINK = f'<a href="{BODY_URL}">Unsubscribe</a>'


def html_newsletter(html, *, header_url=None, encoding="7BIT"):
    item = newsletter(url=header_url, one_click=header_url is not None, html=html)
    payload = html.encode()
    if encoding == "BASE64":
        payload = base64.encodebytes(payload)
    item.sections["1"] = payload
    item.structure = HTML.replace(b"90 1", f"{len(payload)} 1".encode()).replace(
        b'"7BIT"', f'"{encoding}"'.encode()
    )
    return item


def body_fetches(fake):
    return [
        call for call in fake.calls if call[0] == "FETCH" and re.match(r"\(BODY\.PEEK\[\d", call[2])
    ]


def test_truncated_html_does_not_block_independently_fetched_header(settings, monkeypatch):
    item = html_newsletter("x" * (MAX_HTML_BYTES + 100), header_url=URL)
    mailbox, fake = setup(settings, items={"1": item})
    requests = []

    def perform(target, **kwargs):
        requests.append(target)
        return 204

    monkeypatch.setattr("proton_mail_mcp.unsubscribe_http.perform", perform)
    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])
    assert preview["token"] and not preview["blocked"] and not requests
    assert preview["targets"] == [{"method": "POST", "host": "news.example.com"}]
    result = mailbox.unsubscribe(preview["token"])
    assert result["count"] == 1 and not result["partial"]
    assert requests == [{"method": "POST", "url": URL, "host": "news.example.com"}]
    assert not body_fetches(fake)
    assert not any(call[0] == "FETCH" and call[2] == "(BODYSTRUCTURE)" for call in fake.calls)
    assert any(call == ("FETCH", "1", "(BODY.PEEK[HEADER])") for call in fake.calls)
    assert_read_only(fake)


@pytest.mark.parametrize("one_click", [False, True], ids=["get", "post"])
def test_body_changes_are_not_bound_when_header_target_is_used(settings, monkeypatch, one_click):
    item = newsletter(url=URL, one_click=one_click, html=LINK)
    mailbox, fake = setup(settings, items={"1": item})
    requests = []

    def perform(target, **kwargs):
        requests.append(target)
        return 204

    monkeypatch.setattr("proton_mail_mcp.unsubscribe_http.perform", perform)
    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])
    assert preview["token"]
    item.sections["1"] = item.sections["1"].replace(b"body-secret", b"new--secret")

    result = mailbox.unsubscribe(preview["token"])

    assert result["count"] == 1 and not result["partial"]
    assert requests == [
        {"method": "POST" if one_click else "GET", "host": "news.example.com", "url": URL}
    ]
    assert not body_fetches(fake)
    assert_read_only(fake)


@pytest.mark.parametrize(
    "change",
    [{"enable_send": False}, {"enable_send": True, "send_max_recipients": 1}],
    ids=["send-disabled", "recipient-cap"],
)
def test_unusable_header_mailto_falls_back_to_bounded_body_https(settings, monkeypatch, change):
    item = html_newsletter("x" * (2 * 65536) + LINK)
    item.headers = (
        b"List-Unsubscribe: <mailto:leave@example.com,second@example.com>\r\n" + item.headers
    )
    mailbox, fake = setup(replace(settings, **change), items={"1": item})
    requests = []
    forbid_requests(monkeypatch)

    def perform(target, **kwargs):
        requests.append(target)
        return 204

    monkeypatch.setattr("proton_mail_mcp.unsubscribe_http.perform", perform)
    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])

    assert preview["token"] and not preview["blocked"] and not requests
    assert preview["targets"] == [{"method": "GET", "host": "example.com"}]
    result = mailbox.unsubscribe(preview["token"])
    assert result["count"] == 1 and not result["partial"]
    assert requests == [{"method": "GET", "host": "example.com", "url": BODY_URL}]
    assert body_fetches(fake) == [("FETCH", "1", f"(BODY.PEEK[1]<0.{MAX_HTML_BYTES}>)")] * 2
    assert_read_only(fake)


@pytest.mark.parametrize("encoding", ["7BIT", "BASE64"])
def test_fallback_finds_link_after_previous_display_cut(settings, monkeypatch, encoding):
    item = html_newsletter("x" * (2 * 65536) + LINK, encoding=encoding)
    mailbox, fake = setup(settings, items={"1": item})
    forbid_requests(monkeypatch)
    with mailbox._session() as (connection, _):
        mailbox._select(connection, "INBOX")
        displayed, _ = mailbox._body(connection, "1", max_bytes=65536)
    assert "Unsubscribe" not in displayed
    fake.calls.clear()

    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])

    assert preview["token"] and not preview["blocked"]
    assert preview["targets"] == [{"method": "GET", "host": "example.com"}]
    grant = mailbox.cleanup.tokens.inspect(preview["token"], "unsubscribe")
    assert grant.messages[0]["unsubscribe_target"]["url"] == BODY_URL
    assert body_fetches(fake) == [("FETCH", "1", f"(BODY.PEEK[1]<0.{MAX_HTML_BYTES}>)")]
    assert_read_only(fake)


def test_safe_body_link_can_be_approved_before_bounded_scan_truncation(settings, monkeypatch):
    forbid_requests(monkeypatch)
    item = html_newsletter("x" * (2 * 65536) + LINK + "x" * MAX_HTML_BYTES)
    mailbox, fake = setup(settings, items={"1": item})

    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])

    assert preview["token"] and not preview["blocked"]
    assert preview["targets"] == [{"method": "GET", "host": "example.com"}]
    grant = mailbox.cleanup.tokens.inspect(preview["token"], "unsubscribe")
    assert grant.messages[0]["unsubscribe_target"]["url"] == BODY_URL
    assert body_fetches(fake) == [("FETCH", "1", f"(BODY.PEEK[1]<0.{MAX_HTML_BYTES}>)")]
    assert_read_only(fake)


def test_missing_link_beyond_scan_bound_reports_no_method_without_approval(settings, monkeypatch):
    forbid_requests(monkeypatch)
    item = html_newsletter("x" * MAX_HTML_BYTES + LINK)
    mailbox, fake = setup(settings, items={"1": item})

    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])

    assert preview["token"] is None
    reason = preview["blocked"][0]["reason"]
    assert "No permitted unsubscribe method found" in reason
    assert str(MAX_HTML_BYTES) in reason and "truncated" in reason
    assert body_fetches(fake) == [("FETCH", "1", f"(BODY.PEEK[1]<0.{MAX_HTML_BYTES}>)")]
    assert_read_only(fake)


@pytest.mark.parametrize("mailto_header", [False, True])
def test_selected_body_link_beyond_display_cut_remains_bound_to_token(
    settings, monkeypatch, mailto_header
):
    forbid_requests(monkeypatch)
    item = html_newsletter("x" * (2 * 65536) + LINK)
    if mailto_header:
        item.headers = b"List-Unsubscribe: <mailto:leave@example.com>\r\n" + item.headers
    mailbox, fake = setup(settings, items={"1": item})
    token = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])["token"]
    assert token
    item.sections["1"] = item.sections["1"].replace(b"body-secret", b"new--secret")

    with pytest.raises(MailValidationError, match="changed"):
        mailbox.unsubscribe(token)

    assert mailbox.cleanup.tokens.inspect(token, "unsubscribe")
    assert_read_only(fake)
