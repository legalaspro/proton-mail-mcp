"""Installer command line: setup, install, uninstall or detect, each with --dry-run."""

import argparse
import functools
import json
import ntpath
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import asdict, replace
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .. import credential_store, localfs
from ..config import default_attachments_dir, valid_attachments_dir
from ..credential_store import STORES, TOKEN_SERVICE
from . import bridge_cli, clients
from .clients import CLIENTS
from .detect import FAMILIES, Platform, detect_host, simulated
from .execute import (
    Executor,
    InstallError,
    command_line,
    on_terminal,
    pasted,
    terminal_available,
)
from .plan import (
    ATTACHMENTS_KEY,
    BRIDGE_VERSION,
    VENV_KEY,
    CredentialsUnknown,
    Options,
    Paths,
    PlanError,
    Probe,
    bridge_account,
    bridge_executable,
    bridge_processes,
    choose_store,
    configure_plan,
    connector,
    credential_locations,
    default_attachments,
    existing_settings,
    in_uv_cache,
    install_plan,
    is_pinned,
    launcher,
    paths_for,
    service_python,
    uninstall_plan,
    with_venv,
)
from .probe import HostProbe, simulated_probe
from .steps import PACKAGE, NeedsUser, Note, Step

# Shown for the Bridge username in a dry run that has none yet; no store is asked about it.
PLACEHOLDER_ACCOUNT = "you@proton.me"
# Bridge's usernames are addresses. A password pasted at the question rarely looks like one.
_ADDRESS = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s.]+")
# pip's `--trusted-host`: a host name or address, with an optional port.
_TRUSTED_HOST = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:[0-9]+)?")
# Answers without a prompt only while sudo still holds this user's credentials.
SUDO_CACHED = ("sudo", "-n", "true")
# Words that make a run ask nothing; a person running it in their own terminal drops them.
_UNATTENDED = {"--json", "--non-interactive", "--yes", "-y", "--dry-run"}
# Programs that take the token on stdin and keep it on the clipboard, by platform.
_CLIPBOARDS = {
    "macos": (("pbcopy",),),
    "windows": (("clip",),),
    "linux": (
        ("wl-copy",),
        ("xclip", "-selection", "clipboard"),
        ("xsel", "--clipboard", "--input"),
    ),
}
SERVICE_MANAGERS = {
    "debian": "systemd user units",
    "fedora": "systemd user units",
    "arch": "systemd user units",
    "macos": "launchd LaunchAgents",
    "windows": "Task Scheduler logon tasks",
}


