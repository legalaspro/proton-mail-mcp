"""Turn a detected platform, options and read-only probes into installer steps."""

import json
import ntpath
import posixpath
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import Protocol

from .. import credential_store
from ..config import default_attachments_dir
from ..credential_store import BRIDGE_SERVICE, TOKEN_SERVICE
from ..service_entry import parse_env_file
from . import templates
from .detect import FAMILIES, Platform
from .steps import (
    DIR_MARKER,
    MARKER,
    PACKAGE,
    BridgeLogin,
    BridgeService,
    CheckDebSignature,
    CheckKey,
    CheckPolicy,
    CheckRpmSignature,
    CheckSameCopy,
    CheckStopped,
    Command,
    DeleteSecret,
    Download,
    EnsureDir,
    Note,
    RemovePath,
    SelfCheck,
    Step,
    StoreSecret,
    WaitForPort,
    WriteFile,
)

# Proton's Linux packages and signing key, from
# https://proton.me/support/verifying-bridge-package (checked 28 Oct 2026). Proton
# publishes the long key ID E2C75D68E6234B07. The full fingerprint ends in it and is
# the primary key of both the copy on proton.me and the one on keyserver.ubuntu.com
# (both checked 28 Oct 2026); the downloaded file must hold that primary key alone.
BRIDGE_VERSION = "3.27.1"
BRIDGE_DOWNLOAD = "https://proton.me/download/bridge"
BRIDGE_KEY_URL = f"{BRIDGE_DOWNLOAD}/bridge_pubkey.gpg"
BRIDGE_POLICY_URL = f"{BRIDGE_DOWNLOAD}/bridge.pol"
BRIDGE_KEY_FINGERPRINT = "D51E64D3E63EDC3EEF7864CEE2C75D68E6234B07"
BRIDGE_KEY_ID = BRIDGE_KEY_FINGERPRINT[-16:]
BRIDGE_VERIFY_DOC = "https://proton.me/support/verifying-bridge-package"
BRIDGE_DEB_PACKAGE = "protonmail-bridge"
BRIDGE_RPM_PACKAGE = "protonmail-bridge"
# Arch builds Bridge from source in [extra]; pacman checks the repository signature.
BRIDGE_ARCH_PACKAGE = "protonmail-bridge-core"
BRIDGE_CASK = "proton-mail-bridge"
BRIDGE_WINGET_ID = "Proton.ProtonMailBridge"
MACOS_BRIDGE_APP = "/Applications/Proton Mail Bridge.app"
MACOS_BRIDGE_EXE = f"{MACOS_BRIDGE_APP}/Contents/MacOS/Proton Mail Bridge"
WINDOWS_BRIDGE_DIR = ("Proton AG", "Proton Mail Bridge", "proton-bridge.exe")
LINUX_BRIDGE_CANDIDATES = {
    "debian": ("/usr/bin/protonmail-bridge",),
    "fedora": ("/usr/bin/protonmail-bridge",),
    "arch": ("/usr/bin/protonmail-bridge-core", "/usr/bin/protonmail-bridge"),
}
DEFAULT_STORES = {"macos": "keychain", "windows": "wincred"}
LINUX_STORES = ("libsecret", "pass")
# Authenticode must be valid and issued to Proton; the path arrives via the environment.
AUTHENTICODE_CHECK = (
    "$s = Get-AuthenticodeSignature -LiteralPath $env:PMM_VERIFY_PATH; "
    "if ($s.Status -ne 'Valid' -or "
    "$s.SignerCertificate.Subject -notmatch 'O=\\W?Proton( Technologies)? AG') { exit 1 }"
)
_VERSION = re.compile(r"\d+\.\d+\.\d+")
_ACCOUNT_HINT = "the Bridge IMAP username shown by Bridge's `info` command"
_SECRET_TOOL_PACKAGES = {
    "debian": "sudo apt-get install libsecret-tools",
    "fedora": "sudo dnf install libsecret",
    "arch": "sudo pacman -S libsecret",
}
_PASS_PACKAGES = {
    "debian": "sudo apt-get install pass",
    "fedora": "sudo dnf install pass",
    "arch": "sudo pacman -S pass",
}
_ENSUREPIP_HINT = "Install a complete Python 3.12 or newer"
_ENSUREPIP_HINTS = {
    "debian": "Install it with `sudo apt-get install python3-venv` (python3.X-venv for "
    "another version)",
}
# A --venv path, saved so reruns and uninstall find the same environment.
VENV_KEY = "PMM_INSTALLER_VENV"
# Earlier environments the installer created and left in place (a JSON list). It never
# deletes them; every run names each one with its size and how to remove it by hand.
PREVIOUS_VENVS_KEY = "PMM_INSTALLER_PREVIOUS_VENVS"
# Services do not inherit the installing shell, so the state locations it resolves
# are written to the env file; the server would otherwise fall back to its defaults.
STATE_FILES = (
    ("PROTON_MCP_ACTION_LOG", "actions.jsonl"),
    ("PROTON_MCP_IDENTITY_INDEX_DIR", "identity-index"),
)
ATTACHMENTS_KEY = "PROTON_MCP_ATTACHMENTS_DIR"
# Settings that name a file or directory holding user data. A directory that holds
# one gets no removal command, and uninstall names where each one points.
DATA_SETTINGS = (
    *(key for key, _name in STATE_FILES),
    "PROTON_MCP_SAVED_RULES",
    ATTACHMENTS_KEY,
    "PROTON_MCP_PROTECTED_CONFIG",
    "PROTON_BRIDGE_CERT",
    *(key for key, _name in credential_store.LOCATION_SETTINGS),
)
# Notes on directories that stay, with how to remove them by hand, are also appended
# here, in the state directory.
KEPT_LOG = "installer.log"
# systemd creates this directory when it boots the host (sd_booted(3)); without it no
# unit runs. Container engines leave these markers.
SYSTEMD_BOOTED = "/run/systemd/system"
CONTAINER_MARKERS = (("/.dockerenv", "docker"), ("/run/.containerenv", "podman"))
# Answers only when systemctl reaches the user manager; its output is never used.
MANAGER_ENVIRONMENT = ("systemctl", "--user", "show-environment")
# The ActiveState values that prove a unit stopped; anything else (active, activating,
# deactivating, reloading, ...) does not.
UNIT_STOPPED_STATES = ("inactive", "failed")
# `launchctl print` exits with launchd's error code: 113 is "Could not find specified
# service" (`launchctl error 113`). Any other failure proves nothing.
LAUNCHD_NOT_FOUND = 113
# The task query's exit status for a task that exists and is not running.
TASK_NOT_RUNNING = 3
# pgrep -f for a running Bridge, on its whole command line: a program with Bridge in its
# path, up to its first option, and --noninteractive after that. So a command that only
# mentions both, such as an editor or a shell's -c, does not count. Paths may hold spaces
# (macOS's Proton Mail Bridge.app), and a space before a dash ends the program's path.
BRIDGE_PROCESSES = r"^([^ ]| [^-])* ?[Bb]ridge([^ ]| [^-])* (.* )?--noninteractive( |$)"


class PlanError(ValueError):
    """The requested plan cannot run on this host; nothing has been changed."""


class CredentialsUnknown(PlanError):
    """The store cannot show whether a credential is there; setup may ask a person."""


class Probe(Protocol):
    """Read-only facts about the host; never changes anything.

    The planner reads only the installer's own files and names: it never lists a
    directory or reads a service definition it did not write.
    """

    env: dict[str, str]
    home: str
    user_id: str

    def which(self, name: str) -> str | None: ...

    def exists(self, path: str) -> bool: ...

    def realpath(self, path: str) -> str: ...

    def read_text(self, path: str) -> str | None: ...

    def readlink(self, path: str) -> str | None: ...

    def tree_size(self, path: str) -> int | None: ...

    def succeeds(self, argv: tuple[str, ...]) -> bool: ...

    def output(self, argv: tuple[str, ...]) -> str | None: ...

    def can_make_venv(self, python: str) -> bool: ...

    def has_secret(
        self, store: str, service: str, account: str, locations: Mapping[str, str] | None = None
    ) -> bool | None: ...


@dataclass(frozen=True)
class Options:
    bridge_user: str | None = None
    credential_store: str | None = None
    port: int = 8765
    imap_port: int = 1143
    bridge_version: str = BRIDGE_VERSION
    bridge_bin: str | None = None
    venv: str | None = None
    source: str = ""
    # pip's options for where a pinned source comes from, as its installer recorded them.
    source_args: tuple[str, ...] = ()
    python: str = "python3"
    skip_bridge: bool = False
    skip_credentials: bool = False
    skip_services: bool = False
    skip_bridge_service: bool = False
    replace_credentials: bool = False
    remove_credentials: bool = False
    remove_bridge: bool = False
    # Guided setup: offer Bridge's CLI, then prove the result with a self-check.
    setup: bool = False
    skip_bridge_login: bool = False
    # A person answered that a credential the store cannot show may be stored new.
    store_unreadable: bool = False
    # Written to the env file, and created: the server only creates its own default.
    attachments_dir: str | None = None


@dataclass(frozen=True)
class Paths:
    home: str
    config_dir: str
    env_file: str
    data_dir: str
    venv: str
    venv_python: str
    service_exe: str
    cache_dir: str
    state_dir: str
    log_dir: str | None = None
    unit_dir: str | None = None
    agents_dir: str | None = None
    task_dir: str | None = None


def _path_type(platform: Platform) -> type[PurePath]:
    return PureWindowsPath if platform.family == "windows" else PurePosixPath


def _xdg(env: dict[str, str], name: str, fallback: PurePath) -> PurePath:
    # The XDG specification ignores relative values.
    value = env.get(name, "")
    return PurePosixPath(value) if value.startswith("/") else fallback


def _venv_paths(platform: Platform, venv: PurePath) -> dict[str, str]:
    if platform.family == "windows":
        return {
            "venv": str(venv),
            "venv_python": str(venv / "Scripts" / "python.exe"),
            # pythonw has no console window; the service logs to log_dir instead.
            "service_exe": str(venv / "Scripts" / "pythonw.exe"),
        }
    return {
        "venv": str(venv),
        "venv_python": str(venv / "bin" / "python"),
        "service_exe": str(venv / "bin" / "proton-mail-mcp-service"),
    }


