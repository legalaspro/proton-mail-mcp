"""A missing or wrong Bridge password, a Bridge that is not running and a failed STARTTLS say
what to do, in the same words wherever they show, and never the password, the account or the
token: not in a tool error, a log line, setup's output or a state file."""

import json
import logging
import subprocess
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient
from test_installer_execute import executor
from test_installer_selfcheck_server import Mail, in_memory
from test_installer_setup import PASSWORD as SETUP_PASSWORD
from test_installer_setup import TOKEN as SETUP_TOKEN
from test_installer_setup import USER as SETUP_USER
from test_installer_setup import applied as _setup_applied
from test_installer_setup import host as _setup_host
from test_mail import FakeImap

from proton_mail_mcp import bridge_password, credential_store
from proton_mail_mcp.config import Settings
from proton_mail_mcp.installer import cli, selfcheck
from proton_mail_mcp.installer.execute import InstallError
from proton_mail_mcp.installer.steps import SelfCheck, StoreSecret
from proton_mail_mcp.mail import (
    BridgeTLSError,
    BridgeUnavailable,
    MailAuthenticationError,
    MailClient,
)
from proton_mail_mcp.security import Credentials
from proton_mail_mcp.server import create_app

# setup's own run on a fake host, for its end-to-end check below.
applied, host = _setup_applied, _setup_host
SENTINEL = "sentinel-bridge-password-3f9a1c"
USER = "you@example.test"
TOKEN = "sentinel-mcp-token-" + "z" * 32
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}
WHERE = (
    "It is the password Bridge generated, not your Proton account password: open the Proton Mail "
    "Bridge app, select your mailbox, open Mailbox details and copy the password under Password "
    "(Bridge's CLI shows it with `info`)."
)
NEVER = "Never paste the password into a chat, and an assistant must not ask for it."
RUN_SETUP = (
    "Run `uvx mcp-proton-mail setup` yourself, in your own terminal (PowerShell on Windows): it "
    "asks for the password at a hidden prompt and stores it in"
)
RUN_REPLACE = (
    "Run `uvx mcp-proton-mail setup --replace-credentials --client <client>` yourself, in your "
    "own terminal (PowerShell on Windows): it asks for the password at a hidden prompt and "
    "stores it in"
)
CLIENT = (
    "Put the client you set up, such as claude-desktop, in place of <client>: the command also "
    "stores a new MCP token and gives it to that client."
)
STORE_NAMES = {
    "wincred": "Windows Credential Manager",
    "keychain": "the macOS Keychain",
    "libsecret": "the Linux keyring",
    "pass": "pass",
}


def test_a_password_that_cannot_be_read_says_where_bridge_shows_it_and_what_to_run():
    assert bridge_password.missing("wincred") == (
        "Mailbox authentication failed: no Bridge password could be read, because none is stored "
        "or the store is locked (if it is locked, unlock it and retry). "
        f"{WHERE} {RUN_SETUP} Windows Credential Manager. {NEVER}"
    )


@pytest.mark.parametrize("store", ["wincred", "libsecret"])
def test_an_empty_entry_that_setup_asks_for_again_needs_only_setup(store):
    assert bridge_password.missing(store, empty=True) == (
        "Mailbox authentication failed: the stored Bridge password is empty. "
        f"{WHERE} {RUN_SETUP} {STORE_NAMES[store]}. {NEVER}"
    )


@pytest.mark.parametrize("store", ["keychain", "pass", None])
def test_an_empty_entry_that_setup_would_keep_is_replaced(store):
    where = STORE_NAMES.get(
        store or "", "Credential Manager on Windows, the Keychain on macOS or the Linux keyring"
    )
    assert bridge_password.missing(store, empty=True) == (
        "Mailbox authentication failed: the stored Bridge password is empty. "
        f"{WHERE} {RUN_REPLACE} {where}. {NEVER} {CLIENT}"
    )


