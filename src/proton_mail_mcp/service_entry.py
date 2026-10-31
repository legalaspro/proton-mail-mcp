"""Start the HTTP server as an installed service.

The installer's env file holds only non-secret settings. The bearer token comes
from the same OS credential store as the Bridge password, so systemd units,
LaunchAgents and scheduled tasks never contain a secret. The token is passed to
the server as a mapping and is not exported to child processes.
"""

import argparse
import os
import re
import sys
from pathlib import Path

from . import credential_store

SECRET_KEYS = frozenset({"PROTON_MCP_TOKEN", "PROTON_BRIDGE_PASSWORD"})
MAX_ENV_FILE_BYTES = 64 * 1024
_KEY = re.compile(r"(?:PROTON|PMM)_[A-Z0-9_]+")


class EnvFileError(ValueError):
    """The service env file is unsafe or malformed; values are never echoed."""


def parse_env_file(text: str) -> dict[str, str]:
    """Parse KEY=value lines; values are literal, with no quoting or expansion."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not _KEY.fullmatch(key):
            raise EnvFileError(f"Line {number}: expected PROTON_NAME=value")
        if key in SECRET_KEYS:
            raise EnvFileError(
                f"Line {number}: {key} is a secret; keep it in the credential store instead"
            )
        if key in values:
            raise EnvFileError(f"Line {number}: {key} is set more than once")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise EnvFileError(f"Line {number}: {key} contains control characters")
        values[key] = value.strip()
    return values


def read_env_file(path: str | os.PathLike) -> dict[str, str]:
    """Only the account owner may change settings such as confirmation or send."""
    target = Path(path)
    try:
        info = target.stat()
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise EnvFileError(
                "Env file must be owned by the service user and not writable by group or others"
            )
        if info.st_size > MAX_ENV_FILE_BYTES:
            raise EnvFileError("Env file is too large")
        text = target.read_text(encoding="utf-8")
    except OSError:
        raise EnvFileError("Env file cannot be read") from None
    except UnicodeDecodeError:
        raise EnvFileError("Env file must be UTF-8 text") from None
    return parse_env_file(text)


def service_env(base: dict[str, str], values: dict[str, str]) -> dict[str, str]:
    """Combine settings and add the stored token unless the environment supplies one."""
    env = dict(base)
    env.update(values)
    if not env.get("PROTON_MCP_TOKEN"):
        store = env.get("PROTON_MCP_CREDENTIAL_STORE", "keychain")
        account = env.get("PROTON_BRIDGE_USER", "")
        try:
            env["PROTON_MCP_TOKEN"] = credential_store.lookup(
                store,
                credential_store.TOKEN_SERVICE,
                account,
                credential_store.pass_locations(env),
            )
        except credential_store.CredentialStoreError:
            raise EnvFileError("The MCP token is unavailable from the credential store") from None
    return env


def _redirect_output(path: str) -> None:
    """pythonw services have no console; keep diagnostics in a private log file."""
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    descriptor = os.open(target, flags, 0o600)
    stream = open(descriptor, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = stream


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="proton-mail-mcp-service",
        description="Run proton-mail-mcp with an installer env file and stored token.",
    )
    parser.add_argument("--env-file", help="non-secret PROTON_* settings written by the installer")
    parser.add_argument("--log-file", help="append stdout and stderr to this private file")
    args = parser.parse_args(argv)
    if args.log_file:
        _redirect_output(args.log_file)
    try:
        values = read_env_file(args.env_file) if args.env_file else {}
        env = service_env(dict(os.environ), values)
    except EnvFileError as error:
        print(f"Cannot start proton-mail-mcp: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    from .server import main as serve

    serve(env)


if __name__ == "__main__":
    main()
