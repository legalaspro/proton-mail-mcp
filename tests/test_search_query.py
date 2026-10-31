"""Search conditions render to IMAP SEARCH keys that gluon parses back exactly.

tests/gluon_search.py ports gluon's SEARCH grammar, so these tests show that each
rendered command is one Bridge accepts and that it means the query: NOT and OR as
prefix keys, AND groups in parentheses, values as single quoted strings or the
command's one final literal, and no way for a value to add or change keys.
"""

import pytest
from gluon_search import SearchSyntaxError, command_bytes, depth, parse_search

from proton_mail_mcp import search_query
from proton_mail_mcp.mail import MailValidationError

ALL_FIELDS = set(search_query.FIELDS)
SECRET = "do-not-echo-" + "s" * 12
HOSTILE = [
    'a" OR ALL "b',
    "x) ALL (y",
    '\\" ALL',
    "trailing\\",
    '"',
    "\\",
    "{5}",
    '" ALL "',
    "* % ] [",
    "OR UNSEEN",
    "(NOT ALL)",
    "UID 1:*",
]
CONTROLS = ["\r", "\n", "\x00", "\t", "\x7f", "\r\nA1 LOGOUT"]


def rendered(query=None, **filters):
    nodes = search_query.conditions(query=search_query.parse(query), **filters)
    return search_query.plan(nodes, ALL_FIELDS)


def keys_of(args, literal=None):
    return parse_search(command_bytes(args, literal))[1]


def deep(levels):
    query = {"from": "a"}
    for _ in range(levels - 1):
        query = {"not": query}
    return query


def test_not_or_nesting_renders_prefix_keys_that_gluon_parses_back():
    args, literal, rest = rendered({"not": {"or": [{"from": "a@x.test"}, {"subject": "weekly"}]}})
    assert args == ["NOT", "OR", "FROM", b'"a@x.test"', "SUBJECT", b'"weekly"']
    assert literal is None and rest == []
    assert parse_search(command_bytes(["CHARSET", "UTF-8", *args], literal)) == (
        "UTF-8",
        [("NOT", ("OR", ("FROM", "a@x.test"), ("SUBJECT", "weekly")))],
    )


def test_or_chains_pairs_and_puts_and_groups_in_parentheses():
    query = {
        "or": [
            {"from": "a"},
            {"from": "b", "subject": "c"},
            {"not": {"text": "d", "to": "e"}},
        ]
    }
    args, literal, rest = rendered(query)
    # OR is binary: OR a OR b c. No space follows "(" or precedes ")".
    assert args == [
        "OR",
        "FROM",
        b'"a"',
        "OR",
        b'(FROM "b" SUBJECT "c")',
        "NOT",
        b'(TEXT "d" TO "e")',
    ]
    assert literal is None and rest == []
    assert keys_of(args) == [
        (
            "OR",
            ("FROM", "a"),
            (
                "OR",
                ("AND", (("FROM", "b"), ("SUBJECT", "c"))),
                ("NOT", ("AND", (("TEXT", "d"), ("TO", "e")))),
            ),
        )
    ]


def test_filters_exclusions_and_query_all_apply_in_order():
    args, literal, rest = rendered(
        {"or": [{"to": "team"}, {"text": "agenda"}]},
        from_="alice",
        exclude_subject="weekly",
        exclude_text="unsubscribe",
    )
    assert args == [
        "FROM",
        b'"alice"',
        "NOT",
        "SUBJECT",
        b'"weekly"',
        "NOT",
        "TEXT",
        b'"unsubscribe"',
        "OR",
        "TO",
        b'"team"',
        "TEXT",
        b'"agenda"',
    ]
    assert literal is None and rest == []


def test_equivalent_queries_normalize_to_one_tree():
    tree = search_query.parse({"subject": "s", "from": "f"})
    assert tree == ("and", (("from", "f"), ("subject", "s")))
    assert search_query.parse({"from": "f", "subject": "s"}) == tree
    assert search_query.parse({"and": [{"from": "f"}, {"and": [{"subject": "s"}]}]}) == tree
    assert search_query.parse({"not": {"not": {"from_": "f"}}}) == ("from", "f")
    assert search_query.parse({"not": {"not": {"not": {"to": "t"}}}}) == ("not", ("to", "t"))
    assert search_query.parse({"or": [{"or": [{"to": "a"}, {"to": "b"}]}, {"to": "c"}]}) == (
        "or",
        (("to", "a"), ("to", "b"), ("to", "c")),
    )
    assert search_query.parse({"or": [{"text": "x"}]}) == ("text", "x")
    assert search_query.parse(None) is None


@pytest.mark.parametrize("value", HOSTILE)
def test_quotes_backslashes_and_parentheses_stay_inside_one_quoted_value(value):
    args, literal, _ = rendered({"or": [{"subject": value}, {"not": {"text": value}}]})
    assert keys_of(args, literal) == [("OR", ("SUBJECT", value), ("NOT", ("TEXT", value)))]
    args, literal, _ = rendered({"not": {"from": value, "to": value}})
    assert keys_of(args, literal) == [("NOT", ("AND", (("FROM", value), ("TO", value))))]


