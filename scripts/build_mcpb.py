#!/usr/bin/env python3
"""Build the Claude Desktop bundle (.mcpb) from this checkout; nothing is uploaded.

The bundle is a zip with mcpb/manifest.json at its root, mcpb/main.py as its entry point,
and what `uv run` needs to install the project from it: pyproject.toml, README.md, LICENSE,
the package and every file pyproject.toml force-includes in the wheel. The same checkout
always gives the same bytes. The manifest's version must be the package's own.

    python scripts/build_mcpb.py [--output DIR]
"""

import argparse
import json
import re
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "mcpb" / "manifest.json"
ENTRY = ROOT / "mcpb" / "main.py"
PACKAGE = ROOT / "src" / "proton_mail_mcp"
# The package's one version, as the wheel's metadata and the server read it.
VERSION = re.search(
    r'^__version__ = "([^"]+)"$',
    (PACKAGE / "__init__.py").read_text(encoding="utf-8"),
    flags=re.M,
).group(1)
# A fixed time and mode for every entry, so a rebuild gives the same bytes.
STAMP = (1980, 1, 1, 0, 0, 0)
MODE = 0o644 << 16


def members() -> dict[str, Path]:
    """Name in the bundle -> file in this checkout."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    found = {
        "manifest.json": MANIFEST,
        manifest["server"]["entry_point"]: ENTRY,
        "pyproject.toml": ROOT / "pyproject.toml",
        "README.md": ROOT / "README.md",
        "LICENSE": ROOT / "LICENSE",
    }
    for path in sorted(PACKAGE.rglob("*")):
        # Hatch leaves out what .gitignore names, and so does the bundle.
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            found[path.relative_to(ROOT).as_posix()] = path
    with (ROOT / "pyproject.toml").open("rb") as stream:
        wheel = tomllib.load(stream)["tool"]["hatch"]["build"]["targets"]["wheel"]
    for source in wheel.get("force-include", {}):
        found[source] = ROOT / source
    return found


def build(output: Path) -> Path:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("version") != VERSION:
        raise ValueError(
            f"{MANIFEST.relative_to(ROOT)} says version {manifest.get('version')}, but the "
            f"package is {VERSION}; make them the same"
        )
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"proton-mail-mcp-{VERSION}.mcpb"
    with zipfile.ZipFile(target, "w") as bundle:
        for name, path in sorted(members().items()):
            entry = zipfile.ZipInfo(name, date_time=STAMP)
            entry.external_attr = MODE
            entry.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(entry, path.read_bytes())
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", default=str(ROOT / "dist"), help="folder for the .mcpb")
    args = parser.parse_args(argv)
    try:
        target = build(Path(args.output))
    except (OSError, ValueError, KeyError) as error:
        print(f"build_mcpb: {error}", file=sys.stderr)
        return 2
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