@pytest.mark.parametrize("store", credential_store.STORES)
def test_the_command_for_an_empty_entry_is_the_one_that_stores_a_new_one(
    store, monkeypatch, tmp_path
):
    """Plain setup asks again only where the store's existence check calls an empty entry
    absent; where it reports the entry, setup keeps it, so the text says --replace-credentials."""
    empty = subprocess.CompletedProcess([], 0, "\n", "")
    monkeypatch.setattr(credential_store, "_run", lambda *_args, **_kwargs: empty)
    monkeypatch.setattr(credential_store, "_wincred_read", lambda _target: "")
    entry = tmp_path / "proton-mail-mcp" / f"{USER}.gpg"
    entry.parent.mkdir()
    entry.write_bytes(b"")
    locations = {"PASSWORD_STORE_DIR": str(tmp_path)}
    kept = credential_store.exists(store, credential_store.BRIDGE_SERVICE, USER, locations)
    text = bridge_password.missing(store, empty=True)
    assert (RUN_REPLACE in text) is kept
    assert (RUN_SETUP in text) is not kept
    assert kept is (store in ("keychain", "pass"))


def test_a_rejected_login_names_the_username_and_password_and_replaces_them():
    assert bridge_password.rejected("keychain") == (
        "Mailbox authentication failed: Bridge rejected the Bridge username or password. "
        f"{WHERE} If Bridge is not signed in to this mailbox, sign in there first. "
        f"{RUN_REPLACE} the macOS Keychain. {NEVER} {CLIENT} If setup was given a username other "
        "than the username shown in Bridge's Mailbox details, also add `--bridge-user` with that "
        "one."
    )


def test_a_bridge_that_is_not_running_or_not_in_starttls_mode_is_named_with_its_address():
    assert bridge_password.not_running("127.0.0.1", 1144) == (
        "Proton Mail Bridge is not running: nothing answers on 127.0.0.1:1144. Start Bridge (its "
        "app, its service, or `bridge --noninteractive`), then retry"
    )
    assert bridge_password.tls_failed("::1", 1143) == (
        "Bridge STARTTLS failed on [::1]:1143. Bridge's IMAP connection mode must be STARTTLS, "
        "not SSL: in the Proton Mail Bridge app's settings, under Connection mode, choose "
        "STARTTLS for IMAP, then retry"
    )


def test_each_failure_has_one_fixed_log_line():
    assert bridge_password.LOGGED_REJECTED == (
        "Bridge rejected the login: wrong Bridge username or password"
    )
    assert bridge_password.logged_missing("pass") == (
        "Bridge password missing: none could be read from pass"
    )
    assert bridge_password.logged_missing("keychain", empty=True) == (
        "Bridge password missing: the entry in keychain is empty"
    )
    assert bridge_password.logged_not_running("127.0.0.1", 1144) == (
        "Bridge not running: nothing answers on 127.0.0.1:1144"
    )
    assert bridge_password.logged_tls_failed("127.0.0.1", 1143) == (
        "Bridge STARTTLS failed on 127.0.0.1:1143: Bridge's IMAP connection mode must be STARTTLS"
    )


@pytest.mark.parametrize("store", [*credential_store.STORES, None])
def test_every_message_names_the_store_keeps_the_lead_and_fits_a_description(store):
    assert set(credential_store.STORES) <= set(bridge_password._STORES)
    where = STORE_NAMES.get(
        store or "", "Credential Manager on Windows, the Keychain on macOS or the Linux keyring"
    )
    for text in (
        bridge_password.missing(store),
        bridge_password.missing(store, empty=True),
        bridge_password.rejected(store),
    ):
        assert text.startswith("Mailbox authentication failed: ")
        assert f"stores it in {where}. " in text
        assert len(text) <= 2048


# The server, with a real MailClient behind the HTTP app.


