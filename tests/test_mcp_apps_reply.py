"""Reply composer view: prefilled reply fields, saved only through create_draft."""

import base64
import json
from email import policy
from email.parser import BytesParser
from email.utils import format_datetime, parsedate_to_datetime

import anyio
import pytest
from attachment_fixtures import mail_with, part
from mcp.shared.memory import create_connected_server_and_client_session
from test_mail import ATTACHMENT, PLAIN, FakeImap, client, message
from test_mail import settings as mail_settings  # noqa: F401
from view_assertions import assert_self_contained_view, tool_calls, view_data
from view_browser import HOST_CAPABILITIES, browser, requests, run_view, tool_result

from proton_mail_mcp import mcp_apps, views
from proton_mail_mcp.attachments import ATTACHED_SIZE_NOTE
from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import CallDeadlineExceeded, MailValidationError
from proton_mail_mcp.server import create_mcp

settings = mail_settings
TOKEN = "mcp-apps-reply-token-" + "x" * 32
OWN = {"reader@example.test"}


def item(**overrides):
    values = {
        "message_id": "<parent@example.test>",
        "from": "Alice Example <alice@example.test>",
        "to": "reader@example.test, Bob <bob@example.test>",
        "subject": "Quarterly plan",
        "date": "Wed, 28 Oct 2026 10:00:00 +0000",
        "headers": {"Cc": "Carol <carol@example.test>, READER@example.test"},
        "text": "Line one\n\nLine three",
        "attachments": [],
        "truncated": False,
    }
    return {**values, **overrides}


def reply(entry=None, requested="<parent@example.test>", **options):
    result = {"messages": [entry or item()], "partial": False}
    return views.reply_view(result, requested, own=OWN, **options)


def test_reply_prefills_sender_subject_threading_and_quote():
    text, view = reply()
    draft = view["draft"]
    assert draft == text["draft"]
    assert draft["to"] == ["alice@example.test"]
    assert draft["cc"] == []
    assert draft["subject"] == "Re: Quarterly plan"
    assert draft["in_reply_to"] == "<parent@example.test>"
    assert draft["body"] == (
        "\n\nOn Wed, 28 Oct 2026 10:00:00 +0000, Alice Example <alice@example.test> wrote:\n"
        "> Line one\n>\n> Line three\n"
    )
    # Reply all is offered with everyone else, never this account.
    assert view["reply_all_cc"] == ["bob@example.test", "carol@example.test"]
    assert view["reply_all"] is False and view["mode"] == "reply"
    assert view["original"]["message_id"] == "<parent@example.test>"
    assert text["warnings"] == view["warnings"] == []
    assert "nothing is sent from the view" in text["note"]


def test_reply_all_uses_reply_to_and_excludes_own_and_duplicate_addresses():
    entry = item(
        headers={"Reply-To": "Lists <list@example.test>", "Cc": "list@example.test, x@example.test"}
    )
    _, view = reply(entry, reply_all=True)
    assert view["draft"]["to"] == ["list@example.test"]
    assert view["draft"]["cc"] == ["bob@example.test", "x@example.test"]
    assert view["reply_all"] is True


def test_reply_to_your_own_message_goes_to_its_recipients():
    entry = item(**{"from": "Me <Reader@Example.test>", "to": "bob@example.test"})
    _, view = reply(entry)
    assert view["draft"]["to"] == ["bob@example.test"]
    assert view["draft"]["cc"] == []
    assert view["reply_all_cc"] == ["carol@example.test"]


@pytest.mark.parametrize(
    "subject,expected",
    [
        ("Quarterly plan", "Re: Quarterly plan"),
        ("Re: Quarterly plan", "Re: Quarterly plan"),
        ("RE:Quarterly plan", "RE:Quarterly plan"),
        ("Re[2]: Quarterly plan", "Re[2]: Quarterly plan"),
        ("Tabs\tand\r\nbreaks\x00", "Re: Tabs and breaks"),
        ("", "Re:"),
    ],
)
def test_reply_subjects_are_prefixed_once_and_control_free(subject, expected):
    _, view = reply(item(subject=subject))
    assert view["draft"]["subject"] == expected


QUOTE_INTRO = "\n\nOn Wed, 28 Oct 2026 10:00:00 +0000, Alice Example <alice@example.test> wrote:\n"


@pytest.mark.parametrize(
    "original,quoted",
    [
        # A run keeps one line; a lone empty line was never a run.
        ("one\n\n\n\ntwo", "> one\n>\n> two"),
        ("one\n\ntwo\n\nthree", "> one\n>\n> two\n>\n> three"),
        # Nested quotes, spaced or not.
        ("> one\n>\n>\n> two", "> > one\n> >\n> > two"),
        (">> one\n>>\n>>\n>> two", "> >> one\n> >>\n> >> two"),
        ("> > one\n> >\n> >\n> > two", "> > > one\n> > >\n> > > two"),
        # Whitespace only, also a non-breaking space; the run's first line stays as it was.
        ("one\n   \n\t\n\xa0\n\ntwo", "> one\n>    \n> two"),
        (">  \n> \n>\none", "> >  \n> one"),
        # One run, whatever the depth of its lines.
        ("one\n\n>\n> >\n\ntwo", "> one\n>\n> two"),
        ("> one\n>\n\n> >\ntwo", "> > one\n> >\n> two"),
        # At the start and end, and a message with no text at all.
        ("\n\n\none\n\n\n", ">\n> one\n>"),
        ("\n\n\n", ">"),
        ("", ">"),
        # Line endings do not matter.
        ("one\r\n\r\n\r\ntwo\r\r\rthree", "> one\n>\n> two\n>\n> three"),
        # Text is never touched: its spacing, its own ">" and adjacent quoted lines.
        (
            "  one  \n>two\n>two\n> > three  \n-->\n>\n>.\n>\nfour  >",
            ">   one  \n> >two\n> >two\n> > > three  \n> -->\n> >\n> >.\n> >\n> four  >",
        ),
    ],
)
def test_runs_of_empty_quote_lines_collapse_and_quoted_text_is_unchanged(original, quoted):
    text, view = reply(item(text=original))
    assert view["draft"]["body"] == text["draft"]["body"] == f"{QUOTE_INTRO}{quoted}\n"


def test_the_truncation_note_follows_the_collapsed_quote():
    _, view = reply(item(text="one\n\n\n", truncated=True))
    note = "> [The original message is longer; only its beginning is quoted.]"
    assert view["draft"]["body"] == f"{QUOTE_INTRO}> one\n>\n{note}\n"


def test_a_reopened_drafts_own_text_stays_exact():
    saved = "Mine\n\n\n> one\n>\n>\n> two\n> >\n> >\n"
    draft = item(message_id="<draft@example.test>", text=saved)
    _, view = reply(draft, "<draft@example.test>", edit=True)
    assert view["draft"]["body"] == saved


def test_unusable_addresses_are_reported_not_prefilled():
    entry = item(
        **{"from": "=?utf-8?q?J=C3=B6rg?= <jörg@exämple.test>", "message_id": "opaque-provider-id"},
        to="reader@example.test",
        headers={},
        truncated=True,
    )
    result = {"messages": [entry], "partial": True}
    _, view = views.reply_view(result, "opaque-provider-id", own=OWN)
    assert view["draft"]["to"] == []
    # Opaque provider identities are exact; create_draft accepts them unchanged.
    assert view["draft"]["in_reply_to"] == "opaque-provider-id"
    assert view["draft"]["body"].endswith(
        "> [The original message is longer; only its beginning is quoted.]\n"
    )
    warnings = " ".join(view["warnings"])
    assert "not plain ASCII addresses" in warnings and "jörg@exämple.test" in warnings
    assert "could not be read completely" in warnings


