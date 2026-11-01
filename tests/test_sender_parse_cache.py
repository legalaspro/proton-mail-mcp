"""The parse caches change the cost of a sender check, never its answer."""

import sys
from email import message_from_string, policy

import pytest

from proton_mail_mcp import protected, sender_matching
from proton_mail_mcp.mail import MailError, _ImapValues
from proton_mail_mcp.protected import ProtectedPolicy
from proton_mail_mcp.sender_matching import CACHE_ENTRIES, CACHE_VALUE_CHARS

CACHES = (
    protected._parsed_sender,
    protected._domain_rules,
    protected._name_has_keyword,
    sender_matching._parsed_text,
)
# Everything the four caches hold at their worst, chosen by whoever sends the mail.
MAX_RETAINED_BYTES = 544 * 1024 * 1024
# An upper estimate of one entry's LRU link and dict slot, which sys.getsizeof cannot see.
ENTRY_OVERHEAD_BYTES = 200


def test_a_changed_rule_applies_to_a_sender_already_checked():
    policy = ProtectedPolicy()
    message = {"from": "Someone <a@changed.example>"}
    assert policy.check(message) == []
    policy.rules["domains"] += ("changed.example",)
    assert policy.check(message) == ["Protected sender domain in From"]
    policy.rules["domains"] = ()
    policy.rules["keywords"] += ("someone",)
    assert policy.check(message) == ["Protected sender display name in From"]


def test_two_policies_do_not_share_an_answer(tmp_path):
    path = tmp_path / "protected.json"
    path.write_text('{"domains": ["local-only.example"]}')
    message = {"from": "a@local-only.example"}
    assert ProtectedPolicy(str(path)).check(message) == ["Protected sender domain in From"]
    assert ProtectedPolicy().check(message) == []


def test_the_same_value_reports_under_each_header_that_holds_it():
    message = {"from": "a@gmail.com", "sender": "a@gmail.com", "reply_to": "a@gmail.com"}
    assert ProtectedPolicy().check(message) == [
        "Personal mail provider in From",
        "Personal mail provider in Sender",
        "Personal mail provider in Reply-To",
    ]


def test_a_bad_address_fails_closed_every_time_and_keeps_what_came_before_it():
    policy = ProtectedPolicy()
    for _ in range(2):
        assert policy.check({"from": "a@gmail.com, b@nodot"}) == [
            "Personal mail provider in From",
            "Unparseable From sender",
        ]
        assert policy.check({"from": "a@x.example\x7f"}) == ["Unparseable From sender"]
        assert policy.check({"from": ["a@x.example"]}) == ["Unparseable From sender"]
        assert policy.check({"from": "undisclosed-recipients:;"}) == ["Unparseable From sender"]


def test_an_allowlisted_address_keeps_its_bypass_record_on_a_second_check(tmp_path):
    path = tmp_path / "protected.json"
    path.write_text('{"personal_provider_allowlist": ["me@gmail.com"]}')
    policy = ProtectedPolicy(str(path))
    first = policy.evaluate({"from": "me@gmail.com"})
    assert first[0] == [] and first[1][0]["header"] == "From"
    assert policy.evaluate({"from": "me@gmail.com"}) == first


def retained(value, seen):
    """Bytes a cached value keeps; anything but plain data, such as a header object, fails."""
    if type(value) is tuple:
        return sys.getsizeof(value) + sum(retained(item, seen) for item in value)
    assert type(value) in (str, bool), type(value)
    # True and the empty string exist once, however many addresses refer to them.
    if value is True or value is False or value == "":
        new = id(value) not in seen
        seen.add(id(value))
        return sys.getsizeof(value) if new else 0
    return sys.getsizeof(value)


def padded(fill, tail, chars=CACHE_VALUE_CHARS):
    return fill * (chars - len(tail)) + tail


