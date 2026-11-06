#!/usr/bin/env python3
"""Set a new version everywhere the package names it, or with --plugin where the plugin does.

Without --plugin it sets VERSION in src/proton_mail_mcp/__init__.py, in both versions in
server.json and in mcpb/manifest.json's version and uvx command, and gives CHANGELOG.md a heading
for it: `## Unreleased` becomes `## VERSION - DATE`, or, with no such section, that heading is
added with no entries, which Publish refuses to release until they are written. The Claude Code
plugin stays on the release before, since its lock may name only PyPI's files.

After VERSION's upload to PyPI, --plugin moves the plugin instead: it sets VERSION in
plugin/.claude-plugin/plugin.json, plugin/.mcp.json, plugin/pyproject.toml and plugin/README.md,
then runs `uv lock --upgrade-package mcp-proton-mail` in plugin/, which needs uv and the network.
VERSION must be the package's version, or the release before it.

It changes nothing when the result would fail scripts/check_versions.py, and puts back every file
it wrote when a write or uv fails, the new lock fails scripts/check_plugin_lock.py, or Ctrl-C
stops it. --dry-run shows each change as a diff and writes nothing. docs/ci.md, Releasing, has
the whole flow. Exit 0 when done, 1 when refused, 2 for a bad argument.

    python scripts/bump_version.py 1.31.0 [--dry-run]
    python scripts/bump_version.py 1.31.0 --plugin [--dry-run]
"""

import argparse
import datetime
import difflib
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNRELEASED = re.compile(r"(?m)^## \[?Unreleased\]?[ \t]*$")
LOCK_ARGS = ["lock", "--upgrade-package", "mcp-proton-mail"]


def sibling(name: str):
    """Another script in this folder, which has no package to import it from."""
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


versions = sibling("check_versions")
lock_guard = sibling("check_plugin_lock")
release_notes = sibling("release_notes")


