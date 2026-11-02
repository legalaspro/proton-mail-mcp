"""The bundle's declared entry point, for a host that starts it with Python instead of running
the manifest's command: it runs that same command, `uvx mcp-proton-mail@<version> connect`.

The bundle holds no copy of the package, so this uses only the standard library. A desktop app
on macOS starts with a short PATH that leaves out where uvx is usually installed, so on macOS the
manifest runs uvx through /bin/sh, which puts those folders in front of PATH; this runs that same
command. On every system it also puts uv's own folder after PATH, and passes that PATH on.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "manifest.json"
# A setting the host would have filled in; empty here, so connect uses its default.
_SETTING = re.compile(r"\$\{user_config\.[^}]+\}")


def search_path(environ: Mapping[str, str], home: str) -> str:
    """PATH with uv's own folder after it: where the command is found, and what it gets."""
    folders = [environ.get("PATH", ""), os.path.join(home, ".local", "bin")]
    return os.pathsep.join(filter(None, folders))


def command(manifest: dict, path: str, argv: list[str]) -> list[str]:
    """The manifest's command for this system, found on path by absolute path, its arguments,
    then argv. A platform's override replaces the command and the arguments, as in a host."""
    config = manifest["server"]["mcp_config"]
    config = {**config, **config.get("platform_overrides", {}).get(sys.platform, {})}
    found = shutil.which(config["command"], path=path)
    if not found:
        raise LookupError(
            f"{config['command']} was not found: install uv (https://docs.astral.sh/uv/), "
            "then start the client again"
        )
    return [found, *(_SETTING.sub("", arg) for arg in config["args"]), *argv]


def main(argv: list[str] | None = None) -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    environ = dict(os.environ)
    environ["PATH"] = search_path(environ, str(Path.home()))
    try:
        words = command(manifest, environ["PATH"], sys.argv[1:] if argv is None else argv)
    except LookupError as error:
        print(f"proton-mail-mcp bundle: {error}", file=sys.stderr)
        return 127
    if os.name == "nt":
        return subprocess.call(words, env=environ)
    os.execve(words[0], words, environ)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
