"""Unsubscribe policy tests; every HTTP connection terminates at a local TLS fake."""

import http.client
import smtplib
import socket
import ssl
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser, HeaderParser
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from proton_mail_mcp import unsubscribe_http as unsubscribe

_REAL_CONNECT = socket.socket.connect
# Windows spends more than 0.15 s reading its certificate store for the first TLS context.
TIGHT, TIGHT_BOUND, DRIP_PAUSE = (1.5, 4, 0.15) if sys.platform == "win32" else (0.15, 1, 0.025)
_PUBLIC_IP = "93.184.216.34"


def links(url, post=False):
    headers = {"List-Unsubscribe": f"<{url}>"}
    if post:
        headers["List-Unsubscribe-Post"] = unsubscribe.ONE_CLICK
    return unsubscribe.extract(headers)


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "example.com")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("example.com")]), critical=False)
        .sign(key, algorithm=None)
    )
    certificate_path, key_path = tmp_path / "certificate.pem", tmp_path / "private.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    requests = []
    state = {"status": 204, "location": "https://foreign.example/unsubscribe", "drip": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(
                (
                    self.command,
                    self.path,
                    dict(self.headers),
                    self.rfile.read(int(self.headers.get("Content-Length", "0"))),
                )
            )
            try:
                if state["drip"]:
                    for byte in b"HTTP/1.1 200 OK\r\n":
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(DRIP_PAUSE)
                    return
                self.send_response(state["status"])
                if 300 <= state["status"] < 400:
                    self.send_header("Location", state["location"])
                self.send_header("Set-Cookie", "private=response-secret")
                self.end_headers()
                self.wfile.write(b"response-body-must-not-be-read")
            except (OSError, ssl.SSLError):
                pass

        do_POST = do_GET

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate_path, key_path)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    context = ssl.create_default_context(cafile=certificate_path)
    monkeypatch.setattr(unsubscribe.ssl, "create_default_context", lambda: context)
    connections = []

    def connect(sock, address):
        assert address == (_PUBLIC_IP, 443), "Only the pinned fake endpoint may connect"
        connections.append(address)
        return _REAL_CONNECT(sock, server.server_address)

    def resolve(host, port, **kwargs):
        assert host in ("example.com", "wrong.example.com") and port == 443
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 443))]

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    try:
        yield requests, state, connections
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_extract_multiple_headers_uris_and_html_links():
    headers = EmailMessage()
    headers["List-Unsubscribe"] = (
        "<mailto:leave@example.com>, <https://example.com/unsubscribe?a=1>"
    )
    headers["List-Unsubscribe"] = "<https://example.com/second>"
    headers["List-Unsubscribe-Post"] = unsubscribe.ONE_CLICK
    result = unsubscribe.extract(
        headers,
        [
            '<a href="https://example.com/remove?a=1&amp;b=2">Please <b>unsubscribe</b></a>'
            '<a href="https://example.com/opt-out">Leave</a>'
            '<a href="https://example.com/read">Read article</a>'
        ],
    )
    assert len(result["list_unsubscribe"]) == 2
    assert result["list_unsubscribe_post"] == [unsubscribe.ONE_CLICK]
    assert result["header_urls"] == [
        "mailto:leave@example.com",
        "https://example.com/unsubscribe?a=1",
        "https://example.com/second",
    ]
    assert result["html_links"] == [
        "https://example.com/remove?a=1&b=2",
        "https://example.com/opt-out",
    ]


def test_extract_without_headers_and_unclosed_anchor():
    result = unsubscribe.extract({}, ['<a href="https://example.com/remove">Opt out'])
    assert result["list_unsubscribe"] == result["list_unsubscribe_post"] == []
    assert result["header_urls"] == []
    assert result["html_links"] == ["https://example.com/remove"]


