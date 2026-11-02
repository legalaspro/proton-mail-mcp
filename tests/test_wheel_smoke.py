"""The wheel smoke's installs by name: PyPI gives them the dependencies, never this package."""

import functools
import http.server
import importlib.util
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wheel_smoke.py"
# Taken before conftest's fixture blocks connections: these tests reach 127.0.0.1 only.
REAL_CONNECT = socket.socket.connect

# The smoke drives setup on a pseudo-terminal, so it imports pty, which needs termios.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the wheel smoke needs pty")


@pytest.fixture(autouse=True)
def loopback(forbid_network, monkeypatch):
    """urllib connects through socket.create_connection, here to 127.0.0.1 only."""

    def create_connection(address, timeout=None, source_address=None, **kwargs):
        host, port = address
        assert host == "127.0.0.1", host
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            REAL_CONNECT(sock, (host, port))
        except OSError:
            sock.close()
            raise
        return sock

    monkeypatch.setattr(socket, "create_connection", create_connection)


def smoke():
    spec = importlib.util.spec_from_file_location("wheel_smoke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def serve(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def get(url, accept="text/html"):
    request = urllib.request.Request(url, headers={"Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers.get("Content-Type"), response.read()
    except urllib.error.HTTPError as error:
        return error.code, None, b""


@pytest.fixture
def indexes(tmp_path, monkeypatch):
    """The smoke's file server, with a stand-in for PyPI that records what it was asked."""
    asked = []

    class FakePyPI(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            asked.append((self.path, self.headers.get("Accept")))
            body = f"page for {self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.pypi.simple.v1+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002
            pass

    module = smoke()
    upstream, upstream_url = serve(FakePyPI)
    monkeypatch.setattr(module, "PYPI", f"{upstream_url}/simple")
    project = tmp_path / "index" / module.DISTRIBUTION
    project.mkdir(parents=True)
    project.joinpath("index.html").write_text("the build under test")
    files, base = serve(functools.partial(module.QuietFiles, directory=str(tmp_path)))
    yield base, asked
    for server in (files, upstream):
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("name", ["mcp-proton-mail", "MCP_Proton.Mail", "mcp--proton_mail"])
def test_the_filtered_pypi_never_offers_this_package(indexes, name):
    base, asked = indexes
    assert get(f"{base}/pypi/{name}/")[0] == 404
    assert asked == []


def test_the_filtered_pypi_relays_every_other_project(indexes):
    base, asked = indexes
    accept = "application/vnd.pypi.simple.v1+json, text/html; q=0.01"
    status, kind, body = get(f"{base}/pypi/Typing_Extensions/", accept)
    assert (status, kind, body) == (
        200,
        "application/vnd.pypi.simple.v1+json",
        b"page for /simple/typing-extensions/",
    )
    assert asked == [("/simple/typing-extensions/", accept)]


def test_the_local_index_is_still_served_from_files(indexes):
    base, asked = indexes
    assert get(f"{base}/index/mcp-proton-mail/")[2] == b"the build under test"
    assert asked == []


def test_no_install_by_name_offers_pypi_directly():
    # pip prefers PyPI's release to a local build of the same version, so an install by name
    # that names PyPI itself tests a published copy instead of the wheel the smoke built.
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.count("pypi.org") == 1
    assert 'PYPI = "https://pypi.org/simple"' in source
