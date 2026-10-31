"""Standard-library installer for Bridge, the MCP server, credentials and services.

`proton-mail-mcp setup` from an installed wheel, and `scripts/install.sh` (Linux,
macOS) and `scripts/install.ps1` (Windows) from a checkout, which find a Python 3.12+
interpreter and run `python -m proton_mail_mcp.installer`. Detection and planning are
pure functions so every platform's plan is testable on Linux without network access.
"""