def test_missing_message_is_a_validation_error():
    with pytest.raises(MailValidationError, match="Message not found"):
        reply(requested="<other@example.test>")
    with pytest.raises(MailValidationError, match="Draft not found"):
        reply(requested="<other@example.test>", edit=True)


@pytest.mark.parametrize("edit", [False, True])
def test_limited_seed_lookup_retains_read_details_without_draft_fields(edit):
    details = {
        "partial": True,
        "partial_reason": (
            "Search index has no verified message entry; bounded message lookup incomplete"
        ),
        "index_note": (
            "Search index rebuilding: 2/10 messages indexed (20%); results may be incomplete"
        ),
        "index_status": {"state": "rebuilding", "indexed_count": 2, "target": 10},
    }

    text, view = views.reply_view(
        {"messages": [], **details}, "<saved@example.test>", own=OWN, edit=edit
    )

    for key, value in details.items():
        assert text[key] == view[key] == value
    assert "lookup incomplete" in text["error"]
    assert "not found" not in text["error"]
    assert view["lookup_incomplete"] is True
    assert "draft" not in text and "draft" not in view


def test_editing_a_draft_reloads_its_fields():
    draft = item(
        message_id="<draft@example.test>",
        **{"from": "reader@example.test"},
        to="alice@example.test",
        subject="Re: Quarterly plan",
        headers={"Cc": "bob@example.test", "In-Reply-To": " <parent@example.test> "},
        text="My reply so far",
    )
    text, view = reply(draft, "<draft@example.test>", edit=True, send_enabled=True)
    assert view["mode"] == text["mode"] == "edit"
    assert view["draft"] == {
        "to": ["alice@example.test"],
        "cc": ["bob@example.test"],
        "subject": "Re: Quarterly plan",
        "body": "My reply so far",
        "in_reply_to": "<parent@example.test>",
    }
    assert view["reply_all"] is False and view["reply_all_cc"] == []
    assert view["send_enabled"] is True
    assert "the original stays in Drafts" in text["note"]
    # The note says what a saved edit holds: formatting is not kept.
    assert "with only these fields, as plain text;" in text["note"]
    # Nothing that create_draft cannot write, so nothing to warn about.
    assert text["not_copied"] == view["not_copied"] == {"bcc": []}
    assert text["warnings"] == view["warnings"] == []
    assert "not_copied" not in text["note"]


PLAN_PDF = {"filename": "plan.pdf", "type": "application/pdf", "size": 48213}
UNNAMED_PNG = {"filename": None, "type": "image/png", "size": 9}


@pytest.mark.parametrize(
    "bcc,hidden,left_out",
    [
        (
            "Hidden <hidden@example.test>, =?utf-8?q?J=C3=B6rg?= <jörg@exämple.test>",
            ["hidden@example.test", "jörg@exämple.test"],
            "Bcc recipients (hidden@example.test, jörg@exämple.test)",
        ),
        (
            "hidden@example.test",
            ["hidden@example.test"],
            "Bcc recipients (hidden@example.test)",
        ),
        # A Bcc header that cannot be parsed still names someone: it is listed whole.
        (
            '"a" <a@x.test> trailing junk',
            ['"a" <a@x.test> trailing junk'],
            'Bcc recipients ("a" <a@x.test> trailing junk)',
        ),
    ],
)
def test_editing_a_draft_warns_what_a_saved_edit_leaves_out(bcc, hidden, left_out):
    draft = item(
        message_id="<draft@example.test>",
        to="alice@example.test",
        headers={"Cc": "bob@example.test", "Bcc": bcc},
        text="My reply so far",
    )
    text, view = reply(draft, "<draft@example.test>", edit=True)
    assert text["not_copied"] == view["not_copied"] == {"bcc": hidden}
    warning = (
        f"Saving this edit creates a new draft without the original's {left_out}. "
        "The original draft keeps them."
    )
    # The same warning reaches the model and the composer.
    assert text["warnings"] == view["warnings"] == [warning]
    assert "leaves out the Bcc recipients listed in not_copied" in text["note"]
    # The saved fields are unchanged: create_draft gets no Bcc.
    assert set(view["draft"]) == {"to", "cc", "subject", "body", "in_reply_to"}
    assert view["draft"]["to"] == ["alice@example.test"]
    assert view["draft"]["cc"] == ["bob@example.test"]


def test_editing_a_draft_keeps_its_attachments():
    draft = item(
        message_id="<draft@example.test>",
        attachments=[PLAN_PDF, UNNAMED_PNG],
        text="My reply so far",
    )
    text, view = reply(draft, "<draft@example.test>", edit=True)
    # create_draft copies them from the saved draft, so nothing warns about them.
    assert text["warnings"] == view["warnings"] == []
    assert text["not_copied"] == {"bcc": []}
    assert view["draft"]["attachments_from"] == "<draft@example.test>"
    assert view["draft"]["attachment_indexes"] == [1, 2]
    assert view["attachments_source"] == "<draft@example.test>"
    assert text["attached"] == view["attached"]
    assert view["attached"] == [
        {
            "source": "message",
            "index": 1,
            "name": "plan.pdf",
            "type": "application/pdf",
            "size": 48213,
            "size_encoded": True,
            "checked": True,
        },
        {
            "source": "message",
            "index": 2,
            "name": "(unnamed)",
            "type": "image/png",
            "size": 9,
            "size_encoded": True,
            "checked": True,
        },
    ]
    assert "attached lists the attachments" in text["note"]


def test_a_composer_with_files_says_which_sizes_are_encoded():
    local = [{"name": "mine.csv", "type": "text/csv", "size": 3}]
    text, view = reply(item(attachments=[PLAN_PDF]), local=local)
    assert text["size_note"] == ATTACHED_SIZE_NOTE
    assert "transfer-encoded" in text["size_note"] and "decoded" in text["size_note"]
    assert [row.get("size_encoded") for row in text["attached"]] == [None, True]
    assert text["attached"] == view["attached"]
    new_text, _ = views.new_draft_view("a@example.test", None, "S", "B", local=local)
    assert new_text["size_note"] == ATTACHED_SIZE_NOTE


def test_a_composer_without_files_has_no_size_note():
    assert "size_note" not in reply()[0]
    assert "size_note" not in views.new_draft_view("a@example.test", None, "S", "B")[0]


def test_a_reply_offers_the_originals_attachments_without_keeping_them():
    entry = item(headers={"Bcc": "hidden@example.test"}, attachments=[PLAN_PDF])
    text, view = reply(entry)
    # A reply does not carry the original's attachments or Bcc unless asked, so nothing warns.
    assert "not_copied" not in text and "not_copied" not in view
    assert text["warnings"] == view["warnings"] == []
    assert set(view["draft"]) == {"to", "cc", "subject", "body", "in_reply_to"}
    assert view["attached"][0]["name"] == "plan.pdf" and view["attached"][0]["checked"] is False
    assert view["attachments_source"] == "<parent@example.test>"


