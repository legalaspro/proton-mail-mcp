"""Static contract checks shared by the MCP Apps view tests."""

import re
from html.parser import HTMLParser

from proton_mail_mcp import mcp_apps

# DOM sinks that would parse text as markup or code. Views build nodes instead.
FORBIDDEN_JS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    "createContextualFragment",
    "srcdoc",
    "javascript:",
    "localStorage",
    "sessionStorage",
    "indexedDB",
    "document.cookie",
    "XMLHttpRequest",
    "fetch(",
    "WebSocket",
    "EventSource",
    "sendBeacon",
    "importScripts",
    "window.open",
)


class _Inventory(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.attributes = []
        self.scripts = []
        self._script = None

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend((tag, name, value) for name, value in attrs)
        if tag == "script":
            self._script = {"attrs": dict(attrs), "text": ""}

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            self.scripts.append(self._script)
            self._script = None

    def handle_data(self, data):
        if self._script is not None:
            self._script["text"] += data


def inventory(html):
    parser = _Inventory()
    parser.feed(html)
    parser.close()
    return parser


def assert_self_contained_view(html, *, allowed_iframes=False):
    """No runtime dependency, no unresolved placeholder and a strict page policy."""
    assert html.startswith("<!doctype html>")
    assert not re.search(r"/\*@[A-Z_]+@\*/|@[A-Z_]{4,}@", html), "unresolved placeholder"
    assert not re.search(r"(?i)\b[a-z][a-z0-9+.-]*://", html), "URL in view document"
    assert "@import" not in html
    parsed = inventory(html)
    css = "\n".join(re.findall(r"(?s)<style>(.*?)</style>", html))
    assert css and not re.search(r"(?i)url\s*\(|@font-face|image-set|expression", css)
    assert {"link", "base", "object", "embed", "form", "img"}.isdisjoint(parsed.tags)
    assert "iframe" not in parsed.tags  # nested frames are created by script only
    for tag, name, value in parsed.attributes:
        assert name not in {"src", "href", "action", "srcset", "background", "poster"}, (tag, name)
        assert not name.startswith("on"), (tag, name)
    csp = [
        value
        for tag, name, value in parsed.attributes
        if tag == "meta" and name == "content" and "default-src" in (value or "")
    ]
    assert csp == [mcp_apps.VIEW_CSP]
    for directive in (
        "default-src 'none'",
        "connect-src 'none'",
        "img-src data:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    ):
        assert directive in mcp_apps.VIEW_CSP
    executable = [script for script in parsed.scripts if "type" not in script["attrs"]]
    data = [script for script in parsed.scripts if "type" in script["attrs"]]
    assert executable, "view has no script"
    assert all(set(script["attrs"]) == set() for script in executable)
    assert all(
        script["attrs"]["type"] == "application/json" and set(script["attrs"]) == {"type", "id"}
        for script in data
    )
    code = "\n".join(script["text"] for script in executable)
    for sink in FORBIDDEN_JS:
        if sink == "srcdoc" and allowed_iframes:
            continue
        assert sink not in code, sink
    return code


def view_data(result):
    """A view tool result's data for its view: only in _meta, never in structuredContent."""
    if isinstance(result, dict):  # A JSON-RPC result over HTTP.
        assert "structuredContent" not in result
        return result["_meta"][mcp_apps.VIEW_META_KEY]
    assert result.structuredContent is None
    assert set(result.meta) == {mcp_apps.VIEW_META_KEY}
    return result.meta[mcp_apps.VIEW_META_KEY]


def tool_calls(code):
    """Tool names a view script calls through the host."""
    return set(re.findall(r'callTool\(\s*"([a-z_]+)"', code))


def host_requests(code):
    return set(re.findall(r'request\(\s*"([a-z/_-]+)"', code))