def paths_for(platform: Platform, env: dict[str, str], home: str) -> Paths:
    kind = _path_type(platform)
    root = kind(home)
    if platform.family == "windows":
        local = env.get("LOCALAPPDATA") or str(root / "AppData" / "Local")
        base = kind(local) / "proton-mail-mcp"
        return Paths(
            home=str(root),
            config_dir=str(base),
            env_file=str(base / "proton-mail-mcp.env"),
            data_dir=str(base),
            **_venv_paths(platform, base / "venv"),
            cache_dir=str(base / "cache"),
            state_dir=str(root / ".local" / "state" / "proton-mail-mcp"),
            log_dir=str(base / "logs"),
            task_dir=str(base / "tasks"),
        )
    state = _xdg(env, "XDG_STATE_HOME", root / ".local" / "state") / "proton-mail-mcp"
    if platform.family == "macos":
        base = root / "Library" / "Application Support" / "proton-mail-mcp"
        return Paths(
            home=str(root),
            config_dir=str(base),
            env_file=str(base / "proton-mail-mcp.env"),
            data_dir=str(base),
            **_venv_paths(platform, base / "venv"),
            cache_dir=str(root / "Library" / "Caches" / "proton-mail-mcp" / "installer"),
            state_dir=str(state),
            log_dir=str(root / "Library" / "Logs" / "proton-mail-mcp"),
            agents_dir=str(root / "Library" / "LaunchAgents"),
        )
    config = _xdg(env, "XDG_CONFIG_HOME", root / ".config")
    data = _xdg(env, "XDG_DATA_HOME", root / ".local" / "share") / "proton-mail-mcp"
    return Paths(
        home=str(root),
        config_dir=str(config / "proton-mail-mcp"),
        env_file=str(config / "proton-mail-mcp" / "proton-mail-mcp.env"),
        data_dir=str(data),
        **_venv_paths(platform, data / "venv"),
        cache_dir=str(
            _xdg(env, "XDG_CACHE_HOME", root / ".cache") / "proton-mail-mcp" / "installer"
        ),
        state_dir=str(state),
        unit_dir=str(config / "systemd" / "user"),
    )


def with_venv(platform: Platform, paths: Paths, venv: str | None) -> Paths:
    """Relocate the virtual environment to --venv or the path an earlier run saved."""
    if not venv:
        return paths
    root = _path_type(platform)(venv)
    if not root.is_absolute():
        raise PlanError("--venv must be an absolute path")
    return replace(paths, **_venv_paths(platform, root))


def _join(platform: Platform, *parts: str) -> str:
    return str(_path_type(platform)(*parts))


def _owner(probe: Probe, path: str) -> str | None:
    """None when nothing is at path, "installer" for a file with MARKER, else "other"."""
    if not probe.exists(path):
        return None
    text = probe.read_text(path)
    return "installer" if text is not None and MARKER in text else "other"


def _claim_dir(probe: Probe, path: str, marker: str, alternative: str) -> None:
    """Only a missing or already marked directory may be marked as installer-owned."""
    if probe.exists(path) and not probe.exists(marker):
        raise PlanError(
            f"{path} exists but was not created by this installer, which neither reuses "
            f"nor removes it; remove it yourself, or {alternative}"
        )


def bridge_executable(platform: Platform, options: Options, probe: Probe) -> str:
    """The installed Bridge binary, or where the official package will put it."""
    if options.bridge_bin:
        return options.bridge_bin
    if platform.family == "macos":
        return MACOS_BRIDGE_EXE
    if platform.family == "windows":
        program_files = probe.env.get("ProgramFiles") or "C:\\Program Files"
        return _join(platform, program_files, *WINDOWS_BRIDGE_DIR)
    candidates = LINUX_BRIDGE_CANDIDATES[platform.family]
    for candidate in candidates:
        if probe.exists(candidate):
            return candidate
    return candidates[0]


def existing_settings(paths: Paths, probe: Probe) -> tuple[str | None, dict[str, str]]:
    """Read the installer's env file, refusing one that holds a secret, and one the
    installer did not write: nothing in that is used or shown but its keys."""
    text = probe.read_text(paths.env_file)
    if text is None:
        return None, {}
    try:
        settings = parse_env_file(text)
    except ValueError as error:
        raise PlanError(f"{paths.env_file}: {error}") from None
    if MARKER not in text:
        raise PlanError(
            f"{paths.env_file} was not written by the installer, which neither uses nor shows "
            f"what it sets ({', '.join(settings) or 'nothing'}). Nothing was changed. Move it "
            "elsewhere, then rerun"
        )
    return text, settings


def validate_options(options: Options, platform: Platform | None = None) -> None:
    for name, port in (("--port", options.port), ("--bridge-imap-port", options.imap_port)):
        if not 1 <= port <= 65535:
            raise PlanError(f"{name} must be between 1 and 65535")
    if not _VERSION.fullmatch(options.bridge_version):
        raise PlanError("--bridge-version must look like 3.27.1")
    if options.credential_store and options.credential_store not in credential_store.STORES:
        raise PlanError("--credential-store must be keychain, libsecret, pass or wincred")
    for value in (options.bridge_bin, options.venv, options.source, options.python):
        if value and any(char in value for char in "\r\n\0"):
            raise PlanError("Paths must not contain line breaks")
    # Service managers start programs from their own working directory: systemd
    # rejects a relative ExecStart, and launchd and Task Scheduler would miss it.
    if (
        platform is not None
        and options.bridge_bin
        and not _path_type(platform)(options.bridge_bin).is_absolute()
    ):
        raise PlanError(f"--bridge-bin must be an absolute path, not {options.bridge_bin}")


def bridge_account(options: Options, settings: dict[str, str]) -> str:
    account = options.bridge_user or settings.get("PROTON_BRIDGE_USER")
    if not account:
        raise PlanError(f"Pass --bridge-user: {_ACCOUNT_HINT}")
    try:
        return credential_store.validate_account(account)
    except credential_store.CredentialStoreError as error:
        raise PlanError(str(error)) from None


def choose_store(
    platform: Platform, options: Options, probe: Probe, settings: dict[str, str]
) -> str:
    store = options.credential_store or settings.get("PROTON_MCP_CREDENTIAL_STORE")
    if platform.family in DEFAULT_STORES:
        default = DEFAULT_STORES[platform.family]
        if store and store != default:
            raise PlanError(f"On {platform.family} the credential store must be {default}")
        return default
    if store and store not in LINUX_STORES:
        raise PlanError("On Linux the credential store must be libsecret or pass")
    if store:
        return store
    if not probe.which("secret-tool") and _pass_ready(probe, settings):
        return "pass"
    return "libsecret"


def pass_settings(
    probe: Probe, settings: dict[str, str], *, prefer_saved: bool = False
) -> dict[str, str]:
    """Env file values naming the pass store and a custom GnuPG home.

    Installing, a variable exported in the shell wins over the saved value, as for
    the state locations; uninstall and show-token prefer the saved value, which the
    service uses. The store defaults to ~/.password-store; gpg's own default home
    needs no setting.
    """
    values: dict[str, str] = {}
    for key, name in credential_store.LOCATION_SETTINGS:
        explicit, saved = probe.env.get(name, "").strip(), settings.get(key, "")
        value = (saved or explicit) if prefer_saved else (explicit or saved)
        if name == "PASSWORD_STORE_DIR":
            value = value or f"{probe.home}/.password-store"
        if value:
            values[key] = value
    return values


def credential_locations(
    store: str, probe: Probe, settings: dict[str, str], *, prefer_saved: bool = False
) -> dict[str, str]:
    """PASSWORD_STORE_DIR and GNUPGHOME for the pass calls the service also makes."""
    if store != "pass":
        return {}
    return credential_store.pass_locations(
        pass_settings(probe, settings, prefer_saved=prefer_saved)
    )


def _pass_ready(probe: Probe, settings: dict[str, str]) -> bool:
    store_dir = pass_settings(probe, settings)["PROTON_MCP_PASSWORD_STORE_DIR"]
    return bool(probe.which("pass")) and probe.exists(f"{store_dir}/.gpg-id")


def _pass_steps(probe: Probe, settings: dict[str, str]) -> tuple[dict[str, str], list[Step]]:
    """The pass locations to write to the env file, and notes about changed ones."""
    values = pass_settings(probe, settings)
    notes: list[Step] = []
    for key, name in credential_store.LOCATION_SETTINGS:
        value, saved = values.get(key), settings.get(key)
        if value is None:
            continue
        if not value.startswith("/"):
            raise PlanError(f"{name} ({key}) must be an absolute path")
        if saved and saved != value:
            notes.append(
                Note(
                    f"{key} changes from {saved} to {value}; pass entries and keys at the "
                    "old location are not moved."
                )
            )
    return values, notes


def _check_store_tools(
    platform: Platform, store: str, probe: Probe, settings: dict[str, str]
) -> None:
    if store == "libsecret" and not probe.which("secret-tool"):
        raise PlanError(
            "secret-tool (libsecret) is missing: install it with "
            f"`{_SECRET_TOOL_PACKAGES[platform.family]}`, or use --credential-store pass"
        )
    if store == "pass" and not _pass_ready(probe, settings):
        raise PlanError(
            f"pass is missing or uninitialised: `{_PASS_PACKAGES[platform.family]}`, then "
            "`pass init <your-gpg-key-id>`"
        )