@pytest.mark.parametrize("control", CONTROLS)
def test_controls_in_values_are_refused(control):
    value = "a" + control + "b"
    for query in ({"subject": value}, {"or": [{"from": "x"}, {"not": {"text": value}}]}):
        with pytest.raises(MailValidationError, match="no control characters"):
            search_query.parse(query)
    for name in ("exclude_subject", "exclude_text"):
        with pytest.raises(MailValidationError, match="no control characters"):
            search_query.conditions(**{name: value})


def test_one_non_ascii_value_becomes_the_final_literal():
    args, literal, rest = rendered({"or": [{"subject": "Café"}, {"from": "a"}]}, text="plain")
    assert args == ["TEXT", b'"plain"', "OR", "FROM", b'"a"', "SUBJECT"]
    assert literal == "Café".encode() and rest == []
    assert keys_of(args, literal) == [("TEXT", "plain"), ("OR", ("FROM", "a"), ("SUBJECT", "Café"))]
    assert rendered(exclude_text="Brûlée") == (["NOT", "TEXT"], "Brûlée".encode(), [])
    assert search_query.leaf_criteria("from", "Zoë") == (["FROM"], "Zoë".encode())
    assert search_query.leaf_criteria("to", 'a"b') == (["TO", b'"a\\"b"'], None)


def test_other_non_ascii_values_are_left_to_one_search_per_value():
    args, literal, rest = rendered({"subject": "Café"}, exclude_text="Brûlée")
    assert (args, literal) == (["NOT", "TEXT"], "Brûlée".encode())
    assert rest == [("subject", "Café")]
    # A literal inside a parenthesized group would be followed by ")".
    grouped = {"or": [{"from": "a", "subject": "Thé"}, {"to": "b"}]}
    assert rendered(grouped) == ([], None, [search_query.parse(grouped)])
    two = {"or": [{"text": "Crème"}, {"text": "Brûlée"}]}
    assert rendered(two) == ([], None, [search_query.parse(two)])
    # Fields outside the server's set are always evaluated per message.
    nodes = search_query.conditions(subject="weekly", text="agenda")
    assert search_query.plan(nodes, {"text"}) == (["TEXT", b'"agenda"'], None, [nodes[0]])


@pytest.mark.parametrize(
    "query,expected",
    [
        ([], "nonempty object"),
        ("from:" + SECRET, "nonempty object"),
        ({}, "nonempty object"),
        ({"cc": SECRET}, "Unsupported query key"),
        ({SECRET: "a"}, "Unsupported query key"),
        ({"from": 5}, "nonempty text"),
        ({"from": ""}, "nonempty text"),
        ({"from": None}, "nonempty text"),
        ({"from": "x" * 1001}, "at most 1000 characters"),
        ({"or": []}, "list of 1 to 20 queries"),
        ({"or": {"from": SECRET}}, "list of 1 to 20 queries"),
        ({"not": [{"from": SECRET}]}, "nonempty object"),
        ({"and": [1]}, "nonempty object"),
        ({"or": [{"from": str(i)} for i in range(21)]}, "list of 1 to 20 queries"),
        ({"and": [{"from": "a", "to": "b"}] * 11}, "at most 20 conditions"),
        (deep(6), "limited to 5 levels"),
        ({1: "a"}, "Unsupported query key"),
        ({f"{SECRET}{index}": "a" for index in range(1000)}, "Unsupported query key"),
        ({"from": "a", "to": "c", "subject": "d", "text": "e", "x": "f"}, "Unsupported query key"),
        ({"text": "\ud800"}, "valid UTF-8"),
    ],
)
def test_invalid_queries_are_refused_without_echoing_them(query, expected):
    with pytest.raises(MailValidationError, match=expected) as raised:
        search_query.parse(query)
    assert SECRET not in str(raised.value)


def test_largest_allowed_queries_stay_within_gluons_nesting_limit():
    assert search_query.parse(deep(5)) == ("from", "a")
    chain = {"or": [{"from": f"s{index}"} for index in range(20)]}
    nested = {"from": "a", "to": "b"}
    for level in range(2):
        # The nested key is the last OR child, so it sits at the end of the chain.
        nested = {"or": [{"subject": f"x{level}{index}"} for index in range(8)] + [{"not": nested}]}
    for query in (chain, nested):
        args, literal, rest = rendered(query)
        assert rest == []
        assert max(map(depth, keys_of(args, literal))) <= 64


@pytest.mark.parametrize(
    "command",
    [
        b'SEARCH ( FROM "a")',
        b'SEARCH (FROM "a" )',
        b'SEARCH FROM "a"b"',
        b'SEARCH FROM "\\a"',
        b'SEARCH FROM "a\r\nb"',
        # Stricter than gluon: non-ASCII belongs in a literal.
        'SEARCH FROM "café"'.encode(),
        b"SEARCH SINCE 1-Oct-2026",
        b"SEARCH  ALL",
        b"SEARCH OR ALL",
        b"SEARCH NOT",
        b"SEARCH " + b"NOT " * 65 + b"ALL",
        b"SEARCH FROM {3}\r\nab",
        b"SEARCH UNKNOWN",
    ],
)
def test_the_gluon_port_refuses_malformed_commands(command):
    with pytest.raises(SearchSyntaxError):
        parse_search(command)
    assert parse_search(b"SEARCH " + b"NOT " * 64 + b"ALL")[1]