@pytest.mark.parametrize("post", [False, True])
def test_header_comments_do_not_supply_action_targets(endpoint, post):
    requests, _, connections = endpoint
    comment_host = "foreign.test" if post else "example.com"
    value = f"(Documentation: <https://{comment_host}/docs>) <https://example.com/unsubscribe>"
    headers = {"List-Unsubscribe": value}
    if post:
        headers["List-Unsubscribe-Post"] = unsubscribe.ONE_CLICK
    extracted = unsubscribe.extract(headers)
    assert extracted["list_unsubscribe"] == [value]
    assert extracted["header_urls"] == ["https://example.com/unsubscribe"]
    target = unsubscribe.select_target("sender@example.com", extracted)
    assert target["url"] == "https://example.com/unsubscribe"
    assert target["method"] == ("POST" if post else "GET")
    assert unsubscribe.perform(target) == 204
    assert len(requests) == len(connections) == 1
    assert requests[0][:2] == (target["method"], "/unsubscribe")


@pytest.mark.parametrize("post", [False, True])
@pytest.mark.parametrize("header_policy", [policy.default, policy.compat32])
@pytest.mark.parametrize(
    "encoded_comment",
    ["=29_=3Chttps=3A//{host}/docs=3E_=28", ")_<https://{host}/docs>_("],
)
def test_encoded_comment_delimiters_remain_comment_data(post, header_policy, encoded_comment):
    comment_host = "foreign.test" if post else "example.com"
    value = (
        f"(=?utf-8?q?{encoded_comment.format(host=comment_host)}?=) "
        "<https://example.com/unsubscribe>"
    )
    raw = f"List-Unsubscribe: {value}\r\n".encode()
    if post:
        raw += b"List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n"
    headers = BytesParser(policy=header_policy).parsebytes(raw + b"\r\n")

    extracted = unsubscribe.extract(headers)

    assert extracted["list_unsubscribe"] == [value]
    assert extracted["header_urls"] == ["https://example.com/unsubscribe"]
    assert unsubscribe.select_target("sender@example.com", extracted) == {
        "method": "POST" if post else "GET",
        "host": "example.com",
        "url": "https://example.com/unsubscribe",
    }


@pytest.mark.parametrize(
    "comment",
    [
        "(outer (inner <https://example.com/docs>) <https://example.com/more>)",
        r"(escaped close \) <https://example.com/docs> and open \( nested)",
        r"(escaped slash \\) (second <https://example.com/docs>)",
        r"(outer (inner \) <https://example.com/docs>) trailing)",
    ],
)
def test_nested_and_escaped_comments_are_ignored(comment):
    value = comment + " <https://example.com/unsubscribe>"
    extracted = unsubscribe.extract({"List-Unsubscribe": value})
    assert extracted["list_unsubscribe"] == [value]
    assert extracted["header_urls"] == ["https://example.com/unsubscribe"]
    assert unsubscribe.select_target("sender@example.com", extracted)["url"] == (
        "https://example.com/unsubscribe"
    )


@pytest.mark.parametrize(
    "value",
    [
        "(Documentation: <https://example.com/docs>)",
        "(outer (nested <https://example.com/docs>))",
        r"(escaped close \) <https://example.com/docs>)",
        "(unclosed comment <https://example.com/docs>",
        "<outer <https://example.com/docs> <https://example.com/more>>",
    ],
)
def test_comments_and_nested_brackets_are_not_candidates(value):
    extracted = unsubscribe.extract(
        {"List-Unsubscribe": value, "List-Unsubscribe-Post": unsubscribe.ONE_CLICK}
    )
    assert extracted["header_urls"] == []
    with pytest.raises(unsubscribe.UnsubscribeRefused):
        unsubscribe.select_target("sender@example.com", extracted)


@pytest.mark.parametrize(
    "value",
    [
        "<\r\n https://example.com/unsubscribe\r\n >",
        "<mailto:leave@example.com>,\r\n <https://example.com/unsubscribe>",
        "(=?utf-8?q?Unsubscribe?=) <https://example.com/unsubscribe>",
    ],
)
def test_folded_headers_and_encoded_words(value):
    headers = HeaderParser(policy=policy.default).parsestr(f"List-Unsubscribe: {value}\r\n\r\n")
    target = unsubscribe.select_target("sender@example.com", unsubscribe.extract(headers))
    assert target["url"] == "https://example.com/unsubscribe"


