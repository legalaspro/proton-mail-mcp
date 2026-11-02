"""`proton-mail-mcp introspect`: the server's tool list on stdio, with no Bridge behind it.

Registries such as Glama check a server by starting it in a container and asking for its tools
over stdin and stdout. This answers initialize, tools/list and the views' resources as the
default configuration does, sending off, and refuses every tool call before it starts. It reads
no settings or credentials: of the environment, only the home folder's location, for an action
log path it never opens. It holds no mailbox client and starts no watcher, so it opens no socket,
reads and changes no mail, and writes no file.
"""

import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .config import Settings
from .server import create_mcp

REFUSAL = (
    "This is proton-mail-mcp introspect, which lists the tools with no Proton Mail Bridge behind "
    "it; nothing was read or changed. To use the tools, install proton-mail-mcp on the computer "
    "that runs Bridge and run `proton-mail-mcp setup`."
)


def create_introspection_mcp() -> FastMCP:
    # The defaults throughout, sending off. stdio carries no bearer, so the token only satisfies
    # validation, and the account is a reserved name nothing ever contacts.
    settings = Settings(
        token=secrets.token_urlsafe(32), bridge_user="introspection@example.invalid"
    )
    mcp = create_mcp(settings, mail_client=object())

    async def refuse(name: str, arguments: dict[str, Any], **_options: Any) -> Any:
        raise ToolError(REFUSAL)

    # Replaces the audited dispatch whole: no call reaches a tool or the action log.
    mcp._tool_manager.call_tool = refuse
    return mcp


def main() -> int:
    create_introspection_mcp().run("stdio")
    return 0
