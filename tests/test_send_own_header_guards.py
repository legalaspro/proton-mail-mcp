"""Only a draft to the user's own addresses alone skips the host form, however it is spelled."""

import base64

import pytest
from test_send import DRAFT_ID, RECIPIENT, Host, local_certificate, records, redeem  # noqa: F401
from test_send import rig as _send_rig
from test_send_own_recipients import bridge_draft
from test_send_token_approval import FAILURES, assert_burned
from test_send_token_approval import own_rig as _own_rig

from proton_mail_mcp import send as send_module
from proton_mail_mcp import send_address as address_module

rig = _send_rig
own_rig = _own_rig
OTHER = "other@example.test"
TOKEN, HOST, REFUSED = "token", "host_confirmation", None
BIDI_NAME = "Na\u202eme"


def encoded_name(value):
    return "=?utf-8?b?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def case(to, expected, *extra_headers, prefix="To: ", id):
    return pytest.param(prefix + to, extra_headers, expected, id=id)


@pytest.mark.parametrize(
    "to_line,extra_headers,expected",
    [
        # Whitespace and a plain display name do not change the recipient.
        case(RECIPIENT, TOKEN, id="no-space-after-colon", prefix="To:"),
        case(RECIPIENT, TOKEN, id="two-spaces-after-colon", prefix="To:  "),
        case(RECIPIENT, TOKEN, id="tab-after-colon", prefix="To:\t"),
        case(f"{RECIPIENT} ", TOKEN, id="trailing-space"),
        case(f"Recipient <{RECIPIENT}>", TOKEN, id="unquoted-display"),
        # A valid draft with any other recipient set needs the host form.
        case(f"{RECIPIENT}, {RECIPIENT}", TOKEN, id="duplicate-owner"),
        case(f"{RECIPIENT}, {OTHER}", HOST, id="owner-and-other"),
        case(f'"Name" <{RECIPIENT}>, {OTHER}', HOST, id="named-owner-and-other"),
        case(f"Name <{OTHER}>", HOST, id="nonowner-named"),
        case(RECIPIENT.upper(), TOKEN, id="uppercase-owner"),
        case(RECIPIENT, HOST, f"Cc: {OTHER}", id="cc-other"),
        case(RECIPIENT, HOST, f"Bcc: {OTHER}", id="bcc-other"),
        case(f"{RECIPIENT}\r\nBcc: {OTHER}", HOST, id="header-injection"),
        # Everything else is refused before any token exists, on either path.
        case(f'"{RECIPIENT}" <{OTHER}>', REFUSED, id="owner-display-other-address"),
        case(f'"{OTHER}" <{RECIPIENT}>', REFUSED, id="other-display-owner-address"),
        case(f"grp: {RECIPIENT};", REFUSED, id="group"),
        case("grp:;", REFUSED, id="empty-group"),
        case(f"{RECIPIENT} ({OTHER})", REFUSED, id="comment-hides-address"),
        case(f"(Name) {RECIPIENT}", REFUSED, id="leading-comment"),
        case("recipient(Name)@example.test", REFUSED, id="embedded-comment"),
        case(f'"Na(me" <{RECIPIENT}>', REFUSED, id="quoted-open-parenthesis"),
        case(f'"Na)me" <{RECIPIENT}>', REFUSED, id="quoted-close-parenthesis"),
        case(f"<{RECIPIENT}> <{OTHER}>", REFUSED, id="second-angle-pair"),
        case(f"<<{RECIPIENT}>>", REFUSED, id="nested-angle-pair"),
        case(f'"Na<me" <{RECIPIENT}>', REFUSED, id="angle-in-name"),
        case(f'"Na,me" <{RECIPIENT}>', REFUSED, id="comma-in-name"),
        case(f'"Na;me" <{RECIPIENT}>', REFUSED, id="semicolon-in-name"),
        case(f'"Na:me" <{RECIPIENT}>', REFUSED, id="colon-in-name"),
        case(f'"Na\\me" <{RECIPIENT}>', REFUSED, id="backslash-in-name"),
        case(f"Na\x00me <{RECIPIENT}>", REFUSED, id="nul-in-name"),
        case(f"Na\x1fme <{RECIPIENT}>", REFUSED, id="control-in-name"),
        case(f"Na\x7fme <{RECIPIENT}>", REFUSED, id="delete-in-name"),
        case(f"Náme <{RECIPIENT}>", REFUSED, id="raw-unicode"),
        case(f"Na\u202eme <{RECIPIENT}>", REFUSED, id="raw-bidi"),
        case("rеcipient@example.test", REFUSED, id="raw-homoglyph"),
        case(f"Name\r\n <{RECIPIENT}>", REFUSED, id="folded-to"),
        case(f"\r\n {RECIPIENT}", REFUSED, id="fold-right-after-colon", prefix="To:"),
        case(f"\r\n\t{RECIPIENT}", REFUSED, id="tab-fold-right-after-colon", prefix="To:"),
        case(f"\r\n {RECIPIENT}", REFUSED, id="fold-after-colon-and-space"),
        case(f"\n {RECIPIENT}", REFUSED, id="lf-fold-right-after-colon", prefix="To:"),
        case(RECIPIENT, REFUSED, f"Cc:\r\n {OTHER}", id="cc-fold-right-after-colon"),
        case(RECIPIENT, REFUSED, f"Cc: \r\n\t{OTHER}", id="cc-fold-after-colon-and-space"),
        case(RECIPIENT, REFUSED, f"Bcc:\r\n {OTHER}", id="bcc-fold-right-after-colon"),
        case(RECIPIENT, REFUSED, f"bCC:\n {OTHER}", id="bcc-lf-fold-right-after-colon"),
        case(f"{RECIPIENT}\r", REFUSED, id="bare-cr"),
        case(f"{RECIPIENT}\n", REFUSED, id="bare-lf"),
        case(f"{RECIPIENT}\x00", REFUSED, id="bare-nul"),
        case(f"{RECIPIENT}\t", REFUSED, id="trailing-tab"),
        case(f"{RECIPIENT}\r\nTo: {RECIPIENT}", REFUSED, id="duplicate-to"),
        case(f"{RECIPIENT},", REFUSED, id="trailing-comma"),
        case(f",{RECIPIENT}", REFUSED, id="leading-comma"),
        case(f"{RECIPIENT},,", REFUSED, id="empty-extra-mailboxes"),
        case("", REFUSED, id="empty-to"),
        case(" ", REFUSED, id="blank-to"),
        case("not-an-address", REFUSED, id="unparsable"),
        case(f"Name <{RECIPIENT}", REFUSED, id="missing-close-angle"),
        case(f"Name {RECIPIENT}>", REFUSED, id="missing-open-angle"),
        case(f'"Name <{RECIPIENT}>', REFUSED, id="unclosed-quote"),
        case(RECIPIENT, REFUSED, "Cc: ", id="empty-cc-header"),
        case(RECIPIENT, REFUSED, "Bcc: ", id="empty-bcc-header"),
        # Encoded names refuse however they decode; only From may carry one.
        case(f"{encoded_name('Plain Owner')} <{RECIPIENT}>", REFUSED, id="encoded-plain-name"),
        case(f"{encoded_name('René')} <{RECIPIENT}>", REFUSED, id="encoded-unicode-name"),
        case(f"{encoded_name(OTHER)} <{RECIPIENT}>", REFUSED, id="encoded-other-address"),
        case(f"{encoded_name(BIDI_NAME)} <{RECIPIENT}>", REFUSED, id="encoded-bidi"),
        case(f"=?utf-8?q?Name <{RECIPIENT}>", REFUSED, id="incomplete-encoded-word"),
    ],
)
def test_raw_draft_gets_only_its_approval_path(own_rig, to_line, extra_headers, expected):
    rig = own_rig
    raw = bridge_draft(RECIPIENT, extra_headers=extra_headers)
    rig.client.raw = raw.replace(f"To: {RECIPIENT}".encode("ascii"), to_line.encode("utf-8"))
    if expected is REFUSED:
        with pytest.raises(FAILURES):
            rig.service.preview_send(DRAFT_ID)
        assert rig.tokens._grants == {}
        return
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == expected
    if expected == TOKEN:
        assert {address.lower() for address in preview["to"]} == {RECIPIENT}
        assert preview["cc"] == preview["bcc"] == []
    else:
        host = Host(support=False)
        with pytest.raises(FAILURES):
            redeem(rig, preview["token"], host)
        assert records(rig)[-1]["result"] == "confirmation_unavailable"
    assert rig.smtp.calls == []


