"""Network access and the real user state directory are off limits to the offline suite."""

import gc
import os
import socket
import subprocess
import sys
import threading
from email import headerregistry
from functools import lru_cache
from pathlib import Path

import pytest
from windows_skips import skip_reason

from proton_mail_mcp import protected

GENERIC_PROTECTED = Path(__file__).parent / "fixtures" / "generic_protected" / "protected.py"
REAL_CONNECT = socket.socket.connect
REAL_SOCKETPAIR = socket.socketpair
_pairing = threading.local()


def _socketpair(*args, **kwargs):
    """Windows builds a socket pair from a loopback connection, which is not network access."""
    _pairing.active = True
    try:
        return REAL_SOCKETPAIR(*args, **kwargs)
    finally:
        _pairing.active = False


def pytest_collection_modifyitems(items):
    """Tests of POSIX facts stay out of a Windows run, each for the reason windows_skips gives."""
    if sys.platform == "win32":
        for item in items:
            reason = skip_reason(item.nodeid.removeprefix("tests/"))
            if reason:
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(autouse=True)
def windows_installer_host(monkeypatch):
    """CI runners and Wine are elevated, and the installer must not leave the test's folder."""
    if sys.platform == "win32":
        monkeypatch.setattr("proton_mail_mcp.installer.cli._elevated", lambda: False)
        monkeypatch.setattr("proton_mail_mcp.installer.cli._leave_current_folder", lambda: None)


# Parsing is pure and senders repeat: the hit rate across the suite is 99.4%.
_parse_header = lru_cache(maxsize=1 << 14)(headerregistry.HeaderRegistry.__call__)
headerregistry.HeaderRegistry.__call__ = lambda self, name, value: _parse_header(self, name, value)
gc.set_threshold(100_000, 50, 50)


def pytest_collection_finish(session):
    gc.collect()
    gc.freeze()


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise AssertionError("Tests must not open a network connection")

    def connect(sock, address):
        return REAL_CONNECT(sock, address) if getattr(_pairing, "active", False) else fail()

    monkeypatch.setattr(socket, "socketpair", _socketpair)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    # Nothing may ask the system for this name; a macOS runner took 70 s to answer once.
    monkeypatch.setattr(socket, "getfqdn", lambda name="": "test.invalid")
    monkeypatch.setattr(subprocess, "run", fail)
    for name in tuple(os.environ):
        if name.startswith("PROTON_"):
            monkeypatch.delenv(name)
    # A default action/audit log must never reach the real user state directory.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))


@pytest.fixture(autouse=True)
def skip_disk_flushes(request, monkeypatch):
    """Only a power cut can tell a flush happened; waiting for the disk took a third of the suite.

    The call still happens, so tests that make it fail still see it, and fstat still refuses
    a bad descriptor. Windows flushes differ (read-only descriptors fail), so it stays real.
    """
    if os.name == "posix" and not request.node.get_closest_marker("real_fsync"):
        monkeypatch.setattr(os, "fsync", os.fstat)


@pytest.fixture(autouse=True)
def generic_protected_lists(request, monkeypatch):
    """Tests of enforcement read generic lists as defaults; the shipped ones list nothing."""
    if not getattr(request.module, "SHIPPED_PROTECTED_DEFAULTS", False):
        monkeypatch.setattr(protected, "__file__", str(GENERIC_PROTECTED))