def test_a_reply_to_a_saved_draft_keeps_its_attachments():
    entry = item(flags=["\\Seen", "\\Draft"], attachments=[PLAN_PDF])
    _, view = reply(entry)
    assert view["attached"][0]["checked"] is True
    assert view["draft"]["attachment_indexes"] == [1]
    assert view["draft"]["attachments_from"] == "<parent@example.test>"


def test_a_message_without_attachments_lists_none():
    text, view = reply()
    assert "attached" not in text and "attached" not in view
    assert "attachments_source" not in view


@pytest.mark.parametrize("bcc", ["undisclosed-recipients:;", "  ", ""])
def test_a_bcc_header_that_names_nobody_is_not_listed(bcc):
    draft = item(message_id="<draft@example.test>", headers={"Bcc": bcc})
    text, _ = reply(draft, "<draft@example.test>", edit=True)
    assert text["not_copied"] == {"bcc": []}
    assert text["warnings"] == []


def test_long_lists_of_what_is_left_out_are_shortened():
    bcc = ", ".join(f"b{index}@example.test" for index in range(11))
    draft = item(message_id="<draft@example.test>", headers={"Bcc": bcc})
    text, _ = reply(draft, "<draft@example.test>", edit=True)
    assert len(text["not_copied"]["bcc"]) == 11
    [warning] = text["warnings"]
    assert "b9@example.test and 1 more)" in warning and "b10@" not in warning


def test_an_incomplete_draft_read_says_attachments_may_be_missing():
    # A read that timed out before the draft's structure was read lists no attachments.
    draft = item(message_id="<draft@example.test>", truncated=True)
    result = {"messages": [draft], "partial": True, "timed_out": True}
    text, view = views.reply_view(result, "<draft@example.test>", own=OWN, edit=True)
    warnings = [
        "The draft is longer than the read limit; only its beginning loaded.",
        "The draft could not be read completely, so some of its text or attachments may be "
        "missing here.",
    ]
    assert text["warnings"] == view["warnings"] == warnings


def server_settings(**overrides):
    values = {
        "token": TOKEN,
        "bridge_user": "reader@example.com",
        "confirm_mutations": False,
        "action_log_path": None,
    }
    return Settings(**{**values, **overrides})


async def _session_calls(mcp, calls):
    async with create_connected_server_and_client_session(mcp) as session:
        return [await session.call_tool(name, arguments) for name, arguments in calls]


def test_compose_reply_returns_the_size_note_with_its_files(settings):
    source = mail_with("<files@example.com>", part("a.pdf", "application/pdf", b"file"))
    mcp = create_mcp(server_settings(), client(settings, FakeImap({"INBOX": {"1": source}})))
    [opened] = anyio.run(
        _session_calls, mcp, [("compose_reply", {"message_id": "<files@example.com>"})]
    )
    assert json.loads(opened.content[0].text)["size_note"] == ATTACHED_SIZE_NOTE
    assert view_data(opened)["attached"][0]["size_encoded"] is True


def test_composer_fields_save_through_create_draft_as_a_threaded_reply(settings):
    fake = FakeImap(
        {
            "INBOX": {
                "1": message(
                    "<parent@example.com>",
                    references="<ancestor@example.com>",
                    subject="Plans",
                    sections={"1": b"Original line"},
                )
            },
            "Drafts": {},
        }
    )
    mcp = create_mcp(server_settings(), client(settings, fake))
    [opened] = anyio.run(
        _session_calls, mcp, [("compose_reply", {"message_id": "<parent@example.com>"})]
    )
    assert opened.isError is False
    view = view_data(opened)
    assert view["view"] == "reply"
    assert json.loads(opened.content[0].text)["draft"] == view["draft"]
    assert not [call for call in fake.calls if call[0] in {"APPEND", "STORE"}]
    # The Save button sends exactly these fields (plus the user's edits) to create_draft.
    draft = dict(view["draft"])
    draft["body"] = "Thanks!" + draft["body"]
    arguments = {key: value for key, value in draft.items() if value not in (None, [])}
    [saved] = anyio.run(_session_calls, mcp, [("create_draft", arguments)])
    assert saved.isError is False
    draft_id = saved.structuredContent["result"]
    [(folder, flags, _, raw)] = fake.appended
    assert (folder, flags) == ('"Drafts"', r"(\Draft)")
    written = BytesParser(policy=policy.default).parsebytes(raw)
    assert written["Message-ID"] == draft_id
    assert written["To"] == "sender@example.com"
    assert written["Subject"] == "Re: Plans"
    assert written["In-Reply-To"] == "<parent@example.com>"
    assert written["References"] == "<ancestor@example.com> <parent@example.com>"
    content = written.get_content().replace("\r\n", "\n")
    # The quoted date is the original's time in this computer's zone.
    when = format_datetime(parsedate_to_datetime("Wed, 14 Oct 2026 10:00:00 +0000").astimezone())
    assert content.startswith(f"Thanks!\n\nOn {when}, Sender <sender")
    assert "\n> Original line\n" in content


class Reads:
    def __init__(self, entry):
        self.entry = entry
        self.calls = []

    def read_thread(self, **kwargs):
        self.calls.append(kwargs)
        return {"messages": [self.entry], "partial": False}


@pytest.mark.parametrize(
    "arguments,error",
    [
        (
            {"message_id": "<parent@example.test>", "draft_id": "<d@example.test>"},
            "at most one of message_id or draft_id",
        ),
        ({"draft_id": "<d@example.test>", "reply_all": True}, "reply_all applies only"),
        ({"reply_all": True}, "reply_all applies only"),
        ({"message_id": "<parent@example.test>", "to": "a@example.test"}, "apply only without"),
        ({"message_id": "<parent@example.test>", "cc": []}, "apply only without"),
        ({"draft_id": "<d@example.test>", "subject": "New"}, "apply only without"),
        ({"draft_id": "<d@example.test>", "body": "New"}, "apply only without"),
        ({"message_id": "<parent@example.test>", "reply_all": "yes"}, "reply_all"),
        ({"message_id": "<parent@example.test>", "send": True}, "send"),
    ],
)
def test_compose_reply_refuses_ambiguous_arguments_before_reading(arguments, error):
    backend = Reads(item())
    [result] = anyio.run(
        _session_calls, create_mcp(server_settings(), backend), [("compose_reply", arguments)]
    )
    assert result.isError is True
    assert error in result.content[0].text
    assert backend.calls == []


def compose(arguments, backend=None):
    backend = backend or Reads(item())
    mcp = create_mcp(server_settings(), backend)
    [result] = anyio.run(_session_calls, mcp, [("compose_reply", arguments)])
    return result, backend


BLANK = {"to": [], "cc": [], "subject": "", "body": "", "in_reply_to": None}


