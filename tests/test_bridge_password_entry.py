"""Setup refuses an empty or short Bridge password, at the hidden prompt and on stdin, and never
shows what it refused or how long that was. A paste at a hidden prompt in classic PowerShell or
cmd can leave a single character, which Bridge then rejects."""

import base64
import io
import re
import secrets

import pytest
from test_installer_execute import executor
from test_installer_setup import TOKEN, USER
from test_installer_setup import applied as _setup_applied
from test_installer_setup import host as _setup_host

from proton_mail_mcp import bridge_password, credential_store
from proton_mail_mcp.credential_store import BRIDGE_SERVICE, TOKEN_SERVICE
from proton_mail_mcp.installer import cli
from proton_mail_mcp.installer.execute import PASSWORD_ATTEMPTS, InstallError
from proton_mail_mcp.installer.plan import PlanError
from proton_mail_mcp.installer.steps import StoreSecret

applied, host = _setup_applied, _setup_host
# Shaped as Bridge makes them: 16 random bytes in unpadded base64url, 22 characters.
VALID = "sentinelBridgePass-k3Q"
OTHER = "sentinelBridgePass-x7W"
# What a failed paste leaves; 9 characters, a number nothing else here prints.
SHORT = "sentinelZ"
HINT = (
    "In classic PowerShell or cmd, pasting at a hidden prompt often doesn't work. Use Windows "
    "Terminal and paste with Ctrl+Shift+V, or type the password."
)
LABEL = (
    f"Bridge IMAP password for {USER}, the password Bridge generated (not your Proton "
    "password); input hidden: "
)
REPEAT = "Repeat the Bridge password: "
EMPTY = "Nothing was entered, so nothing was kept."
TOO_SHORT = "That is too short to be a Bridge password, so it was not kept."
DIFFER = "The two entries differ, so neither was kept."
EXHAUSTED = (
    f"no usable Bridge password in 3 attempts, so none was stored. {HINT} Then rerun setup, or "
    "give the password on stdin with --password-stdin"
)
STDIN_TAIL = (
    ". Nothing was stored or changed. It is the password Bridge generated, not your Proton "
    "account password: open the Proton Mail Bridge app, select your mailbox, open Mailbox "
    "details and copy the password under Password (Bridge's CLI shows it with `info`). Pipe it "
    "in again, or run setup at a terminal without --password-stdin, which asks at a hidden prompt"
)
TERMINAL = (
    "--password-stdin reads the Bridge password from a pipe, and stdin here is a terminal, so "
    "setup read nothing. Nothing was changed. At a terminal, run setup without "
    "--password-stdin: it asks for the password at a hidden prompt"
)


def never_shown(text, *entries, lengths=True):
    """No refused or stored entry, nor the length of one, anywhere in text. A whole setup run
    numbers its steps, so there only the entries are looked for: its refusals are these."""
    for entry in entries:
        assert entry not in text
        if entry and lengths:
            assert not re.search(rf"(?<![\w.]){len(entry)}(?![\w.])", text), len(entry)


def prompting(monkeypatch, *entries):
    """An executor whose hidden prompt answers entries in turn; store_secret is recorded."""
    stored = []
    monkeypatch.setattr(credential_store, "store_secret", lambda *args: stored.append(args))
    labels = []
    pending = list(entries)

    def prompt(label):
        labels.append(label)
        assert pending, f"asked once too often: {label}"
        return pending.pop(0)

    run, out = executor(prompt=prompt)
    return run, out, labels, stored


def store_step(store="wincred"):
    return StoreSecret(store, BRIDGE_SERVICE, USER, "prompt", "Prompt for the Bridge password")


# The minimum ------------------------------------------------------------------------------


def test_the_minimum_takes_every_password_bridge_makes_and_refuses_a_failed_paste():
    assert bridge_password.MIN_LENGTH == 16
    for _ in range(200):
        made = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()
        assert len(made) == 22 and bridge_password.refusal(made) is None
        # So --password-stdin can store it in the Keychain without its prompt.
        assert credential_store.keychain_accepts(USER, made)
    # Bridge's imaptest build gives every account this one.
    assert bridge_password.refusal("abcdefgh12345678") is None
    assert bridge_password.refusal("x" * 15) == TOO_SHORT
    assert bridge_password.refusal("") == EMPTY