def test_bare_and_internally_folded_headers_keep_complete_targets():
    headers = BytesParser(policy=policy.default).parsebytes(
        b"List-Unsubscribe: < HTTPS://example.com/unsub\r\n\tscribe?token=whole >\r\n"
        b"List-Unsubscribe: MailTo:leave@example.com?subject=Leave%20list, "
        b"https://example.com/other\r\n"
        b"List-Unsubscribe-Post: \tList-Unsubscribe=\r\n\tOne-Click \r\n\r\n"
    )
    extracted = unsubscribe.extract(headers)
    assert extracted["header_urls"] == [
        "HTTPS://example.com/unsubscribe?token=whole",
        "MailTo:leave@example.com?subject=Leave%20list",
        "https://example.com/other",
    ]
    selected = unsubscribe.select_target("sender@example.com", extracted)
    assert selected["method"] == "POST" and selected["url"] == extracted["header_urls"][0]
    extracted["header_urls"] = extracted["header_urls"][1:2]
    assert unsubscribe.select_target("sender@example.com", extracted)["subject"] == "Leave list"


@pytest.mark.parametrize(
    "value",
    [
        "<https://example.com/unsubscribe?token=truncated",
        "https://example.com/unsubscribe<broken",
        "<https://example.com/unsubscribe?token=broken%2>",
        "<https://example.com/unsubscribe\r\nCookie: injected>",
    ],
)
def test_truncated_or_injected_header_links_are_not_executable(value):
    extracted = unsubscribe.extract(
        {"List-Unsubscribe": value, "List-Unsubscribe-Post": unsubscribe.ONE_CLICK}
    )
    with pytest.raises(unsubscribe.UnsubscribeRefused):
        unsubscribe.select_target("sender@example.com", extracted)


@pytest.mark.parametrize(
    "value",
    [
        "(=?utf-8?q?=29_=3Chttps=3A//example.com/docs=3E_=28?=)",
        "=?utf-8?q?=3Chttps://example.com/unsubscribe=3E?=",
        "=?utf-8?q?<https://example.com/unsubscribe>?=",
        "<=?utf-8?q?https://example.com/unsubscribe?=>",
        "<ht=?utf-8?q?x?=tps://example.com/unsubscribe>",
    ],
)
def test_encoded_words_never_supply_uri_candidates(value):
    headers = BytesParser(policy=policy.default).parsebytes(
        f"List-Unsubscribe: {value}\r\n\r\n".encode()
    )

    extracted = unsubscribe.extract(headers)

    assert extracted["list_unsubscribe"] == [value]
    assert extracted["header_urls"] == []
    with pytest.raises(unsubscribe.UnsubscribeRefused):
        unsubscribe.select_target("sender@example.com", extracted)


def test_parentheses_inside_uri_are_preserved():
    url = "https://example.com/unsubscribe(list)"
    extracted = links(url)
    assert extracted["header_urls"] == [url]
    assert unsubscribe.select_target("sender@example.com", extracted)["url"] == url


@pytest.mark.parametrize("tag", ["script", "style"])
def test_mime_html_parts_have_independent_parser_state(tag):
    anchor = '<a href="https://example.com/unsubscribe">Unsubscribe</a>'
    extracted = unsubscribe.extract({}, [f"<{tag}>", anchor, anchor])
    assert extracted["html_links"] == ["https://example.com/unsubscribe"]


