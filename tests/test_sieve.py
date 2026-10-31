"""Sieve script rendering, escaping, validation and parse-back checks: pure, no mail."""

import pytest

from proton_mail_mcp import sieve
from proton_mail_mcp.folder_rules import FolderRule
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.sieve import (
    MAX_SCRIPT_BYTES,
    Condition,
    SieveFilter,
    SieveUnavailable,
    UnavailableSieveBackend,
    backend_status,
    filter_spec,
    parse_filter,
    render_script,
    require_backend,
    script_sha256,
    sieve_folder,
    verify_script,
)

GITHUB = [{"field": "from", "contains": "notifications@github.com"}]
PREAMBLE = (
    'require ["include", "environment", "variables", "relational", '
    '"comparator-i;ascii-numeric", "spamtest"];\n'
    'require ["fileinto", "imap4flags"];\n'
    "\n"
    "# Never run on spam, like the filters Proton's editor generates.\n"
    'if allof (environment :matches "vnd.proton.spam-threshold" "*",\n'
    '          spamtest :value "ge" :comparator "i;ascii-numeric" "${1}") {\n'
    "    return;\n"
    "}\n"
    "\n"
)


def spec(conditions=GITHUB, folder="Folders/GitHub", **options):
    return filter_spec(options.pop("name", "GitHub"), folder, conditions, **options)


def tree(script):
    return sieve._Parser(script).script()


def actions(script):
    """Command names inside the filter's block, in order."""
    return [command[1] for command in tree(script)[3][4]]


def values(script):
    """Each condition's decoded key string, as a Sieve interpreter reads it."""
    return [test[2][-1][1] for test in tree(script)[3][3][3]]


def test_golden_script_for_one_sender_condition():
    assert render_script(spec()) == PREAMBLE + (
        "if allof (\n"
        '    address :all :comparator "i;unicode-casemap" :contains "From" '
        '"notifications@github.com"\n'
        ") {\n"
        '    fileinto "GitHub";\n'
        "}\n"
    )


def test_golden_script_for_any_match_marking_read():
    script = render_script(
        spec(
            [{"field": "to", "is": "me@example.com"}, {"field": "subject", "contains": "Invoice"}],
            folder="Folders/Work/Receipts",
            match="any",
            mark_read=True,
        )
    )
    assert script == PREAMBLE + (
        "if anyof (\n"
        '    address :all :comparator "i;unicode-casemap" :is "To" "me@example.com",\n'
        '    header :comparator "i;unicode-casemap" :contains "Subject" "Invoice"\n'
        ") {\n"
        '    addflag "\\\\Seen";\n'
        '    fileinto "Work/Receipts";\n'
        "}\n"
    )
    assert actions(script) == ["addflag", "fileinto"]
    assert tree(script)[3][4][0][2] == (("string", "\\Seen"),)


def test_without_mark_read_the_script_only_files_and_never_flags_or_deletes():
    script = render_script(spec())
    assert actions(script) == ["fileinto"]
    assert [command[1] for command in tree(script)] == ["require", "require", "if", "if"]
    for word in ("addflag", "setflag", "Seen", "discard", "reject", "redirect", "vacation"):
        assert word not in script
    for word in ("keep", "trash", "expire", "stop"):
        assert word not in script.casefold()


TRICKY = [
    'He said "hi"',
    "back\\slash",
    '\\"',
    '"; discard; #',
    'x" :is "From" "attacker@example.test',
    '"\\";\\\\',
    "*?[]{}();,#/* not a comment",
    "end with a backslash \\",
    '"',
    "\\",
    "'single' quotes",
    "$1, a lone $ and {braces}",
    "Rechnung für März 🎉",
    "Ünïcödé Ελληνικά 日本語",
]


@pytest.mark.parametrize("value", TRICKY)
@pytest.mark.parametrize("field", ["from", "to", "subject"])
@pytest.mark.parametrize("operator", ["contains", "is"])
def test_quotes_backslashes_and_syntax_in_values_stay_one_string(value, field, operator):
    script = render_script(spec([{"field": field, operator: value}]))
    verify_script(script, spec([{"field": field, operator: value}]))
    assert values(script) == [value]
    assert actions(script) == ["fileinto"]
    assert len(tree(script)) == 4


def test_folder_names_with_sieve_punctuation_stay_one_string():
    folder = "Folders/a; discard; #b {c} (d) [e], keep"
    script = render_script(spec(folder=folder))
    assert actions(script) == ["fileinto"]
    assert tree(script)[3][4][0][2] == (("string", sieve_folder(folder)),)
    assert sieve_folder(folder) == "a; discard; #b {c} (d) [e], keep"