def test_a_short_entry_gets_the_same_words_whatever_its_length():
    assert {bridge_password.refusal("x" * size) for size in range(1, 16)} == {TOO_SHORT}
    assert bridge_password.PASTE_HINT == HINT


# The hidden prompt ------------------------------------------------------------------------


def test_a_valid_entry_typed_twice_is_stored_with_no_hint(monkeypatch):
    run, out, labels, stored = prompting(monkeypatch, VALID, VALID)
    run.run([store_step()])
    assert [args[3] for args in stored] == [VALID]
    assert labels == [LABEL, REPEAT]
    assert HINT not in out.getvalue()
    never_shown(out.getvalue(), VALID)


@pytest.mark.parametrize(
    ("entries", "said"),
    [
        (["", VALID, VALID], EMPTY),
        ([SHORT, VALID, VALID], TOO_SHORT),
        ([VALID, "", VALID, VALID], EMPTY),
        ([VALID, SHORT, VALID, VALID], TOO_SHORT),
        ([VALID, OTHER, VALID, VALID], DIFFER),
    ],
    ids=["empty", "short", "empty-repeat", "short-repeat", "mismatch"],
)
def test_a_refused_entry_asks_again_with_the_hint(monkeypatch, entries, said):
    run, out, labels, stored = prompting(monkeypatch, *entries)
    run.run([store_step()])
    assert [args[3] for args in stored] == [VALID]
    first = [LABEL] if len(entries) == 3 else [LABEL, REPEAT]
    assert labels == [*first, LABEL, REPEAT]
    assert f"    {said} {HINT} Try again.\n" in out.getvalue()
    never_shown(out.getvalue(), SHORT, VALID, OTHER)


@pytest.mark.parametrize(
    ("entries", "last"),
    [
        (["", "", ""], EMPTY),
        ([SHORT, SHORT, SHORT], TOO_SHORT),
        ([VALID, OTHER] * 3, DIFFER),
        (["", VALID, SHORT, SHORT], TOO_SHORT),
    ],
    ids=["empty", "short", "mismatch", "mixed"],
)
def test_three_refusals_stop_and_store_nothing(monkeypatch, entries, last):
    run, out, labels, stored = prompting(monkeypatch, *entries)
    with pytest.raises(InstallError) as caught:
        run.run([store_step()])
    assert str(caught.value) == f"Step 1 (Prompt for the Bridge password) failed: {EXHAUSTED}"
    assert stored == [] and run.changed == set()
    assert labels.count(LABEL) == PASSWORD_ATTEMPTS == 3
    assert out.getvalue().count(f"{HINT} Try again.") == 2
    assert out.getvalue().endswith(f"    {last}\n")
    never_shown(out.getvalue() + str(caught.value), SHORT, VALID, OTHER)


def test_setup_exits_nonzero_after_three_refusals_and_stores_nothing(applied, monkeypatch, capsys):
    real = cli.Executor
    typed = iter([SHORT] * 3)

    def pasting(*, dry_run, **kwargs):
        executor = real(dry_run=dry_run, **kwargs)
        executor.prompt = lambda _label: next(typed)
        return executor

    monkeypatch.setattr(cli, "Executor", pasting)
    assert cli.main(["setup", "--yes", "--bridge-user", USER]) == 1
    captured = capsys.readouterr()
    assert EXHAUSTED in captured.err
    assert captured.out.count(f"    {TOO_SHORT} {HINT} Try again.") == 2
    assert not [call for call in applied.store_calls if call[0][1] == "store"]
    never_shown(captured.out + captured.err, SHORT, lengths=False)


def test_the_keychain_still_prompts_by_itself(monkeypatch):
    # `security` reads the password itself, so setup never sees it to check it.
    run, _out, labels, stored = prompting(monkeypatch)
    run.run([store_step("keychain")])
    assert labels == [] and stored == []
    assert run.runner.calls[0][0][-1] == "-w"


# Reading stdin ----------------------------------------------------------------------------


class Pipe(io.TextIOWrapper):
    """stdin as a pipe gives it: bytes behind a text stream that is not a terminal."""

    def __init__(self, data: bytes, terminal: bool = False):
        super().__init__(io.BytesIO(data), encoding="utf-8")
        self.terminal = terminal

    def isatty(self):
        return self.terminal