def test_compose_reply_without_ids_opens_a_blank_new_draft_and_reads_nothing():
    result, backend = compose({})
    assert result.isError is False
    assert backend.calls == []
    text = json.loads(result.content[0].text)
    view = view_data(result)
    assert view["view"] == "reply"
    assert text["mode"] == view["mode"] == "new"
    assert text["draft"] == view["draft"] == BLANK
    assert text["warnings"] == view["warnings"] == []
    # Nothing belongs to a reply: no original, no Reply all and no threading.
    assert "original" not in text and "original" not in view
    assert view["reply_all"] is False and view["reply_all_cc"] == []
    assert "create_draft" in text["note"] and "at least one recipient" in text["note"]


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ({"to": "alice@example.test"}, {"to": ["alice@example.test"]}),
        (
            {"to": ["a@example.test", "b@example.test"]},
            {"to": ["a@example.test", "b@example.test"]},
        ),
        ({"cc": "carol@example.test"}, {"cc": ["carol@example.test"]}),
        (
            {
                "to": "Alice <alice@example.test>, bob@example.test",
                "cc": ["Carol <c@example.test>"],
            },
            {"to": ["alice@example.test", "bob@example.test"], "cc": ["c@example.test"]},
        ),
        # A JSON-encoded list, which the SDK decodes as it does for create_draft.
        (
            {"to": '["a@example.test", "b@example.test"]'},
            {"to": ["a@example.test", "b@example.test"]},
        ),
        ({"to": "", "cc": []}, {}),
        # A new draft is no reply: its subject gets no prefix, only loses control characters.
        ({"subject": "Hello"}, {"subject": "Hello"}),
        (
            {"subject": "Tabs\tand\r\nbreaks\x85of\u2028any\u2029kind\x00"},
            {"subject": "Tabs and breaks of any kind"},
        ),
        ({"body": "Hi\n\n> kept\n>\n>\n  there\n"}, {"body": "Hi\n\n> kept\n>\n>\n  there\n"}),
        # Text that happens to be JSON stays text.
        ({"subject": "null", "body": '["a"]'}, {"subject": "null", "body": '["a"]'}),
        ({"subject": "[1, 2]", "body": '{"a": 1}'}, {"subject": "[1, 2]", "body": '{"a": 1}'}),
    ],
)
def test_compose_reply_without_ids_prefills_the_fields_given(arguments, expected):
    result, backend = compose(arguments)
    assert result.isError is False
    assert backend.calls == []
    text = json.loads(result.content[0].text)
    assert text["mode"] == view_data(result)["mode"] == "new"
    assert text["draft"] == view_data(result)["draft"] == {**BLANK, **expected}
    assert text["warnings"] == []


@pytest.mark.parametrize("field", ["to", "cc"])
@pytest.mark.parametrize(
    "value",
    [
        "not an address",
        "a@example.test, not an address",
        "a@example.test,",
        "a@example.test\r\nX: y",
    ],
)
def test_a_new_draft_refuses_recipients_that_create_draft_would_refuse(field, value):
    result, backend = compose({field: value, "subject": "Kept out of the error"})
    assert result.isError is True
    assert "Validation error" in result.content[0].text
    assert "Kept out" not in result.content[0].text
    assert backend.calls == []


@pytest.mark.parametrize(
    "arguments,to,listed",
    [
        (
            {"to": "Bob <bob@exämple.test>, alice@example.test", "cc": "a@localhost"},
            ["alice@example.test"],
            "bob@exämple.test, a@localhost",
        ),
        # With nothing skipped in to, the address in cc must still not disappear unannounced.
        ({"cc": "a@localhost"}, [], "a@localhost"),
    ],
)
def test_a_new_draft_lists_addresses_it_cannot_prefill(arguments, to, listed):
    # create_draft accepts these, but only plain ASCII addresses are prefilled, as in a reply.
    result, _ = compose(arguments)
    assert result.isError is False
    text = json.loads(result.content[0].text)
    assert text["draft"]["to"] == to and text["draft"]["cc"] == []
    assert text["warnings"] == [
        f"Some recipients are not plain ASCII addresses and were not prefilled: {listed}"
    ]
    assert view_data(result)["warnings"] == text["warnings"]


def test_a_new_draft_that_cannot_be_prepared_reports_no_details(monkeypatch):
    def explode(*args):
        raise RuntimeError("internal detail password=never-shown")

    monkeypatch.setattr(views, "new_draft_view", explode)
    result, _ = compose({})
    assert result.isError is True
    assert "New draft could not be prepared" in result.content[0].text
    assert "never-shown" not in result.content[0].text


def test_a_new_draft_saves_only_through_create_draft_and_never_sends(settings, monkeypatch):
    connections = []
    for name in ("SMTP", "SMTP_SSL"):
        monkeypatch.setattr(f"smtplib.{name}", lambda *args, **kwargs: connections.append(name))
    fake = FakeImap({"INBOX": {}, "Drafts": {}})
    mcp = create_mcp(server_settings(enable_send=True), client(settings, fake))
    fields = {
        "to": ["alice@example.com", "bob@example.com"],
        "cc": "carol@example.com",
        "subject": "Plans",
        "body": "Hello\n",
    }
    [opened] = anyio.run(_session_calls, mcp, [("compose_reply", fields)])
    assert opened.isError is False
    # Opening touches no mail, and the view offers no send review even with send enabled.
    assert fake.calls == [] and fake.appended == []
    view = view_data(opened)
    assert view["send_enabled"] is False
    # The Save button sends exactly these fields (plus the user's edits) to create_draft.
    arguments = {key: value for key, value in view["draft"].items() if value not in (None, [])}
    [saved] = anyio.run(_session_calls, mcp, [("create_draft", arguments)])
    assert saved.isError is False
    [(folder, flags, _, raw)] = fake.appended
    assert (folder, flags) == ('"Drafts"', r"(\Draft)")
    written = BytesParser(policy=policy.default).parsebytes(raw)
    assert written["Message-ID"] == saved.structuredContent["result"]
    assert written["To"] == "alice@example.com, bob@example.com"
    assert written["Cc"] == "carol@example.com"
    assert written["Subject"] == "Plans"
    assert written["In-Reply-To"] is None and written["References"] is None
    assert written.get_content().replace("\r\n", "\n") == "Hello\n"
    assert connections == []


def test_a_new_draft_without_a_recipient_is_not_saved(settings):
    fake = FakeImap({"INBOX": {}, "Drafts": {}})
    mcp = create_mcp(server_settings(), client(settings, fake))
    [opened] = anyio.run(_session_calls, mcp, [("compose_reply", {"subject": "S", "body": "B"})])
    arguments = {key: value for key, value in view_data(opened)["draft"].items() if value}
    [saved] = anyio.run(_session_calls, mcp, [("create_draft", {"to": [], **arguments})])
    assert saved.isError is True
    assert "At least one recipient is required" in saved.content[0].text
    assert fake.appended == []


@pytest.mark.parametrize(
    "argument,read",
    [("message_id", {"include_flags": True}), ("draft_id", {"include_bcc": True})],
)
def test_compose_reply_with_an_id_is_unchanged(argument, read):
    target = "<parent@example.test>"
    result, backend = compose({argument: target})
    assert result.isError is False
    assert backend.calls == [{"message_id": target, "include_thread": False, **read}]
    expected_text, expected_view = views.reply_view(
        {"messages": [item()], "partial": False},
        target,
        own={"reader@example.com"},
        edit=argument == "draft_id",
        sender="reader@example.com",
        # Without PROTON_MCP_TIMEZONE, dates are in this computer's zone.
        zone=None,
    )
    assert json.loads(result.content[0].text) == expected_text
    assert view_data(result) == expected_view
    assert expected_text["mode"] == ("reply" if argument == "message_id" else "edit")


@pytest.mark.parametrize("argument", ["message_id", "draft_id"])
def test_an_empty_id_is_not_a_new_draft(argument):
    result, _ = compose({argument: ""})
    assert result.isError is True
    assert "Validation error" in result.content[0].text


