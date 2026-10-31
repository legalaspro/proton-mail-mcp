import ast
import socket
import ssl
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.security import (
    BridgeSecurityError,
    CredentialError,
    Credentials,
    bridge_endpoint,
    get_credentials,
    tls_context,
    verify_certificate,
)

TOKEN = "token-secret-" + "x" * 32
PASSWORD = "bridge-password-never-print-this"
USER = "bridge-user@example.test"


def settings(**kwargs):
    return Settings(token=TOKEN, bridge_user=USER, **kwargs)


@pytest.fixture
def local_certificate(tmp_path):
    key = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
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
        .sign(key, algorithm=None)
    )
    path = tmp_path / "bridge.pem"
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return path, certificate.public_bytes(serialization.Encoding.DER)


@pytest.mark.parametrize("token", ["", "short", "a" * 31])
def test_token_must_be_long_enough(token):
    with pytest.raises(ConfigError) as caught:
        Settings(token=token, bridge_user=USER)
    assert TOKEN not in str(caught.value)
    if token:
        assert token not in str(caught.value)


def test_token_is_required_from_environment():
    with pytest.raises(ConfigError):
        Settings.from_env({"PROTON_BRIDGE_USER": USER})


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "::", "localhost"])
def test_http_bind_refuses_anything_except_ipv4_loopback(host):
    with pytest.raises(ConfigError):
        settings(host=host)


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_ports_must_be_in_range(port):
    with pytest.raises(ConfigError):
        settings(port=port)
    with pytest.raises(ConfigError):
        settings(imap_port=port)


def test_configuration_and_credentials_hide_secrets():
    assert TOKEN not in repr(settings())
    assert PASSWORD not in repr(Credentials(USER, PASSWORD))


def test_environment_configuration_parses_all_documented_fields():
    configuration = Settings.from_env(
        {
            "PROTON_MCP_TOKEN": TOKEN,
            "PROTON_BRIDGE_USER": USER,
            "PROTON_MCP_HOST": "127.0.0.1",
            "PROTON_MCP_PORT": "8877",
            "PROTON_MCP_PUBLIC_HOST": "mac.example.ts.net",
            "PROTON_BRIDGE_HOST": "127.0.0.2",
            "PROTON_BRIDGE_IMAP_PORT": "2243",
            "PROTON_BRIDGE_CERT": "/test/bridge.pem",
            "PROTON_BRIDGE_DRAFTS_FOLDER": "Custom Drafts",
            "PROTON_BRIDGE_TIMEOUT": "15.5",
        }
    )
    assert configuration.port == 8877
    assert configuration.public_host == "mac.example.ts.net"
    assert configuration.imap_host == "127.0.0.2"
    assert configuration.imap_port == 2243
    assert configuration.imap_cert_path == "/test/bridge.pem"
    assert configuration.drafts_folder == "Custom Drafts"
    assert configuration.timeout == 15.5


@pytest.mark.parametrize("public_host", ["https://mac.ts.net", "mac.ts.net:443", "mac.ts.net/a"])
def test_public_hostname_must_have_no_scheme_port_or_path(public_host):
    with pytest.raises(ConfigError):
        settings(public_host=public_host)


def test_environment_password_fallback_does_not_invoke_keychain(monkeypatch):
    monkeypatch.setenv("PROTON_BRIDGE_PASSWORD", PASSWORD)
    process = Mock(side_effect=AssertionError("Unexpected Keychain access"))
    monkeypatch.setattr(subprocess, "run", process)
    result = get_credentials(settings())
    assert result.username == USER
    assert result.password == PASSWORD
    process.assert_not_called()