@pytest.mark.parametrize(
    "data",
    [
        VALID.encode() + b"\n",
        VALID.encode() + b"\r\n",
        VALID.encode(),
        b"\xef\xbb\xbf" + VALID.encode() + b"\r\n",
    ],
    ids=["lf", "crlf", "no-newline", "bom-crlf"],
)
def test_stdin_gives_one_line_without_its_line_break(data):
    assert cli._password_from_stdin(Pipe(data)) == VALID


@pytest.mark.parametrize("data", [b"", b"\n", b"\r\n", None], ids=["empty", "lf", "crlf", "closed"])
def test_an_empty_pipe_is_refused(data):
    with pytest.raises(PlanError) as caught:
        cli._password_from_stdin(None if data is None else Pipe(data))
    assert str(caught.value) == "--password-stdin found no Bridge password on stdin" + STDIN_TAIL


@pytest.mark.parametrize("data", [SHORT.encode() + b"\r\n", b"x\n"], ids=["short", "one"])
def test_a_short_pipe_is_refused_without_saying_what_or_how_long(data):
    with pytest.raises(PlanError) as caught:
        cli._password_from_stdin(Pipe(data))
    assert str(caught.value) == (
        "--password-stdin found a line on stdin too short to be a Bridge password" + STDIN_TAIL
    )
    never_shown(str(caught.value), SHORT)


@pytest.mark.parametrize(
    "data",
    [f"{VALID}\n{VALID}\n".encode(), f"{VALID}\r\n\r\n".encode(), b"y" * 2000],
    ids=["two-lines", "blank-line", "too-long"],
)
def test_more_than_one_line_is_refused(data):
    with pytest.raises(PlanError) as caught:
        cli._password_from_stdin(Pipe(data))
    assert str(caught.value) == (
        "--password-stdin takes one line, the Bridge password, and stdin held more" + STDIN_TAIL
    )
    never_shown(str(caught.value), VALID)


def test_a_terminal_on_stdin_is_never_read():
    stream = Pipe(VALID.encode() + b"\n", terminal=True)
    with pytest.raises(PlanError) as caught:
        cli._password_from_stdin(stream)
    assert str(caught.value) == TERMINAL
    assert stream.buffer.tell() == 0


# setup --password-stdin, run for real on the fake host ---------------------------------------


def piped(monkeypatch, data: bytes, terminal: bool = False) -> Pipe:
    stream = Pipe(data, terminal)
    monkeypatch.setattr(cli.sys, "stdin", stream)
    return stream


def stored_secrets(applied):
    """What secret-tool was given to store, by service."""
    return {
        argv[argv.index("service") + 1]: kwargs["input"]
        for argv, kwargs in applied.store_calls
        if argv[1] == "store"
    }


def files_text(root):
    return "".join(
        path.read_bytes().decode("utf-8", "replace") for path in root.rglob("*") if path.is_file()
    )