REFUSED_TEXT = [
    "line\nbreak",
    "carriage\rreturn",
    "header\r\nX-Injected: yes",
    'quote"\nfileinto "Trash',
    "tab\tvalue",
    "nul\0value",
    "delete\x7fvalue",
    "next line\x85value",
    "line separator",
    "paragraph separator",
    "bidi‮override",
    "zero​width",
    "byte order﻿mark",
    "lone\ud800surrogate",
    "privateuse",
    "unassigned͸",
    " leading space",
    "trailing space ",
    " no-break space",
    "",
    "${1}",
    "a${name}b",
    "${dollar}{1}",
]


@pytest.mark.parametrize("value", REFUSED_TEXT)
def test_newlines_controls_invisible_characters_and_variables_are_refused(value):
    for field in ("from", "to", "subject"):
        with pytest.raises(MailValidationError):
            spec([{"field": field, "contains": value}])
    with pytest.raises(MailValidationError):
        spec(name=value)


def test_value_and_name_lengths_are_bounded():
    assert values(render_script(spec([{"field": "subject", "contains": "x" * 500}]))) == ["x" * 500]
    with pytest.raises(MailValidationError):
        spec([{"field": "subject", "contains": "x" * 501}])
    assert spec(name="n" * 100).name == "n" * 100
    with pytest.raises(MailValidationError):
        spec(name="n" * 101)


REFUSED_FOLDERS = [
    None,
    7,
    "",
    "GitHub",
    "INBOX",
    "Archive",
    "Trash",
    "Spam",
    "Labels/News",
    "folders/News",
    "Folders",
    "Folders/",
    "Folders/a//b",
    "Folders/./b",
    "Folders/../b",
    "Folders/ News",
    "Folders/News ",
    'Folders/a"b',
    "Folders/a\\b",
    "Folders/a'b",
    "Folders/*",
    "Folders/%",
    "Folders/a\nb",
    "Folders/a‮b",
    "Folders/a${1}",
    "Folders/Trash",
    "Folders/inbox",
    "Folders/ARCHIVE/Sub",
    "Folders/All Mail",
    "Folders/spam",
    "Folders/" + "x" * 493,
]


@pytest.mark.parametrize("folder", REFUSED_FOLDERS)
def test_targets_must_be_plain_real_folders_not_system_names(folder):
    with pytest.raises(MailValidationError):
        spec(folder=folder)


def test_nested_and_unicode_folders_map_to_their_proton_path():
    assert sieve_folder("Folders/GitHub") == "GitHub"
    assert sieve_folder("Folders/Work/Project 1") == "Work/Project 1"
    assert sieve_folder("Folders/Café & News") == "Café & News"
    assert sieve_folder("Folders/Inbox Zero") == "Inbox Zero"
    assert sieve_folder("Folders/" + "x" * 492) == "x" * 492


REFUSED_CONDITIONS = [
    None,
    "from contains x",
    {"field": "from", "contains": "x"},
    [],
    [{}],
    ["from contains x"],
    [{"field": "from"}],
    [{"contains": "x"}],
    [{"field": "cc", "contains": "x"}],
    [{"field": "From", "contains": "x"}],
    [{"field": "body", "contains": "x"}],
    [{"field": "from", "contains": "x", "is": "x"}],
    [{"field": "from", "matches": "x*"}],
    [{"field": "from", "regex": ".*"}],
    [{"field": "from", "contains": "x", "folder": "Folders/Other"}],
    [{"field": ["from"], "contains": "x"}],
    [{"field": {"from": 1}, "contains": "x"}],
    [{"field": "from", "contains": 7}],
    [{"field": "from", "contains": None}],
    [{"field": "from", "contains": ["x"]}],
    [{"field": "from", "contains": "x"}, {"field": "from", "contains": "x"}],
    [{"field": "subject", "contains": f"term {index}"} for index in range(11)],
]


@pytest.mark.parametrize("conditions", REFUSED_CONDITIONS)
def test_malformed_conditions_are_refused(conditions):
    with pytest.raises(MailValidationError):
        spec(conditions)


def test_ten_conditions_are_accepted():
    many = [{"field": "subject", "contains": f"term {index}"} for index in range(10)]
    assert values(render_script(spec(many))) == [f"term {index}" for index in range(10)]


@pytest.mark.parametrize("match", ["ALL", "", None, 1, "allof", "none", ["all"]])
def test_match_must_be_all_or_any(match):
    with pytest.raises(MailValidationError):
        spec(match=match)


