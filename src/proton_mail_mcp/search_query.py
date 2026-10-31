"""Search conditions: validated not/or/and queries, IMAP SEARCH keys and local matching.

A query is a JSON object whose entries all apply (AND). ``from``, ``to``,
``subject`` and ``text`` match case-insensitive substrings; ``and`` and ``or``
take a nonempty list of queries and ``not`` takes one. Keys come only from this
fixed vocabulary, never from a value. Each value is sent as one IMAP quoted
string, with backslash and double quote escaped, or, if it is not ASCII, as the
command's literal. Controls, including CR and LF, are refused, so no value can
end the command, add a search key or change the nesting.

imaplib sends at most one literal per command, after its last argument. Only a
condition whose non-ASCII value can be that final token joins the command; any
other condition is evaluated per message instead (``plan``), its server-side
values each looked up by a separate ``UID SEARCH``.
"""

from __future__ import annotations

from typing import Any, Callable, Iterator

from .mail import MailValidationError, _quote

FIELDS = {"from": "FROM", "to": "TO", "subject": "SUBJECT", "text": "TEXT"}
MAX_CONDITIONS = 20
MAX_DEPTH = 5
MAX_VALUE_LENGTH = 1000
_ALIASES = {"from_": "from"}
_KEYS = "from, to, subject, text, and, or, not"
_ALLOWED = frozenset({*FIELDS, *_ALIASES, "and", "or", "not"})


def value(item: Any, name: str = "Query values") -> str:
    """Validate one exclusion or query value: nonempty, bounded, controls refused."""
    if not isinstance(item, str) or not item:
        raise MailValidationError(f"{name} must be nonempty text")
    if len(item) > MAX_VALUE_LENGTH:
        raise MailValidationError(f"{name} must be at most {MAX_VALUE_LENGTH} characters")
    try:
        _quote(item)
    except UnicodeEncodeError:
        raise MailValidationError(f"{name} must be valid UTF-8 text") from None
    return item


def parse(query: Any) -> tuple | None:
    """Validate a tool's query object into a normalized condition tree.

    Nodes are ``(field, value)`` leaves, ``("not", node)`` and ``("and" | "or",
    (node, ...))`` with at least two children. Object entries are ordered by key
    and nested lists of the same operator are flattened, so equivalent queries
    give the same tree (and the same search cursor).
    """
    if query is None:
        return None
    return _simplify(_node(query, 1, [0]))


def _node(query: Any, depth: int, count: list[int]) -> tuple:
    if depth > MAX_DEPTH:
        raise MailValidationError(f"Query nesting is limited to {MAX_DEPTH} levels")
    if not isinstance(query, dict) or not query:
        raise MailValidationError("Each query must be a nonempty object using " + _KEYS)
    # Refuse before sorting: a valid object holds each allowed key at most once.
    if len(query) > len(_ALLOWED) or any(key not in _ALLOWED for key in query):
        raise MailValidationError("Unsupported query key; use " + _KEYS)
    parts = []
    for key in sorted(query):
        item = query[key]
        field = _ALIASES.get(key, key)
        if field in FIELDS:
            count[0] += 1
            if count[0] > MAX_CONDITIONS:
                raise MailValidationError(f"A query has at most {MAX_CONDITIONS} conditions")
            parts.append((field, value(item)))
        elif key == "not":
            parts.append(("not", _node(item, depth + 1, count)))
        else:
            if not isinstance(item, list) or not 1 <= len(item) <= MAX_CONDITIONS:
                raise MailValidationError(
                    f"Query {key} needs a list of 1 to {MAX_CONDITIONS} queries"
                )
            parts.append((key, tuple(_node(child, depth + 1, count) for child in item)))
    return parts[0] if len(parts) == 1 else ("and", tuple(parts))


def _simplify(node: tuple) -> tuple:
    kind = node[0]
    if kind in FIELDS:
        return node
    if kind == "not":
        child = _simplify(node[1])
        return child[1] if child[0] == "not" else ("not", child)
    children: list[tuple] = []
    for child in map(_simplify, node[1]):
        children.extend(child[1] if child[0] == kind else (child,))
    return children[0] if len(children) == 1 else (kind, tuple(children))


def conditions(
    *,
    from_: str | None = None,
    to: str | None = None,
    subject: str | None = None,
    text: str | None = None,
    exclude_subject: str | None = None,
    exclude_text: str | None = None,
    query: tuple | None = None,
) -> list[tuple]:
    """The conditions that all apply, in order: filters, exclusions, then the query's."""
    result: list[tuple] = [
        (field, item)
        for field, item in (("from", from_), ("to", to), ("subject", subject), ("text", text))
        if item is not None
    ]
    for field, item, name in (
        ("subject", exclude_subject, "exclude_subject"),
        ("text", exclude_text, "exclude_text"),
    ):
        if item is not None:
            result.append(("not", (field, value(item, name))))
    if query is not None:
        result.extend(query[1] if query[0] == "and" else (query,))
    return result


