"""Email HTML sanitizing for views: allowlisted markup, no active or remote content."""

import re

import pytest
from view_assertions import inventory

from proton_mail_mcp.email_html import FRAME_CSP, email_document, sanitize_email_html

REMOTE = re.compile(r"(?i)\b(?:https?|ftp|wss?):|//[a-z0-9]|url\s*\(|@import")


def clean(markup, **options):
    return sanitize_email_html(markup, **options)


def assert_inert(html):
    """No script, handler, frame, form, loadable URL or style escape survives."""
    parsed = inventory(html)
    assert {
        "script",
        "iframe",
        "frame",
        "object",
        "embed",
        "form",
        "input",
        "button",
        "link",
        "meta",
        "base",
        "svg",
        "math",
        "video",
        "audio",
        "source",
        "textarea",
        "select",
        "noscript",
        "template",
        "title",
    }.isdisjoint(parsed.tags)
    for tag, name, value in parsed.attributes:
        assert not name.startswith("on"), (tag, name)
        assert name not in {"href", "srcset", "background", "poster", "action", "id", "name"}
        if name == "src":
            assert tag == "img" and re.fullmatch(
                r"data:image/(png|gif|jpe?g|webp|bmp);base64,\S+", value
            )
        if name == "data-pmm-href":
            assert re.match(r"(https?|mailto):", value)
    without_links = re.sub(r'(data-pmm-href|title)="[^"]*"', "", html)
    assert not REMOTE.search(without_links), html
    assert "javascript" not in html.lower() and "expression" not in html.lower()


@pytest.mark.parametrize(
    "markup",
    [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "<scr<script>ipt>alert(1)</script>",
        '<a href="jav&#x09;ascript:alert(1)">x</a>',
        '<a href="JaVaScRiPt:alert(1)">x</a>',
        '<a href="data:text/html;base64,PHNjcmlwdD4=">x</a>',
        '<a href="vbscript:msgbox">x</a>',
        '<svg><script>alert(1)</script><a xlink:href="javascript:1">x</a></svg>',
        "<svg/onload=alert(1)>",
        '<math><mi xlink:href="javascript:1">x</mi></math>',
        '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
        '<iframe src="https://evil.example/"></iframe>',
        '<meta http-equiv="refresh" content="0;url=https://evil.example/">',
        '<base href="https://evil.example/">',
        '<form action="https://evil.example/"><input name=a><button formaction=x>Go</button>',
        '<object data="https://evil.example/x.swf"></object>',
        '<embed src="https://evil.example/x">',
        '<link rel=stylesheet href="https://evil.example/x.css">',
        "<style>@import url(https://evil.example/x.css);</style>",
        "<style>p{background:url(https://evil.example/p.png)}</style>",
        "<style>p{background:u\\72l(https://evil.example/p.png)}</style>",
        "<style>@font-face{font-family:x;src:url(https://evil.example/f.woff)}</style>",
        "<style>p{width:expression(alert(1))}</style>",
        "<style>p{-moz-binding:url(x.xml#x)}</style>",
        '<div style="background:url(http://evil.example/y.png)">t</div>',
        "<div style=\"background-image: image-set('https://evil.example/a.png' 1x)\">t</div>",
        '<div style="width: expression(alert(1))">t</div>',
        '<div style="color: red; behavior: url(x.htc)">t</div>',
        '<div style="background: u\\rl(https://evil.example/y.png)">t</div>',
        '<img src="https://tracker.example/p.gif" width=1 height=1>',
        '<img srcset="https://evil.example/1x.png 1x">',
        '<img src="data:image/svg+xml;base64,PHN2Zz4=">',
        '<table background="https://evil.example/bg.png"><tr><td>c</td></tr></table>',
        '<video poster="https://evil.example/p.png"><source src="https://evil.example/v.mp4"></video>',
        '<audio src="https://evil.example/a.mp3"></audio>',
        '<input type="image" src="https://evil.example/x.png">',
        "<!--[if mso]><script>alert(1)</script><![endif]-->",
        '<noscript><p title="</noscript><img src=x onerror=alert(1)>"></noscript>',
        "<title><img src=x onerror=alert(1)></title>",
        "<textarea><img src=x onerror=alert(1)></textarea>",
        "<template><img src=x onerror=alert(1)></template>",
        '<p title="&quot;><script>alert(1)</script>">x</p>',
        "<x-custom onmouseover=alert(1)>c</x-custom>",
        '<div id="body" name="forms">clobber</div>',
        '<a href="https://ex.example/" onclick="steal()">x</a>',
        "<style>p{color:red}</sty" + "le><p>after</p><style>a{x:1}</style >",
        "<![CDATA[<script>alert(1)</script>]]>",
        "<?xml-stylesheet href='https://evil.example/x.xsl'?>",
        '<img src="  https://evil.example/padded.png  ">',
        '<img src="//evil.example/protocol-relative.png">',
        '<picture><source srcset="https://evil.example/p.webp"><img src="https://evil.example/p.png"></picture>',
        "<details open ontoggle=alert(1)><summary>S</summary>D</details>",
        '<a href="https://ex.example/" target="_top" ping="https://evil.example/ping">x</a>',
        '<area href="javascript:alert(1)">',
        "<marquee onstart=alert(1)>m</marquee>",
        '<body onload=alert(1) background="https://evil.example/b.png">b</body>',
    ],
)
def test_hostile_markup_is_rendered_inert(markup):
    result = clean(markup)
    assert result["html"] is not None
    assert_inert(result["html"])
    # The frame document adds only its own head: policy, referrer and fixed styles.
    document = email_document(result["html"])
    assert document.endswith("<body>" + result["html"] + "</body></html>")
    assert document.count("<script") == result["html"].count("<script") == 0


