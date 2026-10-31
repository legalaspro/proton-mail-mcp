"""Network access and the real user state directory are off limits to the offline suite."""

import os
import socket
import subprocess
from pathlib import Path

import pytest

from proton_mail_mcp import protected

GENERIC_PROTECTED = Path(__file__).parent / "fixtures" / "generic_protected" / "protected.py"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise AssertionError("Tests must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket.socket, "connect_ex", fail)
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    monkeypatch.setattr(subprocess, "run", fail)
    for name in tuple(os.environ):
        if name.startswith("PROTON_"):
            monkeypatch.delenv(name)
    # A default action/audit log must never reach the real user state directory.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


@pytest.fixture(autouse=True)
def generic_protected_lists(request, monkeypatch):
    """Tests of enforcement read generic lists as defaults; the shipped ones list nothing."""
    if not getattr(request.module, "SHIPPED_PROTECTED_DEFAULTS", False):
        monkeypatch.setattr(protected, "__file__", str(GENERIC_PROTECTED))
