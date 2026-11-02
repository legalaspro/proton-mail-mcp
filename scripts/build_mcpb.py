#!/usr/bin/env python3
"""Build the Claude Desktop bundle (.mcpb) from this checkout; nothing is uploaded.

The bundle is a zip of three files: mcpb/manifest.json at its root, mcpb/main.py as the
declared entry point, and the LICENSE. It holds no copy of the package. The manifest's command
is `uvx mcp-proton-mail@<version> connect`, so uv fetches that release from PyPI on the user's
computer. The same checkout always gives the same bytes. The manifest's version, and the
release its command pins, must be the package's own, and a platform's own arguments must end
with that command's.

    python scripts/build_mcpb.py [--output DIR]
"""

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "mcpb" / "manifest.json"
ENTRY = ROOT / "mcpb" / "main.py"
PACKAGE = ROOT / "src" / "proton_mail_mcp"
# The distribution on PyPI that the manifest's command runs.
DIST = "mcp-proton-mail"
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
    return {
        "manifest.json": MANIFEST,
        manifest["server"]["entry_point"]: ENTRY,
        "LICENSE": ROOT / "LICENSE",
    }


def build(output: Path) -> Path:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("version") != VERSION:
        raise ValueError(
            f"{MANIFEST.relative_to(ROOT)} says version {manifest.get('version')}, but the "
            f"package is {VERSION}; make them the same"
        )
    config = manifest["server"]["mcp_config"]
    pinned = config["args"][0]
    if pinned != f"{DIST}@{VERSION}":
        raise ValueError(
            f"{MANIFEST.relative_to(ROOT)} runs {pinned}, but the package is {VERSION}; make "
            f"its first argument {DIST}@{VERSION}"
        )
    for system, override in config.get("platform_overrides", {}).items():
        if "args" in override and override["args"][-len(config["args"]) :] != config["args"]:
            raise ValueError(
                f"{MANIFEST.relative_to(ROOT)}'s {system} override runs other arguments; end "
                f"them with {' '.join(config['args'])}"
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
    except (OSError, ValueError, KeyError, IndexError) as error:
        print(f"build_mcpb: {error}", file=sys.stderr)
        return 2
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