@pytest.fixture
def state(monkeypatch, tmp_path):
    """Every file the server may write lands here, to be searched for the sentinels."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("PROTON_BRIDGE_PASSWORD", raising=False)
    return tmp_path


def settings(state, store="keychain", **overrides):
    return Settings(
        token=TOKEN,
        bridge_user=USER,
        credential_store=store,
        confirm_mutations=False,
        action_log_path=str(state / "actions.jsonl"),
        identity_index_dir=str(state / "identity-index"),
        **overrides,
    )


def call(client, name, arguments):
    app = create_app(client.settings, mail_client=client)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    with TestClient(app, base_url="http://127.0.0.1") as http:
        reply = http.post("/mcp", json=body, headers=HEADERS)
    assert reply.status_code == 200, reply.text
    result = reply.json()["result"]
    assert result["isError"] is True
    (content,) = result["content"]
    return content["text"], reply.text


def nothing_secret(state, *texts):
    files = [path for path in state.rglob("*") if path.is_file()]
    seen = [*texts, *(path.read_bytes().decode("utf-8", "replace") for path in files)]
    for secret in (SENTINEL, USER, TOKEN):
        assert not any(secret in text for text in seen), secret


def logged(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "proton_mail_mcp.server" and record.levelno == logging.WARNING
    ]


def factory(fake):
    return lambda _host, _port, *, timeout: fake


TOOLS = [
    ("count_mail", {"folder": "INBOX"}),
    ("search_mail", {"subject": "Example"}),
    ("preview_action", {"action": "archive", "query": {"folder": "INBOX"}}),
]


@pytest.mark.parametrize("name,arguments", TOOLS, ids=[name for name, _ in TOOLS])
@pytest.mark.parametrize("store", credential_store.STORES)
def test_an_empty_stored_password_reaches_the_client_as_empty(
    monkeypatch, caplog, state, store, name, arguments
):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0, "\n", SENTINEL))
    )
    monkeypatch.setattr(credential_store, "_wincred_read", lambda _target: "")
    fake = FakeImap()
    fake.login = lambda *_: pytest.fail("logged in with an empty password")
    client = MailClient(settings(state, store), None, factory(fake))
    text, raw = call(client, name, arguments)
    assert text == f"Error executing tool {name}: {bridge_password.missing(store, empty=True)}"
    assert logged(caplog) == [f"Bridge password missing: the entry in {store} is empty"]
    nothing_secret(state, raw, caplog.text)


@pytest.mark.parametrize("name,arguments", TOOLS, ids=[name for name, _ in TOOLS])
@pytest.mark.parametrize(
    "process",
    [
        Mock(return_value=subprocess.CompletedProcess([], 1, SENTINEL, SENTINEL)),
        Mock(side_effect=OSError(SENTINEL)),
    ],
    ids=["store-refused", "store-missing"],
)
def test_a_password_the_store_cannot_give_is_missing_not_rejected(
    monkeypatch, caplog, state, process, name, arguments
):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(subprocess, "run", process)
    fake = FakeImap()
    fake.login = lambda *_: pytest.fail("logged in without a password")
    client = MailClient(settings(state, "pass"), None, factory(fake))
    text, raw = call(client, name, arguments)
    assert text == f"Error executing tool {name}: {bridge_password.missing('pass')}"
    assert logged(caplog) == ["Bridge password missing: none could be read from pass"]
    nothing_secret(state, raw, caplog.text)


@pytest.mark.parametrize("name,arguments", TOOLS, ids=[name for name, _ in TOOLS])
@pytest.mark.parametrize("store", credential_store.STORES)
def test_a_rejected_login_says_so_and_never_shows_the_password(
    caplog, state, store, name, arguments
):
    caplog.set_level(logging.DEBUG)
    fake = FakeImap()
    tried = []

    def reject(username, password):
        tried.append(password)
        return "NO", [f"[AUTHENTICATIONFAILED] {username} {password}".encode()]

    fake.login = reject
    client = MailClient(
        settings(state, store), lambda _settings: Credentials(USER, SENTINEL), factory(fake)
    )
    text, raw = call(client, name, arguments)
    assert tried and set(tried) == {SENTINEL}
    assert text == f"Error executing tool {name}: {bridge_password.rejected(store)}"
    assert logged(caplog) == ["Bridge rejected the login: wrong Bridge username or password"]
    nothing_secret(state, raw, caplog.text)


@pytest.mark.parametrize("name,arguments", TOOLS, ids=[name for name, _ in TOOLS])
def test_a_bridge_that_is_not_running_is_logged_with_its_address(caplog, state, name, arguments):
    caplog.set_level(logging.DEBUG)

    def refused(_host, _port, *, timeout):
        raise ConnectionRefusedError(111, SENTINEL)

    client = MailClient(
        settings(state, imap_port=1144), lambda _settings: Credentials(USER, SENTINEL), refused
    )
    text, raw = call(client, name, arguments)
    assert text == f"Error executing tool {name}: {bridge_password.not_running('127.0.0.1', 1144)}"
    assert logged(caplog) == ["Bridge not running: nothing answers on 127.0.0.1:1144"]
    nothing_secret(state, raw, caplog.text)


def tls_refused(fake):
    def starttls(*, ssl_context):
        fake.calls.append(("STARTTLS",))
        return "NO", [SENTINEL.encode()]

    return starttls


def tls_missing(fake):
    def starttls(*, ssl_context):
        raise fake.error(f"STARTTLS extension not supported by server. {SENTINEL}")

    return starttls


def tls_handshake(fake):
    import ssl

    def starttls(*, ssl_context):
        raise ssl.SSLError(1, f"[SSL: WRONG_VERSION_NUMBER] {SENTINEL}")

    return starttls


@pytest.mark.parametrize("name,arguments", TOOLS, ids=[name for name, _ in TOOLS])
@pytest.mark.parametrize("failure", [tls_refused, tls_missing, tls_handshake])
def test_a_failed_starttls_says_bridge_must_use_starttls(caplog, state, failure, name, arguments):
    import imaplib

    caplog.set_level(logging.DEBUG)
    fake = FakeImap()
    fake.error = imaplib.IMAP4.error
    fake.starttls = failure(fake)
    fake.login = lambda *_: pytest.fail("logged in without TLS")
    client = MailClient(
        settings(state), lambda _settings: Credentials(USER, SENTINEL), factory(fake)
    )
    text, raw = call(client, name, arguments)
    assert text == f"Error executing tool {name}: {bridge_password.tls_failed('127.0.0.1', 1143)}"
    assert logged(caplog) == [
        "Bridge STARTTLS failed on 127.0.0.1:1143: Bridge's IMAP connection mode must be STARTTLS"
    ]
    nothing_secret(state, raw, caplog.text)


def test_a_deadline_during_starttls_stays_a_timeout_not_a_tls_failure(state):
    fake = FakeImap()

    def slow(*, ssl_context):
        raise TimeoutError(SENTINEL)

    fake.starttls = slow
    client = MailClient(
        settings(state), lambda _settings: Credentials(USER, SENTINEL), factory(fake)
    )
    with pytest.raises(Exception) as caught:
        with client._session():
            pytest.fail("A timed-out STARTTLS opened a session")
    assert not isinstance(caught.value, BridgeTLSError)
    assert SENTINEL not in str(caught.value)


# Setup: its prompt and the self-check against the real server.


def test_the_prompt_keeps_asking_twice_and_never_prints_the_entry(monkeypatch):
    stored = []
    monkeypatch.setattr(credential_store, "store_secret", lambda *args: stored.append(args))
    answers = iter([SENTINEL, SENTINEL])
    labels = []
    run, out = executor(prompt=lambda label: labels.append(label) or next(answers))
    run.run([StoreSecret("wincred", credential_store.BRIDGE_SERVICE, USER, "prompt", "password")])
    assert stored[0][3] == SENTINEL and not run.warnings
    assert labels[1] == "Repeat the Bridge password: "
    assert SENTINEL not in out.getvalue()


def test_the_username_question_names_bridges_mailbox_details(monkeypatch, capsys):
    labels = []
    answers = iter([SENTINEL, SENTINEL, SENTINEL])
    monkeypatch.setattr("builtins.input", lambda label: labels.append(label) or next(answers))
    with pytest.raises(cli.PlanError) as caught:
        cli._ask_account()
    label = (
        "Bridge IMAP username, the username shown in Bridge's Mailbox details (Bridge's `info` "
        "command shows it too): "
    )
    assert labels == [label] * 3
    assert str(caught.value) == (
        "No Bridge username given. Nothing was changed; pass --bridge-user with the username "
        "shown in Bridge's Mailbox details"
    )
    said = capsys.readouterr()
    assert (
        said.err
        == (
            "That is not an address like you@proton.me; it was not kept. Type the username shown "
            "in Bridge's Mailbox details, not a password.\n"
        )
        * 3
    )
    assert SENTINEL not in said.out + said.err + str(caught.value)


def self_check(mail, store, *, sleep=lambda _seconds: None):
    """setup's self-check against the real MCP app, in memory."""
    server = Settings(token=TOKEN, bridge_user=USER, credential_store=store)
    said = []
    with TestClient(create_app(server, mail_client=mail), base_url="http://127.0.0.1:8765") as http:
        selfcheck.check_service(
            8765, TOKEN, said.append, store=store, open_url=in_memory(http), sleep=sleep
        )
    return said


