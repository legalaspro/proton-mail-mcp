#!/usr/bin/env python3
"""Print a release's section of CHANGELOG.md, the notes of its GitHub release; nothing is changed.

The section is the text under the release's `## <version>` heading, up to the next `## ` heading,
without the heading itself. Publish runs this on the tag before the upload, and stops when the
section is missing or empty, so a tag without notes never reaches PyPI, which cannot take an
upload back; the release job takes the notes from the build after the upload (docs/ci.md,
Releasing). Exit 0 with the notes on stdout, or in the --output file, 1 when there are none, 2
for a bad argument.

    python scripts/release_notes.py 1.30.2 [--output notes.md]
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# A release, as scripts/check_versions.py and every CHANGELOG.md heading spell it.
VERSION = r"\d+\.\d+\.\d+(?![\w-]|\.\d)"


def notes(changelog: str, version: str) -> str | None:
    """The release's section, without its heading or the blank lines around it; None when the
    changelog has no heading for it."""
    lines = changelog.splitlines()
    heading = re.compile(rf"## {re.escape(version)}(?![\w-]|\.\d)")
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1 : end]).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("version", help="the release, such as 1.30.2, without a v")
    parser.add_argument("--output", type=Path, help="write the notes here, in UTF-8")
    args = parser.parse_args(argv)
    if not re.fullmatch(VERSION, args.version):
        parser.error(f"{args.version!r} is not a release such as 1.30.2")
    changelog = ROOT / "CHANGELOG.md"
    try:
        text = notes(changelog.read_bytes().decode("utf-8"), args.version)
    except (OSError, ValueError) as exc:
        print(f"cannot read {changelog}: {exc}", file=sys.stderr)
        return 1
    if text is None:
        print(
            f"CHANGELOG.md has no ## {args.version} section, which the release's notes come "
            f"from: add it, with its entries, and tag a commit that has it "
            f"(scripts/bump_version.py {args.version} adds the heading)",
            file=sys.stderr,
        )
        return 1
    if not text:
        print(
            f"CHANGELOG.md's ## {args.version} section is empty: write its entries, which the "
            "release's notes come from, and tag a commit that has them",
            file=sys.stderr,
        )
        return 1
    if args.output:
        args.output.write_bytes(f"{text}\n".encode("utf-8"))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
