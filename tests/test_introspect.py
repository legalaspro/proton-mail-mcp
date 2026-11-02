"""`proton-mail-mcp introspect`: the default tool list on stdio, every call refused, no Bridge."""

import asyncio
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from proton_mail_mcp import __version__, cli
from proton_mail_mcp.config import Settings
from proton_mail_mcp.introspect import REFUSAL, create_introspection_mcp
from proton_mail_mcp.server import create_mcp

ROOT = Path(__file__).resolve().parents[1]
SOURCE = str(ROOT / "src")
# Once imported, the child fails any name lookup, outbound connection or process start;
# asyncio's own socket pair still works, which on Windows is a loopback connect by address.
CHILD = """
import socket, subprocess, sys
import proton_mail_mcp.introspect
from proton_mail_mcp import cli

def refuse(*args, **kwargs):
    raise AssertionError("introspect reached for the network or a process")

socket.create_connection = socket.getaddrinfo = subprocess.Popen = refuse
sys.exit(cli.main(["introspect"]))
"""
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "Example Registry", "version": "1.0"},
    },
}


def default_server(tmp_path):
    values = {
        "PROTON_MCP_TOKEN": "t" * 32,
        "PROTON_BRIDGE_USER": "user@example.test",
        "PROTON_MCP_ACTION_LOG": str(tmp_path / "actions.jsonl"),
    }
    return create_mcp(Settings.from_env(values), mail_client=object())


def listed(mcp) -> list[dict]:
    return [tool.model_dump() for tool in asyncio.run(mcp.list_tools())]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty home that must stay empty."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    for name in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    return path


def test_it_lists_the_default_tools_and_instructions_with_sending_off(tmp_path, home):
    mcp = create_introspection_mcp()
    default = default_server(tmp_path)
    assert listed(mcp) == listed(default)
    assert mcp.instructions == default.instructions
    names = {tool["name"] for tool in listed(mcp)}
    assert not names & {"preview_send", "send_draft", "review_send"}


def test_every_tool_call_refuses_before_it_starts_and_writes_nothing(home):
    mcp = create_introspection_mcp()
    names = [tool["name"] for tool in listed(mcp)]

    async def call_each():
        for name in [*names, "no_such_tool"]:
            with pytest.raises(ToolError) as refused:
                await mcp.call_tool(name, {})
            assert str(refused.value) == REFUSAL

    asyncio.run(call_each())
    assert list(home.iterdir()) == []


def test_stdio_answers_initialize_and_tools_list_and_refuses_a_call(tmp_path, home):
    env = {**os.environ, "PYTHONPATH": SOURCE, "PYTHONDONTWRITEBYTECODE": "1"}
    log = tmp_path / "stderr.log"
    lines: queue.Queue = queue.Queue()

    def read(stream) -> None:
        for line in stream:
            lines.put(line)
        lines.put(b"")

    # A client keeps stdin open until each answer arrives, as the SDK cancels calls at EOF.
    def ask(child, message: dict) -> dict | None:
        child.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        child.stdin.flush()
        if "id" not in message:
            return None
        line = lines.get(timeout=60)
        assert line, log.read_text()
        return json.loads(line)

    with open(log, "wb") as stderr:
        child = subprocess.Popen(
            [sys.executable, "-P", "-c", CHILD],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=env,
        )
        threading.Thread(target=read, args=(child.stdout,), daemon=True).start()
        try:
            initialized = ask(child, INITIALIZE)["result"]
            ask(child, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools = ask(child, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]
            call = ask(
                child,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "search_mail", "arguments": {"text": "invoice"}},
                },
            )["result"]
            child.stdin.close()
            assert child.wait(timeout=60) == 0, log.read_text()
        finally:
            child.stdin.close()
            child.kill()
            child.wait()
            child.stdout.close()
    assert initialized["serverInfo"]["name"] == "proton-mail-mcp"
    assert initialized["serverInfo"]["version"] == __version__
    default = [tool["name"] for tool in listed(default_server(tmp_path))]
    assert [tool["name"] for tool in tools["tools"]] == default
    assert call["isError"] is True
    assert call["content"] == [{"type": "text", "text": REFUSAL}]
    assert list(home.iterdir()) == []


def test_introspect_takes_no_options_and_never_starts_on_a_typo(monkeypatch, capsys):
    started = []
    stand_in = SimpleNamespace(main=lambda: started.append(True) or 0)
    monkeypatch.setitem(sys.modules, "proton_mail_mcp.introspect", stand_in)
    assert cli.main(["introspect", "--port", "1"]) == 2
    assert started == []
    assert "unknown arguments: introspect --port 1" in capsys.readouterr().err
    assert cli.main(["introspect"]) == 0
    assert started == [True]


def test_the_image_pins_its_base_and_every_runtime_package_within_the_ranges():
    stages = re.findall(r"^FROM (\S+)", (ROOT / "Dockerfile").read_text(encoding="utf-8"), re.M)
    assert len(stages) == 2 and len(set(stages)) == 1
    assert re.fullmatch(r"python:3\.12-slim@sha256:[0-9a-f]{64}", stages[0])
    pins = {}
    for line in (ROOT / "docker" / "constraints.txt").read_text(encoding="utf-8").splitlines():
        name, version = line.split("==")
        pins[canonicalize_name(name)] = version
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    for dependency in map(Requirement, project["dependencies"]):
        name = canonicalize_name(dependency.name)
        assert name in pins, f"{name} has no pin in docker/constraints.txt"
        assert dependency.specifier.contains(pins[name]), f"{name}=={pins[name]}"
    # Only what the build is allowed to see can reach it.
    assert "!docker/constraints.txt" in (ROOT / ".dockerignore").read_text(encoding="utf-8")
