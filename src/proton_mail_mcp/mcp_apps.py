"""MCP Apps views: ui:// HTML resources that hosts render in sandboxed iframes.

Implements the MCP Apps extension (``io.modelcontextprotocol/ui``, specification
2026-01-26). Every view is one self-contained HTML document: its styles and
scripts are inlined from this package, and nothing is fetched at runtime.
A view talks to its host only through postMessage JSON-RPC. Each view
tool still returns one complete text block, which the model reads and clients
without MCP Apps support show. A view reads its data from the result's _meta,
which hosts keep out of model context; only sanitized message HTML is never in
the text. A host that passes a view only the result's content (Claude Desktop
does) leaves it without that data: the view then calls its own tool once more,
with the arguments the host delivered, and renders that result.
"""

from __future__ import annotations

from functools import cache
from importlib import resources
from typing import Any

import pydantic_core
from mcp.server.fastmcp.resources import TextResource
from mcp.types import CallToolResult, TextContent

EXTENSION_ID = "io.modelcontextprotocol/ui"
MIME_TYPE = "text/html;profile=mcp-app"
# The MCP Apps protocol version that the bundled view bridge requests.
PROTOCOL_VERSION = "2026-01-26"

THREAD_URI = "ui://proton-mail/thread.html"
REPLY_URI = "ui://proton-mail/reply.html"
REVIEW_SEND_URI = "ui://proton-mail/review-send.html"

# The result _meta key that carries a view's data. Most MCP Apps hosts pass the
# whole result to the view but keep _meta out of model context. structuredContent
# is ordinary tool output that clients may give the model, so view tools return none.
VIEW_META_KEY = "proton-mail-mcp/view"

# Declared CSP: no origin of any kind, so hosts keep their restrictive default.
RESOURCE_META = {
    "ui": {
        "csp": {
            "connectDomains": [],
            "resourceDomains": [],
            "frameDomains": [],
            "baseUriDomains": [],
        },
        "prefersBorder": True,
    }
}

# view name -> the tool whose result it shows. Each is read-only and issues no token.
VIEW_TOOLS = {"thread": "show_thread", "reply": "compose_reply", "review_send": "review_send"}

# Hosts must refuse a view's tools/call for a tool with visibility ["model"].
# The tools views may call: create_draft (appends one unsent draft) and the view
# tools. A view calls only create_draft and its own view tool, which is built into
# its bridge, and calls that tool only to read its data again with the arguments
# its host delivered (tool-input), when the host passed it a result without that
# data. Every other tool is model-only. So a view can never obtain or redeem an
# approval token, change mail other than by saving a draft, or read anything but
# what its own tool call returned.
VIEW_CALLABLE = frozenset({"create_draft", *VIEW_TOOLS.values()})

# The view document's own policy. It also governs nested message frames, which
# inherit it. Hosts add their policy on top; both must allow a load.
VIEW_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'"
)

# name -> (uri, title, description); the HTML file is ui/<name>.html.
VIEWS = {
    "thread": (
        THREAD_URI,
        "Proton Mail thread",
        "Read-only thread view: headers, sanitized message bodies and each attachment's name, "
        "type and size.",
    ),
    "reply": (
        REPLY_URI,
        "Proton Mail reply",
        "Reply or new-draft composer that saves unsent drafts, with their attachments, using "
        "create_draft; it never sends.",
    ),
    "review_send": (
        REVIEW_SEND_URI,
        "Proton Mail send review",
        "The exact preview_send preview of one draft, without a token; sending stays gated.",
    ),
}


def tool_meta(name: str, uri: str) -> dict[str, Any]:
    """Link a tool to its view; the flat key is the spelling older hosts read."""
    visibility = ["model", "app"] if name in VIEW_CALLABLE else ["model"]
    return {"ui": {"resourceUri": uri, "visibility": visibility}, "ui/resourceUri": uri}


def model_only(meta: dict[str, Any] | None) -> dict[str, Any]:
    """A tool's meta with visibility ["model"], keeping any link to a view."""
    meta = meta or {}
    return {**meta, "ui": {**meta.get("ui", {}), "visibility": ["model"]}}


def _asset(name: str) -> str:
    return resources.files(__package__).joinpath("ui").joinpath(name).read_text(encoding="utf-8")


@cache
def view_html(name: str) -> str:
    """The complete, self-contained HTML document of one view."""
    if name not in VIEWS:
        raise KeyError(name)
    document = _asset(f"{name}.html")
    replacements = {
        "@VIEW_CSP@": VIEW_CSP,
        "/*@BASE_CSS@*/": _asset("base.css"),
        "/*@BRIDGE_JS@*/": _asset("bridge.js")
        .replace("@PROTOCOL_VERSION@", PROTOCOL_VERSION)
        .replace("@VIEW_META_KEY@", VIEW_META_KEY)
        .replace("@VIEW_TOOL@", VIEW_TOOLS[name])
        .replace("@VIEW_NAME@", name),
    }
    for placeholder, value in replacements.items():
        document = document.replace(placeholder, value)
    return document


def register_resources(mcp: Any, names: list[str]) -> None:
    for name in names:
        uri, title, description = VIEWS[name]
        mcp.add_resource(
            TextResource(
                uri=uri,
                name=f"{name}-view",
                title=title,
                description=description,
                mime_type=MIME_TYPE,
                text=view_html(name),
                meta=RESOURCE_META,
            )
        )


def client_ui_support(ctx: Any) -> bool | None:
    """Whether this session's client declared MCP Apps HTML views; None if unknown."""
    try:
        params = ctx.session.client_params
    except Exception:  # Stateless HTTP has no initialized session.
        return None
    if params is None or params.capabilities is None:
        return None
    capabilities = params.capabilities
    for group in ("extensions", "experimental"):
        declared = getattr(capabilities, group, None)
        if isinstance(declared, dict) and isinstance(declared.get(EXTENSION_ID), dict):
            mime_types = declared[EXTENSION_ID].get("mimeTypes")
            return isinstance(mime_types, list) and MIME_TYPE in mime_types
    return False


def view_result(text: Any, view: dict[str, Any]) -> CallToolResult:
    """One text block for the model and plain clients; the view's data only in _meta."""
    if not isinstance(text, str):
        # The serialization FastMCP gives ordinary dictionary tool results.
        text = pydantic_core.to_json(text, fallback=str, indent=2).decode()
    return CallToolResult(
        content=[TextContent(type="text", text=text)], _meta={VIEW_META_KEY: view}
    )