@pytest.mark.parametrize("mark_read", [1, 0, "true", "false", None, [True]])
def test_mark_read_must_be_an_explicit_boolean(mark_read):
    with pytest.raises(MailValidationError):
        spec(mark_read=mark_read)


def test_params_round_trip_and_folder_rules_map_onto_filters():
    built = spec(
        [{"field": "to", "is": "me@example.com"}, {"field": "subject", "contains": "x"}],
        match="any",
        mark_read=True,
    )
    assert built.params() == {
        "name": "GitHub",
        "folder": "Folders/GitHub",
        "conditions": [
            {"field": "to", "is": "me@example.com"},
            {"field": "subject", "contains": "x"},
        ],
        "match": "any",
        "mark_read": True,
    }
    assert filter_spec(**built.params()) == built
    # The shape a reviewed connector rule (and later P-E) hands to the backend.
    rule = FolderRule("from", "notifications@github.com", "Folders/GitHub")
    from_rule = filter_spec(
        "Rule 1", rule.folder, [{"field": rule.field, "contains": rule.contains}]
    )
    assert values(render_script(from_rule)) == [rule.contains]


@pytest.mark.parametrize(
    "built",
    [
        SieveFilter("GitHub", "Folders/GitHub", [Condition("from", "contains", "x")]),
        SieveFilter("GitHub", "Folders/GitHub", (Condition("cc", "contains", "x"),)),
        SieveFilter("GitHub", "Folders/GitHub", (Condition("from", "matches", "x*"),)),
        SieveFilter("GitHub", "Folders/GitHub", (Condition("from", "contains", "a\nb"),)),
        SieveFilter("GitHub", "Trash", (Condition("from", "contains", "x"),)),
        SieveFilter("GitHub", "Folders/GitHub", (Condition("from", "contains", "x"),), "or"),
        SieveFilter("GitHub", "Folders/GitHub", (Condition("from", "contains", "x"),), "all", 1),
    ],
)
def test_filters_built_directly_are_revalidated_before_rendering(built):
    with pytest.raises(MailValidationError):
        render_script(built)


TAMPERED = {
    "appended discard": lambda script: script + "discard;\n",
    "added keep": lambda script: script.replace(
        'fileinto "GitHub";', 'fileinto "GitHub";\n    keep;'
    ),
    "added flag": lambda script: script.replace(
        '    fileinto "GitHub";', '    addflag "\\\\Seen";\n    fileinto "GitHub";'
    ),
    "other folder": lambda script: script.replace('"GitHub";', '"Trash";'),
    "missing semicolon": lambda script: script.replace('fileinto "GitHub";', 'fileinto "GitHub"'),
    "matches": lambda script: script.replace(":contains", ":matches"),
    "key list": lambda script: script.replace(
        '"notifications@github.com"', '["notifications@github.com", "x"]'
    ),
    "any": lambda script: script.replace("if allof (\n", "if anyof (\n"),
    "stop": lambda script: script.replace("return;", "stop;"),
    "no spam guard": lambda script: script.replace("    return;\n", ""),
    "uppercase": lambda script: script.replace("fileinto", "FILEINTO"),
    "vacation": lambda script: script.replace('"imap4flags"]', '"imap4flags", "vacation"]'),
    "truncated": lambda script: script[:-3],
    "multi-line string": lambda script: script.replace('"GitHub";', "text:\nGitHub\n.\n;"),
    "unterminated string": lambda script: script + '"',
    "unterminated comment": lambda script: script + "/* never closed",
    "stray character": lambda script: script + "@",
    "number": lambda script: script.replace('fileinto "GitHub";', "fileinto 5;"),
    "nested": lambda script: script + "if " + "not " * 50 + "true { }",
    "surrogate": lambda script: script + "#\ud800",
    "oversized": lambda script: script + "#" * MAX_SCRIPT_BYTES,
}


@pytest.mark.parametrize("case", list(TAMPERED))
def test_verify_refuses_any_script_that_is_not_exactly_the_filter(case):
    script = render_script(spec())
    changed = TAMPERED[case](script)
    assert changed != script
    with pytest.raises(MailValidationError, match="preview again"):
        verify_script(changed, spec())


@pytest.mark.parametrize("script", [None, b"bytes", 7, ["require"]])
def test_verify_refuses_non_text(script):
    with pytest.raises(MailValidationError, match="not valid text"):
        verify_script(script, spec())


