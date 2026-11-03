"""What to do when the server cannot use Bridge, said the same way everywhere.

The server's tool errors and log lines, setup's password checks and its self-check all build
their text here, so a person and their assistant read the same steps. The text names a store, a
command or Bridge's address, never a password, its length, an account or a token.
"""

# The lead every authentication error keeps; clients and tests may match it.
AUTH_FAILED = "Mailbox authentication failed"
# Bridge answered the login with a refusal, as opposed to nothing stored to log in with.
REJECTED = f"{AUTH_FAILED}: Bridge rejected the Bridge username or password."
# Stores what is missing and keeps a token it can read.
SETUP = "uvx mcp-proton-mail setup"
# Stores a new Bridge password and a new MCP token, then gives the client that token.
REPLACE = "uvx mcp-proton-mail setup --replace-credentials --client <client>"
_STORES = {
    "wincred": "Windows Credential Manager",
    "keychain": "the macOS Keychain",
    "libsecret": "the Linux keyring",
    "pass": "pass",
}
# Plain setup asks again only where an empty entry counts as none: credential_store.exists
# reports any Keychain item and any pass file, whatever it holds.
_EMPTY_IS_NONE = ("wincred", "libsecret")
_ANY_STORE = "Credential Manager on Windows, the Keychain on macOS or the Linux keyring"
USERNAME = "the username shown in Bridge's Mailbox details"
WHERE = (
    "It is the password Bridge generated, not your Proton account password: open the Proton Mail "
    "Bridge app, select your mailbox, open Mailbox details and copy the password under Password "
    "(Bridge's CLI shows it with `info`)."
)
# Bridge makes each password from 16 random bytes in unpadded base64url, so 22 characters:
# newRandomToken(16) in internal/vault/vault.go, encoded by algo.B64RawEncode in
# internal/user/user.go (Bridge 3), and generatePassword in internal/users/credentials/pass.go
# (Bridge 2, whose passwords Bridge 3 migrates as they are). 16 refuses what a failed paste
# leaves, often one character, and stays well below every password Bridge makes; it also takes
# the 16-character password of Bridge's own imaptest build.
MIN_LENGTH = 16
# Setup's hint after it refuses an entry; docs/windows.md says the same.
PASTE_HINT = (
    "In classic PowerShell or cmd, pasting at a hidden prompt often doesn't work. Use Windows "
    "Terminal and paste with Ctrl+Shift+V, or type the password."
)
NOT_RUNNING = "Proton Mail Bridge is not running: nothing answers on"
TLS_FAILED = "Bridge STARTTLS failed on"
# The server's log line for a rejected login; the others name the store or Bridge's address.
LOGGED_REJECTED = "Bridge rejected the login: wrong Bridge username or password"


def address(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _run(command: str, store: str | None) -> str:
    where = _STORES.get(store or "", _ANY_STORE)
    text = (
        f"Run `{command}` yourself, in your own terminal (PowerShell on Windows): it asks for "
        f"the password at a hidden prompt and stores it in {where}. Never paste the password "
        "into a chat, and an assistant must not ask for it."
    )
    if command == REPLACE:
        text += (
            " Put the client you set up, such as claude-desktop, in place of <client>: the "
            "command also stores a new MCP token and gives it to that client."
        )
    return text


def missing(store: str | None = None, *, empty: bool = False) -> str:
    """Nothing usable is stored: the entry is empty, absent or locked."""
    if empty:
        found = "the stored Bridge password is empty."
        # Plain setup would keep an empty Keychain item or pass file.
        command = SETUP if store in _EMPTY_IS_NONE else REPLACE
    else:
        found = (
            "no Bridge password could be read, because none is stored or the store is locked "
            "(if it is locked, unlock it and retry)."
        )
        command = SETUP
    return f"{AUTH_FAILED}: {found} {WHERE} {_run(command, store)}"


def rejected(store: str | None = None) -> str:
    """Bridge refused the login with the saved username and the stored password."""
    return (
        f"{REJECTED} {WHERE} If Bridge is not signed in to this mailbox, sign in there first. "
        f"{_run(REPLACE, store)} If setup was given a username other than {USERNAME}, also add "
        "`--bridge-user` with that one."
    )


def refusal(entry: str) -> str | None:
    """Why setup keeps no such Bridge password, or None. It names neither the entry nor its
    length, so a mistyped password stays as hidden as one typed right."""
    if not entry:
        return "Nothing was entered, so nothing was kept."
    if len(entry) < MIN_LENGTH:
        return "That is too short to be a Bridge password, so it was not kept."
    return None


def not_running(host: str, port: int) -> str:
    """Nothing accepts a connection on Bridge's IMAP address."""
    return (
        f"{NOT_RUNNING} {address(host, port)}. Start Bridge (its app, its service, or "
        "`bridge --noninteractive`), then retry"
    )


def tls_failed(host: str, port: int) -> str:
    """Something answers on Bridge's IMAP address, but STARTTLS did not complete there."""
    return (
        f"{TLS_FAILED} {address(host, port)}. Bridge's IMAP connection mode must be STARTTLS, "
        "not SSL: in the Proton Mail Bridge app's settings, under Connection mode, choose "
        "STARTTLS for IMAP, then retry"
    )


def logged_missing(store: str | None, *, empty: bool = False) -> str:
    """The server's log line when the store gives no usable Bridge password."""
    where = store or "the credential store"
    if empty:
        return f"Bridge password missing: the entry in {where} is empty"
    return f"Bridge password missing: none could be read from {where}"


def logged_not_running(host: str, port: int) -> str:
    """The server's log line, and the self-check's message, for a Bridge that is not running."""
    return f"Bridge not running: nothing answers on {address(host, port)}"


def logged_tls_failed(host: str, port: int) -> str:
    return f"{TLS_FAILED} {address(host, port)}: Bridge's IMAP connection mode must be STARTTLS"
