"""Installer command line: setup, install, uninstall or detect, each with --dry-run."""

import argparse
import json
import ntpath
import os
import posixpath
import re
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
from .detect import FAMILIES, Platform, detect_host, simulated
from .execute import Executor, InstallError, on_terminal, terminal_available
from .plan import (
    ATTACHMENTS_KEY,
    BRIDGE_VERSION,
    CredentialsUnknown,
    Options,
    Paths,
    PlanError,
    Probe,
    bridge_account,
    choose_store,
    credential_locations,
    default_attachments,
    existing_settings,
    install_plan,
    is_pinned,
    paths_for,
    uninstall_plan,
)
from .probe import HostProbe, simulated_probe
from .steps import PACKAGE, Note, Step, StoreSecret

# Shown for the Bridge username in a dry run that has none yet; no store is asked about it.
PLACEHOLDER_ACCOUNT = "you@proton.me"
# Bridge's usernames are addresses. A password pasted at the question rarely looks like one.
_ADDRESS = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s.]+")
# pip's `--trusted-host`: a host name or address, with an optional port.
_TRUSTED_HOST = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:[0-9]+)?")
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
    return 'name = "proton-mail-mcp"' in text


def is_wheel(path: Path) -> bool:
    return path.is_file() and path.name.startswith("proton_mail_mcp-") and path.suffix == ".whl"


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


def build_parser(prog: str = "install") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Install or remove Proton Mail Bridge, proton-mail-mcp and their services.",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    common.add_argument("--yes", "-y", action="store_true", help="apply without asking")
    common.add_argument(
        "--simulate", choices=FAMILIES, help="plan for another platform (needs --dry-run)"
    )
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
    actions.add_parser("install", parents=[common, target], help="install or update")
    setup = actions.add_parser(
        "setup",
        parents=[common, target],
        help="guided install: Bridge check or install, login, password, services, self-check",
    )
    setup.add_argument(
        "--skip-bridge-login", action="store_true", help="do not offer to open Bridge's CLI"
    )
    setup.add_argument("--attachments-dir", help="the one folder attachment tools may use")
    uninstall = actions.add_parser(
        "uninstall", parents=[common], help="stop and remove services and settings (keeps the venv)"
    )
    uninstall.add_argument("--remove-credentials", action="store_true")
    uninstall.add_argument("--remove-bridge", action="store_true")
    show = actions.add_parser(
        "show-token", help="print the stored MCP bearer token, to a terminal only"
    )
    show.add_argument("--bridge-user")
    show.add_argument("--credential-store", choices=STORES)
    detect = actions.add_parser("detect", help="show what the installer would target")
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


def _options(args: argparse.Namespace) -> Options:
    common = {
        "bridge_user": args.bridge_user,
        "credential_store": args.credential_store,
        "venv": args.venv,
        "python": sys.executable,
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
    )


def _ask_account() -> str:
    """Ask for the Bridge username. A rejected answer is never shown again: a password
    pasted here by mistake must not reach the plan, the settings file or the screen."""
    label = "Bridge IMAP username (your Proton address; Bridge's `info` command shows it): "
    for _attempt in range(3):
        answer = input(label).strip()
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

    A dry run asks nothing and changes nothing: it shows a placeholder username and the
    steps that a first setup takes. Without a terminal or with --yes, the refusals stand.
    """
    _text, settings = existing_settings(paths, probe)
    can_ask = not args.dry_run and not args.yes and on_terminal()
    notes: list[Step] = []
    if not options.bridge_user and not settings.get("PROTON_BRIDGE_USER"):
        if args.dry_run:
            options = replace(options, bridge_user=PLACEHOLDER_ACCOUNT, replace_credentials=True)
            notes.append(
                Note(
                    f"No --bridge-user yet, so this plan shows {PLACEHOLDER_ACCOUNT}; setup "
                    "asks for the username when applied."
                )
            )
        elif can_ask:
            options = replace(options, bridge_user=_ask_account())
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
        steps = install_plan(platform, replace(options, store_unreadable=True), probe, paths)
    if not args.dry_run and not terminal_available():
        if any(isinstance(step, StoreSecret) and step.source == "prompt" for step in steps):
            raise PlanError(
                "The Bridge password can only be typed at a terminal, and there is none here. "
                "Nothing was changed. Run setup in a terminal, or add --skip-credentials and "
                "run setup in a terminal later"
            )
    return [*notes, *steps]


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
    """Reveal the token on explicit request; Credential Manager's UI cannot show it."""
    if not sys.stdout.isatty():
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
    print("MCP bearer token (secret; paste it into your client's secret storage):", file=sys.stderr)
    print(token)
    return 0


