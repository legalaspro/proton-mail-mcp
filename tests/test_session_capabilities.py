"""Authentication capability refresh fails closed using in-memory peers only."""

import pytest
from test_cleanup_socket import socket_mailbox as socket_mailbox
from test_mail import FakeImap, client

from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import MailError, _Deadline


@pytest.fixture
def settings():
    return Settings(
        token="session-capabilities-test-token-" + "x" * 32,
        bridge_user="reader@example.test",
    )


@pytest.mark.parametrize(
    "status,data",
    [
        ("NO", [b"private-server-detail"]),
        ("BAD", [b"private-server-detail"]),
        ("OK", None),
        ("OK", []),
        ("OK", [None]),
        ("OK", [b""]),
        ("OK", [b" \t "]),
        ("OK", [b"IMAP4rev1 \xff"]),
        ("OK", ["IMAP4rev1 m\u00f6ve"]),
        ("OK", [(b"IMAP4rev1", b"MOVE")]),
        ("OK", [b"IMAP4rev1 ] prose MOVE"]),
        ("OK", [b"IMAP4rev1\tMOVE"]),
        ("OK", [b"IMAP4rev1\x00 MOVE"]),
        ("OK", [b"IMAP4rev1 \x7f MOVE"]),
        ("OK", [b'IMAP4rev1 "MOVE"']),
    ],
    ids=[
        "no",
        "bad",
        "missing-data",
        "empty-list",
        "missing-response",
        "empty-response",
        "whitespace",
        "non-ascii-bytes",
        "non-ascii-text",
        "invalid-item",
        "bracketed-prose",
        "tab-separator",
        "nul",
        "del",
        "quoted-capability",
    ],
)
def test_failed_refresh_never_exposes_session_or_uses_cached_capabilities(settings, status, data):
    fake = FakeImap()
    original = fake.capabilities = (b"IMAP4rev1", b"MOVE")

    def capability():
        fake.calls.append(("CAPABILITY",))
        return status, data

    fake.capability = capability
    with pytest.raises(MailError) as error:
        with client(settings, fake)._session():
            pytest.fail("A failed capability refresh exposed an authenticated session")

    assert str(error.value) in {
        "Mailbox operation failed",
        "Mailbox returned invalid capabilities",
    }
    assert fake.capabilities == original
    assert [call[0] for call in fake.calls] == ["STARTTLS", "LOGIN", "CAPABILITY", "LOGOUT"]


def test_failed_login_never_refreshes_or_exposes_session(settings):
    fake = FakeImap()

    def login(username, password):
        fake.calls.append(("LOGIN",))
        return "NO", [b"private-server-detail"]

    fake.login = login
    with pytest.raises(MailError, match="^Mailbox authentication failed$"):
        with client(settings, fake)._session():
            pytest.fail("A failed login exposed a session")
    assert [call[0] for call in fake.calls] == ["STARTTLS", "LOGIN", "LOGOUT"]


@pytest.mark.parametrize("login_response", [False, True])
def test_pre_login_capability_response_is_discarded(settings, login_response):
    fake = FakeImap()
    fake.capabilities = (b"IMAP4rev1", b"MOVE")
    queued = [b"IMAP4rev1 MOVE"]
    observed = []

    def response(name):
        assert name == "CAPABILITY"
        observed.append(tuple(queued))
        data = list(queued) or [None]
        queued.clear()
        return name, data

    def login(username, password):
        assert not queued, "Pre-authentication capabilities survived until LOGIN"
        fake.calls.append(("LOGIN",))
        if login_response:
            queued.append(b"IMAP4rev1 uidplus")
        return "OK", []

    def capability():
        fake.calls.append(("CAPABILITY",))
        return "OK", [b"IMAP4rev1 uidplus"]

    fake.response, fake.login, fake.capability = response, login, capability
    with client(settings, fake)._session() as (connection, _):
        assert set(connection.capabilities) == {"IMAP4REV1", "UIDPLUS"}
    assert observed == [(b"IMAP4rev1 MOVE",), (b"IMAP4rev1 uidplus",) if login_response else ()]
    expected = ["STARTTLS", "LOGIN"]
    if not login_response:
        expected.append("CAPABILITY")
    assert [call[0] for call in fake.calls] == [*expected, "LOGOUT"]


@pytest.mark.parametrize("latest_has_move", [False, True])
def test_latest_login_capability_replaces_earlier_wire_advertisement(
    socket_mailbox, latest_has_move
):
    earlier = ("IMAP4rev1", "IDLE") + (() if latest_has_move else ("MOVE",))
    latest = ("IMAP4rev1", "UIDPLUS") + (("MOVE",) if latest_has_move else ())
    backend, peer, _ = socket_mailbox(
        pre_auth_capabilities=("IMAP4rev1", "STARTTLS"),
        capabilities=latest,
        login_capability_response="tagged",
        login_untagged_capabilities=earlier,
    )
    # Real imaplib queues the untagged response before the tagged LOGIN code.
    # The tagged response can both grant and revoke the earlier MOVE capability.
    with backend._session() as (connection, _):
        assert connection.capabilities == tuple(atom.upper() for atom in latest)
    commands = [command for _, command in peer.commands]
    assert commands == [b"CAPABILITY", b"STARTTLS", b"LOGIN", b"LOGOUT"]


@pytest.mark.parametrize("failure", ["elapsed-deadline", "socket-timeout"])
def test_timed_out_capability_refresh_closes_without_yield_or_reuse(settings, monkeypatch, failure):
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])
    deadline = _Deadline(1, 1)
    fake = FakeImap()
    original = fake.capabilities = (b"IMAP4rev1",)

    def capability():
        fake.calls.append(("CAPABILITY",))
        if failure == "socket-timeout":
            raise TimeoutError("private-server-detail")
        clock[0] = 102.0
        return "OK", [b"IMAP4rev1 MOVE"]

    fake.capability = capability
    fake.shutdown = lambda: fake.calls.append(("SHUTDOWN",))
    backend = client(settings, fake)
    with pytest.raises(MailError, match="^Mailbox connection timed out$"):
        with backend._session(deadline):
            pytest.fail("An expired capability refresh exposed a session")
    assert deadline.expired
    assert fake.capabilities == original
    assert [call[0] for call in fake.calls] == ["STARTTLS", "LOGIN", "CAPABILITY", "SHUTDOWN"]
    calls = list(fake.calls)
    with pytest.raises(MailError, match="^Mailbox connection timed out$"):
        with backend._session(deadline):
            pytest.fail("An expired session deadline was reused")
    assert fake.calls == calls
