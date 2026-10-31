"""Exact-address provider exemptions never bypass the other sender protections."""

import hashlib
import json
from pathlib import Path

import pytest

from proton_mail_mcp import protected
from proton_mail_mcp.config import ConfigError
from proton_mail_mcp.protected import ProtectedPolicy

APPROVED = "approved-sender@gmail.com"
ENTRY_DIGEST = hashlib.sha256(APPROVED.encode()).hexdigest()


def local_policy(tmp_path, **fields):
    path = tmp_path / "synthetic-policy.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return ProtectedPolicy(str(path))


@pytest.fixture
def allowlisted(tmp_path):
    # The committed allowlist is empty: an approved sender is always local configuration.
    return local_policy(tmp_path, personal_provider_allowlist=[APPROVED])


def test_committed_defaults_name_no_sender_or_institution():
    policy = ProtectedPolicy()
    assert policy.rules["personal_provider_allowlist"] == ()
    assert policy.rules["addresses"] == policy.rules["domains"] == ()
    # Pinned: a name added back to the generic lists must fail here.
    assert policy.rules["domain_patterns"] == (
        "*bank*",
        "*broker*",
        "*insurance*",
        "*versicherung*",
        "*assurance*",
        "*steueramt*",
        "*steuerverwaltung*",
        "*finanzamt*",
        "*.gov",
        "*.gov.*",
        "*.gouv.*",
        "*.gob.*",
        "*.gv.*",
    )
    assert policy.rules["keywords"] == (
        "bank",
        "broker",
        "insurance",
        "versicherung",
        "assurance",
        "steueramt",
        "steuerverwaltung",
        "finanzamt",
        "tax authority",
        "tax administration",
        "internal revenue",
        "government",
        "bundesamt",
        "canton",
        "kanton",
        "confederation",
        "confédération",
        "federal office",
        "gemeinde",
        "municipality",
    )
    assert policy.check({"from": APPROVED}) == ["Personal mail provider in From"]


def test_local_allowlist_entry_suppresses_the_provider_reason(allowlisted):
    reasons, bypasses = allowlisted.evaluate({"from": APPROVED})
    assert reasons == []
    assert bypasses == [{"header": "From", "entry_sha256": ENTRY_DIGEST}]
    assert allowlisted.check({"from": APPROVED}) == []


@pytest.mark.parametrize(
    "sender",
    ["other@gmail.com", "approved-sender+extra@gmail.com", "approved-sender@sub.gmail.com"],
)
def test_only_the_full_allowlisted_address_bypasses_the_provider(allowlisted, sender):
    reasons, bypasses = allowlisted.evaluate({"from": sender})
    assert reasons == ["Personal mail provider in From"]
    assert bypasses == []


@pytest.mark.parametrize(
    ("rules", "sender", "expected"),
    [
        ({"addresses": [APPROVED]}, APPROVED, "Protected personal address in From"),
        ({"domains": ["gmail.com"]}, APPROVED, "Protected sender domain in From"),
        (
            {"domain_patterns": ["*gmail*"]},
            APPROVED,
            "Protected financial, insurance or government domain in From",
        ),
        ({}, f"Bank Newsletter <{APPROVED}>", "Protected sender display name in From"),
    ],
)
def test_allowlist_does_not_bypass_other_protected_rules(tmp_path, rules, sender, expected):
    policy = local_policy(tmp_path, personal_provider_allowlist=[APPROVED], **rules)
    reasons, bypasses = policy.evaluate({"from": sender})
    assert expected in reasons
    assert "Personal mail provider in From" not in reasons
    assert bypasses == [{"header": "From", "entry_sha256": ENTRY_DIGEST}]