def _linux_bridge_steps(platform: Platform, options: Options, probe: Probe, paths: Paths):
    if platform.family == "arch":
        return [
            Command(
                ("pacman", "-S", "--needed", "--noconfirm", BRIDGE_ARCH_PACKAGE),
                f"Install {BRIDGE_ARCH_PACKAGE} from Arch [extra] (pacman verifies its signature)",
                sudo=True,
                interactive=True,
            )
        ]
    if platform.machine != "x86_64":
        raise PlanError(
            "Proton publishes Linux Bridge packages for x86_64 only; install Bridge yourself "
            "and rerun with --skip-bridge"
        )
    version = options.bridge_version
    cache = paths.cache_dir
    key = f"{cache}/bridge_pubkey.gpg"
    steps: list[Step] = []
    if platform.family == "debian":
        package = f"protonmail-bridge_{version}-1_amd64.deb"
        steps.append(
            Command(("apt-get", "update"), "Refresh apt package lists", sudo=True, interactive=True)
        )
        if not probe.which("debsig-verify") or not probe.which("gpg"):
            steps.append(
                Command(
                    ("apt-get", "install", "-y", "debsig-verify", "gnupg"),
                    "Install debsig-verify and GnuPG for Proton's signature check",
                    sudo=True,
                    interactive=True,
                )
            )
    else:
        package = f"protonmail-bridge-{version}-1.x86_64.rpm"
        if not probe.which("gpg"):
            steps.append(
                Command(
                    ("dnf", "install", "-y", "gnupg2"),
                    "Install GnuPG to check Proton's signing key",
                    sudo=True,
                    interactive=True,
                )
            )
    local = f"{cache}/{package}"
    _claim_dir(probe, cache, f"{cache}/{DIR_MARKER}", "set XDG_CACHE_HOME to download elsewhere")
    steps += [
        EnsureDir(cache, "Create the private download directory"),
        WriteFile(
            f"{cache}/{DIR_MARKER}",
            f"{MARKER}; the installer created it and never deletes it.\n",
            "Mark the download directory as installer-owned",
        ),
        Download(BRIDGE_KEY_URL, key, "Download Proton's Bridge signing key"),
        Download(f"{BRIDGE_DOWNLOAD}/{package}", local, f"Download Bridge {version}"),
        CheckKey(
            key,
            BRIDGE_KEY_FINGERPRINT,
            f"Check the file holds only Proton's primary key {BRIDGE_KEY_FINGERPRINT}",
        ),
    ]
    if platform.family == "debian":
        policy = f"{cache}/bridge.pol"
        steps += [
            Download(BRIDGE_POLICY_URL, policy, "Download Proton's debsig policy"),
            CheckPolicy(policy, BRIDGE_KEY_ID, "Check the policy names only the pinned key"),
            # The system debsig directories are not used: they trust every configured origin.
            CheckDebSignature(
                local,
                key,
                policy,
                BRIDGE_KEY_ID,
                BRIDGE_KEY_FINGERPRINT,
                "Verify the package signature against Proton's policy and key alone "
                "(temporary debsig directories)",
            ),
            Command(
                ("apt-get", "install", "-y", local),
                f"Install Bridge {version} with apt",
                sudo=True,
                interactive=True,
            ),
        ]
    else:
        steps += [
            # The system rpm database is not used: it trusts every repository's key.
            CheckRpmSignature(
                local,
                key,
                "Verify the package signature against Proton's key alone (temporary rpm database)",
            ),
            Command(
                ("dnf", "install", "-y", local),
                f"Install Bridge {version} with dnf",
                sudo=True,
                interactive=True,
            ),
        ]
    steps.append(RemovePath(local, "Remove the downloaded package", managed_only=False))
    return steps


def _macos_verify_steps() -> list[Step]:
    return [
        Command(
            ("codesign", "--verify", "--deep", "--strict", MACOS_BRIDGE_APP),
            "Verify the Bridge app's code signature",
        ),
        Command(
            ("spctl", "--assess", "--type", "execute", MACOS_BRIDGE_APP),
            "Check Gatekeeper accepts the notarized app",
        ),
        Command(
            ("codesign", "-dv", "--verbose=2", MACOS_BRIDGE_APP),
            "Check the app is signed with Proton's Developer ID",
            expect="Authority=Developer ID Application: Proton",
        ),
    ]


def bridge_steps(platform: Platform, options: Options, probe: Probe, paths: Paths) -> list[Step]:
    if options.skip_bridge:
        return [Note("Skipping Bridge installation (--skip-bridge).")]
    exe = bridge_executable(platform, options, probe)
    installed = probe.exists(exe)
    steps: list[Step] = []
    if installed:
        steps.append(Note(f"Bridge is already installed at {exe}; not reinstalling."))
    if platform.family == "macos":
        if not installed:
            brew = probe.which("brew") or next(
                (p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if probe.exists(p)),
                None,
            )
            if not brew:
                raise PlanError(
                    "Homebrew is required to install Bridge (https://brew.sh); or install "
                    "Bridge from proton.me and rerun with --skip-bridge"
                )
            steps.append(
                Command(
                    (brew, "install", "--cask", BRIDGE_CASK),
                    "Install Bridge with Homebrew (the cask pins the download's SHA-256)",
                    interactive=True,
                )
            )
        if options.bridge_bin is None:
            steps += _macos_verify_steps()
        return steps
    if platform.family == "windows":
        if not installed:
            if not probe.which("winget"):
                raise PlanError(
                    "winget (App Installer) is required to install Bridge; or install Bridge "
                    "from proton.me and rerun with -SkipBridge"
                )
            steps.append(
                Command(
                    (
                        "winget",
                        "install",
                        "--id",
                        BRIDGE_WINGET_ID,
                        "--exact",
                        "--source",
                        "winget",
                        "--accept-package-agreements",
                        "--accept-source-agreements",
                        "--silent",
                        "--disable-interactivity",
                    ),
                    "Install Bridge with winget (the manifest pins the installer's SHA-256)",
                    interactive=True,
                )
            )
        steps.append(
            Command(
                ("powershell", "-NoProfile", "-NonInteractive", "-Command", AUTHENTICODE_CHECK),
                "Verify Bridge's Authenticode signature is valid and issued to Proton AG",
                env=(("PMM_VERIFY_PATH", exe),),
            )
        )
        return steps
    if not installed:
        steps += _linux_bridge_steps(platform, options, probe, paths)
    return steps


def _mcp_steps(platform: Platform, options: Options, probe: Probe, paths: Paths) -> list[Step]:
    marker = _join(platform, paths.venv, DIR_MARKER)
    _claim_dir(probe, paths.venv, marker, "pass --venv with a path that does not exist yet")
    # The marker goes in before the environment, so an interrupted run still owns it.
    steps: list[Step] = [
        EnsureDir(paths.data_dir, "Create the private data directory"),
        EnsureDir(paths.venv, "Create the virtual environment directory"),
        WriteFile(
            marker,
            f"{MARKER}; the installer created it and never deletes it.\n",
            "Mark the virtual environment as installer-owned",
        ),
    ]
    if not probe.exists(paths.venv_python):
        if not probe.can_make_venv(options.python):
            # venv would leave a Python without pip behind, which a rerun would then reuse.
            raise PlanError(
                f"{options.python} cannot create a virtual environment: its ensurepip module "
                f"is missing. {_ENSUREPIP_HINTS.get(platform.family, _ENSUREPIP_HINT)}, then "
                "rerun; nothing was changed"
            )
        steps.append(
            Command(
                (options.python, "-m", "venv", paths.venv),
                "Create the MCP virtual environment",
            )
        )
    # pip keeps an installed version when given a wheel of that same version, which a
    # rebuilt wheel is. A checkout is always rebuilt and an index name is pinned.
    rebuilt = ("--force-reinstall",) if options.source.endswith(".whl") else ()
    pinned = is_pinned(options.source)
    # Wheels only: building an sdist would run its code before the check below.
    wheels_only = (f"--only-binary={PACKAGE}",) if pinned else ()
    hint = _pinned_source_hint(options.source) if pinned else ""
    steps += [
        Command(
            (
                paths.venv_python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                *rebuilt,
                *wheels_only,
                *options.source_args,
                options.source,
            ),
            f"Install proton-mail-mcp from {options.source}",
            interactive=True,
            marks=("mcp-package",),
            hint=hint,
        ),
    ]
    if pinned:
        steps.append(
            CheckSameCopy(
                paths.venv_python,
                paths.venv,
                "Check the virtual environment holds the copy running this installer",
                hint=hint,
            )
        )
    return steps


def is_pinned(source: str) -> bool:
    """A version for pip to find on an index, not a checkout or a wheel file."""
    return source.startswith(f"{PACKAGE}==")


def _pinned_source_hint(source: str) -> str:
    return (
        "If this copy came from another index or a wheel directory, pass --source with a "
        f"checkout or a wheel file; `pip download --no-deps {source}`, with that index, "
        "fetches the wheel"
    )


def _credential_steps(
    store: str, account: str, options: Options, probe: Probe, locations: dict[str, str]
) -> list[Step]:
    if options.skip_credentials:
        return [Note("Skipping credential storage (--skip-credentials).")]
    steps: list[Step] = []
    for service, source, label in (
        (BRIDGE_SERVICE, "prompt", "Bridge password"),
        (TOKEN_SERVICE, "generate", "MCP bearer token"),
    ):
        found = not options.replace_credentials and probe.has_secret(
            store, service, account, locations
        )
        # A failed check is no proof of absence, nor is secret-tool's "nothing found",
        # which a locked entry gives too: storing could replace what is there.
        if found is None and not options.store_unreadable:
            other = ", or use --credential-store pass" if store == "libsecret" else ""
            raise CredentialsUnknown(
                f"Cannot tell whether the {label} is already in {store}: the check failed, "
                "timed out, or found nothing it could read, which is also what a locked "
                "entry shows. Nothing was changed, and what is stored stays. Make sure "
                f"{store} is unlocked and answers, then rerun{other}; or rerun with "
                "--skip-credentials, which leaves credentials alone. If none is stored yet, "
                "rerun with --replace-credentials, which stores a new Bridge password and "
                "token over any already there"
            )
        if not found:
            verb = "Prompt for" if source == "prompt" else "Generate"
            steps.append(
                StoreSecret(
                    store,
                    service,
                    account,
                    source,
                    f"{verb} the {label} and save it in {store}",
                    locations=tuple(locations.items()),
                )
            )
        else:
            steps.append(
                Note(
                    f"The {label} is already in {store}; keeping it "
                    "(--replace-credentials replaces it)."
                )
            )
    return steps