def test_token_approval_is_rechecked_with_unchanged_binding_before_send(own_rig, monkeypatch):
    eligible = [True]
    checked = []

    def own_recipients(snapshot):
        checked.append(eligible[0])
        return eligible[0]

    monkeypatch.setattr(send_module, "_own_recipients", own_recipients)
    preview = own_rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "token" and checked == [True]
    eligible[0] = False
    host = Host(support=False)
    with pytest.raises(FAILURES):
        redeem(own_rig, preview["token"], host)
    assert False in checked
    assert own_rig.smtp.calls == [] and host.prompts == []
    assert records(own_rig)[-1]["result"] == "recipients_changed"
    assert_burned(own_rig, preview["token"])


def test_exact_quoted_address_display_is_unambiguous_for_snapshot():
    assert address_module.parse_addresses(
        f'"{RECIPIENT}" <{RECIPIENT}>', allow_display_names=True
    ) == [RECIPIENT]


@pytest.mark.parametrize(
    "display,address",
    [(RECIPIENT, OTHER), (OTHER, RECIPIENT), ("Name " + RECIPIENT, RECIPIENT)],
)
def test_other_at_bearing_display_names_keep_refusing(display, address):
    with pytest.raises(ValueError):
        address_module.parse_addresses(f'"{display}" <{address}>', allow_display_names=True)


def test_exact_address_display_still_refuses_in_bare_configuration_parser():
    with pytest.raises(ValueError):
        address_module.parse_addresses(f'"{RECIPIENT}" <{RECIPIENT}>')