@pytest.mark.parametrize(("field", "label"), [("sender", "Sender"), ("reply_to", "Reply-To")])
def test_personal_address_in_another_header_still_blocks(allowlisted, field, label):
    reasons, bypasses = allowlisted.evaluate({"from": APPROVED, field: "other@gmail.com"})
    assert reasons == [f"Personal mail provider in {label}"]
    assert bypasses == [{"header": "From", "entry_sha256": ENTRY_DIGEST}]


@pytest.mark.parametrize(
    ("field", "label"), [("from", "From"), ("sender", "Sender"), ("reply_to", "Reply-To")]
)
def test_each_address_in_each_header_is_checked(allowlisted, field, label):
    row = {"from": "news@newsletter.example", field: f"{APPROVED}, other@gmail.com"}
    reasons, bypasses = allowlisted.evaluate(row)
    assert reasons == [f"Personal mail provider in {label}"]
    assert bypasses == [{"header": label, "entry_sha256": ENTRY_DIGEST}]


def test_bypasses_identify_each_header_without_duplicate_entries(allowlisted):
    reasons, bypasses = allowlisted.evaluate(
        {"from": f"{APPROVED}, {APPROVED}", "sender": APPROVED, "reply_to": APPROVED}
    )
    assert reasons == []
    assert bypasses == [
        {"header": label, "entry_sha256": ENTRY_DIGEST} for label in ("From", "Sender", "Reply-To")
    ]


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (
            {"from": APPROVED, "sender_invalid": True},
            "Unavailable, unsupported or ambiguous sender headers",
        ),
        ({"sender": APPROVED}, "Missing or unparseable From sender"),
        ({"from": None, "sender": APPROVED}, "Missing or unparseable From sender"),
        ({"from": APPROVED, "sender": None}, "Missing or unparseable Sender sender"),
        ({"from": APPROVED, "reply_to": ""}, "Missing or unparseable Reply-To sender"),
        ({"from": f"{APPROVED}, broken"}, "Unparseable From sender"),
        ({"from": f"{APPROVED}\nReply-To: hidden@example.test"}, "Unparseable From sender"),
        *[
            (
                {"from": "news@newsletter.example", field: f"{APPROVED}, {invalid}"},
                f"Unparseable {label} sender",
            )
            for field, label in (("from", "From"), ("sender", "Sender"), ("reply_to", "Reply-To"))
            for invalid in (f"x@{'a' * 64}.example", "x@[192.0.2.1]", "x@nodot")
        ],
    ],
)
def test_allowlist_never_clears_invalid_or_missing_sender_guards(allowlisted, row, reason):
    assert reason in allowlisted.check(row)


@pytest.mark.parametrize("sender", ["APPROVED-SENDER@GMAIL.COM", "Approved-Sender@Gmail.com"])
def test_allowlist_uses_complete_address_normalization(allowlisted, sender):
    reasons, bypasses = allowlisted.evaluate({"from": sender})
    assert reasons == []
    assert bypasses == [{"header": "From", "entry_sha256": ENTRY_DIGEST}]


@pytest.mark.parametrize(
    ("entry", "sender"),
    [
        ("News@BÜCHER.example", "NEWS@xn--bcher-kva.example"),
        ("NEWS@XN--BCHER-KVA.EXAMPLE", "News@bücher.example"),
    ],
)
def test_local_allowlist_normalizes_case_and_idna(tmp_path, entry, sender):
    policy = local_policy(
        tmp_path,
        personal_domains=["xn--bcher-kva.example"],
        personal_provider_allowlist=[entry],
    )
    expected = hashlib.sha256(b"news@xn--bcher-kva.example").hexdigest()
    reasons, bypasses = policy.evaluate({"from": sender})
    assert reasons == []
    assert bypasses == [{"header": "From", "entry_sha256": expected}]
    assert "news@xn--bcher-kva.example" in policy.rules["personal_provider_allowlist"]