def _config_steps(
    store: str,
    account: str,
    options: Options,
    paths: Paths,
    existing: str | None,
    extra: dict[str, str],
) -> list[Step]:
    owned = {
        "PROTON_BRIDGE_USER": account,
        "PROTON_MCP_CREDENTIAL_STORE": store,
        "PROTON_MCP_HOST": "127.0.0.1",
        "PROTON_MCP_PORT": str(options.port),
        "PROTON_BRIDGE_HOST": "127.0.0.1",
        "PROTON_BRIDGE_IMAP_PORT": str(options.imap_port),
        **extra,
    }
    # Written once; a deliberate later change in the file is kept but reported.
    defaults = {"PROTON_MCP_CONFIRM_MUTATIONS": "1"}
    try:
        content = templates.render_env_file(existing, owned, defaults)
    except ValueError as error:
        raise PlanError(f"{paths.env_file}: {error}") from None
    steps: list[Step] = []
    if paths.config_dir != paths.data_dir:
        steps.append(EnsureDir(paths.config_dir, "Create the private config directory"))
    # First, so that a folder that cannot be created leaves the settings file as it was.
    if options.attachments_dir:
        steps.append(EnsureDir(options.attachments_dir, "Create the private attachments folder"))
    steps += [
        WriteFile(
            paths.env_file,
            content,
            "Write the non-secret service settings",
            tag="env-file",
        ),
    ]
    settings = parse_env_file(content)
    if settings.get("PROTON_MCP_CONFIRM_MUTATIONS") != "1":
        steps.append(
            Note(
                "Warning: the env file sets PROTON_MCP_CONFIRM_MUTATIONS to something other "
                "than 1, the safe default."
            )
        )
    if settings.get("PROTON_MCP_ENABLE_SEND") == "1":
        steps.append(Note("Warning: the env file enables guarded send (PROTON_MCP_ENABLE_SEND=1)."))
        if settings.get("PROTON_MCP_SEND_TYPED_OK") != "0":
            steps.append(
                Note(
                    "Warning: a client whose initialize declares no form can then send to any "
                    "recipient on a typed OK, which the server cannot verify. "
                    "PROTON_MCP_SEND_TYPED_OK=0 refuses those clients."
                )
            )
        if settings.get("PROTON_MCP_SEND_SELF_NO_FORM") == "1":
            steps.append(
                Note(
                    "Warning: with PROTON_MCP_SEND_SELF_NO_FORM=1, every address on "
                    "PROTON_MCP_SEND_FROM can receive mail with no form. List only addresses "
                    "that are yours alone."
                )
            )
    listed = [
        name.strip()
        for name in settings.get("PROTON_MCP_SEND_TYPED_OK_CLIENTS", "").split(",")
        if name.strip()
    ]
    if listed and settings.get("PROTON_MCP_SEND_TYPED_OK") == "0":
        steps.append(
            Note(
                "PROTON_MCP_SEND_TYPED_OK=0, so PROTON_MCP_SEND_TYPED_OK_CLIENTS is ignored: "
                f"{', '.join(listed)}."
            )
        )
    elif listed:
        steps.append(
            Note(
                "Warning: PROTON_MCP_SEND_TYPED_OK_CLIENTS treats these clients as having no "
                f"form: {', '.join(listed)}. Their mailbox changes that need a form refuse, and "
                "with PROTON_MCP_ENABLE_SEND=1 they can send to any recipient on a typed OK, "
                "which the server cannot verify. These names are self-reported "
                "(clientInfo.name), so any client holding the server token can claim one."
            )
        )
    if "PMM_SEND_TOKEN_APPROVAL" in settings:
        steps.append(
            Note(
                "Warning: the env file sets PMM_SEND_TOKEN_APPROVAL, which is now ignored. "
                "PROTON_MCP_SEND_SELF_NO_FORM=1 turns on the own-address path without a form."
            )
        )
    return steps


def _state_settings(
    platform: Platform, probe: Probe, paths: Paths, settings: dict[str, str]
) -> tuple[dict[str, str], dict[str, str], list[Step]]:
    """Env file values that keep the service's state where this plan reports it.

    Returns the values to write, every resolved location, and notes. A value already
    in the env file is kept; one exported in the installing shell is written as given;
    otherwise the default under the state directory is written.
    """
    owned: dict[str, str] = {}
    notes: list[Step] = []
    kind = _path_type(platform)
    for key, name in STATE_FILES:
        explicit = probe.env.get(key, "").strip()
        saved = settings.get(key, "")
        if explicit:
            if not kind(explicit).is_absolute():
                raise PlanError(f"{key} must be an absolute path")
            if saved and saved != explicit:
                notes.append(
                    Note(
                        f"{key} changes from {saved} to {explicit}; files at the old "
                        "location are not moved."
                    )
                )
            owned[key] = explicit
        elif not saved:
            owned[key] = _join(platform, paths.state_dir, name)
    resolved = {key: owned.get(key) or settings[key] for key, _name in STATE_FILES}
    return owned, resolved, notes


MCP_RESTART_TAGS = ("mcp-package", "env-file", "credentials")


def _foreign_unit(probe: Probe, unit_dir: str, unit: str) -> str | None:
    """The file systemd loads for this unit name when it is not the installer's path,
    "" when it is or none is loaded, and None when systemd does not say."""
    argv = ("systemctl", "--user", "show", "--property=FragmentPath", "--value", unit)
    loaded = probe.output(argv)
    if loaded is None:
        return None
    loaded = loaded.strip()
    return loaded if loaded and loaded != f"{unit_dir}/{unit}" else ""


def _unit_owner(probe: Probe, unit_dir: str, unit: str, ready: bool) -> str | None:
    """Ours only if systemd would act on the installer's own file for this name;
    "unknown" when systemd does not say which file that is. Without a user manager
    to ask (not ready), only the file says."""
    loaded = _foreign_unit(probe, unit_dir, unit) if ready else ""
    if loaded is None:
        return "unknown"
    if loaded:
        return "other"
    return _owner(probe, f"{unit_dir}/{unit}")


def _linux_service_steps(exe: str, options: Options, probe: Probe, paths: Paths) -> list[Step]:
    unit_dir = paths.unit_dir
    manage_bridge = not options.skip_bridge_service
    units = ([templates.BRIDGE_UNIT] if manage_bridge else []) + [templates.MCP_UNIT]
    for unit in units:
        loaded = _foreign_unit(probe, unit_dir, unit)
        if loaded is None:
            raise _not_ours(unit, f"systemctl --user status {unit}")
        # An unowned file at the installer's own path stops at WriteFile unless --force.
        if loaded:
            raise PlanError(
                f"systemd already loads {unit} from {loaded}; the installer does not shadow "
                "or restart units it did not write. Remove or rename that unit, then rerun"
            )
    steps: list[Step] = [EnsureDir(unit_dir, "Create the systemd user unit directory", 0o755)]
    if manage_bridge:
        steps.append(
            WriteFile(
                f"{unit_dir}/{templates.BRIDGE_UNIT}",
                templates.systemd_bridge_unit(exe),
                "Write the Bridge user unit",
                mode=0o644,
                tag="bridge-unit",
            )
        )
    steps += [
        WriteFile(
            f"{unit_dir}/{templates.MCP_UNIT}",
            templates.systemd_mcp_unit(
                paths.service_exe, paths.env_file, options.port, manage_bridge
            ),
            "Write the MCP user unit",
            mode=0o644,
            tag="mcp-unit",
        ),
        # By path: a user manager started with another XDG_CONFIG_HOME than this
        # shell does not search unit_dir, so systemd links the files into its own
        # configuration directory. Every later command then finds them by name.
        Command(
            ("systemctl", "--user", "enable", *(f"{unit_dir}/{unit}" for unit in units)),
            "Enable the units at login (by path, so the user manager links them)",
        ),
        Command(
            ("systemctl", "--user", "daemon-reload"),
            "Reload systemd user units before starting them",
        ),
    ]
    if manage_bridge:
        steps += [
            Command(
                ("systemctl", "--user", "restart", templates.BRIDGE_UNIT),
                "Restart Bridge for its changed unit",
                when_changed=("bridge-unit",),
            ),
            Command(("systemctl", "--user", "start", templates.BRIDGE_UNIT), "Start Bridge"),
        ]
    steps += [
        Command(
            ("systemctl", "--user", "restart", templates.MCP_UNIT),
            "Restart the MCP server for changed code, settings or credentials",
            when_changed=("mcp-unit", *MCP_RESTART_TAGS),
        ),
        Command(("systemctl", "--user", "start", templates.MCP_UNIT), "Start the MCP server"),
        WaitForPort(
            options.port,
            "Check the MCP server listens on 127.0.0.1",
            hint=f"journalctl --user -u {templates.MCP_UNIT}",
        ),
        Note(
            "Units start at login. To keep them running after logout (headless hosts), run "
            "`sudo loginctl enable-linger $USER` once."
        ),
    ]
    return steps