def test_mailto_discovery_and_missing_links_never_open_socket_or_smtp(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No SMTP or socket may be opened")

    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    target = unsubscribe.select_target("sender@example.com", links("mailto:leave@example.com"))
    assert target == {
        "method": "MAILTO",
        "url": "mailto:leave@example.com",
        "host": "example.com",
        "to": ["leave@example.com"],
        "subject": "",
        "body": "",
    }
    with pytest.raises(unsubscribe.UnsubscribeRefused, match="No permitted unsubscribe method"):
        unsubscribe.select_target("sender@example.com", unsubscribe.extract({}))
    with pytest.raises(unsubscribe.UnsubscribeRefused):
        unsubscribe.perform({"method": "GET", "url": "mailto:leave@example.com", "host": ""})
    with pytest.raises(unsubscribe.UnsubscribeRefused):
        unsubscribe.perform(target)


def test_mailto_decodes_utf8_subject_body_and_preserves_plus_once():
    target = unsubscribe.parse_mailto(
        "mailto:leave+list@example.com?subject=D%C3%A9sabonner%20%2B+%2520"
        "&body=Remove%20me%0D%0AList%3A%20caf%C3%A9%26tea%3Dyes%3F%23"
    )
    assert target["to"] == ["leave+list@example.com"]
    assert target["subject"] == "Désabonner ++%20"
    assert target["body"] == "Remove me\r\nList: café&tea=yes?#"


@pytest.mark.parametrize(
    "local_part, expected",
    [
        ("unsub-abc=def", "unsub-abc=def"),
        ("unsub/abc", "unsub/abc"),
        ("unsub-abc%3Ddef", "unsub-abc=def"),
        ("unsub%2Fabc", "unsub/abc"),
        ("unsub%3Fabc", "unsub?abc"),
        ("unsub%253Fabc", "unsub%3Fabc"),
    ],
)
def test_mailto_address_literals_and_encoded_addr_spec_characters(local_part, expected):
    url = f"mailto:{local_part}@list.example.com?subject=unsubscribe"
    target = unsubscribe.parse_mailto(url)
    assert target["to"] == [f"{expected}@list.example.com"]
    assert target["url"] == url
    assert target["subject"] == "unsubscribe"


@pytest.mark.parametrize("field", ["subject", "body"])
@pytest.mark.parametrize(
    "value",
    [
        "https://list.example.com/unsubscribe?token%3Done%26part%3Dtwo",
        "https:%2F%2Flist.example.com%2Funsubscribe%3Ftoken%3Done%26part%3Dtwo",
    ],
)
def test_mailto_slash_and_question_in_field_values_preserve_field_boundaries(field, value):
    target = unsubscribe.parse_mailto(f"mailto:leave@example.com?{field}={value}")
    assert target[field] == "https://list.example.com/unsubscribe?token=one&part=two"
    assert target["to"] == ["leave@example.com"]


@pytest.mark.parametrize(
    "url",
    [
        "mailto:one@example.com,two@example.net?to=three@example.com",
        "MAILTO:?TO=one@example.com%2Ctwo@example.net,three@example.com",
        "mailto:one@example.com?to=two@example.net,three@example.com,one@example.com",
    ],
)
def test_mailto_multiple_addresses_from_path_and_to_query(url):
    target = unsubscribe.parse_mailto(url)
    assert target["to"] == ["one@example.com", "two@example.net", "three@example.com"]
    assert target["host"] == "example.com, example.net"


def test_mailto_decodes_encoded_word_subject_but_keeps_literal_body():
    value = "%3D%3Futf-8%3FQ%3FD%3DC3%3DA9sabonner%3F%3D"
    target = unsubscribe.parse_mailto(f"mailto:leave@example.com?subject={value}&body={value}")
    assert target["subject"] == "Désabonner"
    assert target["body"] == "=?utf-8?Q?D=C3=A9sabonner?="


@pytest.mark.parametrize(
    "url",
    [
        "mailto:",
        "mailto://leave@example.com",
        "mailto:leave@example.com#fragment",
        "mailto:leave@example.com?subject=bad%escape",
        "mailto:leave@example.com?subject=%FF",
        "mailto:leave@example.com?subject=raw space",
        "mailto:leave@example.com?subject=raw\nline",
        "mailto:leave@example.com?subject=one&SUBJECT=two",
        "mailto:leave@example.com?body=one&BODY=two",
        "mailto:leave@example.com?to=one@example.com&to=two@example.com",
        "mailto:leave@example.com?subject",
        "mailto:leave@example.com?subject=one?body=two",
        "mailto:leave@example.com?subject=one=two",
        "mailto:leave@example.com?subject=one&%73ubject=two",
        "mailto:leave@example.com?subject=%0D%0ABcc%3Ahidden@example.com",
        "mailto:leave@example.com?subject=%3D%3Futf-8%3FQ%3Fbad%3D0Aline%3F%3D",
        "mailto:leave@example.com?subject=%3D%3Futf-8%3Fb%3FA%3F%3D",
        "mailto:leave@example.com?subject=%3D%3Funknown-charset%3Fb%3FYQ%3D%3D%3F%3D",
        "mailto:leave@example.com?subject=%E2%80%AEhidden",
        "mailto:leave@example.com?body=bad%00body",
        "mailto:leave@example.com?body=bad%0Abreak",
        "mailto:leave@example.com?body=bad%0Dbreak",
        "mailto:leave@example.com?body=bad%7Fbody",
        "mailto:leave@example.com?cc=hidden@example.com",
        "mailto:leave@example.com?bcc=hidden@example.com",
        "mailto:leave@example.com?from=hidden@example.com",
        "mailto:leave@example.com?attachment=/etc/passwd",
        "mailto:unsub-abc=def@list.example.com?cc=hidden@example.com",
        "mailto:unsub/abc@list.example.com?bcc=hidden@example.com",
        "mailto:unsub/abc@list.example.com?subject=one?bcc=hidden@example.com",
        "mailto:unsub-abc=def@list.example.com?from=hidden@example.com",
        "mailto:unsub-abc=def@list.example.com?subject=one&subject=two",
        "mailto:unsub-abc=def@list.example.com?body=/%0Dhidden",
        "mailto:unsub%escape@list.example.com",
        "mailto:unsub%00abc@list.example.com",
        "mailto:unsub%0D%0Aabc@list.example.com",
        "mailto:unsub?abc@list.example.com",
        "mailto:unsub&abc@list.example.com",
        "mailto:leave@example.com?to=hidden@example.com%0D%0ABcc%3Asecret@example.com",
        "mailto:Display%20%3Cleave@example.com%3E",
        "mailto:leave@example.com,",
        "mailto:leave@localhost",
        "mailto:l%C3%A9ave@example.com",
        "https://example.com/unsubscribe",
        "javascript:alert(1)",
    ],
)
def test_unsafe_or_ambiguous_mailto_targets_are_refused(url):
    with pytest.raises(unsubscribe.UnsubscribeRefused):
        unsubscribe.parse_mailto(url)


@pytest.mark.parametrize("post", [False, True])
def test_header_https_is_preferred_over_mailto_regardless_of_order(post):
    extracted = links("https://example.com/unsubscribe", post=post)
    extracted["header_urls"].insert(0, "mailto:leave@example.com")
    assert unsubscribe.select_target("sender@example.com", extracted) == {
        "method": "POST" if post else "GET",
        "url": "https://example.com/unsubscribe",
        "host": "example.com",
    }


def test_header_mailto_fallback_precedes_body_links():
    extracted = links("https://foreign.example/unsubscribe")
    extracted["header_urls"].append("mailto:leave@example.com")
    extracted["html_links"] = ["https://example.com/unsubscribe"]
    assert unsubscribe.select_target("sender@example.com", extracted)["method"] == "MAILTO"


def test_invalid_mailto_can_fall_back_to_safe_body_link():
    extracted = links("mailto:leave@example.com?bcc=hidden@example.com")
    with pytest.raises(unsubscribe.UnsubscribeRefused, match="only to, subject and body"):
        unsubscribe.select_target("sender@example.com", extracted)
    extracted["html_links"] = ["https://example.com/unsubscribe"]
    assert unsubscribe.select_target("sender@example.com", extracted)["method"] == "GET"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/unsubscribe",
        "ftp://example.com/unsubscribe",
        "file:///unsubscribe",
        "javascript:unsubscribe()",
        "data:text/html,unsubscribe",
        "https://user:secret@example.com/unsubscribe",
        "https://example.com:444/unsubscribe",
        "https://example.com./unsubscribe",
        "https://127.0.0.1/unsubscribe",
        "https://[::1]/unsubscribe",
        "https://example.com/unsubscribe\r\nCookie: secret",
    ],
)
def test_invalid_urls_refused_even_with_one_click(url):
    with pytest.raises(unsubscribe.UnsubscribeRefused):
        unsubscribe.select_target("sender@example.com", links(url, post=True))