def _shared_folder(paths: Paths) -> str | None:
    """What the service would refuse at every start on Windows, said before anything is written."""
    for folder in (paths.config_dir, paths.data_dir, paths.state_dir, paths.log_dir):
        refusal = localfs.path_refusal(folder) if folder else None
        if refusal:
            return f"{folder} {refusal}. Nothing was changed"
    return None


def main(argv: list[str] | None = None, prog: str = "install") -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in {"-h", "--help"}):
        argv.insert(0, "install")
    parser = build_parser(prog)
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
    if not platform.supported:
        print(f"{prog}: {platform.refusal}", file=sys.stderr)
        return 2
    if args.action == "show-token":
        return _show_token(args, platform, prog)
    if args.simulate and not args.dry_run:
        parser.error("--simulate only plans; add --dry-run")
    if args.action in {"install", "setup"} and args.source:
        source = Path(args.source)
        if not is_source(source) and not is_wheel(source):
            parser.error("--source must be a proton-mail-mcp checkout or wheel file")
    if not args.simulate and hasattr(os, "geteuid") and os.geteuid() == 0:
        print(
            f"{prog}: run as your normal user; package steps use sudo themselves",
            file=sys.stderr,
        )
        return 2
    refusal = None if args.simulate else _windows_refusal(args)
    if refusal:
        print(f"{prog}: {refusal}", file=sys.stderr)
        return 2
    probe = simulated_probe(platform) if args.simulate else HostProbe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    refusal = None
    if args.action in {"install", "setup"} and platform.family == "windows" and not args.simulate:
        refusal = _shared_folder(paths)
    if refusal:
        print(f"{prog}: {refusal}", file=sys.stderr)
        return 2
    try:
        # After _options, which makes the paths given relative to this folder absolute.
        options = _options(args)
        if not args.simulate:
            _leave_current_folder()
        if args.action == "setup":
            steps = _setup_plan(args, platform, options, probe, paths)
        else:
            build = install_plan if args.action == "install" else uninstall_plan
            steps = build(platform, options, probe, paths)
    except PlanError as error:
        print(f"{prog}: {error}", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print(f"\n{prog}: interrupted; nothing was changed.", file=sys.stderr)
        return 130
    print(f"{args.action} plan for {platform.name} ({platform.family}, {platform.machine})")
    windows = platform.family == "windows"
    try:
        Executor(dry_run=True, force=args.force, windows=windows).run(steps)
        if args.dry_run:
            print("Dry run: nothing was changed.")
            return 0
        if not args.yes:
            if not (sys.stdin and sys.stdin.isatty()):
                print(f"{prog}: confirm with --yes when not on a terminal", file=sys.stderr)
                return 2
            if input("Apply these steps? [y/N] ").strip().lower() not in {"y", "yes"}:
                print("Nothing was changed.")
                return 1
        executor = Executor(dry_run=False, force=args.force, windows=windows, assume_yes=args.yes)
        executor.run(steps)
    except InstallError as error:
        print(f"{prog}: {error}", file=sys.stderr)
        print("Fix the cause and rerun; completed steps are skipped or repeated safely.")
        return 1
    except (EOFError, KeyboardInterrupt):
        print(f"\n{prog}: interrupted; rerun to continue.", file=sys.stderr)
        return 130
    print(
        f"{args.action} finished"
        + (f" with {len(executor.warnings)} warning(s)." if executor.warnings else ".")
    )
    return 0