def numbers(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def changelog(text: str, version: str, date: str) -> str:
    """CHANGELOG.md with a heading for version: Unreleased renamed, or a new one, above the newest
    release, with no entries. Unchanged when it has the heading already."""
    if version in versions.RELEASE.findall(text):
        return text
    heading = f"## {version} - {date}"
    newest = versions.RELEASE.search(text)
    unreleased = UNRELEASED.search(text)
    if unreleased and (newest is None or unreleased.start() < newest.start()):
        return text[: unreleased.start()] + heading + text[unreleased.end() :]
    if newest is None:
        return text.rstrip("\n") + f"\n\n{heading}\n"
    return text[: newest.start()] + f"{heading}\n\n" + text[newest.start() :]


def bumped(files: dict[str, str], version: str, date: str, plugin: bool) -> dict[str, str]:
    """The files with version set where the package names it, or with plugin where the plugin's
    text files do; uv writes the plugin's lock."""
    new = dict(files)
    for where in versions.PLUGIN if plugin else versions.PACKAGE:
        new[where.path] = where.pattern.sub(version, new[where.path])
    if not plugin:
        new[versions.CHANGELOG] = changelog(new[versions.CHANGELOG], version, date)
    return new


def write(files: dict[str, str], paths) -> None:
    for path in paths:
        (ROOT / path).write_bytes(files[path].encode("utf-8"))


def put_back(files: dict[str, str], paths) -> list[str]:
    """Write each file's old text again, every one even when another fails; the paths that
    failed."""
    missed = []
    for path in paths:
        try:
            write(files, [path])
        except OSError:
            missed.append(path)
    return missed


def refuse(lines, heading: str = "Nothing was changed:") -> int:
    print(heading, file=sys.stderr)
    for line in lines:
        print(f"  {line}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("version", help="the new version, such as 1.31.0, without a v")
    parser.add_argument(
        "--plugin",
        action="store_true",
        help="move the Claude Code plugin to VERSION instead, once VERSION is on PyPI",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show each change as a diff and write nothing"
    )
    parser.add_argument(
        "--date",
        type=datetime.date.fromisoformat,
        default=datetime.datetime.now(datetime.UTC).date(),
        help="the date a new CHANGELOG.md heading gets, as YYYY-MM-DD (default: today, in UTC)",
    )
    args = parser.parse_args(argv)
    if not re.fullmatch(versions.VERSION, args.version):
        parser.error(f"{args.version!r} is not a version such as 1.31.0")
    version = args.version

    try:
        files = versions.read(ROOT)
    except (OSError, ValueError) as exc:
        return refuse([f"cannot read the files that name the version: {exc}"])
    current = versions.PACKAGE[0].pattern.findall(files[versions.INIT])
    if len(current) != 1:
        return refuse([f"{versions.INIT}: cannot find the one __version__"])
    if not args.plugin and numbers(version) < numbers(current[0]):
        return refuse([f"{version} is older than the package's {current[0]}"])

    new = bumped(files, version, args.date.isoformat(), args.plugin)
    try:
        # uv writes the plugin's lock after the other files, so it is checked after uv.
        found = versions.problems(new, lock=not args.plugin)
        relock = args.plugin and bool(versions.problems(new))
    except (ValueError, LookupError, TypeError, AttributeError) as exc:
        return refuse([f"cannot check the versions: {exc}"])
    if found:
        return refuse(found)
    changed = [path for path in versions.FILES if new[path] != files[path]]
    if not changed and not relock:
        print(f"Every file names {version} already; nothing to change.")
        return 0

    for path in changed:
        sys.stdout.writelines(
            difflib.unified_diff(
                files[path].splitlines(keepends=True),
                new[path].splitlines(keepends=True),
                f"a/{path}",
                f"b/{path}",
                n=0,
            )
        )
    if relock:
        print(f"Then, in plugin/: uv {' '.join(LOCK_ARGS)}")
    if args.dry_run:
        print("Dry run: nothing was written.")
        return 0

    uv = shutil.which("uv") if relock else None
    if relock and uv is None:
        return refuse(["--plugin runs uv, which is not on PATH: install uv and run it again"])
    written = [*changed, versions.LOCK] if relock else changed
    # found stays None until every step is done, so Ctrl-C at any point puts every file back.
    found = None
    try:
        write(new, changed)
        if not relock:
            found = []
        else:
            sys.stdout.flush()
            result = subprocess.run([uv, *LOCK_ARGS], cwd=ROOT / "plugin")
            if result.returncode:
                found = [f"uv lock exited with {result.returncode}"]
            else:
                try:
                    found = [
                        *versions.problems(versions.read(ROOT)),
                        *lock_guard.problems(ROOT / "plugin"),
                    ]
                except (OSError, ValueError, LookupError, TypeError, AttributeError) as exc:
                    found = [f"cannot check {versions.LOCK}: {exc}"]
    except OSError as exc:
        found = [f"cannot write the files: {exc}"]
    finally:
        # Ctrl-C, or an error not caught here, goes on after the files are back.
        missed = [] if found == [] else put_back(files, written)
        if missed:
            print(
                f"Could not put back {', '.join(missed)}: restore them with git.", file=sys.stderr
            )
    if found:
        print(file=sys.stderr)
        if missed:
            return refuse(found, "Not every file is as it was:")
        hint = f"; {versions.LOCK} can lock {version} only once it is on PyPI" if relock else ""
        return refuse([*found, f"every file is as it was{hint}"])

    print(f"Set {version} in {', '.join(written)}.")
    if args.plugin:
        print("Next: make check, then a pull request with these files (docs/ci.md, Releasing).")
        return 0
    if not release_notes.notes(new[versions.CHANGELOG], version):
        print(f"Write the entries under ## {version} in CHANGELOG.md: the release's notes.")
    print(
        f"Next: make check and a pull request; after the merge, tag v{version}. Once PyPI has "
        f"it, run scripts/bump_version.py {version} --plugin (docs/ci.md, Releasing)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