def test_compose_reply_collapses_empty_quote_lines_of_a_real_read(settings):
    original = message(
        "<parent@example.com>", sections={"1": b"One\r\n\r\n\r\n\r\nTwo\r\n> >\r\n> >"}
    )
    fake = FakeImap({"INBOX": {"1": original}, "Drafts": {}})
    result, _ = compose({"message_id": "<parent@example.com>"}, client(settings, fake))
    assert result.isError is False
    assert json.loads(result.content[0].text)["draft"]["body"].endswith(
        " wrote:\n> One\n>\n> Two\n> > >\n"
    )


def test_compose_reply_reads_only_the_requested_message_and_excludes_own_addresses():
    backend = Reads(item(to="reader@example.com, Me <ME@example.test>", headers={}))
    config = server_settings(enable_send=True, send_from_allowlist=("me@example.test",))
    [result, missing] = anyio.run(
        _session_calls,
        create_mcp(config, backend),
        [
            ("compose_reply", {"message_id": "<parent@example.test>", "reply_all": True}),
            ("compose_reply", {"message_id": "<absent@example.test>"}),
        ],
    )
    assert backend.calls == [
        {"message_id": "<parent@example.test>", "include_thread": False, "include_flags": True},
        {"message_id": "<absent@example.test>", "include_thread": False, "include_flags": True},
    ]
    assert result.isError is False
    assert view_data(result)["draft"]["cc"] == []
    assert view_data(result)["send_enabled"] is True
    assert missing.isError is True
    assert "Message not found" in missing.content[0].text


@pytest.mark.parametrize("argument", ["message_id", "draft_id"])
def test_compose_reply_limited_seed_returns_retryable_error_with_index_details(argument):
    details = {
        "partial": True,
        "partial_reason": (
            "Search index has no verified message entry; bounded message lookup incomplete"
        ),
        "index_note": (
            "Search index rebuilding: 2/10 messages indexed (20%); results may be incomplete"
        ),
        "index_status": {"state": "rebuilding", "indexed_count": 2, "target": 10},
    }

    class LimitedRead:
        def read_thread(self, **kwargs):
            return {"messages": [], **details}

    [result] = anyio.run(
        _session_calls,
        create_mcp(server_settings(), LimitedRead()),
        [("compose_reply", {argument: "<saved@example.test>"})],
    )

    assert result.isError is True
    payload = json.loads(result.content[0].text)
    for key, value in details.items():
        assert payload[key] == view_data(result)[key] == value
    assert "not found" not in payload["error"]
    assert "lookup incomplete" in payload["error"]
    assert "draft" not in payload and "draft" not in view_data(result)


@pytest.mark.parametrize("argument", ["message_id", "draft_id"])
def test_compose_reply_real_read_retries_an_uncached_message_without_false_absence(
    settings, monkeypatch, argument
):
    fake = FakeImap(
        {"All Mail": {"501": message("<indexed@example.com>")}, "Drafts": {}}, uidnext=True
    )
    mailbox = client(settings, fake)
    mailbox.rebuild_search_index("All Mail")
    assert mailbox.search_index.reporting_count("All Mail", "1") == 1
    target = "<saved@example.com>"
    uncached = message(
        target,
        subject="Saved message",
        flags={r"\Draft"} if argument == "draft_id" else set(),
    )
    # The message exists before this request. Like a newly saved draft, it has
    # no seed hint in the index that covered the preceding mailbox inventory.
    fake.folders["All Mail"]["502"] = uncached
    if argument == "draft_id":
        fake.folders["Drafts"]["9"] = uncached
    assert mailbox.search_index.seed_uid("All Mail", "1", target) is None
    original_search = mailbox._search
    searches = []

    def limited_seed(connection, criteria, *args, **kwargs):
        searches.append(criteria)
        raise CallDeadlineExceeded("Synthetic seed lookup deadline")

    monkeypatch.setattr(mailbox, "_search", limited_seed)
    mcp = create_mcp(server_settings(), mailbox)
    [limited] = anyio.run(_session_calls, mcp, [("compose_reply", {argument: target})])

    assert limited.isError is True
    assert len(searches) == 1 and searches[0][:2] == ["HEADER", "Message-ID"]
    payload = json.loads(limited.content[0].text)
    assert payload["partial"] is True
    assert "lookup incomplete" in payload["partial_reason"]
    assert "not found" not in payload["error"]
    assert payload["index_status"]["indexed_count"] == 1
    assert payload["index_status"]["target"] == 2
    assert "1/2 messages indexed (50%)" in payload["index_note"]
    assert "draft" not in payload and "draft" not in view_data(limited)
    assert "timed_out" not in payload

    monkeypatch.setattr(mailbox, "_search", original_search)
    [retried] = anyio.run(_session_calls, mcp, [("compose_reply", {argument: target})])
    assert retried.isError is False
    data = view_data(retried)
    assert data["original"]["message_id"] == target
    assert data["draft"]["body"]
    assert fake.appended == []


@browser
@pytest.mark.parametrize("edit", [False, True])
def test_limited_seed_reply_view_shows_the_error_without_enabling_save(tmp_path, edit):
    text, view = views.reply_view(
        {
            "messages": [],
            "partial": True,
            "partial_reason": "bounded message lookup incomplete",
            "index_note": "search index incomplete: 1/2 messages indexed (50%)",
        },
        "<saved@example.test>",
        own=OWN,
        edit=edit,
    )
    result = mcp_apps.view_result(text, view)
    result.isError = True
    report = run_view(
        tmp_path,
        "reply",
        tool_result(result),
        """
const doc = t.doc;
await t.waitFor(() => doc.getElementById("status").textContent, "the lookup error");
t.check("error", doc.getElementById("status").textContent);
t.check("initial_save_disabled", doc.getElementById("save").disabled);
t.input(doc.getElementById("to"), "alice@example.test");
t.input(doc.getElementById("body"), "A new body must not create a blank reply");
t.check("edited_save_disabled", doc.getElementById("save").disabled);
doc.getElementById("save").disabled = false;
doc.getElementById("save").click();
await t.sleep(50);
""",
    )
    checks = report["checks"]
    assert checks["initial_save_disabled"] is checks["edited_save_disabled"] is True
    assert "lookup incomplete" in checks["error"]
    assert "1/2 messages indexed (50%)" in checks["error"]
    assert requests(report, "tools/call") == []


def test_compose_reply_reads_a_drafts_bcc_only_when_editing_it():
    backend = Reads(item(message_id="<d@example.test>", headers={"Bcc": "hidden@example.test"}))
    [edited] = anyio.run(
        _session_calls,
        create_mcp(server_settings(), backend),
        [("compose_reply", {"draft_id": "<d@example.test>"})],
    )
    # A reply reads no Bcc (above); a draft's is read to say that an edit leaves it out.
    assert backend.calls == [
        {"message_id": "<d@example.test>", "include_thread": False, "include_bcc": True}
    ]
    assert edited.isError is False
    text = json.loads(edited.content[0].text)
    assert text["not_copied"] == {"bcc": ["hidden@example.test"]}
    assert view_data(edited)["not_copied"] == text["not_copied"]
    assert view_data(edited)["warnings"] == text["warnings"]
    assert "Bcc recipients (hidden@example.test)" in text["warnings"][0]


def with_bcc(item, bcc):
    """A message whose headers carry Bcc, as Bridge serves drafts and sent copies."""
    item.headers = item.headers.replace(b"Subject:", b"Bcc: " + bcc + b"\r\nSubject:")
    return item