def test_keychain_uses_fixed_service_account_and_timeout(monkeypatch):
    monkeypatch.delenv("PROTON_BRIDGE_PASSWORD", raising=False)
    process = Mock(return_value=subprocess.CompletedProcess([], 0, PASSWORD + "\n", ""))
    monkeypatch.setattr(subprocess, "run", process)
    result = get_credentials(settings())
    assert result.username == USER
    assert result.password == PASSWORD
    process.assert_called_once_with(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            "proton-mail-mcp",
            "-a",
            USER,
            "-w",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize("failure", ["status", "timeout", "os_error", "empty"])
def test_keychain_errors_do_not_leak_secrets(monkeypatch, caplog, failure):
    monkeypatch.delenv("PROTON_BRIDGE_PASSWORD", raising=False)
    sensitive = f"{TOKEN} {PASSWORD}"
    if failure == "status":
        process = Mock(return_value=subprocess.CompletedProcess([], 1, sensitive, sensitive))
    elif failure == "timeout":
        process = Mock(side_effect=subprocess.TimeoutExpired(sensitive, 10, sensitive, sensitive))
    elif failure == "os_error":
        process = Mock(side_effect=OSError(sensitive))
    else:
        process = Mock(return_value=subprocess.CompletedProcess([], 0, "", sensitive))
    monkeypatch.setattr(subprocess, "run", process)
    with pytest.raises(CredentialError) as caught:
        get_credentials(settings())
    visible = str(caught.value) + repr(caught.value) + caplog.text
    assert TOKEN not in visible
    assert PASSWORD not in visible


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_only_local_imap_can_allow_self_signed_certificates(host, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        Mock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
            ]
        ),
    )
    context = tls_context(settings(imap_host=host))
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_remote_imap_requires_normal_certificate_validation():
    context = tls_context(settings(imap_host="bridge.example.test"))
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_localhost_resolution_is_pinned_to_a_numeric_loopback(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        Mock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
            ]
        ),
    )
    assert bridge_endpoint(settings(imap_host="localhost")) == "127.0.0.1"


@pytest.mark.parametrize("addresses", [[], ["127.0.0.1", "192.0.2.1"], ["192.0.2.1"]])
def test_localhost_cannot_relax_tls_if_it_resolves_off_loopback(monkeypatch, addresses):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        Mock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses
            ]
        ),
    )
    with pytest.raises(BridgeSecurityError):
        tls_context(settings(imap_host="localhost"))
    with pytest.raises(BridgeSecurityError):
        bridge_endpoint(settings(imap_host="localhost"))


@pytest.mark.parametrize("host", ["127.0.0.1", "bridge.example.test"])
def test_pinned_certificates_keep_chain_verification_enabled(host, local_certificate):
    cert, _ = local_certificate
    result = tls_context(settings(imap_host=host, imap_cert_path=str(cert)))
    assert result.verify_mode == ssl.CERT_REQUIRED
    assert result.check_hostname is (host != "127.0.0.1")


def test_exact_certificate_pin_accepts_only_matching_peer(local_certificate):
    cert, der = local_certificate
    connection = Mock()
    connection.sock.getpeercert.return_value = der
    configuration = settings(imap_cert_path=str(cert))
    verify_certificate(connection, configuration)
    connection.sock.getpeercert.assert_called_once_with(binary_form=True)
    connection.sock.getpeercert.return_value = b"different-certificate"
    with pytest.raises(BridgeSecurityError):
        verify_certificate(connection, configuration)


def test_certificate_pin_failure_does_not_expose_underlying_errors(monkeypatch, tmp_path, caplog):
    cert = tmp_path / "bridge.pem"
    cert.write_text("test PEM")
    monkeypatch.setattr(
        ssl,
        "PEM_cert_to_DER_cert",
        Mock(side_effect=ValueError(f"{TOKEN} {PASSWORD}")),
    )
    with pytest.raises(BridgeSecurityError) as caught:
        verify_certificate(Mock(), settings(imap_cert_path=str(cert)))
    visible = str(caught.value) + caplog.text
    assert TOKEN not in visible
    assert PASSWORD not in visible


def test_smtp_is_confined_to_guarded_send_and_mutations_to_cleanup():
    package = Path(__file__).resolve().parents[1] / "src" / "proton_mail_mcp"
    for source in package.rglob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                if any("smtp" in module.casefold() for module in modules):
                    assert source.name == "send.py", (source.name, node.lineno)
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            method = node.func.attr.casefold()
            if method in {"sendmail", "send_message", "smtp", "smtp_ssl"}:
                assert source.name == "send.py", (source.name, node.lineno)
            assert method != "expunge"
            if isinstance(node.func.value, ast.Name) and node.func.value.id in {
                "connection",
                "timed",
                "imap",
            }:
                assert method not in {"delete", "rename"}
                if method == "create":
                    assert source.name == "create_folder.py"
                if method == "close":
                    assert source.name == "send.py", (source.name, node.lineno)
            if method in {"store", "copy", "move"}:
                pytest.fail(f"Mutation must use explicit UID commands: {source.name}:{node.lineno}")
            if method == "uid" and node.args and isinstance(node.args[0], ast.Constant):
                command = str(node.args[0].value).upper()
                if command in {"STORE", "EXPUNGE"} and source.name == "delete_draft.py":
                    continue
                if command in {"STORE", "COPY", "MOVE", "EXPUNGE"}:
                    assert source.name == "cleanup_imap.py", (source.name, node.lineno)
                else:
                    assert command in {"SEARCH", "FETCH"}, (source.name, node.lineno)
