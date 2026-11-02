"""Nothing answers on Bridge's IMAP address: the client is told to start Bridge."""

import pytest

from proton_mail_mcp import mail as mail_module
from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import (
    BridgeUnavailable,
    CallDeadlineExceeded,
    MailClient,
    MailError,
)
from proton_mail_mcp.security import Credentials

PASSWORD = "bridge-password-secret"


@pytest.fixture
def settings():
    return Settings(
        token="a-test-token-that-is-at-least-32-characters", bridge_user="reader@example.test"
    )


def mailbox(settings, factory=None):
    credentials = lambda _: Credentials("reader@example.test", PASSWORD)  # noqa: E731
    if factory is None:
        return MailClient(settings, credentials)
    return MailClient(settings, credentials, factory)


def test_a_refused_connection_names_bridge_and_its_address(settings):
    def refused(host, port, *, timeout):
        raise ConnectionRefusedError(111, "Connection refused")

    with pytest.raises(BridgeUnavailable) as raised:
        mailbox(settings, refused).count_mail(subject="Example")

    text = str(raised.value)
    assert text.startswith(
        "Proton Mail Bridge is not running: nothing answers on 127.0.0.1:1143. Start Bridge"
    )
    assert PASSWORD not in text and "reader@example.test" not in text
    assert isinstance(raised.value, MailError)


@pytest.mark.parametrize(
    "failure,budget_left,expected",
    [
        (ConnectionRefusedError(111, "Connection refused"), False, BridgeUnavailable),
        (TimeoutError("timed out"), False, BridgeUnavailable),
        # The call's own budget, shorter than the socket timeout, ran out instead.
        (TimeoutError("timed out"), True, CallDeadlineExceeded),
    ],
)
def test_the_real_connection_reports_what_answered(
    settings, monkeypatch, failure, budget_left, expected
):
    def connect(address, timeout=None):
        assert address == ("127.0.0.1", 1143)
        raise failure

    monkeypatch.setattr(mail_module.socket, "create_connection", connect)
    if budget_left:
        settings = Settings(
            token="a-test-token-that-is-at-least-32-characters",
            bridge_user="reader@example.test",
            call_timeout=5.0,
        )

    with pytest.raises(MailError) as raised:
        with mailbox(settings)._session():
            pass
    assert type(raised.value) is expected


def test_an_ipv6_address_is_bracketed():
    assert "nothing answers on [::1]:1143." in str(BridgeUnavailable("::1", 1143))