def test_get_sender_domain_allowed_and_foreign_domain_refused():
    selected = unsubscribe.select_target(
        "Sender <sender@mail.example.com>", links("https://news.example.com/unsubscribe")
    )
    assert selected["method"] == "GET"
    for host in ("foreign.com", "example.com.foreign.com", "notexample.com"):
        with pytest.raises(unsubscribe.UnsubscribeRefused):
            unsubscribe.select_target("sender@example.com", links(f"https://{host}/unsubscribe"))


@pytest.mark.parametrize(
    "sender",
    [
        "first@example.com, second@foreign.com",
        "sender@example.com\r\nFrom: other@example.com",
        "sender@localhost",
        "broken address",
        "",
    ],
)
def test_sender_must_be_one_valid_mailbox(sender):
    with pytest.raises(unsubscribe.UnsubscribeRefused):
        unsubscribe.select_target(sender, links("https://example.com/unsubscribe", post=True))


def test_unreviewed_and_private_suffixes_do_not_allow_sibling_domains():
    selected = unsubscribe.select_target(
        "sender@tenant.github.io", links("https://tenant.github.io/unsubscribe")
    )
    assert selected["method"] == "GET"
    for host in ("other.github.io", "child.tenant.github.io", "tenant.other.io"):
        with pytest.raises(unsubscribe.UnsubscribeRefused):
            unsubscribe.select_target(
                "sender@tenant.github.io", links(f"https://{host}/unsubscribe")
            )


