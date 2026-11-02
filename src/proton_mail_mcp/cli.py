"""The `proton-mail-mcp` command: the MCP server by default, plus the installer.

Nothing here imports the server at load, so the installer commands need only the
standard library.
"""

import sys
from importlib import metadata

_PROG = "proton-mail-mcp"
# The distribution on PyPI; the same entry point is also installed as `mcp-proton-mail`.
_DIST = "mcp-proton-mail"
_INSTALLER_COMMANDS = ("setup", "configure-client", "uninstall", "show-token", "detect")
_HELP = f"""\
usage: {_PROG} [serve | connect | setup | configure-client | uninstall | show-token |
                        detect] [options]

  serve             start the MCP server from PROTON_* settings (the default without a
                    command)
  connect           serve MCP on stdin and stdout for a client that starts local servers
                    as commands, relaying to the running service with the stored token,
                    which it never prints
  setup             guided install: Bridge check or install, login, Bridge password,
                    services, self-check; --dry-run prints the plan and changes nothing,
                    --client writes an MCP client's config, --json is for agents
  configure-client  add this server to an MCP client's config: the connect command, or for
                    ChatGPT the URL with the stored token
  uninstall         stop and remove the services and settings
  show-token        print the stored MCP bearer token, to a terminal only; --copy puts it
                    on the clipboard instead
  detect            show what the installer would target

Each command but serve takes --help.
"""


def _serve() -> int:
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
    if command == "connect":
        from .connect import main as connect

        return connect(argv[1:])
    if command in _INSTALLER_COMMANDS:
        from .installer.cli import main as installer

        return installer(argv, prog=_PROG)
    if command in {"-h", "--help"}:
        print(_HELP, end="")
        return 0
    if command == "--version":
        try:
            print(f"{_PROG} {metadata.version(_DIST)}")
        except metadata.PackageNotFoundError:
            print(f"{_PROG} (not installed; version unknown)")
        return 0
    print(_HELP, end="", file=sys.stderr)
    print(f"{_PROG}: unknown arguments: {' '.join(argv)}", file=sys.stderr)
    return 2