def test_a_drafts_attachments_stay_and_its_bcc_is_listed_before_an_edit_is_saved(settings):
    mixed = b"(" + PLAIN + b" " + ATTACHMENT + b' "MIXED" ("BOUNDARY" "x") NIL NIL)'
    pdf = base64.encodebytes(b"%PDF-1.4 invoice bytes")
    draft = message(
        "<draft@example.com>",
        structure=mixed,
        sections={"1": b"My reply so far", "2": pdf},
        flags={r"\Draft"},
    )
    with_bcc(draft, b"Hid <hidden@example.com>")
    fake = FakeImap({"INBOX": {"1": message("<parent@example.com>")}, "Drafts": {"5": draft}})
    mcp = create_mcp(server_settings(), client(settings, fake))

    def header_reads():
        return [call[2] for call in fake.calls if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]]

    [edited] = anyio.run(
        _session_calls, mcp, [("compose_reply", {"draft_id": "<draft@example.com>"})]
    )
    assert edited.isError is False
    # Bcc comes with the flags that show which messages are drafts.
    assert header_reads()
    assert all(" FLAGS " in query and "SUBJECT DATE BCC)]" in query for query in header_reads())
    text = json.loads(edited.content[0].text)
    assert text["not_copied"] == {"bcc": ["hidden@example.com"]}
    assert text["warnings"] == [
        "Saving this edit creates a new draft without the original's "
        "Bcc recipients (hidden@example.com). The original draft keeps them.",
        "This draft is from sender@example.com, which is not an allowed From address. "
        "Saving writes it from reader@example.com.",
    ]
    view = view_data(edited)
    assert view["not_copied"] == text["not_copied"] and view["warnings"] == text["warnings"]
    assert text["draft"]["body"] == "My reply so far"
    assert [row["name"] for row in view["attached"]] == ["invoice.pdf"]
    # A draft saved from these fields keeps the attachment and, as the warning says, no Bcc.
    arguments = {key: value for key, value in text["draft"].items() if value not in (None, [])}
    [saved] = anyio.run(_session_calls, mcp, [("create_draft", arguments)])
    assert saved.isError is False
    [(_, _, _, raw)] = fake.appended
    written = BytesParser(policy=policy.default).parsebytes(raw)
    assert written["Bcc"] is None
    [kept] = written.iter_attachments()
    assert kept.get_filename() == "invoice.pdf" and kept.get_content_type() == "application/pdf"
    assert kept.get_content() == b"%PDF-1.4 invoice bytes"
    # A reply reads no Bcc header.
    fake.calls.clear()
    [replied] = anyio.run(
        _session_calls, mcp, [("compose_reply", {"message_id": "<parent@example.com>"})]
    )
    assert replied.isError is False
    assert header_reads() and not any("BCC" in query for query in header_reads())
    assert "not_copied" not in json.loads(replied.content[0].text)


@pytest.mark.parametrize("flags", [None, {r"\Seen"}])
def test_a_messages_bcc_is_never_shown_unless_it_is_a_draft(settings, flags):
    # A sent copy keeps its Bcc header, but draft_id must not reveal hidden recipients.
    sent = with_bcc(message("<sent@example.com>", flags=flags), b"secret@example.com")
    fake = FakeImap({"INBOX": {}, "Sent": {"3": sent}})
    [edited] = anyio.run(
        _session_calls,
        create_mcp(server_settings(), client(settings, fake)),
        [("compose_reply", {"draft_id": "<sent@example.com>"})],
    )
    assert edited.isError is False
    assert [call for call in fake.calls if call[0] == "FETCH" and "BCC)]" in call[2]]
    text = json.loads(edited.content[0].text)
    assert text["not_copied"] == {"bcc": []}
    assert "secret@" not in json.dumps(edited.model_dump(mode="json", by_alias=True))


def test_only_drafts_keep_their_bcc_in_a_bcc_read(settings):
    draft = with_bcc(message("<draft@example.com>", flags={r"\Draft", r"\Seen"}), b"d@example.com")
    sent = with_bcc(message("<sent@example.com>", flags={r"\Seen"}), b"s@example.com")
    fake = FakeImap({"INBOX": {"1": draft, "2": sent}})
    reader = client(settings, fake)

    def headers(message_id, **options):
        [item] = reader.read_thread(message_id, include_thread=False, **options)["messages"]
        return {name.lower(): value for name, value in item["headers"].items()}

    def queries():
        return [call[2] for call in fake.calls if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]]

    assert headers("<draft@example.com>", include_bcc=True)["bcc"] == "d@example.com"
    assert "bcc" not in headers("<sent@example.com>", include_bcc=True)
    bcc_reads = queries()
    assert bcc_reads and all("BCC" in query and " FLAGS " in query for query in bcc_reads)
    # Without include_bcc the read asks for neither (the double ignores the field list).
    fake.calls.clear()
    headers("<sent@example.com>")
    [query] = queries()
    assert "BCC" not in query and " FLAGS " not in query


def test_include_bcc_must_be_a_boolean(settings):
    fake = FakeImap({"INBOX": {"1": message("<1@example.com>")}})
    with pytest.raises(MailValidationError, match="include_bcc must be a boolean"):
        client(settings, fake).read_thread("<1@example.com>", include_bcc="yes")
    assert fake.calls == []


def test_reply_view_saves_only_drafts_and_never_sends():
    html = mcp_apps.view_html("reply")
    code = assert_self_contained_view(html)
    view_script = code.split("var PMM = (function", 1)[1].split("})();", 1)[1]
    # compose_reply only through the bridge, for data its host left out.
    assert tool_calls(code) == {"create_draft", "compose_reply"}
    for forbidden in ("preview_send", "send_draft", "delete_draft(", "token"):
        assert forbidden not in view_script
    # Chat messages quote only identifiers that pass the plain Message-ID check.
    assert view_script.count("PMM.sendMessage(") == 2
    assert 'data.send_enabled && PMM.can("message") && quotable' in view_script
    assert "PLAIN_ID.test(id)" in view_script
    assert "PMM.updateModelContext(" in view_script
    # A completed save records what it submitted; Save compares the fields with that.
    assert "savedContent = submitted;" in view_script and "dirty" not in view_script
    assert "saveButton.disabled = !ready || !unsaved();" in view_script
    # Saving needs host tool calls: the button and the click handler share one check.
    can_save = view_script.split("function canSave()", 1)[1].split("\n  }", 1)[0]
    assert 'PMM.can("serverTools")' in can_save
    assert "var ready = canSave() && " in view_script
    assert "if (!canSave()) return;" in view_script
    # Reset restores the prefilled fields together with the original Reply all choice.
    reset = view_script.split('getElementById("reset").addEventListener', 1)[1].split("});", 1)[0]
    assert "restore();" in reset
    restore = view_script.split("function restore()", 1)[1].split("\n  }", 1)[0]
    assert 'getElementById("all").checked = !!data.reply_all;' in restore
    assert "fill(data.draft);" in restore


def composer(**options):
    """The tool result a host delivers to the composer view."""
    return tool_result(mcp_apps.view_result(*reply(**options)))


def saved_draft(draft_id):
    """create_draft's result for one new draft."""
    return {
        "content": [{"type": "text", "text": draft_id}],
        "structuredContent": {"result": draft_id},
    }