def leaves(node: tuple) -> Iterator[tuple[str, str]]:
    if node[0] in FIELDS:
        yield node
    elif node[0] == "not":
        yield from leaves(node[1])
    else:
        for child in node[1]:
            yield from leaves(child)


def fields(node: tuple) -> set[str]:
    return {field for field, _ in leaves(node)}


def matches(node: tuple, test: Callable[[str, str], bool]) -> bool:
    """Evaluate a condition for one message; ``test`` answers each leaf."""
    kind = node[0]
    if kind in FIELDS:
        return test(kind, node[1])
    if kind == "not":
        return not matches(node[1], test)
    if kind == "and":
        return all(matches(child, test) for child in node[1])
    return any(matches(child, test) for child in node[1])


def _wide(node: tuple) -> bool:
    return any(not item.isascii() for _, item in leaves(node))


def _final(node: tuple) -> bool:
    """True if the node's one non-ASCII value can be the command's last token.

    NOT and OR are prefix operators, so ordering the child holding the literal
    last keeps it last; a parenthesized AND group would follow it with ")".
    """
    kind = node[0]
    if kind in FIELDS:
        return True
    if kind == "not":
        return _final(node[1])
    return kind == "or" and all(_final(child) for child in node[1] if _wide(child))


def _render(node: tuple, args: list[Any]) -> bytes | None:
    """Append one search key for node; return its literal, which must stay last."""
    kind = node[0]
    if kind in FIELDS:
        args.append(FIELDS[kind])
        quoted = _quote(node[1])
        if node[1].isascii():
            args.append(quoted)
            return None
        # imaplib sends this literal after the last argument, that is, here.
        return node[1].encode("utf-8")
    if kind == "not":
        args.append("NOT")
        return _render(node[1], args)
    if kind == "or":
        # OR takes exactly two keys: OR a OR b c. A non-ASCII child goes last.
        children = sorted(node[1], key=_wide)
        for child in children[:-1]:
            args.append("OR")
            if _render(child, args) is not None:
                raise AssertionError("A search command carries at most one literal")
        return _render(children[-1], args)
    # An AND group inside NOT or OR is one parenthesized key, with no space
    # after "(" or before ")" (RFC 3501 search-key; Gluon parses exactly that).
    inner: list[Any] = []
    for child in node[1]:
        if _render(child, inner) is not None:
            raise AssertionError("A literal cannot be rendered inside parentheses")
    args.append(b"(" + b" ".join(_bytes(token) for token in inner) + b")")
    return None


def _bytes(token: Any) -> bytes:
    return token if isinstance(token, bytes) else token.encode("ascii")


def criteria(nodes: list[tuple]) -> tuple[list[Any], bytes | None]:
    """IMAP SEARCH keys for conditions that hold at most one placeable literal."""
    args: list[Any] = []
    literal = None
    ordered = sorted(nodes, key=_wide)
    for index, node in enumerate(ordered):
        found = _render(node, args)
        if found is not None:
            if index != len(ordered) - 1:
                raise AssertionError("A search command carries at most one literal")
            literal = found
    return args, literal


def plan(
    nodes: list[tuple], server_fields: set[str]
) -> tuple[list[Any], bytes | None, list[tuple]]:
    """Split conditions between one UID SEARCH and per-message evaluation.

    A condition joins the command when all its fields are server-side and its
    non-ASCII value, if any, is the only one and can be the command's final
    token. Returns the command's keys, its literal and the remaining conditions.
    """
    searched: list[tuple] = []
    rest: list[tuple] = []
    literal_node = None
    for node in nodes:
        if not fields(node) <= server_fields:
            rest.append(node)
            continue
        wide = sum(not item.isascii() for _, item in leaves(node))
        if not wide:
            searched.append(node)
        elif wide == 1 and literal_node is None and _final(node):
            literal_node = node
        else:
            rest.append(node)
    if literal_node is not None:
        searched.append(literal_node)
    args, literal = criteria(searched)
    return args, literal, rest


def leaf_criteria(field: str, item: str) -> tuple[list[Any], bytes | None]:
    """Keys for one condition value searched on its own (at most one literal)."""
    args: list[Any] = []
    return args, _render((field, item), args)