@pytest.mark.parametrize("ending", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_a_valid_pipe_is_stored_and_never_prompted_for(applied, monkeypatch, capsys, ending):
    piped(monkeypatch, VALID.encode() + ending)
    assert cli.main(["setup", "--yes", "--bridge-user", USER, "--password-stdin"]) == 0
    assert stored_secrets(applied)[BRIDGE_SERVICE] == VALID
    assert applied.prompts == []
    captured = capsys.readouterr()
    assert "Read the Bridge password from stdin and save it in libsecret" in captured.out
    never_shown(captured.out + captured.err, VALID, lengths=False)
    assert VALID not in files_text(applied.home)


@pytest.mark.parametrize(
    ("data", "found"),
    [
        (b"", "--password-stdin found no Bridge password on stdin"),
        (b"\r\n", "--password-stdin found no Bridge password on stdin"),
        (
            SHORT.encode() + b"\n",
            "--password-stdin found a line on stdin too short to be a Bridge password",
        ),
    ],
    ids=["empty", "crlf-only", "short"],
)
def test_a_refused_pipe_exits_nonzero_and_changes_nothing(
    applied, monkeypatch, capsys, data, found
):
    piped(monkeypatch, data)
    assert cli.main(["setup", "--yes", "--bridge-user", USER, "--password-stdin"]) == 2
    captured = capsys.readouterr()
    assert f"install: {found}{STDIN_TAIL}\n" in captured.err
    # Not a single step ran: no program, no store, no file.
    assert applied.commands == [] and stored_secrets(applied) == {}
    assert applied.prompts == []
    never_shown(captured.out + captured.err, SHORT, lengths=False)


def test_a_terminal_on_stdin_is_refused_before_anything_is_planned(applied, monkeypatch, capsys):
    stream = piped(monkeypatch, VALID.encode() + b"\n", terminal=True)
    for argv in (["--yes"], ["--dry-run"], []):
        with pytest.raises(SystemExit) as caught:
            cli.main(["setup", *argv, "--bridge-user", USER, "--password-stdin"])
        assert caught.value.code == 2
        assert TERMINAL in capsys.readouterr().err
    assert stream.buffer.tell() == 0
    assert applied.commands == [] and stored_secrets(applied) == {}


def test_a_dry_run_reads_no_password(applied, monkeypatch, capsys):
    stream = piped(monkeypatch, VALID.encode() + b"\n")
    assert cli.main(["setup", "--dry-run", "--bridge-user", USER, "--password-stdin"]) == 0
    out = capsys.readouterr().out
    assert "[dry run] Read the Bridge password from stdin and save it in libsecret" in out
    # No hidden prompt is planned, so nothing asks for a terminal.
    assert "hidden prompt, so a person runs this" not in out
    assert stream.buffer.tell() == 0


def test_stdin_needs_yes_and_credentials(applied, monkeypatch, capsys):
    piped(monkeypatch, VALID.encode() + b"\n")
    for argv, said in (
        ([], "add --yes, once --dry-run has shown the plan"),
        (["--yes", "--skip-credentials"], "which --skip-credentials skips"),
    ):
        with pytest.raises(SystemExit) as caught:
            cli.main(["setup", *argv, "--bridge-user", USER, "--password-stdin"])
        assert caught.value.code == 2
        assert said in capsys.readouterr().err
    assert applied.commands == [] and stored_secrets(applied) == {}


def test_stdin_with_a_stored_password_needs_replace_credentials(applied, host, monkeypatch, capsys):
    host.probe.secrets = {("libsecret", BRIDGE_SERVICE, USER), ("libsecret", TOKEN_SERVICE, USER)}
    stream = piped(monkeypatch, VALID.encode() + b"\n")
    assert cli.main(["setup", "--yes", "--bridge-user", USER, "--password-stdin"]) == 2
    assert (
        "install: --password-stdin gives a Bridge password, but setup keeps the one already "
        in (capsys.readouterr().err)
    )
    assert stream.buffer.tell() == 0
    assert applied.commands == [] and stored_secrets(applied) == {}

    argv = ["setup", "--yes", "--bridge-user", USER, "--password-stdin", "--replace-credentials"]
    assert cli.main(argv) == 0
    stored = stored_secrets(applied)
    assert stored[BRIDGE_SERVICE] == VALID
    # --replace-credentials stores a new token beside it, as without --password-stdin.
    assert stored[TOKEN_SERVICE] != TOKEN and len(stored[TOKEN_SERVICE]) >= 43
    assert applied.prompts == []
    captured = capsys.readouterr()
    never_shown(captured.out + captured.err, VALID, lengths=False)


def test_a_hand_off_drops_password_stdin():
    words = ["setup", "--yes", "--json", "--password-stdin", "--client", "claude-desktop"]
    assert cli._handoff(words, windows=False).endswith(" setup --client claude-desktop")


@pytest.mark.parametrize("store", credential_store.STORES)
def test_a_password_from_stdin_is_stored_in_each_store_without_a_prompt(monkeypatch, store):
    # The Keychain too: `security -i` takes it on stdin, as it takes the token.
    stored = []
    monkeypatch.setattr(credential_store, "store_secret", lambda *args: stored.append(args))
    run, out = executor(stdin_password=VALID)
    run.run([StoreSecret(store, BRIDGE_SERVICE, USER, "stdin", "Read the Bridge password")])
    assert [args[:4] for args in stored] == [(store, BRIDGE_SERVICE, USER, VALID)]
    assert run.runner.calls == [] and run.changed == {"credentials"}
    never_shown(out.getvalue(), VALID)
