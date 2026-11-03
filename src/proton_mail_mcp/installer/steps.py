"""Plan steps: plain data that a dry run prints and the executor applies."""

import re
from dataclasses import dataclass

# The distribution's name on an index, which is not the command's.
PACKAGE = "mcp-proton-mail"
MARKER = "Managed by proton-mail-mcp installer"
# Created inside directories the installer owns. The installer never deletes them; it
# prints how to remove one by hand only when this marker is there.
DIR_MARKER = ".proton-mail-mcp-installer"


def powershell_literal(text: str) -> str:
    """Text as a PowerShell string nothing in it can end or expand; it also reads curly quotes."""
    return "'" + re.sub("(['\u2018\u2019\u201a\u201b])", r"\1\1", text) + "'"


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    summary: str
    sudo: bool = False
    # Inherit the terminal: package managers, sudo prompts and native secret prompts.
    interactive: bool = False
    # Tags marked as changed after success, and tags of which one must have changed.
    marks: tuple[str, ...] = ()
    when_changed: tuple[str, ...] = ()
    # Text the captured output must contain, for verification commands.
    expect: str | None = None
    allow_failure: bool = False
    retries: int = 0
    env: tuple[tuple[str, str], ...] = ()
    # Added to the error when it fails: what a person can do about it.
    hint: str = ""


@dataclass(frozen=True)
class CheckSameCopy:
    """Prove that what pip installed by name is the copy running this installer, before
    anything from it runs. Plain pip and uvx keep no record of the index they used, so
    only the installed files can show it. A failure stops the run."""

    python: str
    venv: str
    summary: str
    hint: str = ""


@dataclass(frozen=True)
class WriteFile:
    path: str
    content: str
    summary: str
    mode: int = 0o600
    tag: str | None = None
    encoding: str = "utf-8"


@dataclass(frozen=True)
class EnsureDir:
    path: str
    summary: str
    mode: int = 0o700


@dataclass(frozen=True)
class RemovePath:
    """Remove one file, or one empty directory; nothing with content is ever removed.

    A file goes only when it is a regular file that carries MARKER, unless
    managed_only is False (the package this run downloaded). A directory goes only
    when it is a real, empty directory (rmdir); one with anything in it, or a
    symbolic link, stays.
    """

    path: str
    summary: str
    managed_only: bool = True
    directory: bool = False


@dataclass(frozen=True)
class CheckStopped:
    """Prove a service is stopped before uninstall removes anything; read-only.

    Proven only when the query exits with status and, with states, its trimmed stdout
    is one of them: systemd's ActiveState (status 0), launchctl's "service not found"
    (113) or the task query's "not running" (3). Any other answer, or a query that
    cannot run, proves nothing. The query's output is never shown.
    """

    argv: tuple[str, ...]
    summary: str
    hint: str
    states: tuple[str, ...] = ()
    status: int = 0


@dataclass(frozen=True)
class Download:
    url: str
    dest: str
    summary: str


@dataclass(frozen=True)
class CheckKey:
    path: str
    # Full primary-key fingerprint; a subkey or an extra primary key never matches.
    fingerprint: str
    summary: str


@dataclass(frozen=True)
class CheckRpmSignature:
    """Verify a package against a temporary rpm database that holds only `key`."""

    package: str
    key: str
    summary: str


@dataclass(frozen=True)
class CheckDebSignature:
    """Verify a package with debsig-verify against temporary policy and keyring
    directories that hold only `policy` and the pinned `key`."""

    package: str
    key: str
    policy: str
    key_id: str
    fingerprint: str
    summary: str


@dataclass(frozen=True)
class CheckPolicy:
    path: str
    key_id: str
    summary: str


@dataclass(frozen=True)
class StoreSecret:
    store: str
    service: str
    account: str
    # "prompt" or, with setup --password-stdin, "stdin" for the Bridge password; "generate"
    # for the MCP token.
    source: str
    summary: str
    tag: str = "credentials"
    # PASSWORD_STORE_DIR and GNUPGHOME for pass, the values the service uses.
    locations: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class DeleteSecret:
    store: str
    service: str
    account: str
    summary: str
    locations: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class WaitForPort:
    port: int
    summary: str
    timeout: float = 20.0
    hint: str = ""


@dataclass(frozen=True)
class BridgeService:
    """The installer's own Bridge service, the only Bridge it may stop for a login."""

    stopped: CheckStopped
    stop: tuple[str, ...]
    start: tuple[str, ...]


@dataclass(frozen=True)
class BridgeLogin:
    """Offer Bridge's own CLI on this terminal, where Bridge itself takes the Proton
    password. Asked first, and only on a terminal: the CLI hangs without one, and it
    cannot run beside a Bridge that already runs. So the installer's own service stops
    first and starts again afterwards, and any other Bridge means no CLI. Declining or
    failing only warns."""

    argv: tuple[str, ...]
    summary: str
    imap_port: int = 1143
    service: BridgeService | None = None
    # Exits 0 while a Bridge --noninteractive process runs and 1 when none does; any
    # other answer proves nothing.
    processes: tuple[str, ...] = ()
    # The IMAP username setup was given; Bridge's `info` must show the same one.
    account: str = ""


@dataclass(frozen=True)
class SelfCheck:
    """Prove the running MCP service answers and reads mail through Bridge, with the
    stored token. A failure stops the run and names the cause; the token is never shown."""

    port: int
    store: str
    service: str
    account: str
    summary: str
    hint: str = ""
    locations: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ConfigureClient:
    """Add or update this server's entry in an MCP client's config.

    Most clients get `command`, the stdio connect command, which reads the token itself; for a
    client that takes HTTP directly the token is read from the credential store only when
    applied, and goes only into that file: never into the plan, the output or an error. With
    `native`, the client's own command line adds the entry; otherwise the installer merges it.
    Every other key and server in the file stays; a changed file is first kept under a new,
    dated .bak name, and an unchanged one is not touched."""

    client: str
    path: str
    port: int
    summary: str
    command: tuple[str, ...] = ()
    # The client's own program, by absolute path, when it is on PATH.
    native: str = ""
    store: str = ""
    service: str = ""
    account: str = ""
    locations: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class StartBackground:
    """Start the MCP service as a background process where no service manager runs it, such as
    a container without systemd. The process id goes to `pidfile`; a rerun stops that process,
    by that id only and only while it still runs the service, when something it reads changed."""

    argv: tuple[str, ...]
    log: str
    pidfile: str
    port: int
    summary: str
    # Tags that, changed by this run, mean a running copy is out of date.
    restart_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class NeedsUser:
    """Something only a person can do, said where it happens in the plan.

    A run that cannot ask a person stops before any change for one that is blocking, or
    that needs the terminal setup runs in. `when` tells an agent when to hand it over:
    before the run, during it, or after it."""

    id: str
    text: str
    when: str = "during"
    blocking: bool = False
    terminal: bool = False
    command: str = ""


@dataclass(frozen=True)
class Note:
    text: str
    # A file the applied note is also appended to, such as the note on a directory
    # that stays and how to remove it by hand; a dry run only prints it.
    log: str = ""


Step = (
    Command
    | CheckSameCopy
    | WriteFile
    | EnsureDir
    | RemovePath
    | CheckStopped
    | Download
    | CheckKey
    | CheckRpmSignature
    | CheckDebSignature
    | CheckPolicy
    | StoreSecret
    | DeleteSecret
    | WaitForPort
    | BridgeLogin
    | SelfCheck
    | ConfigureClient
    | StartBackground
    | NeedsUser
    | Note
)
