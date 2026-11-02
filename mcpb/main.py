"""The bundle's entry point: `proton-mail-mcp connect`, from the copy `uv run` installs.

Claude Desktop runs this with `uv run --directory <bundle>`, which installs the project from
the bundle's pyproject.toml first. connect reaches the service that `proton-mail-mcp setup`
installed on this computer; the bundle installs no service of its own.
"""

import sys

from proton_mail_mcp.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["connect", *sys.argv[1:]]))