CANNOT = "the MCP server runs, but it cannot read INBOX. "


@pytest.mark.parametrize("store", credential_store.STORES)
@pytest.mark.parametrize(
    "error,expected,attempts",
    [
        (
            lambda store: MailAuthenticationError(bridge_password.rejected(store)),
            lambda store: CANNOT + bridge_password.rejected(store),
            selfcheck._BRIDGE_ATTEMPTS,
        ),
        (
            lambda store: MailAuthenticationError(bridge_password.missing(store)),
            lambda store: CANNOT + bridge_password.missing(store),
            selfcheck._BRIDGE_ATTEMPTS,
        ),
        (
            lambda store: MailAuthenticationError(bridge_password.missing(store, empty=True)),
            lambda store: CANNOT + bridge_password.missing(store, empty=True),
            1,
        ),
        (
            lambda _store: BridgeTLSError("127.0.0.1", 1144),
            lambda _store: CANNOT + bridge_password.tls_failed("127.0.0.1", 1144),
            selfcheck._BRIDGE_ATTEMPTS,
        ),
        (
            lambda _store: BridgeUnavailable("127.0.0.1", 1144),
            lambda _store: "Bridge not running: nothing answers on 127.0.0.1:1144",
            selfcheck._BRIDGE_ATTEMPTS,
        ),
    ],
    ids=["rejected", "missing", "empty", "starttls", "not-running"],
)
def test_setups_self_check_shows_the_servers_fixed_text(store, error, expected, attempts):
    pauses = []
    mail = Mail(error(store))
    with pytest.raises(selfcheck.SelfCheckError) as caught:
        self_check(mail, store, sleep=pauses.append)
    assert str(caught.value) == expected(store)
    # An empty entry stays empty; a store may be unlocked at its prompt meanwhile, and a
    # Bridge still loading the account refuses its login, so those are asked again.
    assert len(mail.calls) == attempts and len(pauses) == attempts - 1