def is_source(path: Path) -> bool:
    try:
        text = (path / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return False
    return f'name = "{PACKAGE}"' in text


def is_wheel(path: Path) -> bool:
    return path.is_file() and path.name.startswith("mcp_proton_mail-") and path.suffix == ".whl"


def installed_source() -> str:
    """What pip needs to install this same copy into the service's environment.

    From a local wheel or directory (PEP 610's direct_url.json): that path, and never
    the index, which could hold something else under this name. Any other origin, or a
    path that is gone: unknown (""). No direct_url.json only says it was installed by
    name, from PyPI or from anywhere else: the pinned version, which
    recorded_index_args() points at the same place where it can, and whose installed
    files must then prove it the same copy.
    """
    try:
        installed = metadata.distribution(PACKAGE)
        origin = installed.read_text("direct_url.json")
        if not origin:
            return f"{PACKAGE}=={installed.version}"
        url = urlsplit(json.loads(origin)["url"])
    except (metadata.PackageNotFoundError, KeyError, TypeError, ValueError):
        return ""
    if url.scheme != "file":
        return ""
    path = Path(url2pathname(url.path))
    return str(path) if is_wheel(path) or is_source(path) else ""


def default_source() -> str:
    """The checkout this module runs from (scripts set PYTHONPATH to its src), or else
    where this installed copy came from."""
    root = Path(__file__).resolve().parents[3]
    return str(root) if is_source(root) else installed_source()


class _PipArguments(argparse.ArgumentParser):
    """Reads pip's arguments as pip does, with --opt=value and abbreviations."""

    def error(self, message: str):
        raise ValueError(message)


def _pipx_index_options(text: str) -> list[tuple[str, str]]:
    """pipx keeps the pip arguments it was given, --index-url included."""
    recorded = json.loads(text)["main_package"]["pip_args"]
    if not isinstance(recorded, list) or not all(isinstance(word, str) for word in recorded):
        raise TypeError("pip_args is not a list of words")
    parser = _PipArguments(add_help=False)
    parser.add_argument("--index-url", "-i")
    parser.add_argument("--extra-index-url", action="append", default=[])
    parser.add_argument("--find-links", "-f", action="append", default=[])
    parser.add_argument("--trusted-host", action="append", default=[])
    parser.add_argument("--no-index", action="store_true")
    known, _others = parser.parse_known_args(recorded)
    options = [("--index-url", known.index_url)] if known.index_url else []
    options += [("--extra-index-url", value) for value in known.extra_index_url]
    options += [("--find-links", value) for value in known.find_links]
    options += [("--trusted-host", value) for value in known.trusted_host]
    return options + ([("--no-index", "")] if known.no_index else [])


def _uv_index_options(text: str) -> list[tuple[str, str]]:
    """`uv tool` keeps the index settings it used, from flags, variables and uv.toml."""
    recorded = tomllib.loads(text)["tool"].get("options", {})
    options = [("--index-url", recorded["index-url"])] if "index-url" in recorded else []
    options += [("--extra-index-url", value) for value in recorded.get("extra-index-url", ())]
    options += [("--find-links", value) for value in recorded.get("find-links", ())]
    for index in recorded.get("index", ()):
        # uv uses an explicit index only for a package pinned to it by name.
        if not index.get("explicit"):
            flag = "--extra-index-url"
            if index.get("format") == "flat":
                flag = "--find-links"
            elif index.get("default"):
                flag = "--index-url"
            options.append((flag, index["url"]))
    return options + ([("--no-index", "")] if recorded.get("no-index") else [])


def _carriable(flag: str, value: object) -> bool:
    """Whether pip and the printed plan can take this: an absolute place and nothing
    secret. A URL may hold a password or token; a relative path has lost its directory."""
    if not isinstance(value, str) or not value.isprintable():
        return False
    if flag == "--trusted-host":
        return _TRUSTED_HOST.fullmatch(value) is not None
    if flag == "--find-links" and "://" not in value and not value.startswith("file:"):
        return os.path.isabs(value)
    try:
        url = urlsplit(value)
    except ValueError:
        return False
    return (
        url.scheme in {"http", "https", "file"}
        and "@" not in url.netloc
        and not url.query
        and not url.fragment
    )


def recorded_index_args(source: str) -> tuple[str, ...]:
    """The index options this environment's installer recorded, as pip's arguments for
    the pinned source; () when it has no record, as plain pip and uvx never keep one.
    pip's own settings (pip.conf, PIP_* variables) reach the new pip by themselves. A
    record that cannot be read or carried stops the plan."""
    how = (
        "Nothing was changed. Pass --source with a checkout or a wheel file; "
        f"`pip download --no-deps {source}`, with that index, fetches the wheel"
    )
    for name, read in (
        ("pipx_metadata.json", _pipx_index_options),
        ("uv-receipt.toml", _uv_index_options),
    ):
        path = Path(sys.prefix) / name
        try:
            options = read(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, AttributeError, KeyError, TypeError, ValueError):
            raise PlanError(
                f"Cannot read {path}, which records where this copy of proton-mail-mcp came "
                f"from, so pip cannot be pointed at the same place. {how}"
            ) from None
        if not all(flag == "--no-index" or _carriable(flag, value) for flag, value in options):
            # The value itself is never shown: it may be a password.
            raise PlanError(
                f"{path} records an index or directory for this copy of proton-mail-mcp that "
                "pip cannot be pointed at: a URL with a user name, password or query, which "
                f"the printed plan would show, or a relative path. {how}"
            )
        return tuple(word for option in options for word in option if word)
    return ()


class _Parser(argparse.ArgumentParser):
    """argparse, whose usage errors under --json also come as the one JSON document on stdout,
    so an agent's parser never meets an empty stdout."""

    def __init__(self, *args, json_errors: bool = False, **kwargs) -> None:
        # No abbreviations: --js would be --json to argparse, but not to the check for it.
        super().__init__(*args, allow_abbrev=False, **kwargs)
        self.json_errors = json_errors

    def error(self, message: str):
        if not self.json_errors:
            super().error(message)
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        document = {
            "schema": REPORT_SCHEMA,
            "command": None,
            "result": "refused",
            "exit_code": 2,
            "steps": [],
            "needs_user": [],
            "next": _NEXT["refused"],
            "error": message,
        }
        print(json.dumps(document, indent=2), flush=True)
        raise SystemExit(2)


def build_parser(prog: str = "install", json_errors: bool = False) -> argparse.ArgumentParser:
    parser = _Parser(
        prog=prog,
        description="Install or remove Proton Mail Bridge, proton-mail-mcp and their services.",
        json_errors=json_errors,
    )
    actions = parser.add_subparsers(dest="action", required=True)
    unattended = argparse.ArgumentParser(add_help=False)
    unattended.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    unattended.add_argument("--yes", "-y", action="store_true", help="apply without asking")
    unattended.add_argument(
        "--simulate", choices=FAMILIES, help="plan for another platform (needs --dry-run)"
    )
    unattended.add_argument(
        "--non-interactive",
        action="store_true",
        help="ask nothing and open no prompt; what needs a person stops the run first",
    )
    unattended.add_argument(
        "--json",
        action="store_true",
        help="as --non-interactive, with one JSON result on stdout (progress on stderr)",
    )
    common = argparse.ArgumentParser(add_help=False, parents=[unattended])
    common.add_argument("--bridge-user", help="Bridge IMAP username (from Bridge's `info`)")
    common.add_argument("--credential-store", choices=STORES)
    common.add_argument(
        "--force", action="store_true", help="replace files the installer did not write (.bak kept)"
    )
    common.add_argument(
        "--venv", help="virtual environment path: new or installer-made; later runs reuse it"
    )
    target = argparse.ArgumentParser(add_help=False)
    target.add_argument("--port", type=int, default=8765, help="MCP port on 127.0.0.1")
    target.add_argument("--bridge-imap-port", type=int, default=1143)
    target.add_argument("--bridge-version", default=BRIDGE_VERSION, help="Linux .deb/.rpm only")
    target.add_argument("--bridge-bin", help="Bridge executable for the Bridge service")
    target.add_argument("--source", help="proton-mail-mcp checkout or wheel file to install")
    target.add_argument("--skip-bridge", action="store_true", help="do not install Bridge")
    target.add_argument("--skip-credentials", action="store_true")
    target.add_argument("--skip-services", action="store_true")
    target.add_argument(
        "--skip-bridge-service",
        action="store_true",
        help="leave Bridge to its own autostart (desktop app users)",
    )
    target.add_argument(
        "--replace-credentials", action="store_true", help="store new credentials over old ones"
    )
    target.add_argument(
        "--client",
        action="append",
        dest="clients",
        choices=CLIENTS,
        help="also add this server to this MCP client (repeatable)",
    )
    target.add_argument(
        "--python", help="Python 3.12+ for the service's virtual environment, outside uv's cache"
    )
    shown = argparse.ArgumentParser(add_help=False)
    shown.add_argument(
        "--print-config",
        action="store_true",
        help="print each --client's config entry (the token, where one is needed, as a "
        "placeholder) and change nothing",
    )
    add = functools.partial(actions.add_parser, json_errors=json_errors)
    add("install", parents=[common, target], help="install or update")
    setup = add(
        "setup",
        parents=[common, target, shown],
        help="guided install: Bridge check or install, login, password, services, self-check",
    )
    setup.add_argument(
        "--skip-bridge-login", action="store_true", help="do not offer to open Bridge's CLI"
    )
    setup.add_argument("--attachments-dir", help="the one folder attachment tools may use")
    uninstall = add(
        "uninstall", parents=[common], help="stop and remove services and settings (keeps the venv)"
    )
    uninstall.add_argument("--remove-credentials", action="store_true")
    uninstall.add_argument("--remove-bridge", action="store_true")
    configure = add(
        "configure-client",
        parents=[unattended, shown],
        help="add this server to an MCP client's config",
    )
    configure.add_argument(
        "--client", action="append", dest="clients", choices=CLIENTS, required=True
    )
    configure.add_argument("--port", type=int, help="MCP port (default: the one setup saved)")
    configure.add_argument("--bridge-user")
    configure.add_argument("--credential-store", choices=STORES)
    show = add("show-token", help="print the stored MCP bearer token, to a terminal only")
    show.add_argument("--bridge-user")
    show.add_argument("--credential-store", choices=STORES)
    show.add_argument(
        "--copy",
        action="store_true",
        help="put it on the clipboard instead, at a terminal too; nothing is printed",
    )
    detect = add("detect", help="show what the installer would target")
    detect.add_argument("--json", action="store_true")
    detect.add_argument("--simulate", choices=FAMILIES)
    return parser


def _detect_report(platform: Platform) -> dict:
    report = {**asdict(platform), "system": platform.system, "supported": platform.supported}
    if not platform.supported:
        report["refusal"] = platform.refusal
        return report
    probe = HostProbe(platform) if platform == detect_host() else simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    try:
        store = choose_store(platform, Options(), probe, {})
    except PlanError:
        store = None
    report.update(
        credential_store=store,
        service_manager=SERVICE_MANAGERS[platform.family],
        env_file=paths.env_file,
        venv=paths.venv,
        state_dir=paths.state_dir,
    )
    return report


def _elevated() -> bool:
    """Whether this is an elevated Windows shell."""
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _windows_refusal(args: argparse.Namespace) -> str | None:
    """What stops a real run on Windows, because the service could not start afterwards."""
    if sys.platform != "win32":
        return None
    if args.action in {"install", "setup"} and "\\WindowsApps\\" in sys.base_prefix:
        return (
            "Python from the Microsoft Store keeps %LOCALAPPDATA% in a folder of its own, where a "
            "logon task cannot find the service; use Python from python.org or winget"
        )
    # The files would belong to Administrators, whose rights a normal logon task does not use.
    if not args.dry_run and _elevated():
        return (
            "run in a normal PowerShell window, not as administrator; winget asks for approval "
            "itself when Bridge needs it"
        )
    return None


def _leave_current_folder() -> None:
    """Windows looks for a program in the current folder before System32, so a powershell.exe
    planted here would run, and would pass the check of Bridge's signature."""
    if sys.platform == "win32":
        os.chdir(os.environ.get("SYSTEMROOT") or os.path.expanduser("~"))


def _interpreter() -> str:
    """The Python the service's environment is made from: this run's base Python, not the
    environment uvx or pipx made around it, and outside uv's cache where one links out."""
    base = getattr(sys, "_base_executable", "") or sys.executable
    return service_python(base, HostProbe(detect_host()))


def _given_python(python: str) -> str:
    """--python as a path: a bare name such as python3.12 is looked up on PATH."""
    if os.sep not in python and (os.altsep is None or os.altsep not in python):
        python = shutil.which(python) or python
    return os.path.abspath(python)


# Exits 0 for Python 3.12 or newer, which the service needs.
_NEW_ENOUGH = "import sys; raise SystemExit(sys.version_info < (3, 12))"


def _new_enough(python: str) -> bool:
    try:
        result = subprocess.run(
            [python, "-I", "-c", _NEW_ENOUGH],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _options(args: argparse.Namespace) -> Options:
    if args.action == "configure-client":
        return Options(
            bridge_user=args.bridge_user,
            credential_store=args.credential_store,
            clients=tuple(dict.fromkeys(args.clients)),
        )
    common = {
        "bridge_user": args.bridge_user,
        "credential_store": args.credential_store,
        "venv": args.venv,
        "python": _given_python(args.python) if getattr(args, "python", None) else _interpreter(),
        "dry_run": bool(args.dry_run),
    }
    if args.action == "uninstall":
        return Options(
            **common,
            remove_credentials=args.remove_credentials,
            remove_bridge=args.remove_bridge,
        )
    bridge_bin = args.bridge_bin
    if bridge_bin and not args.simulate:
        # Services start Bridge from their own working directory, not this one.
        bridge_bin = os.path.abspath(bridge_bin)
    # pip must read a path as a path, whatever its name looks like.
    source = os.path.abspath(args.source) if args.source else default_source()
    return Options(
        **common,
        port=args.port,
        imap_port=args.bridge_imap_port,
        bridge_version=args.bridge_version,
        bridge_bin=bridge_bin,
        source=source,
        source_args=recorded_index_args(source) if is_pinned(source) else (),
        skip_bridge=args.skip_bridge,
        skip_credentials=args.skip_credentials,
        skip_services=args.skip_services,
        skip_bridge_service=args.skip_bridge_service,
        replace_credentials=args.replace_credentials,
        setup=args.action == "setup",
        skip_bridge_login=getattr(args, "skip_bridge_login", False),
        clients=tuple(dict.fromkeys(args.clients or ())),
    )


def _ask_account() -> str:
    """Ask for the Bridge username. A rejected answer is never shown again: a password
    pasted here by mistake must not reach the plan, the settings file or the screen."""
    label = "Bridge IMAP username (your Proton address; Bridge's `info` command shows it): "
    for _attempt in range(3):
        answer = pasted(input(label))
        if _ADDRESS.fullmatch(answer):
            try:
                return credential_store.validate_account(answer)
            except credential_store.CredentialStoreError:
                pass
        print("That is not an address like you@proton.me; it was not kept.", file=sys.stderr)
    raise PlanError("No Bridge username given. Nothing was changed; pass --bridge-user")


def _attachments_path(value: str, home: str, flavor=os.path) -> str | None:
    """A folder as a person gave it, in the form the server requires; None if it cannot be."""
    value = value.strip()
    if value == "~" or value.startswith("~" + flavor.sep):
        value = home + value[1:]
    path = flavor.normpath(value)
    # isprintable: other line breaks would split the env file's line, and bytes that are
    # not UTF-8 could not be written to it.
    return path if valid_attachments_dir(path, flavor) and path.isprintable() else None


def _choose_attachments(
    args: argparse.Namespace,
    platform: Platform,
    probe: Probe,
    settings: dict[str, str],
    can_ask: bool,
) -> tuple[str | None, Note]:
    """The folder to write to the env file, if any, and a note naming the one in use.

    The service does not inherit this shell. So a folder that only this shell's settings
    give is written too, and one the service finds by itself is left to create on first save.
    """
    windows = platform.family == "windows"
    flavor = ntpath if windows else posixpath
    given = args.attachments_dir or probe.env.get(ATTACHMENTS_KEY, "")
    chosen = _attachments_path(given, probe.home, flavor) if given else None
    if given and not chosen:
        raise PlanError(
            f"{ATTACHMENTS_KEY} must be an absolute path below the filesystem root, "
            "in printable characters"
        )
    saved = settings.get(ATTACHMENTS_KEY)
    service_default = default_attachments(platform, probe)
    shell_default = (
        service_default if windows else default_attachments_dir({**probe.env, "HOME": probe.home})
    )
    folder = chosen or saved or shell_default or service_default
    if can_ask and not args.attachments_dir:
        for _attempt in range(3):
            answer = input(f"Folder for attachment files ({ATTACHMENTS_KEY}) [{folder}]: ").strip()
            typed = _attachments_path(answer, probe.home, flavor) if answer else folder
            if typed:
                folder = typed
                break
            print(
                "That is not an absolute path below /, in printable characters; it was not kept.",
                file=sys.stderr,
            )
        else:
            raise PlanError(
                f"No usable {ATTACHMENTS_KEY} given. Nothing was changed; pass --attachments-dir"
            )
    write = folder if folder != (saved or service_default) else None
    if write and probe.readlink(write) is not None:
        # The server never follows one, so every attachment call would refuse.
        raise PlanError(f"{ATTACHMENTS_KEY} must not be a symbolic link. Nothing was changed")
    # A simulated plan has no profile of the host it would run on to check.
    refusal = localfs.path_refusal(write) if write and not args.simulate else None
    if refusal:
        raise PlanError(f"{ATTACHMENTS_KEY} {refusal}. Nothing was changed")
    moved = " Files in the earlier folder are not moved." if saved and write else ""
    return write, Note(
        f"Attachments folder ({ATTACHMENTS_KEY}): {folder}. Run setup with --attachments-dir "
        f"to change it.{moved}"
    )


def _setup_plan(
    args: argparse.Namespace, platform: Platform, options: Options, probe: Probe, paths: Paths
) -> list[Step]:
    """The install plan, asking a person what install stops for and takes as a flag.

    On a terminal, an outline of the plan comes first, then the questions, then the full
    plan. A dry run asks nothing and changes nothing: it shows a placeholder username and
    the steps that a first setup takes. Without a terminal, with --yes or with --json, the
    refusals stand.
    """
    _text, settings = existing_settings(paths, probe)
    can_ask = not args.dry_run and not args.yes and not _unattended(args) and on_terminal()
    if can_ask:
        _outline(platform, options, probe, paths, settings)
    notes: list[Step] = []
    if not options.bridge_user and not settings.get("PROTON_BRIDGE_USER"):
        if can_ask:
            account = _login_first(platform, options, probe, paths)
            options = replace(
                options,
                bridge_user=account or _ask_account(),
                # Just logged in: the plan need not offer it again.
                skip_bridge_login=options.skip_bridge_login or bool(account),
            )
        else:
            options = replace(options, bridge_user=PLACEHOLDER_ACCOUNT, replace_credentials=True)
            notes.append(LOGIN_NEED)
            if args.dry_run:
                notes.append(
                    Note(
                        f"No --bridge-user yet, so this plan shows {PLACEHOLDER_ACCOUNT}. At a "
                        "terminal, setup reads the username from Bridge's login where Bridge is "
                        "installed already, and asks for it otherwise."
                    )
                )
    attachments_dir, attachments_note = _choose_attachments(
        args, platform, probe, settings, can_ask
    )
    options = replace(options, attachments_dir=attachments_dir)
    notes.append(attachments_note)
    try:
        steps = install_plan(platform, options, probe, paths)
    except CredentialsUnknown:
        if args.dry_run:
            notes.append(
                Note(
                    "The credential store cannot show whether credentials are stored already, "
                    "so when applied setup asks before storing new ones over them."
                )
            )
        elif not can_ask or not _confirm_new_credentials():
            raise
        options = replace(options, store_unreadable=True)
        steps = install_plan(platform, options, probe, paths)
    if can_ask:
        steps = _settle_bridge_app(platform, options, probe, paths, steps)
    return [*notes, *steps]


LOGIN_NEED = NeedsUser(
    "bridge_login",
    "Setup does not know the Bridge account yet, and only a person can log in to Bridge. They "
    "run the command below in their own terminal: setup opens Bridge's CLI there, where they "
    "type their Proton email and password (pasting works) and the two-factor code, and open any "
    "verification link Bridge prints in a browser. Setup then reads the IMAP username from "
    "Bridge, takes the Bridge password at a hidden prompt and does the rest. Or pass "
    "--bridge-user with the username Bridge's `info` shows.",
    when="before",
    blocking=True,
    terminal=True,
)


def _login_first(platform: Platform, options: Options, probe: Probe, paths: Paths) -> str:
    """Where Bridge is installed but no account is known yet, a person logs in first, in
    Bridge's own CLI, and setup reads the IMAP username from it; "" to ask for it instead."""
    if options.skip_bridge or options.skip_bridge_login or not bridge_cli.available():
        return ""
    exe = bridge_executable(platform, options, probe)
    if not probe.exists(exe):
        return ""
    if probe.port_open(options.imap_port) or probe.succeeds(bridge_processes(platform, probe)):
        print("Bridge is running, so its CLI cannot open here; setup asks for the username.")
        return ""
    answer = input(
        "Bridge is installed, and setup does not know your Bridge account yet. Log in now, in "
        "Bridge's CLI here, so setup can read your IMAP username from it? [Y/n] "
    )
    if answer.strip().lower() in {"n", "no"}:
        return ""
    print(bridge_cli.INSTRUCTIONS)
    try:
        status, username = bridge_cli.login_session((exe, "--cli"))
    except OSError:
        print(f"Bridge's CLI ({exe}) could not start; setup asks for the username instead.")
        return ""
    try:
        account = credential_store.validate_account(username) if username else ""
    except credential_store.CredentialStoreError:
        account = ""
    if not account:
        print(
            f"Bridge's CLI ended (status {status}) without showing an IMAP username; type it "
            "below, as Bridge's `info` shows it."
        )
        return ""
    print(f"Bridge's info shows the IMAP username {account}; setup uses it.")
    return account


def _outline(
    platform: Platform, options: Options, probe: Probe, paths: Paths, settings: dict[str, str]
) -> None:
    """What setup does, before it asks anything; the full plan follows the answers."""
    preview = replace(options, store_unreadable=True)
    if not options.bridge_user and not settings.get("PROTON_BRIDGE_USER"):
        # Like a dry run: no store is asked about the placeholder.
        preview = replace(preview, bridge_user=PLACEHOLDER_ACCOUNT, replace_credentials=True)
    steps = install_plan(platform, preview, probe, paths)
    print(f"Setup on {platform.name} will:")
    number = 0
    for step in steps:
        if isinstance(step, NeedsUser):
            print(f"  ! For you: {step.text}")
        elif not isinstance(step, Note):
            number += 1
            print(f"  {number}. {step.summary}")
    print(
        "It asks its questions next, then shows the full plan with every command; nothing "
        "changes before you approve it."
    )


def _settle_bridge_app(
    platform: Platform, options: Options, probe: Probe, paths: Paths, steps: list[Step]
) -> list[Step]:
    """Wait for a person to quit another Bridge, or to keep it running Bridge instead."""
    for _attempt in range(3):
        need = next((step for step in steps if _is_need(step, "quit_bridge_app")), None)
        if need is None:
            return steps
        print(f"\n{need.text}")
        answer = input(
            "Press Enter once it has quit, to check again, or type k to keep it running "
            "Bridge (as --skip-bridge-service): "
        )
        if answer.strip().lower() in {"k", "keep"}:
            options = replace(options, skip_bridge_service=True)
        steps = install_plan(platform, options, probe, paths)
    if any(_is_need(step, "quit_bridge_app") for step in steps):
        raise PlanError(
            "Another Bridge still runs. Nothing was changed. Quit it and rerun, or rerun with "
            "--skip-bridge-service to keep it"
        )
    return steps


def _is_need(step: Step, name: str) -> bool:
    return isinstance(step, NeedsUser) and step.id == name


def _unattended(args: argparse.Namespace) -> bool:
    """--json and --non-interactive: nothing is asked, and no prompt opens."""
    return bool(getattr(args, "json", False) or getattr(args, "non_interactive", False))


def _stops(steps: list[Step], args: argparse.Namespace, probe: Probe) -> list[NeedsUser]:
    """What a person must do before this run may change anything: a blocking need, or one
    at a terminal this run cannot or must not use."""
    stops = []
    for need in (step for step in steps if isinstance(step, NeedsUser)):
        if need.blocking:
            stops.append(need)
        elif need.terminal and _unattended(args):
            # sudo asks nothing while it still holds this user's credentials.
            if not (need.id == "sudo" and probe.succeeds(SUDO_CACHED)):
                stops.append(need)
        elif need.id == "bridge_password" and args.action == "setup" and not terminal_available():
            stops.append(need)
    return stops


def _stop_text(need: NeedsUser, args: argparse.Namespace, handoff: str) -> str:
    if need.id == "bridge_login":
        return f"{need.text} Nothing was changed. The command: {handoff}"
    if need.id != "bridge_password":
        return f"{need.text} Nothing was changed"
    where = (
        "--json and --non-interactive never open one" if _unattended(args) else "there is none here"
    )
    return (
        f"The Bridge password can only be typed at a terminal, and {where}. Nothing was "
        "changed. Run setup in a terminal, or add --skip-credentials and run setup in a "
        "terminal later"
    )


def handoff_launcher() -> list[str]:
    """How a person starts this same copy in their own terminal. Under uvx it is not on PATH,
    so uvx and the source it came from; otherwise the script that runs, by absolute path."""
    if in_uv_cache(sys.prefix, os.environ):
        source = default_source()
        if source:
            return ["uvx", "--from", source, "proton-mail-mcp"]
    script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    names = ("proton-mail-mcp", "mcp-proton-mail")
    if os.path.basename(script).startswith(names) and os.path.isfile(script):
        return [script]
    return ["proton-mail-mcp"]


def _handoff(argv: list[str], windows: bool) -> str:
    """This command as a person runs it in their own terminal, where it may ask."""
    words: list[str] = []
    rest = iter(argv)
    for word in rest:
        if word == "--simulate":
            next(rest, None)
        elif word not in _UNATTENDED and not word.startswith("--simulate="):
            words.append(word)
    return command_line([*handoff_launcher(), *words], windows)


class _Report:
    """The one JSON document --json prints on stdout. It holds step summaries, notes and
    needs, which never carry a secret; human progress goes to stderr meanwhile."""

    def __init__(self, args: argparse.Namespace, platform: Platform, argv: list[str]) -> None:
        self.enabled = bool(getattr(args, "json", False))
        self.args = args
        self.handoff = _handoff(argv, platform.family == "windows")
        self.planned: list[dict[str, object]] = []
        self.needs: list[NeedsUser] = []
        self.data: dict[str, object] = {
            "schema": REPORT_SCHEMA,
            "command": args.action,
            "dry_run": bool(getattr(args, "dry_run", False)),
            "platform": {
                "family": platform.family,
                "name": platform.name,
                "machine": platform.machine,
            },
            "launcher": command_line(handoff_launcher(), platform.family == "windows"),
        }

    def plan(self, steps: list[Step], executor: Executor) -> None:
        self.planned = [dict(record) for record in executor.records]
        self.needs = [step for step in steps if isinstance(step, NeedsUser)]
        self.data["notes"] = [step.text for step in steps if isinstance(step, Note)]

    def finish(
        self,
        code: int,
        result: str,
        *,
        error: str = "",
        executor: Executor | None = None,
        stops: list[NeedsUser] | tuple = (),
    ) -> int:
        if not self.enabled:
            return code
        steps = self.planned
        if executor is not None:
            applied = {record["n"]: record for record in executor.records}
            steps = [
                {**record, "status": applied.get(record["n"], {}).get("status", "not_run")}
                for record in self.planned
            ]
            self.data["warnings"] = list(executor.warnings)
        document = {
            **self.data,
            "result": result,
            "exit_code": code,
            "steps": steps,
            "needs_user": [self._need(need, need in stops) for need in self.needs],
            "next": _NEXT[result],
        }
        if error:
            document["error"] = error
        print(json.dumps(document, indent=2), flush=True)
        return code

    def _need(self, need: NeedsUser, stops: bool) -> dict[str, object]:
        return {
            "id": need.id,
            "when": need.when,
            "stops_run": stops,
            "blocking": need.blocking,
            "needs_terminal": need.terminal,
            "text": need.text,
            "command": self.handoff if need.terminal else need.command,
        }

    def refuse(self, prog: str, error: str, code: int = 2) -> int:
        print(f"{prog}: {error}", file=sys.stderr)
        return self.finish(code, "refused", error=error)


REPORT_SCHEMA = "proton-mail-mcp/installer-report/1"
_NEXT = {
    "planned": (
        "Nothing was changed. Tell the user about each needs_user entry first; stops_run says "
        "which would stop this command run with --yes and --json. An entry with "
        "needs_terminal is done by the user running its command in their own terminal; "
        "otherwise apply this command with --yes."
    ),
    "printed": (
        "Nothing was changed. Each configs entry is what that client needs; a token in one is "
        "a placeholder, which configure-client replaces with the real one when it writes it."
    ),
    "needs_user": (
        "Nothing was changed. A person must act first: relay each needs_user entry whose "
        "stops_run is true. For one with needs_terminal, the user runs its command in their "
        "own terminal; otherwise rerun this command once it is done."
    ),
    "refused": "Nothing was changed. error says why and what to do.",
    "declined": "Nothing was changed.",
    "failed": "Fix the cause in error and rerun; completed steps are skipped or repeated safely.",
    "interrupted": "Rerun to continue; completed steps are skipped or repeated safely.",
    "done": (
        'Relay the needs_user entries whose when is "after", such as restarting the client. '
        "connector is how any other MCP client on this computer reaches the server."
    ),
}


def _confirm_new_credentials() -> bool:
    print(
        "The credential store cannot show whether a Bridge password or MCP token is stored "
        "already: a first setup and a locked store look the same."
    )
    answer = input(
        "Store new ones for what it cannot show, replacing any there? On a later setup, a new "
        "token means updating your MCP clients. [y/N] "
    )
    return answer.strip().lower() in {"y", "yes"}


def _show_token(args: argparse.Namespace, platform: Platform, prog: str = "install") -> int:
    """Reveal the token on explicit request; Credential Manager's UI cannot show it. With
    --copy it goes to the clipboard instead, and nothing prints it. Either way only at a
    terminal, where a person asked: a clipboard is as readable as a pipe to any process."""
    if args.copy and not on_terminal():
        print(
            f"{prog}: show-token --copy works only at a terminal. To give a client the server, "
            "use configure-client: clients start connect, which reads the token itself",
            file=sys.stderr,
        )
        return 2
    if not args.copy and not sys.stdout.isatty():
        print(f"{prog}: show-token prints only to a terminal, not a pipe or file", file=sys.stderr)
        return 2
    probe = HostProbe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    try:
        _text, settings = existing_settings(paths, probe)
        account = bridge_account(Options(bridge_user=args.bridge_user), settings)
        options = Options(credential_store=args.credential_store)
        store = choose_store(platform, options, probe, settings)
        # The pass store and GnuPG home the service uses, not just this shell's.
        locations = credential_locations(store, probe, settings, prefer_saved=True)
        token = credential_store.lookup(store, TOKEN_SERVICE, account, locations)
    except (PlanError, credential_store.CredentialStoreError) as error:
        print(f"{prog}: {error}", file=sys.stderr)
        return 2
    if not token:
        print(f"{prog}: the stored MCP token is empty; rerun the installer", file=sys.stderr)
        return 2
    if args.copy:
        tool = copy_to_clipboard(token, platform.system)
        if not tool:
            names = ", ".join(argv[0] for argv in _CLIPBOARDS[platform.system])
            print(
                f"{prog}: no clipboard program worked here ({names}); run show-token in a "
                "terminal instead, or configure-client to write the token into a client",
                file=sys.stderr,
            )
            return 2
        synced = (
            " Universal Clipboard may also hand it to your other Apple devices."
            if platform.system == "macos"
            else ""
        )
        print(
            f"Copied the MCP bearer token to the clipboard with {tool}; it was not shown. Paste "
            "it into your client's secret storage, then copy something else: a clipboard "
            f"history may keep it.{synced}"
        )
        return 0
    print("MCP bearer token (secret; paste it into your client's secret storage):", file=sys.stderr)
    print(token)
    return 0


def copy_to_clipboard(token: str, system: str) -> str | None:
    """The first clipboard program that took the token on stdin, or None. The token never
    reaches an argument, and nothing it prints is read: xclip and wl-copy stay running to
    hold the clipboard, so their output is not waited for."""
    for argv in _CLIPBOARDS[system]:
        if not shutil.which(argv[0]):
            continue
        try:
            result = subprocess.run(
                list(argv),
                input=token,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return argv[0]
    return None


def _shared_folder(paths: Paths) -> str | None:
    """What the service would refuse at every start on Windows, said before anything is written."""
    for folder in (paths.config_dir, paths.data_dir, paths.state_dir, paths.log_dir):
        refusal = localfs.path_refusal(folder) if folder else None
        if refusal:
            return f"{folder} {refusal}. Nothing was changed"
    return None


def _client_target(
    args: argparse.Namespace, platform: Platform, probe: Probe, paths: Paths
) -> tuple[Paths, int]:
    """The service environment and port clients reach: as given, else as setup saved them."""
    _text, settings = existing_settings(paths, probe)
    given = getattr(args, "venv", None)
    saved_in = f"{VENV_KEY} in {paths.env_file}"
    venv = given or settings.get(VENV_KEY)
    located = with_venv(platform, paths, venv, "--venv" if given else saved_in)
    if args.port:
        if not 1 <= args.port <= 65535:
            raise PlanError("--port must be between 1 and 65535")
        return located, args.port
    saved = settings.get("PROTON_MCP_PORT", "")
    if not saved:
        return located, Options().port
    if not (saved.isdigit() and 1 <= int(saved) <= 65535):
        raise PlanError(
            f"PROTON_MCP_PORT in {paths.env_file} is not a port from 1 to 65535; fix it there, "
            "or rerun setup with --port"
        )
    return located, int(saved)


def _print_config(
    args: argparse.Namespace, platform: Platform, probe: Probe, paths: Paths, report: "_Report"
) -> int:
    """Each client's entry as a person or agent adds it by hand; no token is read."""
    windows = platform.family == "windows"
    located, port = _client_target(args, platform, probe, paths)
    command = clients.connect_command(launcher(platform, located), located.env_file)
    configs = []
    names = tuple(dict.fromkeys(args.clients or CLIENTS))
    notes = []
    if "chatgpt" in names and "codex" in names:
        # One file, one table: pasting both entries would make it invalid.
        names = tuple(name for name in names if name != "codex")
        notes.append(
            "ChatGPT and Codex share ~/.codex/config.toml and one proton-mail table there, so "
            "only ChatGPT's entry is shown; Codex uses it too. --client codex alone shows "
            "Codex's connect entry."
        )
    for name in names:
        client = CLIENTS[name]
        server = clients.entry(name, port, clients.TOKEN_PLACEHOLDER, command)
        config: dict[str, object] = {
            "client": name,
            "label": client.label,
            "path": clients.config_path(name, platform.family, probe.env, probe.home) or None,
            "format": "toml" if client.form == "toml" else "json",
            "snippet": clients.snippet(name, server),
        }
        if client.native:
            add = clients.native_commands(name, client.native, command)[1]
            config["command"] = command_line(add, windows)
        if name == "cursor":
            config["link"] = clients.cursor_link(server)
        configs.append(config)
    if report.enabled:
        report.data["configs"] = configs
        report.data["notes"] = notes
        report.data["connector"] = connector(platform, located, port)
        return report.finish(0, "printed")
    for config in configs:
        print(f"# {config['label']}: {config['path'] or 'its MCP servers'}")
        print(config["snippet"], end="")
        if "command" in config:
            print(f"# or, with its own command line: {config['command']}")
        if "link" in config:
            print(f"# or open this link: {config['link']}")
        print()
    for note in notes:
        print(note)
    print(
        "Nothing was changed. A token shown is a placeholder: configure-client writes the real "
        "one, and connect reads it itself."
    )
    return 0


def main(argv: list[str] | None = None, prog: str = "install") -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in {"-h", "--help"}):
        argv.insert(0, "install")
    json_errors = any(word.partition("=")[0] == "--json" for word in argv)
    parser = build_parser(prog, json_errors=json_errors)
    args = parser.parse_args(argv)
    simulate = getattr(args, "simulate", None)
    platform = simulated(simulate) if simulate else detect_host()
    if args.action == "detect":
        report = _detect_report(platform)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            for key, value in report.items():
                print(f"{key}: {value}")
        return 0
    if args.action == "show-token":
        if not platform.supported:
            print(f"{prog}: {platform.refusal}", file=sys.stderr)
            return 2
        return _show_token(args, platform, prog)
    printing = getattr(args, "print_config", False)
    if args.simulate and not (args.dry_run or printing):
        parser.error("--simulate only plans; add --dry-run")
    if _unattended(args) and not (args.dry_run or args.yes or printing):
        parser.error("--json and --non-interactive ask nothing: add --dry-run or --yes")
    report = _Report(args, platform, argv)
    if not platform.supported:
        return report.refuse(prog, platform.refusal)
    if args.action in {"install", "setup"} and args.source:
        source = Path(args.source)
        if not is_source(source) and not is_wheel(source):
            parser.error(
                "--source must be a checkout whose pyproject.toml names mcp-proton-mail, "
                "or a wheel file named mcp_proton_mail-<version>-py3-none-any.whl"
            )
    if args.action in {"install", "setup"} and args.python and not printing:
        python = _given_python(args.python)
        # A simulated plan has no such host to run the interpreter on.
        if not os.path.isfile(python) or not (args.simulate or _new_enough(python)):
            parser.error("--python must name a Python 3.12 or newer interpreter")
    if not args.simulate and hasattr(os, "geteuid") and os.geteuid() == 0:
        return report.refuse(prog, "run as your normal user; package steps use sudo themselves")
    refusal = None if args.simulate else _windows_refusal(args)
    if refusal:
        return report.refuse(prog, refusal)
    probe = simulated_probe(platform) if args.simulate else HostProbe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    if printing:
        try:
            return _print_config(args, platform, probe, paths, report)
        except PlanError as error:
            return report.refuse(prog, str(error))
    refusal = None
    if args.action in {"install", "setup"} and platform.family == "windows" and not args.simulate:
        refusal = _shared_folder(paths)
    if refusal:
        return report.refuse(prog, refusal)
    try:
        # After _options, which makes the paths given relative to this folder absolute.
        options = _options(args)
        if not args.simulate:
            _leave_current_folder()
        if args.action == "setup":
            steps = _setup_plan(args, platform, options, probe, paths)
        elif args.action == "configure-client":
            steps = configure_plan(platform, options, probe, paths, args.port)
        else:
            build = install_plan if args.action == "install" else uninstall_plan
            steps = build(platform, options, probe, paths)
        # A dry run reports what would stop the same command run with --yes.
        stops = _stops(steps, args, probe)
        if args.action in {"setup", "configure-client"}:
            report.data["connector"] = connector(
                platform, *_client_target(args, platform, probe, paths)
            )
    except PlanError as error:
        return report.refuse(prog, str(error))
    except (EOFError, KeyboardInterrupt):
        print(f"\n{prog}: interrupted; nothing was changed.", file=sys.stderr)
        return report.finish(130, "interrupted", error="interrupted; nothing was changed")
    if stops and not report.enabled and not args.dry_run:
        # Said before the plan: nothing of it runs until a person has done these.
        for need in stops:
            print(f"{prog}: {_stop_text(need, args, report.handoff)}", file=sys.stderr)
        return 2
    # With --json, stdout carries only the JSON document.
    out = sys.stderr if report.enabled else sys.stdout
    quiet = {"out": out} if report.enabled else {}
    print(
        f"{args.action} plan for {platform.name} ({platform.family}, {platform.machine})",
        file=out,
    )
    windows = platform.family == "windows"
    force = getattr(args, "force", False)
    # An unattended run never lets sudo ask: one whose cached login ran out fails instead.
    sudo = ("sudo", "-n") if _unattended(args) else ("sudo",)
    executor = None
    planned = Executor(dry_run=True, force=force, windows=windows, sudo=sudo, **quiet)
    try:
        planned.run(steps)
    except InstallError as error:
        # The plan refuses before any step runs, as a file a step could not merge: the same
        # with --dry-run as with --yes.
        report.plan(steps, planned)
        print(f"{prog}: {error}", file=sys.stderr)
        print("Nothing was changed.", file=out)
        return report.finish(2, "refused", error=str(error), executor=planned)
    try:
        report.plan(steps, planned)
        if args.dry_run:
            print("Dry run: nothing was changed.", file=out)
            return report.finish(0, "planned", stops=stops, executor=planned)
        if stops:
            for need in stops:
                print(f"{prog}: {_stop_text(need, args, report.handoff)}", file=sys.stderr)
            return report.finish(2, "needs_user", stops=stops)
        if not args.yes:
            if not (sys.stdin and sys.stdin.isatty()):
                print(f"{prog}: confirm with --yes when not on a terminal", file=sys.stderr)
                return 2
            if input("Apply these steps? [y/N] ").strip().lower() not in {"y", "yes"}:
                print("Nothing was changed.")
                return 1
        if _unattended(args):
            # No prompt opens: anything that needed one stopped the run above.
            quiet["terminal"] = lambda: False
        session = bridge_cli.login_session if bridge_cli.available() else None
        executor = Executor(
            dry_run=False,
            force=force,
            windows=windows,
            assume_yes=args.yes,
            sudo=sudo,
            bridge_session=session,
            **quiet,
        )
        executor.run(steps)
    except InstallError as error:
        print(f"{prog}: {error}", file=sys.stderr)
        print("Fix the cause and rerun; completed steps are skipped or repeated safely.", file=out)
        return report.finish(1, "failed", error=str(error), executor=executor)
    except (EOFError, KeyboardInterrupt):
        print(f"\n{prog}: interrupted; rerun to continue.", file=sys.stderr)
        return report.finish(130, "interrupted", error="interrupted", executor=executor)
    print(
        f"{args.action} finished"
        + (f" with {len(executor.warnings)} warning(s)." if executor.warnings else "."),
        file=out,
    )
    return report.finish(0, "done", executor=executor)
