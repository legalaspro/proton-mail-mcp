"""Recovery can distinguish permanent login failures from interrupted transport."""

import imaplib

import pytest
from test_mail import FakeImap, client
from test_mail import settings as settings

from proton_mail_mcp.mail import CallDeadlineExceeded, MailAuthenticationError, MailError
from proton_mail_mcp.security import CredentialError


@pytest.mark.parametrize("mode", ["no", "exception", "credentials"])
def test_authentication_failure_has_a_fixed_safe_reason(settings, mode):
    fake = FakeImap()
    reader = client(settings, fake)
    if mode == "credentials":

        def unavailable(_):
            raise CredentialError("untrusted diagnostic")

        reader.credentials_provider = unavailable
    else:

        def reject(*_):
            if mode == "exception":
                raise imaplib.IMAP4.error("untrusted diagnostic")
            return "NO", [b"untrusted diagnostic"]

        fake.login = reject
    with pytest.raises(MailAuthenticationError) as caught:
        with reader._session():
            pytest.fail("Rejected credentials cannot open a usable session")
    assert str(caught.value) == "Mailbox authentication failed"
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "error", [imaplib.IMAP4.abort("connection closed"), OSError(), CallDeadlineExceeded("timeout")]
)
def test_interrupted_login_is_not_a_permanent_authentication_failure(settings, error):
    fake = FakeImap()

    def interrupted(*_):
        raise error

    fake.login = interrupted
    with pytest.raises(MailError) as caught:
        with client(settings, fake)._session():
            pytest.fail("Interrupted login cannot open a usable session")
    assert not isinstance(caught.value, MailAuthenticationError)