def test_esp_is_reviewed_for_that_sender_only(monkeypatch):
    monkeypatch.setattr(unsubscribe, "ESP_DOMAINS_BY_SENDER", {"example.com": ("esp.test",)})
    target = links("https://campaign.esp.test/unsubscribe")
    assert unsubscribe.select_target("sender@example.com", target)["method"] == "GET"
    with pytest.raises(unsubscribe.UnsubscribeRefused):
        unsubscribe.select_target("sender@github.com", target)


def test_post_requires_exact_header_and_a_header_url():
    foreign = "https://foreign.com/unsubscribe"
    assert unsubscribe.select_target("sender@example.com", links(foreign, post=True))["method"] == (
        "POST"
    )
    for post in ([], ["List-Unsubscribe=Other"], [unsubscribe.ONE_CLICK] * 2):
        extracted = links(foreign)
        extracted["list_unsubscribe_post"] = post
        with pytest.raises(unsubscribe.UnsubscribeRefused):
            unsubscribe.select_target("sender@example.com", extracted)
    extracted = unsubscribe.extract(
        {"List-Unsubscribe-Post": unsubscribe.ONE_CLICK}, [f'<a href="{foreign}">Unsubscribe</a>']
    )
    with pytest.raises(unsubscribe.UnsubscribeRefused):
        unsubscribe.select_target("sender@example.com", extracted)


@pytest.mark.parametrize("post", [False, True])
def test_https_request_local_fake_no_credentials_or_body_echo(endpoint, monkeypatch, post):
    requests, _, connections = endpoint

    def forbidden_read(*args, **kwargs):
        raise AssertionError("Response body must never be read")

    monkeypatch.setattr(http.client.HTTPResponse, "read", forbidden_read)
    target = unsubscribe.select_target(
        "sender@example.com", links("https://example.com/unsubscribe?private=query", post=post)
    )
    assert unsubscribe.perform(target) == 204
    assert connections == [(_PUBLIC_IP, 443)]
    method, path, headers, body = requests[0]
    assert method == ("POST" if post else "GET")
    assert path == "/unsubscribe?private=query"
    assert headers["Host"] == "example.com"
    assert body == (b"List-Unsubscribe=One-Click" if post else b"")
    assert {key.lower() for key in headers}.isdisjoint(
        {"authorization", "cookie", "proxy-authorization", "referer"}
    )
    if post:
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"


@pytest.mark.parametrize("location", ["https://foreign.example/unsubscribe", "/next"])
def test_redirects_refused_without_second_request(endpoint, location):
    requests, state, connections = endpoint
    state.update(status=302, location=location)
    target = unsubscribe.select_target(
        "sender@example.com", links("https://example.com/unsubscribe")
    )
    with pytest.raises(unsubscribe.UnsubscribeHTTPError, match="redirects are refused"):
        unsubscribe.perform(target)
    assert len(requests) == len(connections) == 1


