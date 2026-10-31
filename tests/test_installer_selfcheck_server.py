"""The self-check's client against the real MCP app, in memory: its protocol stays valid."""

import email.message
import io
import urllib.error

import pytest

pytest.importorskip("fcntl")  # The server needs POSIX file locking.

from starlette.testclient import TestClient  # noqa: E402

from proton_mail_mcp.config import Settings  # noqa: E402
from proton_mail_mcp.installer import selfcheck  # noqa: E402
from proton_mail_mcp.server import create_app  # noqa: E402

TOKEN = "t" * 40


class Mail:
    """What the MCP server needs of its mail client for one read-only tool."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def count_mail(self, **arguments):
        self.calls.append(arguments)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Response:
    def __init__(self, reply):
        self.status = reply.status_code
        self.headers = reply.headers
        self.stream = io.BytesIO(reply.content)
        self.read = self.stream.read

    def __iter__(self):
        return iter(self.stream)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def in_memory(http):
    def open_url(request, _timeout):
        reply = http.request(
            request.get_method(),
            "/mcp",
            content=request.data,
            headers=dict(request.header_items()),
        )
        if reply.status_code >= 400:
            headers = email.message.Message()
            raise urllib.error.HTTPError(
                request.full_url, reply.status_code, "refused", headers, io.BytesIO()
            )
        return Response(reply)

    return open_url


def check(mail, *, token=TOKEN, confirm=True, sleep=lambda _seconds: None):
    settings = Settings(token=TOKEN, bridge_user="you@example.com", confirm_mutations=confirm)
    said = []
    app = create_app(settings, mail_client=mail)
    with TestClient(app, base_url="http://127.0.0.1:8765") as http:
        selfcheck.check_service(8765, token, said.append, open_url=in_memory(http), sleep=sleep)
    return said


# confirm_mutations makes the server stateful with SSE replies, the installer's default;
# without it the server is stateless with JSON replies.
@pytest.mark.parametrize("confirm", [True, False], ids=["stateful-sse", "stateless-json"])
def test_the_real_server_passes_with_the_right_token(confirm):
    mail = Mail({"folder": "INBOX", "count": 3})
    said = check(mail, confirm=confirm)
    assert len(said) == 2 and "reads INBOX through Bridge" in said[1]
    assert [call["folder"] for call in mail.calls] == ["INBOX"]


def test_the_real_server_refuses_a_wrong_token():
    with pytest.raises(selfcheck.SelfCheckError, match="refused the stored token"):
        check(Mail({"folder": "INBOX", "count": 3}), token="w" * 40)


def test_a_mail_failure_on_the_real_server_is_a_bridge_failure():
    pauses = []
    mail = Mail(RuntimeError("Bridge is down, password hunter2"))
    with pytest.raises(selfcheck.SelfCheckError) as caught:
        check(mail, sleep=pauses.append)
    assert "cannot read INBOX through Bridge" in str(caught.value)
    assert "hunter2" not in str(caught.value)
    assert len(mail.calls) == selfcheck._BRIDGE_ATTEMPTS and len(pauses) == 3


def test_a_timed_out_count_on_the_real_server_is_not_a_pass():
    mail = Mail({"folder": "INBOX", "count": 0, "partial": True, "timed_out": True})
    with pytest.raises(selfcheck.SelfCheckError, match="cannot read INBOX"):
        check(mail)
