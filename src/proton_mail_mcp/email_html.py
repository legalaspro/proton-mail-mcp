"""Sanitize email HTML for MCP Apps views: allowlisted markup, no active or remote content.

The sanitizer re-serializes parsed tokens; it never passes source markup through.
Only allowlisted elements and attributes survive, text is escaped, and every URL
that could load something is removed and counted. Views add two independent
guarantees on top: the result is shown in a sandboxed frame without script
permission, under a policy that allows images only from data: URLs.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from typing import Any

# Elements rendered with their allowlisted attributes.
_ALLOWED = frozenset(
    {
        "a", "abbr", "acronym", "address", "article", "aside", "b", "bdi", "bdo", "big",
        "blockquote", "br", "caption", "center", "cite", "code", "col", "colgroup", "dd",
        "del", "details", "dfn", "div", "dl", "dt", "em", "fieldset", "figcaption", "figure",
        "font", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "i", "img",
        "ins", "kbd", "label", "legend", "li", "main", "mark", "nav", "ol", "p", "pre", "q",
        "s", "samp", "section", "small", "span", "strike", "strong", "sub", "summary", "sup",
        "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr", "tt", "u", "ul", "var",
        "wbr",
    }
)  # fmt: skip
_VOID = frozenset({"br", "col", "hr", "img", "wbr"})
# Removed together with their whole content.
_DROP = frozenset(
    {
        "applet", "audio", "canvas", "datalist", "embed", "frame", "frameset", "iframe",
        "map", "math", "noembed", "noframes", "noscript", "object", "optgroup", "option",
        "picture", "plaintext", "portal", "script", "select", "svg", "template", "textarea",
        "title", "video", "xmp",
    }
)  # fmt: skip
# Elements that load or submit something; counted when removed.
_REMOTE_ELEMENTS = frozenset(
    {"applet", "audio", "embed", "frame", "frameset", "iframe", "link", "object", "picture",
     "portal", "source", "track", "video"}
)  # fmt: skip
_ACTIVE_ELEMENTS = frozenset({"form", "input", "button", "script", "noscript", "template"})
# Void or single tags removed without content.
_REMOVE = frozenset({"area", "base", "basefont", "input", "keygen", "link", "meta", "param",
                     "source", "track"})  # fmt: skip

_COMMON = frozenset({"title", "dir", "lang", "class", "style", "align", "valign"})
_ATTRIBUTES = {
    "a": {"href"},
    "img": {"src", "alt", "width", "height", "border", "hspace", "vspace"},
    "table": {"width", "height", "border", "cellpadding", "cellspacing", "bgcolor", "frame",
              "rules", "summary"},
    "td": {"width", "height", "bgcolor", "colspan", "rowspan", "nowrap", "abbr", "scope"},
    "th": {"width", "height", "bgcolor", "colspan", "rowspan", "nowrap", "abbr", "scope"},
    "tr": {"height", "bgcolor"},
    "tbody": {"bgcolor"},
    "thead": {"bgcolor"},
    "tfoot": {"bgcolor"},
    "col": {"width", "span"},
    "colgroup": {"width", "span"},
    "font": {"color", "face", "size"},
    "ol": {"start", "type", "reversed"},
    "ul": {"type"},
    "li": {"value", "type"},
    "hr": {"width", "size", "noshade", "color"},
    "time": {"datetime"},
    "del": {"datetime"},
    "ins": {"datetime"},
    "details": {"open"},
}  # fmt: skip
# Attributes that name something to load; always removed and counted.
_REMOTE_ATTRIBUTES = frozenset(
    {"background", "srcset", "poster", "dynsrc", "lowsrc", "longdesc", "data", "codebase",
     "archive", "ping", "manifest", "imagesrcset", "xlink:href", "formaction", "action"}
)  # fmt: skip

_NUMBER = re.compile(r"\d{1,4}")
_LENGTH = re.compile(r"\d{1,4}(?:\.\d{1,2})?(?:px|%)?")
_COLOR = re.compile(r"#[0-9A-Fa-f]{3,8}|[A-Za-z]{1,30}|rgba?\(\s*[\d.%\s,/]{1,40}\)")
_LANG = re.compile(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8}){0,4}")
_CLASS = re.compile(r"[A-Za-z0-9_ -]{1,200}")
_FACE = re.compile(r"[A-Za-z0-9 ,'\"_-]{1,200}")
_DATETIME = re.compile(r"[0-9A-Za-z:+., -]{1,64}")
_KEYWORDS = {
    "dir": {"ltr", "rtl", "auto"},
    "align": {"left", "right", "center", "justify", "middle", "top", "bottom", "absmiddle",
              "baseline", "texttop", "absbottom", "char"},
    "valign": {"top", "middle", "bottom", "baseline"},
    "scope": {"row", "col", "rowgroup", "colgroup"},
    "frame": {"void", "above", "below", "hsides", "lhs", "rhs", "vsides", "box", "border"},
    "rules": {"none", "groups", "rows", "cols", "all"},
}  # fmt: skip
_LIST_TYPES = {"1", "a", "A", "i", "I", "disc", "circle", "square"}
_FLAGS = {"nowrap", "noshade", "reversed", "open"}

_DATA_IMAGE = re.compile(r"data:image/(?:png|gif|jpe?g|webp|bmp);base64,[A-Za-z0-9+/]+={0,2}")
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*):")
# Browsers drop ASCII tab and newline anywhere in a URL and trim C0 controls and spaces.
_URL_IGNORED = re.compile(r"[\t\n\r]")
_URL_TRIM = "".join(chr(code) for code in range(0x21))

_CSS_SAFE_PROPERTIES = frozenset(
    {
        "background-color", "border-collapse", "border-spacing", "bottom", "box-sizing",
        "caption-side", "clear", "color", "direction", "display", "empty-cells", "float",
        "height", "left", "letter-spacing", "line-height", "max-height", "max-width",
        "min-height", "min-width", "opacity", "overflow", "overflow-wrap", "overflow-x",
        "overflow-y", "right", "table-layout", "top", "unicode-bidi", "vertical-align",
        "visibility", "white-space", "width", "word-break", "word-spacing", "word-wrap",
        "background", "background-position", "background-repeat", "background-size",
        "position", "text-align", "text-decoration", "text-indent", "text-transform",
        "text-overflow", "text-shadow", "box-shadow", "border-radius", "column-count",
        "column-gap",
    }
)  # fmt: skip
# No outline: a style attribute's would override the frame's focus outline.
_CSS_SAFE_PREFIXES = ("border", "margin", "padding", "font", "list-style", "text-")
# Anything that can load, run or escape a declaration, including CSS escapes.
_CSS_UNSAFE = re.compile(
    r"(?i)url\s*\(|image-set|image\s*\(|element\s*\(|cross-fade|paint\s*\(|expression|"
    r"javascript|vbscript|behavior|binding|@import|attr\s*\(|var\s*\(|env\s*\(|"
    r"[\\<>{}]|/\*|\*/"
)
_CSS_LOADS = re.compile(r"(?i)url\s*\(|image-set|image\s*\(|cross-fade|@import")
_STYLE_BLOCK_UNSAFE = re.compile(
    r"(?i)url\s*\(|image-set|image\s*\(|element\s*\(|cross-fade|paint\s*\(|expression|"
    r"javascript|vbscript|behavior|binding|@import|@font-face|@namespace|attr\s*\(|"
    r"</?\s*style"
)
# :focus, :focus-visible, :focus-within and prefixed forms such as :-moz-focusring.
_FOCUS_SELECTOR = re.compile(r"(?i):(?:-[a-z]+-)?focus[a-z-]*")

FRAME_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:"
_FRAME_CSS = (
    # The document's first cascade layer: its !important declarations win over every
    # message style sheet, whatever the selector. Style attributes would rank higher;
    # the sanitizer keeps none that set these properties.
    "@layer{"
    # A root sized to the frame (the common html,body{height:100%}) would grow the
    # frame on every fit.
    "html,body{height:auto !important;min-height:0 !important;}"
    # Whatever has keyboard focus (a link, a summary) shows this outline. Transitions
    # outrank even these declarations, so none may hold the outline back.
    "*{transition:none !important;}"
    ":focus-visible{outline:2px solid #3d5bd9 !important;outline-offset:2px !important;}"
    "}"
    "html{background:#fff;color:#1f2328;}"
    "body{margin:0;padding:14px 16px;font:14px/1.5 system-ui,-apple-system,'Segoe UI',"
    "Roboto,sans-serif;overflow-wrap:anywhere;}"
    "img{max-width:100%;height:auto;}"
    "table{max-width:100%;}"
    "pre{white-space:pre-wrap;}"
    "blockquote{margin:0 0 0 6px;padding-left:10px;border-left:3px solid #d0d3db;color:#555b66;}"
    "a[data-pmm-href]{color:#3d5bd9;text-decoration:underline;cursor:pointer;}"
    ".pmm-blocked{display:inline-block;max-width:100%;box-sizing:border-box;overflow:hidden;"
    "vertical-align:middle;border:1px dashed #c3c6d1;border-radius:4px;background:#f3f4f7;"
    "color:#6b6f7b;font:12px/1.3 system-ui,sans-serif;padding:2px 6px;text-align:center;}"
)


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open: list[str] = []
        self.dropping: list[str] = []
        self.style_depth = 0
        self.style_text: list[str] = []
        self.blocked = {"images": 0, "inline_images": 0, "styles": 0, "other": 0}
        self.removed_active = 0
        self.links = 0

    # Element handling -------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, closed=True)

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], *, closed: bool) -> None:
        if self.dropping:
            if tag == self.dropping[-1] and not closed:
                self.dropping.append(tag)
            return
        if self.style_depth:
            return
        if any(name.startswith("on") for name, _ in attrs):
            self.removed_active += 1
        if tag in _REMOTE_ELEMENTS:
            self.blocked["other"] += 1
        if tag in _ACTIVE_ELEMENTS:
            self.removed_active += 1
        if tag == "meta" and any(
            name == "http-equiv" and (value or "").strip().lower() == "refresh"
            for name, value in attrs
        ):
            self.blocked["other"] += 1
        if tag in _DROP:
            if not closed:
                self.dropping.append(tag)
            return
        if tag in _REMOVE:
            return
        if tag == "style":
            if not closed:
                self.style_depth = 1
                self.style_text = []
            return
        if tag == "body":
            self._body(attrs)
            return
        if tag == "img":
            self._image(attrs)
            return
        if tag not in _ALLOWED:
            return  # Unknown wrappers (html, head, form, o:p, ...) keep their content.
        self.out.append(f"<{tag}{self._attributes(tag, attrs)}>")
        if tag not in _VOID and not closed:
            self.open.append(tag)
        elif tag not in _VOID:
            self.out.append(f"</{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if self.dropping:
            if tag == self.dropping[-1]:
                self.dropping.pop()
            return
        if tag == "style" and self.style_depth:
            self.style_depth = 0
            self._style_block("".join(self.style_text))
            return
        if self.style_depth or tag in _VOID or tag not in self.open:
            return
        # Close any elements left open inside it, so output nesting stays exact.
        while self.open:
            current = self.open.pop()
            self.out.append("</div>" if current == "body" else f"</{current}>")
            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.dropping:
            return
        if self.style_depth:
            self.style_text.append(data)
            return
        self.out.append(escape(data, quote=False))

    def handle_comment(self, data: str) -> None:
        return  # Comments, including conditional comments, are dropped.

    def handle_decl(self, decl: str) -> None:
        return

    def handle_pi(self, data: str) -> None:
        return

    def unknown_decl(self, data: str) -> None:
        return

    # Attributes ---------------------------------------------------------------
    def _attributes(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        allowed = _COMMON | _ATTRIBUTES.get(tag, set())
        rendered: list[str] = []
        seen: set[str] = set()
        for name, value in attrs:
            name = name.lower()
            if name in seen:
                continue
            seen.add(name)
            if name in _REMOTE_ATTRIBUTES:
                if value and value.strip():
                    self.blocked["styles" if name == "background" else "other"] += 1
                continue
            if name not in allowed:
                continue
            if tag == "a" and name == "href":
                link = _link(value or "")
                if link is None:
                    if _scheme(value or "") in {"javascript", "vbscript", "data"}:
                        self.removed_active += 1
                    continue
                self.links += 1
                # No href to follow: the view opens it, also by keyboard. Its title
                # is the destination, never text the sender chose.
                rendered.append(
                    f' data-pmm-href="{escape(link)}" tabindex="0" role="link"'
                    f' title="{escape(link)}"'
                )
                continue
            if name == "title" and (tag == "a" or "a" in self.open):
                continue  # A tooltip inside a link could pose as its destination.
            clean = self._value(tag, name, value)
            if clean is None:
                continue
            rendered.append(f" {name}" if clean is True else f' {name}="{escape(clean)}"')
        return "".join(rendered)

    def _value(self, tag: str, name: str, value: str | None) -> str | bool | None:
        if name in _FLAGS:
            return True
        if value is None:
            return None
        value = value.strip()
        if name == "style":
            return self._style_attribute(value) or None
        if name in {"title", "alt", "summary", "abbr"}:
            return value[:500]
        if name in _KEYWORDS:
            return value.lower() if value.lower() in _KEYWORDS[name] else None
        if name in {"width", "height"}:
            return value if _LENGTH.fullmatch(value) else None
        if name in {"colspan", "rowspan", "border", "cellpadding", "cellspacing", "start",
                    "span", "hspace", "vspace", "value"}:  # fmt: skip
            return value if _NUMBER.fullmatch(value) else None
        if name == "size":
            return value if re.fullmatch(r"[+-]?\d{1,3}", value) else None
        if name in {"bgcolor", "color", "text"}:
            return value if _COLOR.fullmatch(value) else None
        if name == "type":
            return value if value in _LIST_TYPES else None
        if name == "lang":
            return value if _LANG.fullmatch(value) else None
        if name == "class":
            return value if _CLASS.fullmatch(value) else None
        if name == "face":
            return value if _FACE.fullmatch(value) else None
        if name == "datetime":
            return value if _DATETIME.fullmatch(value) else None
        return None

    def _style_attribute(self, value: str) -> str:
        kept = []
        for declaration in value.split(";"):
            name, separator, setting = declaration.partition(":")
            name, setting = name.strip().lower(), setting.strip()
            if not separator or not name or not setting:
                continue
            if _CSS_LOADS.search(setting):
                self.blocked["styles"] += 1
                continue
            if (
                len(setting) > 300
                or _CSS_UNSAFE.search(setting)
                or not re.fullmatch(r"-?[a-z][a-z-]{0,40}", name)
                or not (name in _CSS_SAFE_PROPERTIES or name.startswith(_CSS_SAFE_PREFIXES))
                or (name == "position" and setting.lower() not in {"static", "relative"})
            ):
                continue
            kept.append(f"{name}: {setting}")
        return "; ".join(kept)

    def _style_block(self, css: str) -> None:
        css = re.sub(r"(?s)/\*.*?(?:\*/|$)", " ", css)
        # Rules for focused elements match nothing, so message styles can neither hide
        # the focused link nor make something else look focused.
        css = _FOCUS_SELECTOR.sub(":not(*)", css)
        loads = len(_CSS_LOADS.findall(css)) + len(re.findall(r"(?i)@font-face", css))
        if loads or "\\" in css or _STYLE_BLOCK_UNSAFE.search(css):
            # A block that loads, escapes or scripts anything is dropped whole.
            self.blocked["styles"] += max(loads, 1)
            return
        if css.strip():
            self.out.append("<style>" + css.replace("<", " ") + "</style>")

    def _body(self, attrs: list[tuple[str, str | None]]) -> None:
        # The frame has its own body; keep the message body's colors on a wrapper.
        values = {name.lower(): value for name, value in reversed(attrs)}
        colors = [
            f"{prop}: {value.strip()}"
            for prop, value in (
                ("background-color", values.get("bgcolor")),
                ("color", values.get("text")),
            )
            if value and _COLOR.fullmatch(value.strip())
        ]
        if values.get("background"):
            self.blocked["styles"] += 1
        style = self._style_attribute(values.get("style") or "")
        declarations = "; ".join(colors + ([style] if style else []))
        style_attribute = f' style="{escape(declarations)}"' if declarations else ""
        self.out.append(f'<div class="pmm-body"{style_attribute}>')
        self.open.append("body")

    def _image(self, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value for name, value in reversed(attrs)}
        alternatives = [
            name for name in ("srcset", "lowsrc", "dynsrc") if (values.get(name) or "").strip()
        ]
        self.blocked["images"] += len(alternatives)
        if (values.get("longdesc") or "").strip():
            self.blocked["other"] += 1
        safe = [(name, value) for name, value in attrs if name.lower() not in _REMOTE_ATTRIBUTES]
        source = _URL_IGNORED.sub("", values.get("src") or "").strip(_URL_TRIM)
        if _DATA_IMAGE.fullmatch(source):
            keep = [(name, value) for name, value in safe if name.lower() != "src"]
            self.out.append(f'<img src="{escape(source)}"{self._attributes("img", keep)}>')
            return
        if not source:
            return  # Nothing would display.
        inline = _scheme(source) == "cid"
        self.blocked["inline_images" if inline else "images"] += 1
        width, height = (_pixels(values.get(name)) for name in ("width", "height"))
        if (width is not None and width <= 2) or (height is not None and height <= 2):
            return  # Tracking pixels leave no placeholder.
        label = (values.get("alt") or "").strip()[:200] or "image"
        size = "".join(
            f"{name}:{value}px;"
            for name, value in (("width", width), ("height", height))
            if value is not None
        )
        style = f' style="{size}"' if size else ""
        title = "Inline image not downloaded" if inline else "Remote image blocked"
        self.out.append(
            f'<span class="pmm-blocked" title="{title}"{style}>{escape(label, quote=False)}</span>'
        )

    def result(self) -> str:
        while self.open:
            current = self.open.pop()
            self.out.append("</div>" if current == "body" else f"</{current}>")
        return "".join(self.out)


def _scheme(url: str) -> str | None:
    cleaned = _URL_IGNORED.sub("", url).strip(_URL_TRIM)
    match = _SCHEME.match(cleaned)
    return match.group(1).lower() if match else None


def _link(url: str) -> str | None:
    """An absolute http, https or mailto target, as browsers would read it."""
    cleaned = _URL_IGNORED.sub("", url).strip(_URL_TRIM)
    if len(cleaned) > 2048 or _scheme(cleaned) not in {"http", "https", "mailto"}:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        return None
    return cleaned


def _pixels(value: str | None) -> int | None:
    if value is None or not re.fullmatch(r"\s*\d{1,4}(?:px)?\s*", value):
        return None
    return min(int(value.strip().removesuffix("px")), 1200)


def sanitize_email_html(markup: str, *, truncated: bool = False) -> dict[str, Any]:
    """Allowlisted HTML plus counts of what was removed; never raises."""
    if truncated and markup.rfind("<") > markup.rfind(">"):
        # A capped body can end inside a tag; drop that fragment, not its text.
        markup = markup[: markup.rfind("<")]
    parser = _Sanitizer()
    try:
        parser.feed(markup)
        parser.close()
        html = parser.result()
    except Exception:
        return {
            "html": None,
            "remote_blocked": parser.blocked,
            "removed_active": parser.removed_active,
            "links": 0,
        }
    return {
        "html": html,
        "remote_blocked": parser.blocked,
        "removed_active": parser.removed_active,
        "links": parser.links,
    }


def email_document(fragment: str) -> str:
    """The sandboxed frame's whole document: its own policy, base styles, the fragment."""
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{FRAME_CSP}">'
        '<meta name="referrer" content="no-referrer">'
        f"<style>{_FRAME_CSS}</style></head><body>{fragment}</body></html>"
    )