def test_comments_and_line_endings_do_not_change_meaning_but_change_the_digest():
    script = render_script(spec())
    for variant in ("# reviewed\n" + script, script.replace("\n", "\r\n"), script + "/* x */"):
        verify_script(variant, spec())
        assert script_sha256(variant) != script_sha256(script)
    assert script_sha256(script) == script_sha256(render_script(spec()))


ROUND_TRIPS = [
    spec(),
    spec(
        [{"field": "to", "is": "me@example.com"}, {"field": "subject", "contains": "Invoice"}],
        folder="Folders/Work/Receipts",
        match="any",
        mark_read=True,
    ),
    spec([{"field": "subject", "is": 'x"; discard; # \\ caf\u00e9'}], folder="Folders/Caf\u00e9"),
    spec([{"field": ("from", "to")[index % 2], "contains": f"v{index}"} for index in range(10)]),
]


@pytest.mark.parametrize("built", ROUND_TRIPS, ids=["one", "any-read", "escaped", "ten"])
def test_parse_filter_reads_back_exactly_the_rendered_filter(built):
    script = render_script(built)
    assert parse_filter(built.name, script) == built
    assert parse_filter("Other name", script) == filter_spec(
        **(built.params() | {"name": "Other name"})
    )


# These edits happen to render another valid filter; parse_filter reads that one.
OTHER_FILTERS = {"added flag": spec(mark_read=True), "any": spec(match="any")}


@pytest.mark.parametrize("case", list(TAMPERED))
def test_parse_filter_refuses_any_script_that_is_not_a_rendered_filter(case):
    changed = TAMPERED[case](render_script(spec()))
    if case in OTHER_FILTERS:
        assert parse_filter("GitHub", changed) == OTHER_FILTERS[case] != spec()
        assert render_script(OTHER_FILTERS[case]) == changed
        return
    with pytest.raises(MailValidationError, match="^Sieve script is not a reviewed filter; prev"):
        parse_filter("GitHub", changed)


def test_parse_filter_refuses_equivalent_but_different_bytes_and_non_text():
    script = render_script(spec())
    for variant in ("# reviewed\n" + script, script.replace("\n", "\r\n"), script + "/* x */"):
        verify_script(variant, spec())  # Same meaning, other bytes: never stored.
        with pytest.raises(MailValidationError, match="not a reviewed filter"):
            parse_filter("GitHub", variant)
    for value in (None, b"bytes", 7, ["require"], "", "require;"):
        with pytest.raises(MailValidationError, match="not a reviewed filter"):
            parse_filter("GitHub", value)
    for name in ("", "bad\nname", " padded", "${x}", 7):
        with pytest.raises(MailValidationError, match="Filter name"):
            parse_filter(name, script)


def test_default_backend_refuses_every_apply_with_the_reason():
    backend = UnavailableSieveBackend()
    status = backend_status(backend)
    assert status == {
        "backend": "unavailable",
        "available": False,
        "reason": "Proton Mail Bridge offers mail clients only IMAP and SMTP, with no "
        "ManageSieve, Sieve or filter interface, and this server has no other apply path",
    }
    for call in (
        lambda: backend.filter_names(timeout=1),
        lambda: backend.create_filter("n", render_script(spec()), timeout=1),
        lambda: require_backend(backend),
    ):
        with pytest.raises(
            SieveUnavailable, match="cannot be applied: Proton Mail Bridge"
        ) as error:
            call()
        assert str(error.value).endswith("nothing was changed")
        assert isinstance(error.value, MailValidationError)


class Status:
    def __init__(self, name="odd", status=None, error=None):
        self.name, self.result, self.error = name, status, error

    def status(self):
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.parametrize(
    "backend",
    [
        Status(error=RuntimeError("secret detail")),
        Status(status=None),
        Status(status=["available"]),
        Status(status={"available": "yes"}),
        Status(status={"available": 1}),
        Status(name=None, status={"available": True}),
        Status(name="", status={"available": True}),
        Status(name="x" * 65, status={"available": True}),
        Status(name="two\nlines", status={"available": True}),
    ],
)
def test_unexpected_backend_status_reads_unavailable(backend):
    status = backend_status(backend)
    assert status["available"] is False and "secret" not in str(status)
    with pytest.raises(SieveUnavailable):
        require_backend(backend)


def test_available_backend_status_is_sanitized():
    status = backend_status(Status(status={"available": True, "reason": "line\nbreak"}))
    assert status == {"backend": "odd", "available": True, "reason": None}
    require_backend(Status(status={"available": True}))
    refused = backend_status(Status(status={"available": False, "reason": "r" * 500}))
    assert refused["reason"] == "r" * 200
