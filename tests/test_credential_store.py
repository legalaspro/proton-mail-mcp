"""Credential stores: argv shapes, stdin-only secrets, and opaque failures."""

import subprocess
import sys
import uuid
from unittest.mock import Mock

import pytest

from proton_mail_mcp import credential_store
from proton_mail_mcp.config import (
    CREDENTIAL_LOCATION_KEYS,
    CREDENTIAL_STORES,
    ConfigError,
    Settings,
)
from proton_mail_mcp.credential_store import (
    BRIDGE_SERVICE,
    TOKEN_SERVICE,
    CredentialStoreError,
)
from proton_mail_mcp.security import CredentialError, get_credentials

USER = "bridge-user@example.test"
SECRET = "synthetic-secret-never-print-5f2c"
TOKEN = "t" * 40


def settings(**overrides):
    values = {"token": TOKEN, "bridge_user": USER, **overrides}
    return Settings(**values)


def completed(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


@pytest.fixture
def run(monkeypatch):
    process = Mock(return_value=completed(0, SECRET + "\n"))
    monkeypatch.setattr(subprocess, "run", process)
    return process


def test_store_names_match_the_config_whitelist():
    assert credential_store.STORES == CREDENTIAL_STORES


def test_settings_default_to_the_existing_keychain_and_reject_unknown_stores():
    assert settings().credential_store == "keychain"
    for store in CREDENTIAL_STORES:
        env = {"PROTON_MCP_TOKEN": TOKEN, "PROTON_BRIDGE_USER": USER}
        assert Settings.from_env({**env, "PROTON_MCP_CREDENTIAL_STORE": store}).credential_store
    with pytest.raises(ConfigError, match="PROTON_MCP_CREDENTIAL_STORE"):
        settings(credential_store="plaintext-file")


@pytest.mark.parametrize(
    ("store", "argv"),
    [
        (
            "keychain",
            ["/usr/bin/security", "find-generic-password", "-s", BRIDGE_SERVICE, "-a", USER, "-w"],
        ),
        ("libsecret", ["secret-tool", "lookup", "service", BRIDGE_SERVICE, "account", USER]),
        ("pass", ["pass", "show", f"{BRIDGE_SERVICE}/{USER}"]),
    ],
)
def test_lookup_uses_fixed_argv_and_timeout(run, store, argv):
    assert credential_store.lookup(store, BRIDGE_SERVICE, USER) == SECRET
    run.assert_called_once_with(argv, capture_output=True, text=True, check=False, timeout=10)


def test_pass_lookup_returns_only_the_first_line(run):
    run.return_value = completed(0, f"{SECRET}\nurl: example.test\n")
    assert credential_store.lookup("pass", TOKEN_SERVICE, USER) == SECRET


@pytest.mark.parametrize("store", ["keychain", "libsecret", "pass"])
@pytest.mark.parametrize("failure", ["status", "timeout", "missing"])
def test_lookup_failures_are_opaque(monkeypatch, store, failure):
    noisy = f"{SECRET} {TOKEN}"
    if failure == "status":
        process = Mock(return_value=completed(1, noisy, noisy))
    elif failure == "timeout":
        process = Mock(side_effect=subprocess.TimeoutExpired(noisy, 10, noisy, noisy))
    else:
        process = Mock(side_effect=FileNotFoundError(noisy))
    monkeypatch.setattr(subprocess, "run", process)
    with pytest.raises(CredentialStoreError) as caught:
        credential_store.lookup(store, BRIDGE_SERVICE, USER)
    assert SECRET not in str(caught.value) + repr(caught.value)


@pytest.mark.parametrize(
    "account", ["", "a/b", "..", ".hidden", "with space", 'qu"ote', "qu'ote", "back\\slash"]
)
def test_pass_paths_refuse_unsafe_accounts_before_running_anything(run, account):
    with pytest.raises(CredentialStoreError):
        credential_store.lookup("pass", BRIDGE_SERVICE, account)
    run.assert_not_called()


def test_keychain_and_libsecret_accept_any_existing_account_text(run):
    # Settings only required nonempty text before; argv stores need nothing more.
    credential_store.lookup("keychain", BRIDGE_SERVICE, "legacy account")
    credential_store.lookup("libsecret", BRIDGE_SERVICE, "legacy/account")
    assert run.call_count == 2


def test_libsecret_and_pass_store_secrets_on_stdin_only(run):
    run.return_value = completed(0)
    credential_store.store_secret("libsecret", TOKEN_SERVICE, USER, SECRET)
    credential_store.store_secret("pass", TOKEN_SERVICE, USER, SECRET)
    (libsecret_argv,), libsecret_kwargs = run.call_args_list[0]
    (pass_argv,), pass_kwargs = run.call_args_list[1]
    assert libsecret_argv[:2] == ["secret-tool", "store"]
    assert libsecret_argv[-4:] == ["service", TOKEN_SERVICE, "account", USER]
    assert libsecret_kwargs["input"] == SECRET
    assert pass_argv == ["pass", "insert", "--multiline", "--force", f"{TOKEN_SERVICE}/{USER}"]
    assert pass_kwargs["input"] == SECRET + "\n"
    for argv in (libsecret_argv, pass_argv):
        assert SECRET not in " ".join(argv)


def test_keychain_store_uses_interactive_security_on_stdin(run):
    run.return_value = completed(0)
    credential_store.store_secret("keychain", TOKEN_SERVICE, USER, SECRET)
    (argv,), kwargs = run.call_args
    assert argv == ["/usr/bin/security", "-i"]
    assert kwargs["input"] == (
        f'add-generic-password -U -s "{TOKEN_SERVICE}" -a "{USER}" -w "{SECRET}"\n'
    )


@pytest.mark.parametrize("secret", ['has"quote', "has'quote", "has space", "back\\slash"])
def test_keychain_store_refuses_secrets_it_cannot_quote(run, secret):
    with pytest.raises(CredentialStoreError, match="interactive Keychain prompt") as caught:
        credential_store.store_secret("keychain", BRIDGE_SERVICE, USER, secret)
    assert secret not in str(caught.value)
    run.assert_not_called()
    assert credential_store.keychain_prompt_argv(BRIDGE_SERVICE, USER)[-1] == "-w"


@pytest.mark.parametrize("secret", ["", "two\nlines", "carriage\rreturn", "nul\0"])
def test_store_refuses_empty_or_multiline_secrets(run, secret):
    with pytest.raises(CredentialStoreError):
        credential_store.store_secret("libsecret", TOKEN_SERVICE, USER, secret)
    run.assert_not_called()


def test_store_failure_is_opaque(run):
    run.return_value = completed(1, SECRET, SECRET)
    with pytest.raises(CredentialStoreError) as caught:
        credential_store.store_secret("libsecret", TOKEN_SERVICE, USER, SECRET)
    assert SECRET not in str(caught.value)


def test_keychain_exists_never_reads_the_secret(run):
    run.return_value = completed(0, "keychain: attributes only")
    assert credential_store.exists("keychain", TOKEN_SERVICE, USER)
    (argv,), _kwargs = run.call_args
    assert "-w" not in argv
    run.return_value = completed(44)
    assert not credential_store.exists("keychain", TOKEN_SERVICE, USER)


def test_exists_reports_empty_entries_as_absent_and_failed_checks_as_unknown(run):
    run.return_value = completed(0, "\n")
    assert not credential_store.exists("libsecret", TOKEN_SERVICE, USER)
    run.side_effect = FileNotFoundError("secret-tool")
    with pytest.raises(CredentialStoreError):
        credential_store.exists("libsecret", TOKEN_SERVICE, USER)


@pytest.mark.parametrize(
    ("store", "argv"),
    [
        ("keychain", ["/usr/bin/security", "delete-generic-password", "-s", TOKEN_SERVICE]),
        ("libsecret", ["secret-tool", "clear", "service", TOKEN_SERVICE]),
    ],
)
def test_remove_secret_runs_only_for_existing_entries(run, store, argv):
    run.return_value = completed(0, SECRET)
    assert credential_store.remove_secret(store, TOKEN_SERVICE, USER)
    (removal,), _kwargs = run.call_args
    assert removal[: len(argv)] == argv
    run.reset_mock()
    run.return_value = completed(44 if store == "keychain" else 1)  # each store's "not found"
    if store == "keychain":
        assert not credential_store.remove_secret(store, TOKEN_SERVICE, USER)
    else:
        # secret-tool says the same of a locked entry, so it is not reported absent.
        with pytest.raises(CredentialStoreError):
            credential_store.remove_secret(store, TOKEN_SERVICE, USER)
    assert run.call_count == 1  # only the existence check ran


def test_pass_entries_are_found_without_decrypting(run, monkeypatch, tmp_path):
    monkeypatch.setenv("PASSWORD_STORE_DIR", str(tmp_path))
    assert not credential_store.exists("pass", TOKEN_SERVICE, USER)
    assert not credential_store.remove_secret("pass", TOKEN_SERVICE, USER)
    run.assert_not_called()
    entry = tmp_path / TOKEN_SERVICE / f"{USER}.gpg"
    entry.parent.mkdir()
    entry.write_bytes(b"encrypted")
    assert credential_store.exists("pass", TOKEN_SERVICE, USER)
    run.return_value = completed(0)
    assert credential_store.remove_secret("pass", TOKEN_SERVICE, USER)
    run.assert_called_once()
    assert run.call_args.args[0] == ["pass", "rm", "--force", f"{TOKEN_SERVICE}/{USER}"]
    assert not credential_store.exists("pass", TOKEN_SERVICE, "../escape")


def test_location_settings_match_the_config_keys():
    assert tuple(key for key, _name in credential_store.LOCATION_SETTINGS) == (
        CREDENTIAL_LOCATION_KEYS
    )
    assert credential_store.pass_locations(
        {"PROTON_MCP_PASSWORD_STORE_DIR": "/srv/pass", "PROTON_MCP_GNUPGHOME": "", "OTHER": "x"}
    ) == {"PASSWORD_STORE_DIR": "/srv/pass"}


def test_pass_calls_use_the_given_store_and_gnupg_home(run, monkeypatch, tmp_path):
    # The service's own environment may name another store, or none.
    monkeypatch.setenv("PASSWORD_STORE_DIR", str(tmp_path / "shell-store"))
    monkeypatch.setenv("PATH", "/usr/bin")
    located = {"PASSWORD_STORE_DIR": str(tmp_path / "store"), "GNUPGHOME": str(tmp_path / "gpg")}
    assert credential_store.lookup("pass", TOKEN_SERVICE, USER, located) == SECRET
    run.return_value = completed(0)
    credential_store.store_secret("pass", TOKEN_SERVICE, USER, SECRET, located)
    entry = tmp_path / "store" / TOKEN_SERVICE / f"{USER}.gpg"
    entry.parent.mkdir(parents=True)
    entry.write_bytes(b"encrypted")
    assert credential_store.exists("pass", TOKEN_SERVICE, USER, located)
    assert not credential_store.exists("pass", TOKEN_SERVICE, USER)
    assert credential_store.remove_secret("pass", TOKEN_SERVICE, USER, located)
    assert [call.args[0][:2] for call in run.call_args_list] == [
        ["pass", "show"],
        ["pass", "insert"],
        ["pass", "rm"],
    ]
    for call in run.call_args_list:
        assert call.kwargs["env"]["PATH"] == "/usr/bin"
        assert call.kwargs["env"]["PASSWORD_STORE_DIR"] == located["PASSWORD_STORE_DIR"]
        assert call.kwargs["env"]["GNUPGHOME"] == located["GNUPGHOME"]
        assert SECRET not in " ".join(call.args[0])


def test_only_pass_calls_get_pass_locations(run):
    credential_store.lookup("libsecret", TOKEN_SERVICE, USER, {"PASSWORD_STORE_DIR": "/x"})
    credential_store.lookup("pass", TOKEN_SERVICE, USER, {})
    assert all("env" not in call.kwargs for call in run.call_args_list)


def test_settings_pass_their_locations_to_the_bridge_password_lookup(run, monkeypatch, tmp_path):
    monkeypatch.delenv("PROTON_BRIDGE_PASSWORD", raising=False)
    store = str(tmp_path / "store")
    env = {
        "PROTON_MCP_TOKEN": TOKEN,
        "PROTON_BRIDGE_USER": USER,
        "PROTON_MCP_CREDENTIAL_STORE": "pass",
        "PROTON_MCP_PASSWORD_STORE_DIR": store,
    }
    config = Settings.from_env(env)
    assert config.credential_locations == (("PROTON_MCP_PASSWORD_STORE_DIR", store),)
    assert get_credentials(config).password == SECRET
    assert run.call_args.kwargs["env"]["PASSWORD_STORE_DIR"] == store
    with pytest.raises(ConfigError, match="PROTON_MCP_GNUPGHOME must be an absolute path"):
        Settings.from_env({**env, "PROTON_MCP_GNUPGHOME": "relative/gnupg"})


@pytest.mark.skipif(sys.platform == "win32", reason="Credential Manager exists on Windows")
def test_wincred_is_unavailable_elsewhere():
    with pytest.raises(CredentialStoreError, match="only available on Windows"):
        credential_store.lookup("wincred", TOKEN_SERVICE, USER)
    assert credential_store.wincred_target(TOKEN_SERVICE, USER) == f"{TOKEN_SERVICE}/{USER}"


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows Credential Manager")
def test_wincred_round_trip():
    service = f"proton-mail-mcp-test-{uuid.uuid4().hex}"
    value = f"value-{uuid.uuid4().hex}"
    try:
        assert not credential_store.exists("wincred", service, USER)
        credential_store.store_secret("wincred", service, USER, value)
        assert credential_store.lookup("wincred", service, USER) == value
        assert credential_store.exists("wincred", service, USER)
    finally:
        credential_store.remove_secret("wincred", service, USER)
    assert not credential_store.remove_secret("wincred", service, USER)


def test_get_credentials_reads_the_configured_store(monkeypatch, run):
    monkeypatch.delenv("PROTON_BRIDGE_PASSWORD", raising=False)
    result = get_credentials(settings(credential_store="libsecret"))
    assert result.password == SECRET
    (argv,), _kwargs = run.call_args
    assert argv == ["secret-tool", "lookup", "service", BRIDGE_SERVICE, "account", USER]


def test_get_credentials_store_failure_is_opaque(monkeypatch, run, caplog):
    monkeypatch.delenv("PROTON_BRIDGE_PASSWORD", raising=False)
    run.return_value = completed(1, SECRET, SECRET)
    with pytest.raises(CredentialError, match="unavailable from the store") as caught:
        get_credentials(settings(credential_store="pass"))
    assert SECRET not in str(caught.value) + caplog.text


def test_environment_password_still_wins_over_any_store(monkeypatch, run):
    monkeypatch.setenv("PROTON_BRIDGE_PASSWORD", SECRET)
    assert get_credentials(settings(credential_store="libsecret")).password == SECRET
    run.assert_not_called()