SAVE_RACE = """
const doc = t.doc;
await t.waitFor(() => doc.getElementById("to").value, "the prefilled reply");
const save = doc.getElementById("save");
const body = doc.getElementById("body");
t.check("enabled_before_saving", !save.disabled);
t.input(body, "First version" + body.value);
t.hold = true;
save.click();
await t.waitFor(() => t.held.length === 1, "the first create_draft call");
t.check("disabled_while_saving", save.disabled);
// The user keeps typing while the save is in flight.
t.input(body, "Second version, " + body.value);
t.hold = false;
t.release();
await t.waitFor(() => doc.querySelector("#saved .banner.ok"), "the first saved draft");
await t.sleep(50);
t.check("enabled_after_typing_during_save", !save.disabled);
t.check("label", save.textContent);
t.check("status", doc.getElementById("status").textContent);
save.click();
await t.waitFor(
  () => doc.getElementById("saved").textContent.includes("Earlier versions"),
  "the second saved draft"
);
await t.sleep(50);
t.check("disabled_after_clean_save", save.disabled);
t.input(body, body.value + "!");
t.check("enabled_after_new_edit", !save.disabled);
t.input(body, body.value.slice(0, -1));
t.check("disabled_after_reverting", save.disabled);
"""


@browser
def test_edits_made_while_a_draft_saves_stay_unsaved(tmp_path):
    drafts = [saved_draft("<draft-1@example.test>"), saved_draft("<draft-2@example.test>")]
    report = run_view(
        tmp_path, "reply", composer(send_enabled=True), SAVE_RACE, tools={"create_draft": drafts}
    )
    checks = report["checks"]
    assert checks["enabled_before_saving"] is True
    assert checks["disabled_while_saving"] is True
    # The first save completed with older text: the newer edits remain unsaved.
    assert checks["enabled_after_typing_during_save"] is True
    assert checks["label"] == "Save as new draft"
    assert "Save again" in checks["status"]
    first, second = requests(report, "tools/call")
    assert first["params"]["arguments"]["body"].startswith("First version\n\nOn Wed, 28 Oct")
    assert second["params"]["arguments"]["body"].startswith("Second version, First version\n")
    # Clean only while the fields equal the last saved draft.
    assert checks["disabled_after_clean_save"] is True
    assert checks["enabled_after_new_edit"] is True
    assert checks["disabled_after_reverting"] is True


SAVE_NEEDS_TOOLS = """
const doc = t.doc;
const notes = () =>
  Array.from(doc.querySelectorAll(".banner"))
    .filter((node) => t.visible(node))
    .map((node) => node.textContent);
await t.waitFor(() => doc.getElementById("to").value, "the prefilled reply");
const save = doc.getElementById("save");
const body = doc.getElementById("body");
t.check("enabled_when_prefilled", !save.disabled);
t.input(body, "Edited" + body.value);
t.check("enabled_after_edit", !save.disabled);
// Click even a disabled button: the handler itself must refuse without tool calls.
save.disabled = false;
save.click();
await t.sleep(500);
t.check("saved", doc.getElementById("saved").textContent);
t.check("notes", notes());
// Reset clears the status line, not the reason why Save is unavailable.
doc.getElementById("reset").click();
t.check("notes_after_reset", notes());
t.check("enabled_after_reset", !save.disabled);
"""


@browser
@pytest.mark.parametrize("server_tools", [True, False])
def test_save_needs_a_host_that_lets_views_call_tools(tmp_path, server_tools):
    capabilities = dict(HOST_CAPABILITIES)
    if not server_tools:
        del capabilities["serverTools"]
    report = run_view(
        tmp_path,
        "reply",
        composer(),
        SAVE_NEEDS_TOOLS,
        tools={"create_draft": saved_draft("<draft-1@example.test>")},
        capabilities=capabilities,
    )
    checks = report["checks"]
    calls = requests(report, "tools/call")
    if server_tools:
        assert checks["enabled_when_prefilled"] is True
        assert checks["enabled_after_edit"] is True
        assert [call["params"]["name"] for call in calls] == ["create_draft"]
        saved = ["Draft saved in Drafts. It has not been sent."]
        assert checks["notes"] == checks["notes_after_reset"] == saved
        assert checks["enabled_after_reset"] is True
    else:
        # Save stays disabled, and a click that reaches it anyway calls no tool.
        assert checks["enabled_when_prefilled"] is False
        assert checks["enabled_after_edit"] is False
        assert calls == []
        assert checks["saved"] == ""
        # The view keeps saying why, also after Reset.
        warning = "This host does not let views call tools, so the draft cannot be saved from here."
        assert checks["notes"] == checks["notes_after_reset"] == [warning]
        assert checks["enabled_after_reset"] is False


RESET = """
const doc = t.doc;
await t.waitFor(() => doc.getElementById("to").value, "the prefilled reply");
const all = doc.getElementById("all");
const cc = doc.getElementById("cc");
const body = doc.getElementById("body");
const save = doc.getElementById("save");
const reset = doc.getElementById("reset");
t.check("toggle_shown", t.visible(all));
t.check("initial", [all.checked, cc.value]);
all.click();
t.input(body, "Edited" + body.value);
t.check("changed", [all.checked, cc.value]);
reset.click();
t.check("reset", [all.checked, cc.value, body.value.startsWith("Edited")]);
// After saving an edited version, Reset brings back a prefill that differs from it.
t.input(body, "Edited" + body.value);
save.click();
await t.waitFor(() => doc.querySelector("#saved .banner.ok"), "the saved draft");
await t.sleep(50);
t.check("disabled_after_save", save.disabled);
reset.click();
t.check("enabled_after_reset", !save.disabled);
"""


@browser
@pytest.mark.parametrize("reply_all", [False, True])
def test_reset_restores_the_prefilled_fields_and_reply_all_choice(tmp_path, reply_all):
    report = run_view(
        tmp_path,
        "reply",
        composer(reply_all=reply_all),
        RESET,
        tools={"create_draft": saved_draft("<draft-1@example.test>")},
    )
    checks = report["checks"]
    everyone = "bob@example.test, carol@example.test"
    original = [reply_all, everyone if reply_all else ""]
    assert checks["toggle_shown"] is True
    assert checks["initial"] == original
    assert checks["changed"] == [not reply_all, "" if reply_all else everyone]
    # The checkbox and Cc return together: the view never shows Reply all with no Cc.
    assert checks["reset"] == [*original, False]
    assert checks["disabled_after_save"] is True
    assert checks["enabled_after_reset"] is True


LEFT_OUT = """
const doc = t.doc;
await t.waitFor(() => doc.getElementById("to").value, "the draft's fields");
const hint = doc.getElementById("hint");
const save = doc.getElementById("save");
const shown = () => ({
  warnings: Array.from(doc.querySelectorAll("#warnings .banner"))
    .filter((node) => t.visible(node))
    .map((node) => node.textContent),
  hint: t.visible(hint) && hint.textContent,
  warn: hint.classList.contains("warn"),
});
// Before anything is saved, and still after a save.
t.check("before", shown());
t.check("enabled", !save.disabled);
save.click();
await t.waitFor(() => doc.querySelector("#saved .banner.ok"), "the saved draft");
t.check("after", shown());
"""