@pytest.mark.parametrize(
    "said,expected",
    [
        (f"{bridge_password.REJECTED} {SENTINEL}", bridge_password.rejected("pass")),
        (f"{bridge_password.AUTH_FAILED}: {SENTINEL}", bridge_password.missing("pass")),
    ],
    ids=["rejected-extra", "unknown-extra"],
)
def test_other_words_after_a_known_lead_are_never_shown(said, expected):
    mail = Mail(MailAuthenticationError(said))
    with pytest.raises(selfcheck.SelfCheckError) as caught:
        self_check(mail, "pass")
    assert str(caught.value) == CANNOT + expected
    assert SENTINEL not in str(caught.value)


@pytest.mark.parametrize(
    "said",
    [
        f"{bridge_password.NOT_RUNNING} 127.0.0.1:1143{SENTINEL}. Start Bridge",
        f"{bridge_password.TLS_FAILED} 127.0.0.1:1143. {SENTINEL}",
        SENTINEL,
    ],
    ids=["not-running-changed", "starttls-changed", "unknown"],
)
def test_a_tool_error_that_is_not_a_fixed_text_is_never_shown(said):
    mail = Mail(MailAuthenticationError(said))
    with pytest.raises(selfcheck.SelfCheckError) as caught:
        self_check(mail, "pass")
    assert str(caught.value) == (
        "the MCP server runs, but it cannot read INBOX through Bridge: Bridge is not running or "
        "not signed in, or the Bridge username or password is wrong"
    )