def _launchd_path(probe: Probe, label: str) -> str | None:
    """The file launchd loaded the label from ("" if unknown), or None if not loaded."""
    text = probe.output(("launchctl", "print", f"gui/{probe.user_id}/{label}"))
    if text is None:
        return None
    match = re.search(r"^\s*path = (.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _launchd_state(probe: Probe, label: str, path: str) -> tuple[bool, bool]:
    """Whether launchd has the label loaded, and whether from a file other than path;
    a file launchd does not name counts as another."""
    loaded = _launchd_path(probe, label)
    if loaded is None:
        return False, False
    return True, loaded != path


def _launchd_steps(
    label: str, plist: str, path: str, tag: str, probe: Probe, restart_tags=()
) -> list[Step]:
    domain = f"gui/{probe.user_id}"
    loaded, elsewhere = _launchd_state(probe, label, path)
    # A loaded agent whose file at path is not ours stops at WriteFile unless --force.
    if loaded and (elsewhere or _owner(probe, path) is None):
        raise PlanError(
            f"launchd already runs {label} from a definition the installer did not write; "
            f"unload it with `launchctl bootout {domain}/{label}`, then rerun"
        )
    steps: list[Step] = [
        WriteFile(path, plist, f"Write the {label} LaunchAgent", tag=tag),
        Command(
            ("plutil", "-lint", path), f"Validate the {label} LaunchAgent", when_changed=(tag,)
        ),
    ]
    if loaded:
        steps += [
            Command(
                ("launchctl", "bootout", f"{domain}/{label}"),
                f"Unload the previous {label} agent",
                when_changed=(tag,),
                allow_failure=True,
            ),
            Command(
                ("launchctl", "bootstrap", domain, path),
                f"Load the changed {label} agent",
                when_changed=(tag,),
                retries=3,
            ),
        ]
    else:
        steps.append(
            Command(("launchctl", "bootstrap", domain, path), f"Load the {label} agent", retries=3)
        )
    if restart_tags:
        steps.append(
            Command(
                ("launchctl", "kickstart", "-k", f"{domain}/{label}"),
                f"Restart {label} for changed code, settings or credentials",
                when_changed=restart_tags,
            )
        )
    return steps


def _macos_service_steps(exe: str, options: Options, probe: Probe, paths: Paths) -> list[Step]:
    agents = paths.agents_dir
    steps: list[Step] = [
        EnsureDir(agents, "Create the LaunchAgents directory", 0o755),
        EnsureDir(paths.log_dir, "Create the private log directory"),
    ]
    if not options.skip_bridge_service:
        steps += _launchd_steps(
            templates.BRIDGE_LABEL,
            templates.launchd_bridge_plist(exe, paths.log_dir),
            f"{agents}/{templates.BRIDGE_LABEL}.plist",
            "bridge-agent",
            probe,
        )
    steps += _launchd_steps(
        templates.MCP_LABEL,
        templates.launchd_mcp_plist(
            paths.service_exe, paths.env_file, paths.data_dir, paths.log_dir
        ),
        f"{agents}/{templates.MCP_LABEL}.plist",
        "mcp-agent",
        probe,
        MCP_RESTART_TAGS,
    )
    steps += [
        WaitForPort(
            options.port,
            "Check the MCP server listens on 127.0.0.1",
            hint=f"{paths.log_dir}/stderr.log",
        ),
        Note(
            "LaunchAgents read the login Keychain, so they run while you are logged in; "
            "allow the Keychain prompt on first start."
        ),
    ]
    return steps


def _task_owner(probe: Probe, name: str) -> str | None:
    """None when no task has this name; the installer's tasks carry MARKER."""
    xml = probe.output(("schtasks", "/Query", "/TN", name, "/XML"))
    if xml is None:
        return None
    return "installer" if MARKER in xml else "other"


def _task_running_argv(name: str) -> tuple[str, ...]:
    """A query that succeeds only while the task runs and exits with TASK_NOT_RUNNING
    only for a task it found not running; an error exits 1."""
    # State is an enum name, unlike the localized status text of schtasks /Query.
    leaf = name.lstrip("\\")
    check = (
        f"if ((Get-ScheduledTask -TaskPath '\\' -TaskName '{leaf}' -ErrorAction Stop).State "
        f"-eq 'Running') {{ exit 0 }}; exit {TASK_NOT_RUNNING}"
    )
    return ("powershell", "-NoProfile", "-NonInteractive", "-Command", check)


def _task_running(probe: Probe, name: str) -> bool:
    return probe.succeeds(_task_running_argv(name))


def _foreign_task(name: str) -> PlanError:
    return PlanError(
        f"A task named {name} exists that the installer did not create; it is never "
        "stopped or replaced. Delete or rename it in Task Scheduler, then rerun"
    )


def _task_steps(
    name: str, xml: str, path: str, tag: str, probe: Probe, restart_tags=()
) -> list[Step]:
    owner = _task_owner(probe, name)
    if owner == "other":
        raise _foreign_task(name)
    registered = owner == "installer"
    steps: list[Step] = [
        WriteFile(path, xml, f"Write the {name} task definition", tag=tag, encoding="utf-16"),
        Command(
            ("schtasks", "/Create", "/TN", name, "/XML", path, "/F"),
            f"Register the {name} logon task",
            when_changed=(tag,) if registered else (),
        ),
    ]
    if restart_tags:
        steps.append(
            Command(
                ("schtasks", "/End", "/TN", name),
                f"Stop {name} for changed code, settings or credentials",
                when_changed=(tag, *restart_tags),
                allow_failure=True,
            )
        )
    steps.append(Command(("schtasks", "/Run", "/TN", name), f"Start {name}"))
    return steps


def windows_tasks(probe: Probe) -> tuple[str, str]:
    """Bridge and MCP task names for the current Windows account."""
    return (
        templates.task_name(templates.BRIDGE_TASK_PREFIX, probe.user_id),
        templates.task_name(templates.MCP_TASK_PREFIX, probe.user_id),
    )


def _windows_service_steps(exe: str, options: Options, probe: Probe, paths: Paths) -> list[Step]:
    user = probe.user_id
    bridge_task, mcp_task = windows_tasks(probe)
    steps: list[Step] = [
        EnsureDir(paths.task_dir, "Create the task definition directory"),
        EnsureDir(paths.log_dir, "Create the private log directory"),
    ]
    if not options.skip_bridge_service:
        steps += _task_steps(
            bridge_task,
            templates.scheduled_task_xml(
                description="Proton Mail Bridge (noninteractive) for proton-mail-mcp.",
                user_id=user,
                command=exe,
                arguments=["--noninteractive"],
                workdir=paths.data_dir,
            ),
            _join_windows(paths.task_dir, "bridge-task.xml"),
            "bridge-task",
            probe,
        )
    steps += _task_steps(
        mcp_task,
        templates.scheduled_task_xml(
            description=f"proton-mail-mcp on http://127.0.0.1:{options.port}/mcp.",
            user_id=user,
            command=paths.service_exe,
            arguments=[
                "-m",
                "proton_mail_mcp.service_entry",
                "--env-file",
                paths.env_file,
                "--log-file",
                _join_windows(paths.log_dir, "mcp.log"),
            ],
            workdir=paths.data_dir,
            # Bridge starts first at logon; the MCP server connects lazily anyway.
            delay="PT30S",
        ),
        _join_windows(paths.task_dir, "mcp-task.xml"),
        "mcp-task",
        probe,
        MCP_RESTART_TAGS,
    )
    steps += [
        WaitForPort(
            options.port,
            "Check the MCP server listens on 127.0.0.1",
            hint=_join_windows(paths.log_dir, "mcp.log"),
        ),
        Note("Logon tasks run while you are logged in, which Credential Manager requires."),
    ]
    return steps


def _join_windows(*parts: str) -> str:
    return str(PureWindowsPath(*parts))


def _service_steps(platform, options, probe, paths) -> list[Step]:
    if options.skip_services:
        return [Note("Skipping service setup (--skip-services).")]
    exe = bridge_executable(platform, options, probe)
    if platform.family == "macos":
        return _macos_service_steps(exe, options, probe, paths)
    if platform.family == "windows":
        return _windows_service_steps(exe, options, probe, paths)
    return _linux_service_steps(exe, options, probe, paths)


def _windows_update_steps(options: Options, probe: Probe) -> list[Step]:
    """pip cannot replace extension modules that the running MCP task has loaded."""
    task = windows_tasks(probe)[1]
    if _task_owner(probe, task) != "installer":
        # None: nothing to stop. "other": never stopped; _task_steps refuses it.
        return []
    if options.skip_services:
        if _task_running(probe, task):
            raise PlanError(
                f"{task} is running, and pip cannot replace files it has loaded. Stop it "
                f'with `schtasks /End /TN "{task}"` and rerun, or rerun without --skip-services'
            )
        return []
    return [
        Command(
            ("schtasks", "/End", "/TN", task),
            "Stop the MCP task so pip can replace its files",
            allow_failure=True,
        )
    ]


def _bridge_commands(
    platform: Platform, probe: Probe, paths: Paths
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """How to stop the installer's Bridge service and start it again, on Linux or macOS."""
    if platform.family == "macos":
        domain = f"gui/{probe.user_id}"
        plist = f"{paths.agents_dir}/{templates.BRIDGE_LABEL}.plist"
        return (
            ("launchctl", "bootout", f"{domain}/{templates.BRIDGE_LABEL}"),
            ("launchctl", "bootstrap", domain, plist),
        )
    return (
        ("systemctl", "--user", "stop", templates.BRIDGE_UNIT),
        ("systemctl", "--user", "start", templates.BRIDGE_UNIT),
    )


def _bridge_service(platform: Platform, probe: Probe, paths: Paths) -> BridgeService | None:
    """The Bridge service a login may stop: only one whose definition is the installer's
    own, as for any stop (R3, R4). Any other Bridge is found by its port and process."""
    stop, start = _bridge_commands(platform, probe, paths)
    if platform.family == "macos":
        label = templates.BRIDGE_LABEL
        path = f"{paths.agents_dir}/{label}.plist"
        _loaded, elsewhere = _launchd_state(probe, label, path)
        if elsewhere or _owner(probe, path) != "installer":
            return None
        query = ("launchctl", "print", f"gui/{probe.user_id}/{label}")
        check = CheckStopped(query, f"Check that {label} is unloaded", "", status=LAUNCHD_NOT_FOUND)
        return BridgeService(check, stop, start)
    unit = templates.BRIDGE_UNIT
    # Without a user manager to ask, none of the installer's units can be seen or stopped.
    if (
        _systemd_state(probe)[0] != "ready"
        or _unit_owner(probe, paths.unit_dir, unit, True) != "installer"
    ):
        return None
    query = ("systemctl", "--user", "show", "--property=ActiveState", "--value", unit)
    check = CheckStopped(query, f"Check that {unit} is stopped", "", states=UNIT_STOPPED_STATES)
    return BridgeService(check, stop, start)


def _login_note(platform: Platform, options: Options, probe: Probe, paths: Paths) -> Note:
    exe = bridge_executable(platform, options, probe)
    if platform.family == "windows":
        cli = f'& "{exe}" --cli'
        bridge_task = windows_tasks(probe)[0]
        stop = f'schtasks /End /TN "{bridge_task}"'
        start = f'schtasks /Run /TN "{bridge_task}"'
    else:
        cli = shlex.join([exe, "--cli"])
        stop, start = map(shlex.join, _bridge_commands(platform, probe, paths))
    text = (
        f"Bridge login (once per account): in another terminal run `{cli}`, then `login`, "
        "`info` (shows the IMAP username and password) and `exit`."
    )
    if not (options.skip_services or options.skip_bridge_service):
        text += (
            " Bridge's CLI and its service cannot run together: later logins need "
            f"`{stop}` first and `{start}` afterwards. The MCP service keeps running "
            "meanwhile and reaches Bridge again on its next request."
        )
    return Note(text)


def _implicit_homes(platform: Platform, probe: Probe) -> dict[str, str]:
    """GnuPG's and pass's default homes, which those tools use wherever no variable
    names another, whatever the settings say; path -> what it is."""
    if platform.family == "windows":
        appdata = probe.env.get("APPDATA", "")
        return {_join(platform, appdata, "gnupg"): "GnuPG's default home"} if appdata else {}
    return {
        f"{probe.home}/.gnupg": "GnuPG's default home",
        f"{probe.home}/.password-store": "pass's default store",
    }


def _removal_command(platform: Platform, *targets: str) -> str:
    if platform.family == "windows":
        quoted = ", ".join("'" + path.replace("'", "''") + "'" for path in targets)
        return f"Remove-Item -LiteralPath {quoted} -Recurse -Force"
    return f"rm -rf -- {shlex.join(targets)}"


def _file_removal_command(platform: Platform, path: str) -> str:
    if platform.family == "windows":
        return "Remove-Item -LiteralPath '{}' -Force".format(path.replace("'", "''"))
    return f"rm -f -- {shlex.quote(path)}"


def _pending_venvs(platform: Platform, settings: Mapping[str, str]) -> list[str]:
    text = settings.get(PREVIOUS_VENVS_KEY, "")
    try:
        values = json.loads(text) if text else []
    except ValueError:
        values = None
    kind = _path_type(platform)
    if not isinstance(values, list) or not all(
        isinstance(value, str) and kind(value).is_absolute() for value in values
    ):
        raise PlanError(f"{PREVIOUS_VENVS_KEY} must be a JSON list of absolute paths")
    return values


def _remembered_venvs(
    platform: Platform, probe: Probe, paths: Paths, candidates: list[str]
) -> list[str]:
    """Earlier environments the installer created and left in place: each candidate
    that still exists, carries DIR_MARKER and is not the current one by any name."""
    current = probe.realpath(paths.venv)
    return [
        venv
        for venv in dict.fromkeys(candidates)
        if venv != paths.venv
        and probe.exists(venv)
        and probe.exists(_join(platform, venv, DIR_MARKER))
        and probe.realpath(venv) != current
    ]


def _size_text(size: int | None) -> str:
    """Such as "187.3 MB", "12.0 KB" or "512 bytes"; "size unknown" without a size."""
    if size is None:
        return "size unknown"
    if size < 1000:
        return f"{size} bytes"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1000
        if round(value, 1) < 1000 or unit == "TB":
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable")


def _installation_paths(
    platform: Platform,
    options: Options,
    probe: Probe,
    paths: Paths,
    values: Mapping[str, str],
) -> dict[str, str]:
    """What this installation uses, by name: a directory that holds any of it gets no
    removal command (R6).

    The installer's own files and directories, Bridge's executable, GnuPG's and
    pass's default homes, and each absolute data setting in values (by name). Nothing
    here comes from a service definition, which the installer never reads.
    """
    kind = _path_type(platform)
    attachments = default_attachments_dir({"HOME": probe.home})
    named = {
        "the settings file": paths.env_file,
        "the config directory": paths.config_dir,
        "the state directory": paths.state_dir,
        "the log directory": paths.log_dir,
        "the systemd unit directory": paths.unit_dir,
        "the LaunchAgents directory": paths.agents_dir,
        "the task directory": paths.task_dir,
        "the Bridge executable": bridge_executable(platform, options, probe),
        # A setting that names the same folder is named instead, below.
        "the default attachments folder": None if attachments in values.values() else attachments,
        **{what: path for path, what in _implicit_homes(platform, probe).items()},
        **{name: value for name, value in values.items() if value and kind(value).is_absolute()},
    }
    return {name: path for name, path in named.items() if path}


def _kept_directory(
    platform: Platform,
    probe: Probe,
    paths: Paths,
    path: str,
    what: str,
    used: Mapping[str, str],
    *,
    unproven: str = "",
) -> Note:
    """The note on a directory that stays (R1), with its size; printed, and logged when
    applied.

    The installer never deletes a directory with content. The note gives the command
    that removes it by hand only when that cannot obviously harm this installation
    (R6): the directory carries DIR_MARKER, is no symbolic link, does not overlap
    the current virtual environment and holds none of used, by name or resolved.
    unproven says why a service may still run from it: a previous environment no
    service was switched away from, or one no stopped service of the installer's
    proves unused. A note never calls a directory unused: the installer has not
    checked what else of the user's needs it, and it reads no service definition to
    find out.
    """
    kind = _path_type(platform)
    flavor = ntpath if platform.family == "windows" else posixpath
    log = _join(platform, paths.state_dir, KEPT_LOG)
    # A symbolic link has no size of its own: the walk never follows one.
    head = f"{what} {path} ({_size_text(probe.tree_size(path))})"
    real = kind(probe.realpath(path))
    other = path != paths.venv

    def inside(place: str, top: str) -> bool:
        # By name (".." resolved) and with symbolic links resolved: a path that lies in
        # the directory either way goes with it.
        forms = [(kind(flavor.normpath(name)), kind(probe.realpath(name))) for name in (place, top)]
        return any(a.is_relative_to(b) for a in forms[0] for b in forms[1])

    def stays(why: str) -> Note:
        text = f"{head} stays: {why}. The installer never deletes it and suggests no command"
        return Note(f"{text} to remove it.", log=log)

    if probe.readlink(path) is not None:
        return stays("it is a symbolic link")
    if not probe.exists(_join(platform, path, DIR_MARKER)):
        return stays(f"the installer did not create it (it has no {DIR_MARKER} marker)")
    if other and real == kind(probe.realpath(paths.venv)):
        return stays("it is the current virtual environment under another name")
    if other and inside(paths.venv, path):
        return stays(f"it holds the current virtual environment {paths.venv}")
    if other and inside(path, paths.venv):
        return stays(f"it lies inside the current virtual environment {paths.venv}")
    if unproven:
        return stays(unproven)
    # A setting that names the directory itself counts too.
    held = [name for name, place in used.items() if inside(place, path)]
    if held:
        return Note(
            f"{head} stays and still holds what this installation uses: {', '.join(held)}. "
            "The installer never deletes it. Move those out (and update any setting that "
            "names them) before removing it.",
            log=log,
        )
    return Note(
        f"{head} stays. The installer never deletes it and has not checked whether anything "
        "else of yours runs from it, keeps data in it or links into it. Once nothing does, "
        f"remove it with: `{_removal_command(platform, str(real))}`",
        log=log,
    )


def _systemd_state(probe: Probe) -> tuple[str, str]:
    """Whether systemd user units can be set up and controlled here, and why not.

    "ready": systemctl reaches the user manager. "absent": systemd does not run this
    host at all (a container, or another init system such as Artix's), so no unit
    can be running. "unreachable": systemd runs the host, but systemctl is missing
    from PATH or cannot reach the user manager, so units may be running unseen.
    """
    systemctl = probe.which("systemctl")
    if systemctl and probe.output(MANAGER_ENVIRONMENT) is not None:
        return "ready", ""
    if not probe.exists(SYSTEMD_BOOTED):
        container = probe.env.get("container", "") or next(
            (name for marker, name in CONTAINER_MARKERS if probe.exists(marker)), ""
        )
        if container:
            return "absent", f"this host is a container ({container}) that does not run systemd"
        return "absent", (
            "this host does not run systemd (Artix, for example, uses another init system)"
        )
    if not systemctl:
        return "unreachable", "systemctl is not on PATH, although systemd runs this host"
    return "unreachable", "`systemctl --user` cannot reach your systemd user manager"


def _service_preflight(probe: Probe, paths: Paths, options: Options) -> tuple[Options, list[Step]]:
    """Linux services need a systemd user manager; check before anything changes.

    Without systemd at all the plan goes on without services, as with
    --skip-services, and says so. A systemd host whose user manager cannot be reached
    stops the plan, since services there may be running.
    """
    if options.skip_services:
        return options, []
    state, why = _systemd_state(probe)
    start = f"`{shlex.join([paths.service_exe, '--env-file', paths.env_file])}`"
    if state == "absent":
        note = Note(
            f"Services: {why}, so the installer sets up no service, as with "
            f"--skip-services. Have your own service manager run {start} at login."
        )
        return replace(options, skip_services=True), [note]
    if state == "unreachable" and "PATH" in why:
        raise PlanError(
            f"{why}, so the installer can neither set up nor check its services. Put "
            "systemctl back on PATH and rerun, or rerun with --skip-services and have your "
            f"own service manager run {start} at login"
        )
    if state == "unreachable":
        raise PlanError(
            f"{why} (no login session, or a system not booted with systemd). Rerun in a "
            "normal login session (on a headless host run `sudo loginctl enable-linger "
            "$USER` once), or rerun with --skip-services and have your own service manager "
            f"run {start} at login"
        )
    return options, []


def install_plan(platform: Platform, options: Options, probe: Probe, paths: Paths) -> list[Step]:
    """The install plan for a supported platform; Windows is refused for now."""
    if platform.refusal:
        raise PlanError(platform.refusal)
    return install_steps(platform, options, probe, paths)


def install_steps(platform: Platform, options: Options, probe: Probe, paths: Paths) -> list[Step]:
    """Steps for any known family, including Windows plans kept tested for later support."""
    if platform.family not in FAMILIES:
        raise PlanError(f"Unsupported platform: {platform.name}")
    validate_options(options, platform)
    if not options.source:
        raise PlanError(
            "Cannot tell where this copy of proton-mail-mcp came from, so pip cannot install "
            "it for the service; pass --source with a checkout or a wheel file"
        )
    existing, settings = existing_settings(paths, probe)
    account = bridge_account(options, settings)
    store = choose_store(platform, options, probe, settings)
    # The service does not inherit this shell: it must use the same store and keys.
    located, pass_notes = _pass_steps(probe, settings) if store == "pass" else ({}, [])
    if platform.system == "linux" and not options.skip_credentials:
        _check_store_tools(platform, store, probe, settings)
    saved_venv = settings.get(VENV_KEY)
    candidates = [*_pending_venvs(platform, settings), with_venv(platform, paths, saved_venv).venv]
    paths = with_venv(platform, paths, options.venv or saved_venv)
    preflight: list[Step] = []
    if platform.system == "linux":
        options, preflight = _service_preflight(probe, paths, options)
    extra = {VENV_KEY: paths.venv} if options.venv or saved_venv else {}
    state, resolved, notes = _state_settings(platform, probe, paths, settings)
    notes = [*preflight, *notes, *pass_notes]
    extra.update(state)
    extra.update(located)
    attachments = {ATTACHMENTS_KEY: options.attachments_dir} if options.attachments_dir else {}
    extra.update(attachments)
    # Earlier environments stay where they are; the settings remember them.
    left = _remembered_venvs(platform, probe, paths, candidates)
    if left or PREVIOUS_VENVS_KEY in settings:
        extra[PREVIOUS_VENVS_KEY] = json.dumps(left) if left else ""
    saved = {key: settings[key] for key in DATA_SETTINGS if settings.get(key)}
    final = {**saved, **resolved, **located, **attachments}
    # A location the shell replaces still holds what was kept there; nothing moves.
    replaced = {
        f"where {key} pointed before this run": value
        for key, value in saved.items()
        if final[key] != value
    }
    used = _installation_paths(platform, options, probe, paths, {**final, **replaced})
    unswitched = (
        f"with --skip-services (or without systemd) no service was switched to {paths.venv}, "
        "so services may still run from it"
        if options.skip_services
        else ""
    )
    kept = [
        _kept_directory(
            platform,
            probe,
            paths,
            venv,
            "The previous virtual environment",
            used,
            unproven=unswitched,
        )
        for venv in left
    ]
    # Secrets, the service and the token hint all use the pass locations written now.
    locations = credential_store.pass_locations(located)
    steps: list[Step] = []
    steps += bridge_steps(platform, options, probe, paths)
    if not options.skip_bridge:
        steps.append(_login_note(platform, options, probe, paths))
    elif not (options.skip_services or options.skip_bridge_service) and not probe.exists(
        bridge_executable(platform, options, probe)
    ):
        raise PlanError(
            f"Bridge is not at {bridge_executable(platform, options, probe)}; pass --bridge-bin "
            "or --skip-bridge-service"
        )
    if platform.family == "windows":
        steps += _windows_update_steps(options, probe)
    steps += _mcp_steps(platform, options, probe, paths)
    if options.setup:
        stored = _bridge_password_stored(store, account, options, probe, locations)
        steps += _setup_steps_before_credentials(platform, options, probe, paths, stored)
    steps += _credential_steps(store, account, options, probe, locations)
    steps += _config_steps(store, account, options, paths, existing, extra)
    steps += _service_steps(platform, options, probe, paths)
    steps += kept
    steps += [
        *notes,
        Note(f"MCP endpoint: http://127.0.0.1:{options.port}/mcp (bearer token required)."),
        Note(token_hint(store, account, locations)),
        Note(
            f"Audit and undo logs: {resolved['PROTON_MCP_ACTION_LOG']}; identity index: "
            f"{resolved['PROTON_MCP_IDENTITY_INDEX_DIR']}. The env file pins both for the "
            "service, and uninstall keeps them."
        ),
    ]
    if options.setup:
        steps += _self_check_steps(platform, store, account, options, paths, locations)
    return steps


def _bridge_password_stored(
    store: str, account: str, options: Options, probe: Probe, locations: Mapping[str, str]
) -> bool:
    """A store that cannot say counts as no password; a run that replaces the password, or
    skips credentials, does not ask the store about it, as _credential_steps does not."""
    if options.skip_credentials or options.replace_credentials:
        return False
    return probe.has_secret(store, BRIDGE_SERVICE, account, locations) is True


def _setup_steps_before_credentials(
    platform: Platform, options: Options, probe: Probe, paths: Paths, password_stored: bool
) -> list[Step]:
    """A broken environment stops here, not as a service that keeps restarting; the
    Bridge login comes right before the password prompt it gives the answer to."""
    # -I: the environment's own copy, whatever PYTHONPATH the installer runs with.
    steps: list[Step] = [
        Command(
            (paths.venv_python, "-I", "-c", "import proton_mail_mcp.server"),
            "Check the virtual environment can load the server",
        )
    ]
    if not (options.skip_bridge or options.skip_bridge_login):
        exe = bridge_executable(platform, options, probe)
        steps.append(
            BridgeLogin(
                (exe, "--cli"),
                "Offer Bridge's CLI to log in",
                options.imap_port,
                service=_bridge_service(platform, probe, paths),
                processes=("pgrep", "-u", probe.user_id, "-f", BRIDGE_PROCESSES),
                password_stored=password_stored,
            )
        )
    return steps


def _self_check_steps(
    platform: Platform,
    store: str,
    account: str,
    options: Options,
    paths: Paths,
    locations: Mapping[str, str],
) -> list[Step]:
    command = str(PurePosixPath(paths.venv) / "bin" / "proton-mail-mcp")
    steps: list[Step] = [
        Note(
            f"To read the token later: `{shlex.join([command, 'show-token'])}` "
            "(prints to a terminal only)."
        )
    ]
    if options.skip_services:
        return [*steps, Note("Self-check: no service was set up here, so none was checked.")]
    if options.skip_credentials:
        return [
            *steps,
            Note(
                "Self-check skipped (--skip-credentials): it needs the stored token. Run setup "
                "again once the credentials are stored."
            ),
        ]
    if platform.family == "macos":
        logs, bridge_logs = f"{paths.log_dir}/stderr.log", f"{paths.log_dir}/bridge-stderr.log"
    else:
        logs = f"journalctl --user -u {templates.MCP_UNIT}"
        bridge_logs = f"journalctl --user -u {templates.BRIDGE_UNIT}"
    return [
        *steps,
        WaitForPort(
            options.imap_port,
            "Check Bridge listens on 127.0.0.1",
            timeout=30.0,
            hint="Bridge's own app" if options.skip_bridge_service else bridge_logs,
        ),
        SelfCheck(
            options.port,
            store,
            TOKEN_SERVICE,
            account,
            "Check the MCP server answers and reads mail through Bridge",
            hint=logs,
            locations=tuple(locations.items()),
        ),
    ]


def token_hint(store: str, account: str, locations: Mapping[str, str] | None = None) -> str:
    # The same store and keys the service reads, whatever the reading shell exports.
    located = "".join(f"{name}={shlex.quote(value)} " for name, value in (locations or {}).items())
    # Accounts may hold shell syntax such as $ or ;, so the pasted command quotes them.
    quoted = shlex.quote(account)
    commands = {
        "keychain": f"security find-generic-password -s {TOKEN_SERVICE} -a {quoted} -w",
        "libsecret": f"secret-tool lookup service {TOKEN_SERVICE} account {quoted}",
        "pass": f"{located}pass show {shlex.quote(f'{TOKEN_SERVICE}/{account}')}",
        # Credential Manager's window never reveals generic credential secrets.
        "wincred": ".\\scripts\\install.ps1 -Action show-token",
    }
    return (
        "The installer never prints the bearer token. Copy it into your MCP client's secret "
        f"storage from `{commands[store]}` (prints to a terminal only)."
        if store == "wincred"
        else "The installer never prints the bearer token. Copy it into your MCP client's "
        f"secret storage from `{commands[store]}`."
    )


def _bridge_removal(platform: Platform, probe: Probe) -> list[Step]:
    family = platform.family
    if family == "debian":
        argv, sudo = ("apt-get", "remove", "-y", BRIDGE_DEB_PACKAGE), True
    elif family == "fedora":
        argv, sudo = ("dnf", "remove", "-y", BRIDGE_RPM_PACKAGE), True
    elif family == "arch":
        argv, sudo = ("pacman", "-R", "--noconfirm", BRIDGE_ARCH_PACKAGE), True
    elif family == "macos":
        argv, sudo = (probe.which("brew") or "brew", "uninstall", "--cask", BRIDGE_CASK), False
    else:
        argv = (
            "winget",
            "uninstall",
            "--id",
            BRIDGE_WINGET_ID,
            "--exact",
            "--source",
            "winget",
            "--silent",
            "--disable-interactivity",
        )
        sudo = False
    return [
        Command(argv, "Remove the Bridge package", sudo=sudo, interactive=True),
        Note("Bridge's own settings and mail cache stay; Bridge's documentation covers them."),
    ]


def uninstall_plan(platform: Platform, options: Options, probe: Probe, paths: Paths) -> list[Step]:
    """The uninstall plan for a supported platform; Windows is refused for now."""
    if platform.refusal:
        raise PlanError(platform.refusal)
    return uninstall_steps(platform, options, probe, paths)


def _units_kept(units: list[str], files: list[str], why: str) -> str:
    """Why uninstall changes nothing while it cannot control the installer's units,
    and how to remove them by hand."""
    names = " ".join(units)
    them = "it" if len(units) == 1 else "them"
    return (
        f"{' and '.join(units)} {'was' if len(units) == 1 else 'were'} written by the "
        f"installer, but {why}, so uninstall can neither stop {them} nor tell that "
        f"{'it is' if len(units) == 1 else 'they are'} stopped. Nothing was changed: the "
        "units, the virtual environment, the settings and all data stay. To remove the "
        f"units by hand: where `systemctl --user` works (a normal login session), run "
        f"`systemctl --user disable --now {names}`; a host that does not run systemd runs "
        f"no unit. Then run `rm -f -- {shlex.join(files)}`, and `systemctl --user "
        "daemon-reload` where systemd runs. Rerun uninstall afterwards: it then removes "
        "the units and the settings and prints how to remove the virtual environment"
    )


def _not_ours(name: str, check: str) -> PlanError:
    """A plan changes nothing while a definition the installer did not write, or one
    the service manager does not name, holds one of its service names (R4)."""
    return PlanError(
        f"{name} runs, or may run, from a definition the installer did not write (or "
        "one the service manager does not name), so the installer stops nothing and "
        "changes nothing: the services, the settings, the virtual environment and all "
        f"data stay. `{check}` shows that definition; remove or rename it if it is yours, "
        "then rerun"
    )


@dataclass
class _Services:
    """Uninstall's service steps, in the order they run (R3)."""

    # Stops, each followed by the check that proves it.
    stops: list[Step]
    # The service files and registrations; they go only once every check passed.
    removals: list[Step]
    # Whether the installer's own MCP service is stopped and checked: only then can
    # nothing of the installer's still use the settings or the venvs.
    mcp_checked: bool = False


def _stop_hint(command: str, status: str) -> str:
    return (
        f"Nothing was removed. Stop it yourself with `{command}` ({status}), then rerun uninstall."
    )


def _linux_services(probe: Probe, paths: Paths) -> _Services:
    units = [templates.MCP_UNIT, templates.BRIDGE_UNIT]
    state, why = _systemd_state(probe)
    owners = {unit: _unit_owner(probe, paths.unit_dir, unit, state == "ready") for unit in units}
    owned = [unit for unit in units if owners[unit] == "installer"]
    if owned and state != "ready":
        raise PlanError(_units_kept(owned, [f"{paths.unit_dir}/{unit}" for unit in owned], why))
    for unit in units:
        if owners[unit] in ("unknown", "other"):
            raise _not_ours(unit, f"systemctl --user status {unit}")
    services = _Services([], [])
    if owned:
        disable = f"systemctl --user disable --now {' '.join(owned)}"
        services.stops.append(
            Command(
                ("systemctl", "--user", "disable", "--now", *owned),
                "Stop and disable the user units",
                allow_failure=True,
            )
        )
        services.stops += [
            CheckStopped(
                ("systemctl", "--user", "show", "--property=ActiveState", "--value", unit),
                f"Check that {unit} is stopped",
                _stop_hint(
                    disable,
                    "a unit that refuses manual stops needs that setting removed; "
                    f"`systemctl --user status {unit}` shows what keeps it running",
                ),
                states=UNIT_STOPPED_STATES,
            )
            for unit in owned
        ]
    services.removals += [
        RemovePath(f"{paths.unit_dir}/{unit}", f"Remove {unit}") for unit in units
    ]
    services.mcp_checked = templates.MCP_UNIT in owned
    if state == "ready":
        services.removals.append(
            Command(("systemctl", "--user", "daemon-reload"), "Reload systemd user units")
        )
    else:
        services.removals.append(
            Note(
                f"{why[0].upper()}{why[1:]}; no unit the installer wrote is here, so only "
                "files are removed; the virtual environment and downloads stay."
            )
        )
    return services


def _macos_services(probe: Probe, paths: Paths) -> _Services:
    services = _Services([], [])
    for label in (templates.MCP_LABEL, templates.BRIDGE_LABEL):
        path = f"{paths.agents_dir}/{label}.plist"
        target = f"gui/{probe.user_id}/{label}"
        loaded, elsewhere = _launchd_state(probe, label, path)
        owner = _owner(probe, path)
        ours = owner == "installer" and not elsewhere
        if (loaded and not ours) or owner == "other":
            raise _not_ours(label, f"launchctl print {target}")
        if loaded:
            services.stops.append(
                Command(
                    ("launchctl", "bootout", target),
                    f"Unload the {label} agent",
                    allow_failure=True,
                )
            )
        if ours:
            services.mcp_checked |= label == templates.MCP_LABEL
            # Also when it was not loaded while planning: it may be loaded by now. Only
            # launchctl's "not found" proves it unloaded; any other failure proves nothing.
            services.stops.append(
                CheckStopped(
                    ("launchctl", "print", target),
                    f"Check that {label} is unloaded",
                    _stop_hint(
                        f"launchctl bootout {target}", f"`launchctl print {target}` shows it"
                    ),
                    status=LAUNCHD_NOT_FOUND,
                )
            )
        services.removals.append(RemovePath(path, f"Remove the {label} LaunchAgent"))
    return services


def _windows_services(probe: Probe, paths: Paths) -> _Services:
    services = _Services([], [])
    deletes: list[Step] = []
    bridge_task, mcp_task = windows_tasks(probe)
    for name, file in ((mcp_task, "mcp-task.xml"), (bridge_task, "bridge-task.xml")):
        owner = _task_owner(probe, name)
        if owner == "other":
            raise _foreign_task(name)
        if owner == "installer":
            services.mcp_checked |= name == mcp_task
            services.stops += [
                Command(("schtasks", "/End", "/TN", name), f"Stop {name}", allow_failure=True),
                CheckStopped(
                    _task_running_argv(name),
                    f"Check that {name} is not running",
                    _stop_hint(f'schtasks /End /TN "{name}"', "Task Scheduler shows its state"),
                    status=TASK_NOT_RUNNING,
                ),
            ]
            deletes.append(
                Command(("schtasks", "/Delete", "/TN", name, "/F"), f"Delete the {name} task")
            )
        services.removals.append(
            RemovePath(_join_windows(paths.task_dir, file), f"Remove the {name} definition")
        )
    services.removals[:0] = deletes
    return services


def uninstall_steps(platform: Platform, options: Options, probe: Probe, paths: Paths) -> list[Step]:
    """Stop the installer's services and prove them stopped, then remove only the files
    it wrote and marked; every directory with content stays, with how to remove it.

    Services are stopped only after their definition proves them ours, and nothing is
    removed before every check passed (R3). A definition the installer did not write,
    or one the service manager does not name, under any of its service names stops
    the plan before anything changes (R4). The settings file goes only when the
    installer's own MCP service was stopped and checked; otherwise it stays, and so
    does any suggestion to remove a venv.
    """
    if platform.family not in FAMILIES:
        raise PlanError(f"Unsupported platform: {platform.name}")
    validate_options(options, platform)
    _existing, settings = existing_settings(paths, probe)
    paths = with_venv(platform, paths, options.venv or settings.get(VENV_KEY))
    previous = _pending_venvs(platform, settings)
    if platform.family == "macos":
        services = _macos_services(probe, paths)
    elif platform.family == "windows":
        services = _windows_services(probe, paths)
    else:
        services = _linux_services(probe, paths)
    steps: list[Step] = [*services.stops, *services.removals]
    if options.remove_credentials:
        account = bridge_account(options, settings)
        store = choose_store(platform, options, probe, settings)
        located = tuple(credential_locations(store, probe, settings, prefer_saved=True).items())
        steps += [
            DeleteSecret(
                store,
                BRIDGE_SERVICE,
                account,
                f"Delete the Bridge password from {store}",
                locations=located,
            ),
            DeleteSecret(
                store,
                TOKEN_SERVICE,
                account,
                f"Delete the MCP token from {store}",
                locations=located,
            ),
        ]
    else:
        steps.append(
            Note("Credentials stay in the credential store (--remove-credentials deletes them).")
        )
    # The settings file goes only once the MCP service that read it is proven stopped;
    # until then a service the installer cannot see may read it, or run from a venv.
    if not services.mcp_checked:
        stopped = "the installer stopped no MCP service of its own, so one of yours may"
        reader, unproven = f"{stopped} read it", f"{stopped} still run from it"
    else:
        reader = unproven = ""
    kept_settings = bool(reader) and probe.exists(paths.env_file)
    if not reader:
        steps.append(RemovePath(paths.env_file, "Remove the service settings"))
    elif kept_settings:
        steps.append(
            Note(
                f"The settings file {paths.env_file} stays: {reader}. Once nothing does, "
                f"remove it with: `{_file_removal_command(platform, paths.env_file)}`",
                log=_join(platform, paths.state_dir, KEPT_LOG),
            )
        )
    if options.remove_bridge:
        steps += _bridge_removal(platform, probe)
    else:
        steps.append(Note("Bridge stays installed (--remove-bridge removes the package)."))
    directories = [paths.task_dir] if paths.task_dir else []
    for directory in dict.fromkeys((*directories, paths.config_dir, paths.data_dir)):
        steps.append(
            RemovePath(directory, "Remove the installer directory if it is empty", directory=True)
        )
    # Where the data is: the installer's own settings, else the defaults it wrote.
    data = {key: settings[key] for key in DATA_SETTINGS if settings.get(key)}
    for key, name in STATE_FILES:
        data.setdefault(key, _join(platform, paths.state_dir, name))
    # The settings file never names the default folder, and saved attachments stay in it.
    attachments = default_attachments_dir({"HOME": probe.home})
    if attachments:
        data.setdefault(ATTACHMENTS_KEY, attachments)
    data = {key: data[key] for key in DATA_SETTINGS if key in data}
    used = _installation_paths(platform, options, probe, paths, data)
    # Downloads run nothing; a venv may, while no stopped service proves otherwise.
    kept = [
        (paths.venv, "The virtual environment", unproven),
        (paths.cache_dir, "The download directory", ""),
        *(
            (venv, "The previous virtual environment", unproven)
            for venv in _remembered_venvs(platform, probe, paths, previous)
        ),
    ]
    steps += [
        _kept_directory(platform, probe, paths, directory, what, used, unproven=reason)
        for directory, what, reason in kept
        if probe.exists(directory)
    ]
    listed = ", ".join(f"{key}={value}" for key, value in data.items())
    fate = "stays" if kept_settings else "is removed" if not reader else "is not there"
    note = f"Your data stays where these settings put it: {listed}; the settings file {fate}."
    if paths.log_dir:
        note += f" Service logs in {paths.log_dir} are kept."
    steps.append(Note(note))
    return steps
