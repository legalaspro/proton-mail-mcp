#!/usr/bin/env python3
"""Refuse a plugin lock that names any source but PyPI; nothing is changed.

plugin/uv.lock records what the Claude Code plugin's `uvx mcp-proton-mail==<version>` resolves to,
for the Claude directory's review. The directory reports a lock with a local, path, editable, git
or other source, such as the `../dist` that `uv lock --find-links ../dist` records, as "Launcher
lock invalid". So the release sync runs this on the release tree before it pushes, and every
Checks job runs it before the tests: such a lock fails loudly and never reaches the release repo's
main. Every package must come from PyPI's index and name PyPI's own files, each with its sha256
hash; the one other entry is the plugin's own virtual project. Exit 0 when the lock passes, 1 when
it does not or cannot be read.

    python scripts/check_plugin_lock.py [PLUGIN_DIR]
"""

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPI = {"registry": "https://pypi.org/simple"}
FILES = "https://files.pythonhosted.org/packages/"
HASH = re.compile(r"sha256:[0-9a-f]{64}")
# The distribution the plugin's launcher runs.
DIST = "mcp-proton-mail"
FIX = (
    "Lock only a release that is on PyPI: in plugin/, run `uv lock --upgrade-package "
    "mcp-proton-mail` and commit the lock (docs/ci.md, The plugin's lock)."
)


def spell(source) -> str:
    """A lock's source as uv.lock writes it, such as `{ registry = "../dist" }`."""
    if not isinstance(source, dict):
        return repr(source)
    return "{ " + ", ".join(f'{key} = "{value}"' for key, value in source.items()) + " }"


def problems(plugin: Path) -> list[str]:
    """Each way the plugin's uv.lock names something other than PyPI; empty when it names none."""
    with (plugin / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]["name"]
    with (plugin / "uv.lock").open("rb") as stream:
        lock = tomllib.load(stream)
    found = []
    names = set()
    for entry in lock.get("package", []):
        name = entry.get("name", "<no name>")
        names.add(name)
        source = entry.get("source")
        if name == project:
            if source != {"virtual": "."}:
                found.append(f"{name}: source {spell(source)}, not the plugin's virtual project")
            continue
        if source != PYPI:
            found.append(f"{name}: source {spell(source)}, not {spell(PYPI)}")
            continue
        # pywin32 publishes wheels only, with no sdist.
        files = [entry["sdist"]] if "sdist" in entry else []
        files += entry.get("wheels", [])
        if not files:
            found.append(f"{name}: names no sdist and no wheel")
        for file in files:
            url = file.get("url")
            if not isinstance(url, str) or not url.startswith(FILES):
                found.append(f"{name}: file {url or file.get('path')!r} is not under {FILES}")
            elif not HASH.fullmatch(str(file.get("hash", ""))):
                found.append(f"{name}: file {url} has no sha256 hash")
    for name in (project, DIST):
        if name not in names:
            found.append(f"{name}: not in the lock")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "plugin",
        nargs="?",
        type=Path,
        default=ROOT / "plugin",
        help="the plugin's directory, which holds pyproject.toml and uv.lock (default: plugin/)",
    )
    args = parser.parse_args(argv)
    lock = args.plugin / "uv.lock"
    try:
        found = problems(args.plugin)
    except (OSError, ValueError, LookupError, TypeError, AttributeError) as exc:
        # A lock this cannot check is refused, like one that fails.
        print(f"{lock}: cannot check the lock: {exc}", file=sys.stderr)
        return 1
    if found:
        print(
            f'{lock}: the Claude directory reports this lock as "Launcher lock invalid":',
            file=sys.stderr,
        )
        for line in found:
            print(f"  {line}", file=sys.stderr)
        print(FIX, file=sys.stderr)
        return 1
    print(f"{lock}: every package comes from PyPI, with PyPI's files and their hashes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
