#!/usr/bin/env python3
"""Refuse a tree whose files name different releases of the package; nothing is changed.

The package's version lives in src/proton_mail_mcp/__init__.py. server.json repeats it twice, the
Claude Desktop bundle's mcpb/manifest.json in its version and the release its uvx command runs,
and CHANGELOG.md in its newest release heading. The Claude Code plugin names one release in
plugin/.claude-plugin/plugin.json, plugin/.mcp.json, plugin/pyproject.toml, plugin/README.md and
plugin/uv.lock. Its lock may name only PyPI's files, and a release reaches PyPI only with the
upload, so the plugin may trail the package by one release, the one before it in CHANGELOG.md,
and no more (docs/ci.md, Releasing). Every Checks job runs this before the tests, and Publish
runs it with --tag before the upload. scripts/bump_version.py changes these files together. Exit 0
when they agree, 1 when they do not or cannot be read.

    python scripts/check_versions.py [--tag vX.Y.Z]
"""

import argparse
import re
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
# A release: three numbers, as every CHANGELOG.md heading has them, and nothing more after them.
VERSION = r"\d+\.\d+\.\d+(?![\w-]|\.\d)"
RELEASE = re.compile(rf"(?m)^## ({VERSION})")
INIT = "src/proton_mail_mcp/__init__.py"
CHANGELOG = "CHANGELOG.md"
LOCK = "plugin/uv.lock"
# The distribution the bundle and the plugin run.
DIST = "mcp-proton-mail"
FIX_PLUGIN = "move it with scripts/bump_version.py <version> --plugin, to a version on PyPI"


class Place(NamedTuple):
    """Where a file names the release: the text just before and after the version, and how many
    times the file names it (None: at least once)."""

    path: str
    what: str
    pattern: re.Pattern
    count: int | None


def place(path: str, what: str, before: str, after: str = "", count: int | None = 1) -> Place:
    pattern = re.compile(f"(?<={re.escape(before)}){VERSION}(?={re.escape(after)})")
    return Place(path, what, pattern, count)


# The first is where the version lives; the others repeat it.
PACKAGE = (
    place(INIT, "__version__", '__version__ = "', '"'),
    place("server.json", "the entry's and its package's version", '"version": "', '"', 2),
    place("mcpb/manifest.json", "version", '"version": "', '"'),
    place("mcpb/manifest.json", "the release uvx runs", f"{DIST}@", count=3),
)
PLUGIN = (
    place("plugin/.claude-plugin/plugin.json", "version", '"version": "', '"'),
    place("plugin/.mcp.json", "the release uvx runs", f"{DIST}==", '"'),
    place("plugin/pyproject.toml", "the requirement", f"{DIST}==", '"'),
    place("plugin/README.md", "the release uvx runs", f"{DIST}==", count=None),
)
FILES = sorted({where.path for where in PACKAGE + PLUGIN} | {CHANGELOG, LOCK})


def read(root: Path) -> dict[str, str]:
    """Each file this checks, by its path from the root, as it is on disk."""
    return {path: (root / path).read_bytes().decode("utf-8") for path in FILES}


def named(files: Mapping[str, str], places, found: list[str]) -> list[tuple[str, str, str]]:
    """(path, what, version) for each release the places name. A place that names it more or
    fewer times than it should goes in found."""
    seen = []
    for where in places:
        versions = where.pattern.findall(files[where.path])
        if where.count is None and not versions:
            found.append(f"{where.path}: names {where.what} nowhere")
        elif where.count is not None and len(versions) != where.count:
            found.append(
                f"{where.path}: names {where.what} {len(versions)} times, not {where.count}"
            )
        seen += [(where.path, where.what, version) for version in versions]
    return seen


def locked(text: str, found: list[str]) -> list[tuple[str, str, str]]:
    """(path, what, version) for the release plugin/uv.lock locks and the one its project
    requires. A lock that has not exactly one of each goes in found."""
    lock = tomllib.loads(text)
    entries = lock.get("package", [])
    seen = []
    dist = [entry for entry in entries if entry.get("name") == DIST]
    projects = [entry for entry in entries if entry.get("source") == {"virtual": "."}]
    requires = [
        requirement
        for project in projects
        for requirement in project.get("metadata", {}).get("requires-dist", [])
        if requirement.get("name") == DIST
    ]
    if len(dist) != 1:
        found.append(f"{LOCK}: locks {DIST} {len(dist)} times, not once")
    else:
        seen.append((LOCK, f"the {DIST} it locks", str(dist[0].get("version"))))
    specifier = str(requires[0].get("specifier", "")) if len(requires) == 1 else ""
    if not re.fullmatch(f"=={VERSION}", specifier):
        found.append(f"{LOCK}: its project does not require one {DIST}==<version>")
    else:
        seen.append((LOCK, "the release its project requires", specifier.removeprefix("==")))
    return seen


def problems(files: Mapping[str, str], tag: str | None = None, lock: bool = True) -> list[str]:
    """Each way the files disagree on the release; empty when they agree. With tag, the tag must
    be v and the package's version. Without lock, plugin/uv.lock is left out, as before uv
    writes it again."""
    found: list[str] = []
    package_named = named(files, PACKAGE, found)
    versions = [version for path, _, version in package_named if path == INIT]
    if len(versions) != 1:
        return found
    (package,) = versions
    for path, what, version in package_named:
        if version != package:
            found.append(
                f"{path}: {what} is {version}, not {package} as in {INIT}; "
                f"run scripts/bump_version.py {package}"
            )
    releases = RELEASE.findall(files[CHANGELOG])
    if not releases or releases[0] != package:
        newest = f"## {releases[0]}" if releases else "none"
        found.append(
            f"{CHANGELOG}: the newest release heading is {newest}, not ## {package}; "
            f"run scripts/bump_version.py {package}, which adds it"
        )
    if tag is not None and tag != f"v{package}":
        found.append(f"the tag is {tag}, not v{package}: tag the commit that sets its version")

    plugin_named = named(files, PLUGIN, found)
    if lock:
        plugin_named += locked(files[LOCK], found)
    plugins = {version for _, _, version in plugin_named}
    if len(plugins) > 1:
        each = ", ".join(f"{path} {version}" for path, _, version in plugin_named)
        found.append(f"the plugin's files name more than one release ({each}): {FIX_PLUGIN}")
    elif plugins:
        (plugin,) = plugins
        allowed = [package]
        if len(releases) > 1 and releases[0] == package:
            allowed.append(releases[1])
        if plugin not in allowed:
            before = f", or {allowed[1]} until {package} is on PyPI" if allowed[1:] else ""
            found.append(
                f"the plugin runs {plugin}, but may run only {package}{before} "
                f"(docs/ci.md, Releasing): {FIX_PLUGIN}"
            )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--tag", help="also refuse unless this tag is v and the package's version, as Publish does"
    )
    args = parser.parse_args(argv)
    try:
        files = read(ROOT)
        found = problems(files, args.tag)
    except (OSError, ValueError, LookupError, TypeError, AttributeError) as exc:
        # Files this cannot check are refused, like files that disagree.
        print(f"cannot check the versions: {exc}", file=sys.stderr)
        return 1
    if found:
        print("The files do not name one release:", file=sys.stderr)
        for line in found:
            print(f"  {line}", file=sys.stderr)
        return 1
    package = PACKAGE[0].pattern.findall(files[INIT])[0]
    plugin = PLUGIN[0].pattern.findall(files[PLUGIN[0].path])[0]
    trails = "" if plugin == package else f", the release before, until {package} is on PyPI"
    print(f"Every file names {package}; the plugin runs {plugin}{trails}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