def test_the_self_check_names_the_store_setup_used_and_reads_without_a_double_stop(
    monkeypatch,
):
    seen = []

    def check_service(port, token, say, *, store):
        seen.append((port, store))
        raise selfcheck.SelfCheckError(CANNOT + bridge_password.missing(store))

    monkeypatch.setattr(credential_store, "lookup", lambda *_args: TOKEN)
    run, out = executor(check_service=check_service)
    step = SelfCheck(
        8765, "pass", credential_store.TOKEN_SERVICE, USER, "check", hint="/logs/mcp.log"
    )
    with pytest.raises(InstallError) as caught:
        run.run([step])
    assert seen == [(8765, "pass")]
    assert str(caught.value) == (
        f"Step 1 (check) failed: {CANNOT}{bridge_password.missing('pass')} See /logs/mcp.log."
    )
    assert "stores it in pass." in str(caught.value) and ".; see" not in str(caught.value)
    assert TOKEN not in str(caught.value) + out.getvalue()


def test_a_self_check_error_without_a_full_stop_keeps_the_semicolon(monkeypatch):
    def check_service(_port, _token, _say, *, store):
        raise selfcheck.SelfCheckError("no usable answer from http://127.0.0.1:8765/mcp")

    monkeypatch.setattr(credential_store, "lookup", lambda *_args: TOKEN)
    run, _out = executor(check_service=check_service)
    step = SelfCheck(8765, "pass", credential_store.TOKEN_SERVICE, USER, "check", hint="logs")
    with pytest.raises(InstallError) as caught:
        run.run([step])
    assert str(caught.value) == (
        "Step 1 (check) failed: no usable answer from http://127.0.0.1:8765/mcp; see logs"
    )


def test_the_self_check_against_a_real_mail_client_shows_no_secret(monkeypatch, caplog, state):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0, "\n", SENTINEL))
    )
    pauses = []
    mail = MailClient(settings(state), None, factory(FakeImap()))
    with pytest.raises(selfcheck.SelfCheckError) as caught:
        self_check(mail, "keychain", sleep=pauses.append)
    assert str(caught.value) == CANNOT + bridge_password.missing("keychain", empty=True)
    assert pauses == []
    nothing_secret(state, str(caught.value), caplog.text, json.dumps(caplog.messages))


def test_a_rejected_bridge_login_fails_setup_with_the_servers_text_and_no_secret(
    applied, monkeypatch, capsys, caplog
):
    """The real self-check against the real server and MailClient, with a Bridge that refuses
    the password and echoes it: setup says what to run, and no output, log line or file holds
    the password or a token."""
    caplog.set_level(logging.DEBUG)
    fake = FakeImap()
    fake.login = lambda user, password: (
        "NO",
        [f"[AUTHENTICATIONFAILED] {user} {password}".encode()],
    )
    state = applied.home / "state"
    settings = Settings(
        token=SETUP_TOKEN,
        bridge_user=SETUP_USER,
        credential_store="libsecret",
        action_log_path=str(state / "actions.jsonl"),
        identity_index_dir=str(state / "identity-index"),
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    mail = MailClient(
        settings, lambda _settings: Credentials(SETUP_USER, SETUP_PASSWORD), lambda *_a, **_k: fake
    )
    real = cli.Executor

    def checked(*, dry_run, **kwargs):
        executor = real(dry_run=dry_run, **kwargs)

        def check_service(port, token, say, *, store):
            app = create_app(settings, mail_client=mail)
            with TestClient(app, base_url="http://127.0.0.1:8765") as http:
                selfcheck.check_service(
                    port, token, say, store=store, open_url=in_memory(http), sleep=lambda _s: None
                )

        executor.check_service = check_service
        return executor

    monkeypatch.setattr(cli, "Executor", checked)
    assert cli.main(["setup", "--yes", "--bridge-user", SETUP_USER]) == 1
    captured = capsys.readouterr()
    assert (
        "the MCP server runs, but it cannot read INBOX. "
        f"{bridge_password.rejected('libsecret')} See journalctl --user -u "
        "proton-mail-mcp.service.\n"
    ) in captured.err
    generated = next(
        call[1]["input"]
        for call in applied.store_calls
        if credential_store.TOKEN_SERVICE in call[0]
    )
    seen = [captured.out, captured.err, caplog.text]
    seen += [
        path.read_bytes().decode("utf-8", "replace")
        for path in applied.home.rglob("*")
        if path.is_file()
    ]
    for secret in (SETUP_PASSWORD, SETUP_TOKEN, generated):
        assert not any(secret in text for text in seen)