@pytest.mark.parametrize("sender", ["news@bücher.example", "NEWS@XN--BCHER-KVA.EXAMPLE"])
@pytest.mark.parametrize(
    ("field", "configured", "reason"),
    [
        ("addresses", "NEWS@BÜCHER.example", "Protected personal address in From"),
        ("domains", "BÜCHER.example", "Protected sender domain in From"),
        ("domains", "*.BÜCHER.example.", "Protected sender domain in From"),
    ],
)
def test_allowlist_cannot_bypass_unicode_protected_identities(
    tmp_path, sender, field, configured, reason
):
    policy = local_policy(
        tmp_path,
        personal_domains=["xn--bcher-kva.example"],
        personal_provider_allowlist=["News@bücher.example"],
        **{field: [configured]},
    )
    reasons, bypasses = policy.evaluate({"from": sender})
    assert reasons == [reason]
    assert bypasses == [
        {
            "header": "From",
            "entry_sha256": hashlib.sha256(b"news@xn--bcher-kva.example").hexdigest(),
        }
    ]
    assert configured.casefold() in policy.rules[field]


def test_unicode_personal_domain_protects_nonallowlisted_addresses(tmp_path):
    policy = local_policy(
        tmp_path,
        personal_domains=["*.BÜCHER.example."],
        personal_provider_allowlist=["news@bücher.example"],
    )
    assert policy.check({"from": "other@xn--bcher-kva.example"}) == [
        "Personal mail provider in From"
    ]
    assert policy.check({"from": "other@sub.xn--bcher-kva.example"}) == [
        "Personal mail provider in From"
    ]
    assert policy.check({"from": "news@xn--bcher-kva.example"}) == []


@pytest.mark.parametrize("field", ["addresses", "domains", "personal_domains"])
def test_legacy_protected_identity_entries_remain_accepted_and_preserved(tmp_path, field):
    entries = ["Not a mailbox", "a@invalid..example", "invalid..example", "Family@Example.Test"]
    policy = local_policy(tmp_path, **{field: entries})
    assert all(entry.casefold() in policy.rules[field] for entry in entries)