def worst_senders():
    """The costliest shapes at the longest length a cache keeps.

    Long names, by storage per character, then as many short addresses as fit: both address
    caches keep each one, and a non-ASCII domain also grows into its xn-- form. Last, as many
    bare names as fit: the check stops at the first, but the text cache keeps them all.
    """
    return (
        padded("x", " <a@x.example>"),
        padded("\U0001d400", " <a@x.example>"),
        padded("\u0390", " <a@x.example>"),
        ",".join(["a@b.cc"] * ((CACHE_VALUE_CHARS + 1) // 7)),
        ",".join(["a@\U00020000.c"] * ((CACHE_VALUE_CHARS + 1) // 6)),
        ",".join(["a@\U00020000.\U00020000"] * ((CACHE_VALUE_CHARS + 1) // 6)),
        ",".join(["\U00020000"] * ((CACHE_VALUE_CHARS + 1) // 2)),
    )


@pytest.mark.parametrize("cache", CACHES)
def test_the_caches_are_bounded(cache):
    assert cache.cache_info().maxsize == CACHE_ENTRIES


def test_the_worst_case_cache_memory_stays_under_the_limit():
    senders = worst_senders()
    assert all(len(sender) <= CACHE_VALUE_CHARS for sender in senders)
    parsed = [(sender, sender_matching._parsed_text.__wrapped__(sender)) for sender in senders]
    checked = [
        (("Reply-To", sender), protected._parsed_sender.__wrapped__("Reply-To", sender))
        for sender in senders
    ]
    assert [result[1] for _, result in checked] == [True] * (len(senders) - 1) + [False]
    # Rule tuples are shared with the policy, so only the string in each key is the entry's own.
    longest_domain = sys.getsizeof("x" * CACHE_VALUE_CHARS)
    longest_name = sys.getsizeof("\U0001d400" * CACHE_VALUE_CHARS)
    per_entry = (
        max(retained(entry, set()) for entry in parsed)
        + max(retained(entry, set()) for entry in checked)
        + longest_domain
        + longest_name
        + 4 * ENTRY_OVERHEAD_BYTES
    )
    assert per_entry * CACHE_ENTRIES <= MAX_RETAINED_BYTES


def test_a_long_value_is_answered_but_never_kept():
    for cache in CACHES:
        cache.cache_clear()
    # Long enough that the name of the first and the domain of the second also skip their caches.
    long_name = padded("x", " <a@x.example>", 2 * CACHE_VALUE_CHARS)
    long_domain = f"<a@{('x' * 60 + '.') * (CACHE_VALUE_CHARS // 60 + 1)}example>"
    policy_ = ProtectedPolicy()
    for sender in (long_name, long_domain):
        assert sender_matching.sender_matches(sender, "@")
        assert policy_.check({"from": sender}) == []
    assert sender_matching.sender_snapshot(
        message_from_string(f"From: {long_name}\n\n", policy=policy.default)
    ) == ({"from": long_name, "sender": long_name, "reply_to": long_name}, False)
    assert protected._parsed_sender.cache_info().currsize == 0
    assert sender_matching._parsed_text.cache_info().currsize == 0
    # Only the short half of each sender: the second's empty name and the first's domain.
    assert protected._name_has_keyword.cache_info().currsize == 1
    assert protected._domain_rules.cache_info().currsize == 1


def test_the_limit_changes_the_cost_of_a_check_never_its_answer():
    for fill in ("x", "\u00e9"):
        for chars in (CACHE_VALUE_CHARS, CACHE_VALUE_CHARS + 1):
            sender = padded(fill, " <a@gmail.com>", chars)
            assert ProtectedPolicy().check({"from": sender}) == ["Personal mail provider in From"]
            assert sender_matching._parsed_text(sender) == (
                sender_matching._parsed_text.__wrapped__(sender)
            )


def test_sender_text_parses_like_a_header_object_passed_in():
    header = message_from_string("From: A <a@x.example>\n\n", policy=policy.default)["From"]
    assert sender_matching._sender_text(header) == "A <a@x.example>"
    assert sender_matching._sender_text("A <a@x.example>") == "A <a@x.example>"
    assert sender_matching.sender_matches(header, "x.example")
    assert sender_matching.sender_matches("A <a@x.example>", "x.example")


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b'  ("a\\"b" NIL nil {3}\r\nabc (x\t1)\r\n)', [b'a"b', None, None, b"abc", [b"x", b"1"]]),
        (b'"\\\\"', b"\\"),
        (b'"\\x41\\n"', b"A\n"),
        (b"atom)rest", b"atom"),
        (b'""', b""),
    ],
)
def test_imap_values_parse(raw, expected):
    assert _ImapValues(raw).value() == expected


@pytest.mark.parametrize(
    "raw", [b"", b"  \r\n", b'"open', b'"\\"', b"(a", b"{9}\r\nabc", b"{1}abc"]
)
def test_imap_values_refuse_what_does_not_finish(raw):
    with pytest.raises(MailError, match="invalid message metadata"):
        _ImapValues(raw).value()