@browser
@pytest.mark.parametrize(
    "attachments,bcc,keeps",
    [
        ([PLAN_PDF], "hidden@example.test", True),
        ([PLAN_PDF, UNNAMED_PNG], None, True),
        ([], "hidden@example.test", False),
        ([], None, False),
    ],
)
def test_the_composer_says_what_a_saved_edit_keeps_and_leaves_out_before_saving(
    tmp_path, attachments, bcc, keeps
):
    draft = item(
        message_id="<draft@example.test>",
        to="alice@example.test",
        headers={"Bcc": bcc} if bcc else {},
        attachments=attachments,
        text="My reply so far",
    )
    report = run_view(
        tmp_path,
        "reply",
        composer(entry=draft, requested="<draft@example.test>", edit=True),
        LEFT_OUT,
        tools={"create_draft": saved_draft("<draft-2@example.test>")},
    )
    checks = report["checks"]
    saves = "Saving creates a new draft with only these fields"
    if keeps:
        saves += " and the ticked attachments"
    saves += ", as plain text from the address above"
    if bcc:
        warnings = [
            "Saving this edit creates a new draft without the original's Bcc recipients "
            f"({bcc}). The original draft keeps them."
        ]
        hint = (
            f"{saves}, so it leaves out the original's Bcc recipients; "
            "the original draft stays in Drafts, unchanged, until you delete it."
        )
    else:
        warnings = []
        hint = f"{saves}; the original draft stays in Drafts until you delete it."
    # The warning is on screen, and repeated next to Save, before and after a save.
    expected = {"warnings": warnings, "hint": hint, "warn": bool(bcc)}
    assert checks["before"] == checks["after"] == expected
    # It does not block saving, and a save carries the attachments that create_draft keeps.
    assert checks["enabled"] is True
    [call] = requests(report, "tools/call")
    assert call["params"]["name"] == "create_draft"
    arguments = call["params"]["arguments"]
    assert set(arguments) == {"to", "cc", "subject", "body"} | (
        {"attachments_from", "attachment_indexes"} if keeps else set()
    )
    if keeps:
        assert arguments["attachments_from"] == "<draft@example.test>"
        assert arguments["attachment_indexes"] == list(range(1, len(attachments) + 1))


NEW_DRAFT = """
const doc = t.doc;
await t.waitFor(() => doc.getElementById("mode").textContent === "New draft", "the new draft");
const save = doc.getElementById("save");
const to = doc.getElementById("to");
const value = (id) => doc.getElementById(id).value;
t.check("heading", doc.getElementById("heading").textContent);
t.check("fields", [value("to"), value("cc"), value("subject"), value("body")]);
t.check("original", t.visible(doc.getElementById("original")));
t.check("toggle", t.visible(doc.getElementById("all-toggle")));
t.check("hint", doc.getElementById("hint").textContent);
t.check("enabled_when_opened", !save.disabled);
// Like create_draft, Save needs a recipient, and a click without one calls no tool.
const prefilled = value("to");
t.input(to, "");
t.check("enabled_without_recipient", !save.disabled);
save.disabled = false;
save.click();
await t.sleep(50);
t.check("status_without_recipient", doc.getElementById("status").textContent);
t.check("calls_without_recipient", t.requests("tools/call").length);
t.input(to, prefilled || "alice@example.test");
t.check("enabled_with_recipient", !save.disabled);
save.click();
await t.waitFor(() => doc.querySelector("#saved .banner.ok"), "the saved draft");
await t.sleep(50);
t.check("saved", doc.querySelector("#saved .banner.ok").textContent);
t.check("review_button", !!t.button("Review & send…"));
doc.getElementById("reset").click();
t.check("enabled_after_reset", !save.disabled);
"""


@browser
@pytest.mark.parametrize(
    "fields,heading,shown,arguments",
    [
        (
            {},
            "New message",
            ["", "", "", ""],
            {"to": ["alice@example.test"], "cc": None, "subject": "", "body": ""},
        ),
        (
            {
                "to": "alice@example.test",
                "cc": ["bob@example.test", "carol@example.test"],
                "subject": "Plans",
                "body": "Hello\n",
            },
            "Plans",
            ["alice@example.test", "bob@example.test, carol@example.test", "Plans", "Hello\n"],
            {
                "to": ["alice@example.test"],
                "cc": ["bob@example.test", "carol@example.test"],
                "subject": "Plans",
                "body": "Hello\n",
            },
        ),
    ],
)
def test_the_new_draft_composer_saves_only_through_create_draft(
    tmp_path, fields, heading, shown, arguments
):
    text, view = views.new_draft_view(
        fields.get("to"), fields.get("cc"), fields.get("subject", ""), fields.get("body", "")
    )
    report = run_view(
        tmp_path,
        "reply",
        tool_result(mcp_apps.view_result(text, view)),
        NEW_DRAFT,
        tools={"create_draft": saved_draft("<draft-1@example.test>")},
    )
    checks = report["checks"]
    assert checks["heading"] == heading
    assert checks["fields"] == shown
    # Nothing on screen belongs to a reply, and the hint promises no threading.
    assert checks["original"] is False and checks["toggle"] is False
    assert checks["hint"] == (
        "Saving creates an unsent draft in Drafts. "
        "Sending needs a separate review and your confirmation."
    )
    assert checks["enabled_when_opened"] is bool(fields.get("to"))
    assert checks["enabled_without_recipient"] is False
    assert checks["status_without_recipient"] == "Add at least one recipient."
    assert checks["calls_without_recipient"] == 0
    assert checks["enabled_with_recipient"] is True
    assert checks["saved"] == "Draft saved in Drafts. It has not been sent."
    # One create_draft call, no threading fields, and nothing that leads to a send.
    [call] = requests(report, "tools/call")
    assert call["params"]["name"] == "create_draft"
    assert call["params"]["arguments"] == arguments
    assert checks["review_button"] is False
    assert requests(report, "ui/message") == []
    [context] = requests(report, "ui/update-model-context")
    assert "saved an unsent new draft" in context["params"]["content"][0]["text"]
    # Reset brings back what it opened with: no recipient, or the fields just saved.
    assert checks["enabled_after_reset"] is False


def test_compose_reply_is_linked_to_its_view():
    async def listing():
        async with create_connected_server_and_client_session(
            create_mcp(server_settings(), Reads(item()))
        ) as session:
            return {tool.name: tool for tool in (await session.list_tools()).tools}

    tools = anyio.run(listing)
    tool = tools["compose_reply"]
    # Hosts render the view for the model's calls; the view may call it again only to load
    # data its host left out, with the arguments the host delivered.
    assert tool.meta["ui"] == {"resourceUri": mcp_apps.REPLY_URI, "visibility": ["model", "app"]}
    assert tool.annotations.readOnlyHint is True
    assert set(tool.inputSchema["properties"]) == {
        "message_id",
        "draft_id",
        "reply_all",
        "to",
        "cc",
        "subject",
        "body",
        "attachments",
        "from_address",
    }
    assert "required" not in tool.inputSchema
    assert "never sends" in tool.description
    assert "retryable error with partial_reason" in tool.description
    description = tools["read_thread"].description
    assert "partial=true means seed discovery" in description
    assert "shared 12-second lookup/command budget" in description
    assert "fallback folder selection and UIDNEXT discovery" in description
    assert "without timed_out or error" in description
    assert "messages may be empty" in description
    assert "remaining full" in description
    assert "seed lookup and requested" not in description
    # The composer saves through the existing tool, which views may call.
    assert tools["create_draft"].meta is None
