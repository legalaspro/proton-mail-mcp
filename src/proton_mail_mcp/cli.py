"""The `proton-mail-mcp` command: the MCP server by default, plus the installer.

Nothing here imports the server at load: it needs POSIX fcntl, and `setup` must reach
its Windows refusal on a host where that import fails.
"""

import sys
from importlib import metadata

_PROG = "proton-mail-mcp"
_INSTALLER_COMMANDS = ("setup", "uninstall", "show-token", "detect")
_HELP = f"""\
usage: {_PROG} [serve | setup | uninstall | show-token | detect] [options]

  serve       start the MCP server from PROTON_* settings (the default without a command)
  setup       guided install: Bridge check or install, login, Bridge password, services,
              self-check; --dry-run prints the plan and changes nothing
  uninstall   stop and remove the services and settings
  show-token  print the stored MCP bearer token, to a terminal only
  detect      show what the installer would target

setup, uninstall, show-token and detect each take --help.
"""


def _serve() -> int:
    if sys.platform == "win32":
        from .installer.detect import NOT_YET_SUPPORTED

        print(f"{_PROG}: {NOT_YET_SUPPORTED['windows']}", file=sys.stderr)
        return 2
    from .server import main as serve

    try:
        serve()
    except SystemExit as error:
        # The server's own message says what is missing; this says where to get it.
        if error.code == 2:
            print(
                f"To set up Bridge, credentials and a service, run `{_PROG} setup`.",
                file=sys.stderr,
            )
        raise
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "serve"
    if command == "serve" and len(argv) <= 1:
        return _serve()
    if command in _INSTALLER_COMMANDS:
        from .installer.cli import main as installer

        return installer(argv, prog=_PROG)
    if command in {"-h", "--help"}:
        print(_HELP, end="")
        return 0
    if command == "--version":
        try:
            print(f"{_PROG} {metadata.version(_PROG)}")
        except metadata.PackageNotFoundError:
            print(f"{_PROG} (not installed; version unknown)")
        return 0
    print(_HELP, end="", file=sys.stderr)
    print(f"{_PROG}: unknown arguments: {' '.join(argv)}", file=sys.stderr)
    return 2