def test_server_errors_have_no_body_or_query_secrets(endpoint):
    _, state, _ = endpoint
    state["status"] = 500
    target = unsubscribe.select_target(
        "sender@example.com", links("https://example.com/unsubscribe?private=query-secret")
    )
    with pytest.raises(unsubscribe.UnsubscribeHTTPError) as caught:
        unsubscribe.perform(target)
    assert "query-secret" not in str(caught.value)
    assert "response-body" not in str(caught.value)


def test_tls_verifies_original_hostname(endpoint):
    requests, _, _ = endpoint
    target = unsubscribe.select_target(
        "sender@example.com", links("https://wrong.example.com/unsubscribe")
    )
    with pytest.raises(unsubscribe.UnsubscribeHTTPError):
        unsubscribe.perform(target)
    assert requests == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "fec0::1",
        "64:ff9b::a00:1",
        "::ffff:93.184.216.34",
        "2002:c0a8:1::1",
    ],
)
def test_any_nonpublic_dns_answer_refused_before_connect(monkeypatch, address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 443)),
            (family, socket.SOCK_STREAM, 6, "", sockaddr),
        ],
    )
    target = unsubscribe.select_target(
        "sender@example.com", links("https://example.com/unsubscribe")
    )
    with pytest.raises(unsubscribe.UnsubscribeHTTPError, match="public addresses"):
        unsubscribe.perform(target)


def test_dns_wait_is_bounded_without_later_request(monkeypatch):
    finish = threading.Event()
    done = threading.Event()

    def slow_dns(*args, **kwargs):
        finish.wait(timeout=2)
        done.set()
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", slow_dns)
    target = unsubscribe.select_target(
        "sender@example.com", links("https://example.com/unsubscribe")
    )
    try:
        with pytest.raises(unsubscribe.UnsubscribeHTTPError, match="DNS lookup timed out"):
            unsubscribe.perform(target, timeout=0.02)
    finally:
        finish.set()
        assert done.wait(timeout=2)


def test_dripping_response_is_bounded_by_total_deadline(endpoint):
    requests, state, _ = endpoint
    state["drip"] = True
    target = unsubscribe.select_target(
        "sender@example.com", links("https://example.com/unsubscribe")
    )
    start = time.monotonic()
    with pytest.raises(unsubscribe.UnsubscribeHTTPError):
        unsubscribe.perform(target, timeout=TIGHT)
    assert time.monotonic() - start < TIGHT_BOUND
    assert len(requests) == 1


def test_stalled_tls_handshake_respects_total_deadline(monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(1)
    done = threading.Event()
    accepted = threading.Event()
    errors = []

    def serve():
        try:
            connection, _ = listener.accept()
            with connection:
                accepted.set()
                done.wait(1)
        except Exception as error:
            errors.append(type(error).__name__)

    thread = threading.Thread(target=serve)
    thread.start()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 443))],
    )

    def connect(sock, address):
        assert address == (_PUBLIC_IP, 443)
        return _REAL_CONNECT(sock, listener.getsockname())

    monkeypatch.setattr(socket.socket, "connect", connect)
    try:
        start = time.monotonic()
        with pytest.raises(unsubscribe.UnsubscribeHTTPError):
            unsubscribe.perform(
                {"url": "https://example.com/unsubscribe", "host": "example.com", "method": "GET"},
                timeout=TIGHT,
            )
        assert accepted.is_set()
        assert time.monotonic() - start < TIGHT_BOUND
    finally:
        done.set()
        thread.join(2)
        listener.close()
        assert not thread.is_alive()
        assert not errors


@pytest.mark.parametrize("timeout", [0, -1, 6, float("inf"), float("nan")])
def test_timeout_can_only_be_lowered(timeout):
    target = unsubscribe.select_target(
        "sender@example.com", links("https://example.com/unsubscribe")
    )
    with pytest.raises(unsubscribe.UnsubscribeRefused):
        unsubscribe.perform(target, timeout=timeout)