def test_ordinary_email_markup_survives():
    markup = (
        "<html><head><title>T</title><style>.wrap{max-width:560px} h2{color:#6d4aff}</style>"
        '</head><body bgcolor="#f4f4f8" text="#222" style="margin:0"><div class="wrap">'
        "<h2>Update</h2><p>Hello &amp; <b>welcome</b>, <i>friend</i>.</p>"
        '<table width="100%" cellpadding="8" style="border-collapse:collapse">'
        '<tr><td bgcolor="#ffffff" colspan="2" style="border:1px solid #ddd; '
        "font-family: 'Helvetica Neue', Arial\">Cell</td></tr></table>"
        '<ul type="square"><li>One</li></ul><blockquote>Quoted</blockquote>'
        '<font color="red" face="Arial" size="2">Font</font><br><hr width="80%">'
        "<details open><summary>More</summary>Hidden</details></div></body></html>"
    )
    result = clean(markup)
    html = result["html"]
    assert result["remote_blocked"] == {"images": 0, "inline_images": 0, "styles": 0, "other": 0}
    assert result["removed_active"] == 0
    assert "<style>.wrap{max-width:560px} h2{color:#6d4aff}</style>" in html
    assert (
        '<div class="pmm-body" style="background-color: #f4f4f8; color: #222; margin: 0">' in html
    )
    assert "<p>Hello &amp; <b>welcome</b>, <i>friend</i>.</p>" in html
    assert '<td bgcolor="#ffffff" colspan="2" style="border: 1px solid #ddd; ' in html
    assert "font-family: &#x27;Helvetica Neue&#x27;, Arial" in html
    assert '<ul type="square"><li>One</li></ul><blockquote>Quoted</blockquote>' in html
    assert '<font color="red" face="Arial" size="2">Font</font><br><hr width="80%">' in html
    assert "<details open><summary>More</summary>Hidden</details>" in html
    assert "<title>" not in html and ">T<" not in html


def test_links_become_inert_targets_that_the_view_opens_through_the_host():
    result = clean(
        '<a href=" https://ex.example/a?b=1&amp;c=2 ">A</a>'
        '<a href="mailto:x@ex.example" title="Mail">M</a>'
        '<a href="#top">anchor</a><a href="/relative">rel</a><a href="cid:part">cid</a>'
    )
    # Links the view opens are focusable, so Enter and Space reach them, and their
    # title is the destination.
    assert result["html"] == (
        '<a data-pmm-href="https://ex.example/a?b=1&amp;c=2" tabindex="0" role="link" '
        'title="https://ex.example/a?b=1&amp;c=2">A</a>'
        '<a data-pmm-href="mailto:x@ex.example" tabindex="0" role="link" '
        'title="mailto:x@ex.example">M</a>'
        "<a>anchor</a><a>rel</a><a>cid</a>"
    )
    assert result["links"] == 2


def test_link_tooltips_show_the_destination_never_sender_text():
    result = clean(
        '<a title="https://bank.example/" href="https://evil.example/login">'
        '<span title="https://bank.example/secure">Bank</span>'
        '<img src="data:image/png;base64,AAAA" title="https://bank.example/logo" alt="Logo">'
        '</a><a title="Not a link">x</a><p title="A note">Outside</p>'
    )
    assert result["html"] == (
        '<a data-pmm-href="https://evil.example/login" tabindex="0" role="link" '
        'title="https://evil.example/login"><span>Bank</span>'
        '<img src="data:image/png;base64,AAAA" alt="Logo"></a>'
        '<a>x</a><p title="A note">Outside</p>'
    )


