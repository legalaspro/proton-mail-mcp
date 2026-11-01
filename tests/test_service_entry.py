"""Service launcher: non-secret env files and a token that never enters os.environ."""

import os
import sys

import pytest

from proton_mail_mcp import credential_store, service_entry
from proton_mail_mcp.service_entry import EnvFileError, parse_env_file, read_env_file

TOKEN = "stored-token-" + "x" * 32
USER = "bridge-user@example.test"


def test_parse_keeps_literal_values_and_skips_comments():
    text = (
        "# comment\n\n"
        "PROTON_BRIDGE_USER=bridge-user@example.test\n"
        '  PROTON_MCP_IMAP_IDLE_FOLDERS = ["INBOX", "Folders/A=B"]  \n'
        "PROTON_MCP_SEND_SELF_NO_FORM=0\r\n"
    )
    assert parse_env_file(text) == {
        "PROTON_BRIDGE_USER": "bridge-user@example.test",
        "PROTON_MCP_IMAP_IDLE_FOLDERS": '["INBOX", "Folders/A=B"]',
        "PROTON_MCP_SEND_SELF_NO_FORM": "0",
    }


@pytest.mark.parametrize("key", ["PROTON_MCP_TOKEN", "PROTON_BRIDGE_PASSWORD"])
def test_secret_keys_are_refused_without_echoing_the_value(key):
    with pytest.raises(EnvFileError, match="credential store") as caught:
        parse_env_file(f"{key}={TOKEN}\n")
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "line",
    ["HOME=/tmp", "proton_mcp_port=1", "PROTON_MCP_PORT", "export PROTON_MCP_PORT=1", "=1"],
)
def test_only_proton_assignments_are_accepted(line):
    with pytest.raises(EnvFileError, match="Line 1"):
        parse_env_file(line)


def test_duplicates_and_control_characters_are_refused():
    with pytest.raises(EnvFileError, match="more than once"):
        parse_env_file("PROTON_MCP_PORT=1\nPROTON_MCP_PORT=2\n")
    with pytest.raises(EnvFileError, match="control"):
        parse_env_file("PROTON_BRIDGE_USER=a\tb\n")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_env_file_writable_by_others_is_refused(tmp_path):
    path = tmp_path / "service.env"
    path.write_text("PROTON_MCP_PORT=8765\n")
    path.chmod(0o600)
    assert read_env_file(path) == {"PROTON_MCP_PORT": "8765"}
    path.chmod(0o620)
    with pytest.raises(EnvFileError, match="not writable"):
        read_env_file(path)


def test_unreadable_env_file_is_reported_plainly(tmp_path):
    with pytest.raises(EnvFileError, match="cannot be read"):
        read_env_file(tmp_path / "missing.env")


def fake_lookup(calls):
    def lookup(store, service, account, locations=None):
        calls.append((store, service, account, locations))
        return TOKEN

    return lookup


def test_token_comes_from_the_configured_store(monkeypatch):
    calls = []
    monkeypatch.setattr(credential_store, "lookup", fake_lookup(calls))
    env = service_entry.service_env(
        {"PATH": "/usr/bin"},
        {"PROTON_BRIDGE_USER": USER, "PROTON_MCP_CREDENTIAL_STORE": "libsecret"},
    )
    assert env["PROTON_MCP_TOKEN"] == TOKEN
    assert env["PATH"] == "/usr/bin"
    assert calls == [("libsecret", credential_store.TOKEN_SERVICE, USER, {})]


def test_token_lookup_uses_the_pass_store_named_in_the_settings(monkeypatch):
    # The service does not inherit the installing shell's PASSWORD_STORE_DIR.
    calls = []
    monkeypatch.setattr(credential_store, "lookup", fake_lookup(calls))
    service_entry.service_env(
        {"PATH": "/usr/bin"},
        {
            "PROTON_BRIDGE_USER": USER,
            "PROTON_MCP_CREDENTIAL_STORE": "pass",
            "PROTON_MCP_PASSWORD_STORE_DIR": "/srv/pass",
            "PROTON_MCP_GNUPGHOME": "/srv/gnupg",
        },
    )
    located = {"PASSWORD_STORE_DIR": "/srv/pass", "GNUPGHOME": "/srv/gnupg"}
    assert calls == [("pass", credential_store.TOKEN_SERVICE, USER, located)]


def test_existing_environment_token_wins(monkeypatch):
    calls = []
    monkeypatch.setattr(credential_store, "lookup", fake_lookup(calls))
    env = service_entry.service_env({"PROTON_MCP_TOKEN": "from-env"}, {})
    assert env["PROTON_MCP_TOKEN"] == "from-env"
    assert calls == []


def test_missing_token_is_reported_without_details(monkeypatch):
    def unavailable(*args):
        raise credential_store.CredentialStoreError(f"leak {TOKEN}")

    monkeypatch.setattr(credential_store, "lookup", unavailable)
    with pytest.raises(EnvFileError) as caught:
        service_entry.service_env({}, {"PROTON_BRIDGE_USER": USER})
    assert TOKEN not in str(caught.value)


def test_main_passes_the_token_only_in_the_server_mapping(monkeypatch, tmp_path):
    from proton_mail_mcp import server

    path = tmp_path / "service.env"
    path.write_text(f"PROTON_BRIDGE_USER={USER}\nPROTON_MCP_CREDENTIAL_STORE=pass\n")
    path.chmod(0o600)
    monkeypatch.setattr(credential_store, "lookup", fake_lookup([]))
    served = []
    monkeypatch.setattr(server, "main", served.append)
    service_entry.main(["--env-file", str(path)])
    (env,) = served
    assert env["PROTON_MCP_TOKEN"] == TOKEN
    assert env["PROTON_MCP_CREDENTIAL_STORE"] == "pass"
    assert "PROTON_MCP_TOKEN" not in os.environ


def test_main_refuses_secret_env_files_and_logs_privately(monkeypatch, tmp_path, capsys):
    from proton_mail_mcp import server

    path = tmp_path / "service.env"
    path.write_text(f"PROTON_MCP_TOKEN={TOKEN}\n")
    path.chmod(0o600)
    log = tmp_path / "logs" / "mcp.log"
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    monkeypatch.setattr(server, "main", lambda env: pytest.fail("server started"))
    with pytest.raises(SystemExit) as caught:
        service_entry.main(["--env-file", str(path), "--log-file", str(log)])
    stream = sys.stderr
    stream.flush()
    assert caught.value.code == 2
    text = log.read_text()
    assert "Cannot start proton-mail-mcp" in text and TOKEN not in text
    if os.name == "posix":
        assert log.stat().st_mode & 0o777 == 0o600
    assert TOKEN not in capsys.readouterr().err
    stream.close()
