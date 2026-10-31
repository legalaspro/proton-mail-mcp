"""Worker preemption during real IMAP construction releases every owned resource."""

import imaplib
import io
import os
import sys

import pytest
from gluon_search import GluonSearchImap
from test_mail import message
from test_mail import settings as settings

from proton_mail_mcp.index_autorebuild import _Yielded
from proton_mail_mcp.mail import MailClient, _DeadlineIMAP4
from proton_mail_mcp.security import Credentials


def test_constructor_preemption_releases_worker_guard_and_allows_next_session(
    settings, monkeypatch
):
    reader = MailClient(settings, lambda _: Credentials("reader", "offline-password"))
    worker = reader.index_autorebuild
    stream = io.BytesIO()
    foreground = worker.foreground()
    events = []

    class Socket:
        def shutdown(self, how):
            events.append("interrupt")

        def close(self):
            events.append("close")

    def open_socket(connection, *args, **kwargs):
        connection.sock = Socket()
        connection._file = stream
        if sys.version_info < (3, 14):  # Since 3.14 IMAP4.file is a read-only property.
            connection.file = stream
        # A request starts after the worker's initial check, before registration
        # inside _DeadlineIMAP4.open. The constructor has not returned a handle
        # to MailClient._session yet, so only the real constructor can close it.
        foreground.__enter__()

    monkeypatch.setattr(imaplib.IMAP4, "open", open_socket)
    assert reader.imap_factory is _DeadlineIMAP4
    try:
        with pytest.raises(_Yielded):
            with worker._session():
                pytest.fail("A preempted constructor must not yield a usable connection")
        assert stream.closed
        assert events == ["interrupt", "interrupt", "close"]
        assert worker._connection is None
        # The manual rebuild guard must also be available after setup fails.
        guard = reader.search_index.acquire_reindex_guard()
        os.close(guard)
    finally:
        foreground.__exit__(None, None, None)

    fake = GluonSearchImap({"All Mail": {"1": message("<offline@example.test>")}})
    reader.imap_factory = lambda *args, **kwargs: fake
    with worker._session() as (connection, _):
        reader._folders(connection)
    assert not worker._yielded.is_set()
    assert worker._connection is None
    assert fake.calls[-1] == ("LOGOUT",)