def test_images_are_blocked_counted_and_replaced_by_sized_placeholders():
    result = clean(
        '<img src="https://cdn.example/hero.png" width="600" height="200" alt="Hero">'
        '<img src="http://tracker.example/open.gif" width="1" height="1">'
        '<img src="cid:logo@part" alt="Logo">'
        '<img src="data:image/png;base64,iVBORw0KGgo=" alt="Inline" width="10">'
        '<img srcset="https://cdn.example/2x.png 2x" src="https://cdn.example/1x.png">'
        "<img>"
    )
    assert result["remote_blocked"] == {"images": 4, "inline_images": 1, "styles": 0, "other": 0}
    assert result["html"] == (
        '<span class="pmm-blocked" title="Remote image blocked" '
        'style="width:600px;height:200px;">Hero</span>'
        '<span class="pmm-blocked" title="Inline image not downloaded">Logo</span>'
        '<img src="data:image/png;base64,iVBORw0KGgo=" alt="Inline" width="10">'
        '<span class="pmm-blocked" title="Remote image blocked">image</span>'
    )


def test_styles_keep_safe_declarations_and_count_remote_ones():
    result = clean(
        '<p style="color: red; background: url(https://x.example/a.png); position: fixed; '
        'top: 0; width: expression(1); margin:0 auto; mso-line-height-rule: exactly">x</p>'
        "<style>a{color:blue}</style><style>b{background:url(https://x.example/b.png)}</style>"
    )
    assert result["html"] == (
        '<p style="color: red; top: 0; margin: 0 auto">x</p><style>a{color:blue}</style>'
    )
    assert result["remote_blocked"]["styles"] == 2


def test_markup_nesting_is_rebuilt_and_closed():
    assert clean("<p>unclosed <b>bold <i>it")["html"] == "<p>unclosed <b>bold <i>it</i></b></p>"
    assert clean("<div><b>x</div>y</b>")["html"] == "<div><b>x</b></div>y"
    assert clean("</p></div>text<br/>")["html"] == "text<br>"
    assert clean("<svg><svg></svg>still hidden</svg>shown")["html"] == "shown"


def test_truncated_markup_drops_only_the_partial_tag():
    assert clean('<p>Hello</p><img src="https://x.ex', truncated=True)["html"] == "<p>Hello</p>"
    assert clean("<p>1 < 2</p>", truncated=True)["html"] == "<p>1 &lt; 2</p>"


def test_parser_failures_fail_closed(monkeypatch):
    def explode(self, data):
        raise RuntimeError("synthetic parser failure")

    monkeypatch.setattr("proton_mail_mcp.email_html._Sanitizer.feed", explode)
    assert clean("<p>x</p>")["html"] is None


def test_frame_document_carries_its_own_strict_policy():
    document = email_document("<p>x</p>")
    parsed = inventory(document)
    policies = [
        value for tag, name, value in parsed.attributes if tag == "meta" and name == "content"
    ]
    assert FRAME_CSP in policies
    assert FRAME_CSP == (
        "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:"
    )
    assert "script" not in parsed.tags
    assert document.endswith("<body><p>x</p></body></html>")
    # The document's first cascade layer, which outranks every message style sheet,
    # keeps the root at its content height, so a message's html,body{height:100%}
    # cannot make the frame grow with itself, and keeps keyboard focus visible.
    assert (
        "<style>@layer{html,body{height:auto !important;min-height:0 !important;}"
        "*{transition:none !important;}"
        ":focus-visible{outline:2px solid #3d5bd9 !important;outline-offset:2px !important;}}"
    ) in document
    assert document.count("@layer") == 1


def test_message_styles_cannot_hide_or_imitate_keyboard_focus():
    result = clean(
        '<a href="https://ex.example/" style="color: red; outline: none !important; '
        'Outline-Offset: -999px; outline-color: transparent">x</a>'
        '<p style="outline: 2px solid #3d5bd9; margin: 0">Not focused</p>'
        "<style>a:focus{opacity:0} p:has(a:FOCUS-VISIBLE){visibility:hidden} "
        "div:focus-within, a:-moz-focusring{outline:2px solid #3d5bd9} "
        "a:not(:focus){color:blue} a:hover{color:green}</style>"
    )
    # Style attributes lose every outline; nothing else about them changes.
    assert result["html"].startswith(
        '<a data-pmm-href="https://ex.example/" tabindex="0" role="link" '
        'title="https://ex.example/" style="color: red">x</a><p style="margin: 0">Not focused</p>'
    )
    # Rules for focused elements match nothing; every other rule is kept as written.
    assert result["html"].endswith(
        "<style>a:not(*){opacity:0} p:has(a:not(*)){visibility:hidden} "
        "div:not(*), a:not(*){outline:2px solid #3d5bd9} "
        "a:not(:not(*)){color:blue} a:hover{color:green}</style>"
    )
    assert result["remote_blocked"]["styles"] == 0