@pytest.mark.parametrize(
    "entry",
    [
        "gmail.com",
        "@gmail.com",
        "*@gmail.com",
        "friend*@gmail.com",
        "friend?@gmail.com",
        "[friend]@gmail.com",
        "friend@*.gmail.com",
        "^friend.*@gmail.com$",
        "Friend <friend@gmail.com>",
        "friend@gmail.com, other@gmail.com",
        "friends:friend@gmail.com;",
        "friend(comment)@gmail.com",
        "friend @gmail.com",
        "friend@@gmail.com",
        "friend@",
        "x@ ",
        "a.b@",
        "approved-sender@",
        "friend@localhost",
        "friend@gmail.com.",
        "friend@invalid..example",
        "friend@invalid_domain.example",
        "friend@-invalid.example",
        ".friend@gmail.com",
        "friend..news@gmail.com",
        "friend@[127.0.0.1]",
        "friend@gmail.com\n",
        "friend\x7f@gmail.com",
        "",
        None,
        1,
    ],
)
def test_invalid_allowlist_entries_fail_closed_without_echoing_contents_or_path(tmp_path, entry):
    path = tmp_path / "do-not-disclose-this-path.json"
    path.write_text(json.dumps({"personal_provider_allowlist": [entry]}), encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        ProtectedPolicy(str(path))
    assert str(caught.value) == "Cannot load a valid protected sender configuration"
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("value", [APPROVED, {}, None, True])
def test_allowlist_must_be_a_list(tmp_path, value):
    with pytest.raises(ConfigError, match="^Cannot load a valid protected sender configuration$"):
        local_policy(tmp_path, personal_provider_allowlist=value)


def test_terminal_domain_dot_sender_remains_unparseable(allowlisted):
    reasons, bypasses = allowlisted.evaluate({"from": f"{APPROVED}."})
    assert reasons == ["Unparseable From sender"]
    assert bypasses == []


def test_unknown_policy_fields_still_fail_closed(tmp_path):
    with pytest.raises(ConfigError, match="^Cannot load a valid protected sender configuration$"):
        local_policy(tmp_path, personal_provider_allowlist=[APPROVED], allow_any_sender=True)


def test_local_allowlist_normalizes_duplicates(tmp_path):
    policy = local_policy(
        tmp_path,
        personal_provider_allowlist=[APPROVED, APPROVED.upper(), "other@gmail.com"],
    )
    assert policy.rules["personal_provider_allowlist"] == (APPROVED, "other@gmail.com")
    assert policy.check({"from": APPROVED}) == []
    assert policy.check({"from": "other@gmail.com"}) == []
    assert policy.check({"from": "third@gmail.com"}) == ["Personal mail provider in From"]


def test_replace_uses_only_explicit_allowlist(tmp_path):
    policy = local_policy(
        tmp_path,
        mode="replace",
        personal_domains=["gmail.com"],
        personal_provider_allowlist=["other@gmail.com"],
    )
    assert policy.rules["personal_provider_allowlist"] == ("other@gmail.com",)
    assert policy.check({"from": "other@gmail.com"}) == []
    assert policy.check({"from": APPROVED}) == ["Personal mail provider in From"]
    empty = local_policy(tmp_path, mode="replace", personal_domains=["gmail.com"])
    assert empty.rules["personal_provider_allowlist"] == ()
    assert empty.check({"from": APPROVED}) == ["Personal mail provider in From"]


def committed(monkeypatch, tmp_path, **fields):
    data = json.loads(Path(protected.__file__).with_name("protected_default.json").read_text())
    (tmp_path / "protected_default.json").write_text(json.dumps({**data, **fields}))
    monkeypatch.setattr(protected, "__file__", str(tmp_path / "protected.py"))


def test_extend_keeps_committed_allowlist_entries(tmp_path, monkeypatch):
    committed(monkeypatch, tmp_path, personal_provider_allowlist=[APPROVED])
    policy = local_policy(tmp_path, personal_provider_allowlist=["other@gmail.com"])
    assert policy.rules["personal_provider_allowlist"] == (APPROVED, "other@gmail.com")
    assert policy.check({"from": APPROVED}) == []


def test_replace_discards_committed_allowlist_entries(tmp_path, monkeypatch):
    committed(monkeypatch, tmp_path, personal_provider_allowlist=[APPROVED])
    policy = local_policy(tmp_path, mode="replace", personal_domains=["gmail.com"])
    assert policy.rules["personal_provider_allowlist"] == ()
    assert policy.check({"from": APPROVED}) == ["Personal mail provider in From"]


def test_disabling_personal_provider_guard_does_not_create_bypass_records(tmp_path):
    policy = local_policy(
        tmp_path, protect_personal_domains=False, personal_provider_allowlist=[APPROVED]
    )
    assert policy.evaluate({"from": APPROVED}) == ([], [])
    assert policy.evaluate({"from": "other@gmail.com"}) == ([], [])
    assert policy.check({"from": f"Bank <{APPROVED}>"})


def test_nonpersonal_provider_allowlist_entry_does_not_create_bypass(tmp_path):
    policy = local_policy(tmp_path, personal_provider_allowlist=["news@newsletter.example"])
    assert policy.evaluate({"from": "news@newsletter.example"}) == ([], [])


def test_allowlist_digest_uses_normalized_effective_entries_and_detects_changes(tmp_path):
    policy = local_policy(tmp_path, personal_provider_allowlist=["OTHER@GMAIL.COM", APPROVED])
    same = local_policy(tmp_path, personal_provider_allowlist=[APPROVED.upper(), "other@gmail.com"])
    assert policy.allowlist_digest() == same.allowlist_digest()
    policy.rules["personal_provider_allowlist"] = ("third@gmail.com",)
    assert policy.allowlist_digest() != same.allowlist_digest()
