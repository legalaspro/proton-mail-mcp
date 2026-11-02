"""Sieve filter previews, bound tokens and journaled creation; offline doubles only."""

import asyncio
import hashlib
import json
import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from bridge_sim import BridgeSim, make_client
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError
from sieve_fakes import FakeSieveBackend
from test_send import Host

from proton_mail_mcp.action_log import ActionLog, LogTransaction
from proton_mail_mcp.guarded import TokenStore, approval_hash
from proton_mail_mcp.mail import MailError, MailValidationError
from proton_mail_mcp.server import create_mcp
from proton_mail_mcp.sieve import SieveUnavailable, filter_spec, render_script, script_sha256

ACTION = "create_sieve_filter"
GITHUB = [{"field": "from", "contains": "notifications@github.com"}]
NAME = "Private filter name"
FOLDERS = ("Folders/GitHub", "Folders/Reviewed", "Folders/Work", "Folders/Work/Receipts")


@pytest.fixture
def case(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Sieve filter tests must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    simulations = []

    def build(**options):
        sim = BridgeSim(**({"folders": FOLDERS, "labels": ("Labels/News",)} | options))
        simulations.append(sim)
        client = make_client(sim, tmp_path)
        backend = FakeSieveBackend()
        client.sieve_backend = backend
        cleanup = client.cleanup
        return SimpleNamespace(
            sim=sim, client=client, backend=backend, tokens=cleanup.tokens, log=cleanup.backend.log
        )

    built = build()
    built.build = build
    yield built
    for sim in simulations:
        sim.close()
        assert not sim.errors and not sim.violations
        # Filters never change mail: LIST is the only mailbox command.
        commands = {command.split()[0] for *_, command in sim.commands}
        assert commands <= {"CAPABILITY", "STARTTLS", "LOGIN", "LIST", "LOGOUT"}, commands


def preview(case, **options):
    arguments = {"name": NAME, "folder": "Folders/GitHub", "conditions": GITHUB} | options
    return case.client.preview_sieve_filter(**arguments)


def records(case):
    with case.log.transaction(readonly=True) as transaction:
        return transaction.records()


def lists(case):
    return [command for *_, command in case.sim.commands if command.startswith("LIST")]


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_preview_shows_and_binds_the_exact_script_without_changing_anything(case):
    result = preview(case)
    script = render_script(filter_spec(NAME, "Folders/GitHub", GITHUB))
    assert result["script"] == script and result["script_sha256"] == script_sha256(script)
    assert result["fileinto"] == "GitHub" and result["action"] == ACTION
    assert result["conditions"] == GITHUB and result["mark_read"] is False
    assert result["apply"] == {"backend": "fake", "available": True, "reason": None}
    assert result["token"] and result["expires_at"]
    grant = case.tokens.inspect(result["token"], ACTION)
    assert grant.action_hash == result["action_hash"] == approval_hash(ACTION, grant.params, {}, [])
    assert grant.params["script"] == script and grant.params["backend"] == "fake"
    assert grant.messages == [] and grant.selector == {}
    assert not case.log.path.exists() and case.backend.created == []
    assert case.backend.calls == ["filter_names"] and len(lists(case)) == 1


def test_default_backend_previews_the_script_but_refuses_to_apply_it(case):
    case.client.sieve_backend = None
    result = preview(case)
    assert result["apply"]["backend"] == "unavailable" and result["apply"]["available"] is False
    assert "no ManageSieve, Sieve or filter interface" in result["apply"]["reason"]
    assert result["token"] and result["script"]
    with pytest.raises(SieveUnavailable, match="nothing was changed"):
        case.client.sieve_filter_ready()
    with pytest.raises(SieveUnavailable, match="Bridge offers mail clients only IMAP and SMTP"):
        case.client.create_sieve_filter(result["token"])
    assert not case.log.path.exists() and len(lists(case)) == 1
    assert case.tokens.inspect(result["token"], ACTION) and case.backend.calls == []


def test_create_applies_exactly_the_previewed_script_once_after_a_durable_intent(case):
    result = preview(case, mark_read=True)

    def during():
        raw = [json.loads(line) for line in case.log.path.read_text().splitlines()]
        assert raw[0]["action"] == ACTION and raw[0]["status"] == "in_progress"
        assert raw[-1]["action"] == "execute_progress"
        assert raw[-1]["message"]["status"] == "uncertain"
        with pytest.raises(MailValidationError, match="already used"):
            case.tokens.inspect(result["token"], ACTION)

    case.backend.during = during
    created = case.client.create_sieve_filter(result["token"])
    assert case.backend.created == [(NAME, result["script"])]
    assert created == {
        "log_id": created["log_id"],
        "name": NAME,
        "folder": "Folders/GitHub",
        "script_sha256": result["script_sha256"],
        "backend": "fake",
        "created": True,
        "attempted": True,
        "filter_id": "filter-1",
        "partial": False,
        "token_consumed": True,
    }
    [record] = records(case)
    assert record["log_id"] == created["log_id"] and record["status"] == "completed"
    assert record["params"] == {
        "name_sha256": sha256(NAME),
        "folder": "Folders/GitHub",
        "match": "all",
        "fields": ["from:contains"],
        "mark_read": True,
        "script_sha256": result["script_sha256"],
        "backend": "fake",
    }
    assert record["messages"] == [
        {"folder": "Folders/GitHub", "status": "done", "result": "created", "filter_id": "filter-1"}
    ]
    assert record["token_digest"] == sha256(result["token"])
    text = case.log.path.read_text()
    for private in (result["token"], NAME, "notifications@github.com", "fileinto"):
        assert private not in text
    [entry] = case.client.list_actions(token=result["token"])["actions"]
    assert (entry["action"], entry["status"], entry["done"]) == (ACTION, "completed", 1)
    with pytest.raises(MailValidationError, match="already used"):
        case.client.create_sieve_filter(result["token"])
    with pytest.raises(MailValidationError, match="cannot be undone"):
        case.client.undo_action(created["log_id"])
    assert len(case.backend.created) == 1


def test_nested_folder_and_any_match_reach_the_backend_verbatim(case):
    conditions = [{"field": "to", "is": "me@example.test"}, {"field": "subject", "contains": "Rx"}]
    result = preview(case, folder="Folders/Work/Receipts", conditions=conditions, match="any")
    case.client.create_sieve_filter(result["token"])
    [(name, script)] = case.backend.created
    assert name == NAME and script == result["script"]
    assert 'fileinto "Work/Receipts";' in script and "if anyof (" in script
    assert "addflag" not in script


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": "bad\nname"},
        {"folder": "Trash"},
        {"folder": "Labels/News"},
        {"conditions": []},
        {"conditions": [{"field": "subject", "contains": 'x"\ndiscard;'}]},
        {"conditions": [{"field": "from", "contains": "${1}"}]},
        {"match": "most"},
        {"mark_read": "yes"},
    ],
)
def test_invalid_requests_are_refused_before_any_connection(case, arguments):
    with pytest.raises(MailValidationError):
        preview(case, **arguments)
    assert not case.sim.commands and not case.tokens._grants and case.backend.calls == []


def test_missing_view_or_shadowed_folders_refuse_the_preview(case, monkeypatch):
    with pytest.raises(MailValidationError, match="existing real folder"):
        preview(case, folder="Folders/Missing")
    handle = case.sim.handle

    def parent_view(tag, op, uid_op, words, session, command):
        response = handle(tag, op, uid_op, words, session, command)
        if op == "LIST":
            response = b'* LIST (\\Noselect) "/" "Folders/Parent"\r\n' + response
        return response

    monkeypatch.setattr(case.sim, "handle", parent_view)
    with pytest.raises(MailValidationError, match="existing real folder"):
        preview(case, folder="Folders/Parent")
    for flag in (b"\\Sent", b"\\Drafts", b"\\Junk", b"\\Trash", b"\\All", b"\\flagged"):

        def special_use(tag, op, uid_op, words, session, command, flag=flag):
            response = handle(tag, op, uid_op, words, session, command)
            if op == "LIST":
                response = b"* LIST (" + flag + b') "/" "Folders/Renamed"\r\n' + response
            return response

        monkeypatch.setattr(case.sim, "handle", special_use)
        with pytest.raises(MailValidationError, match="existing real folder"):
            preview(case, folder="Folders/Renamed")
    labelled = case.build(folders=("Folders/News",), labels=("Labels/News",))
    with pytest.raises(MailValidationError, match="could not tell them apart"):
        preview(labelled, folder="Folders/News")
    variants = case.build(folders=("Folders/GitHub", "Folders/github"), labels=())
    with pytest.raises(MailValidationError, match="could not tell them apart"):
        preview(variants)
    for built in (case, labelled, variants):
        assert not built.tokens._grants and not built.log.path.exists()


def test_existing_filter_names_are_never_replaced(case):
    for existing in (NAME, NAME.upper(), NAME.casefold()):
        case.backend.names = {existing}
        with pytest.raises(MailValidationError, match="already exists; existing filters are never"):
            preview(case)
    assert not case.tokens._grants
    case.backend.names = set()
    token = preview(case)["token"]
    case.backend.names = {NAME}
    with pytest.raises(MailValidationError, match="already exists"):
        case.client.create_sieve_filter(token)
    assert case.backend.created == [] and records(case) == []
    assert case.tokens.inspect(token, ACTION)


def test_folder_removed_after_preview_refuses_and_keeps_the_token(case):
    token = preview(case)["token"]
    case.sim.custom_folders.remove("Folders/GitHub")
    with pytest.raises(MailValidationError, match="existing real folder"):
        case.client.create_sieve_filter(token)
    assert case.backend.created == [] and records(case) == []
    assert case.tokens.inspect(token, ACTION)
    case.sim.custom_folders.append("Folders/GitHub")
    assert case.client.create_sieve_filter(token)["created"] is True


def test_backend_unavailable_or_replaced_after_preview_refuses(case):
    token = preview(case)["token"]
    case.backend.available = False
    with pytest.raises(SieveUnavailable, match="offline fake; nothing was changed"):
        case.client.create_sieve_filter(token)
    other = FakeSieveBackend()
    other.name = "other"
    case.client.sieve_backend = other
    with pytest.raises(MailValidationError, match="backend changed after preview"):
        case.client.create_sieve_filter(token)
    assert case.backend.created == other.created == [] and not case.log.path.exists()
    assert case.tokens.inspect(token, ACTION)


def stored(case):
    [grant] = case.tokens._grants.values()
    return grant


def test_a_changed_stored_script_is_refused_by_its_binding(case):
    token = preview(case)["token"]
    grant = stored(case)
    grant.params["script"] = grant.params["script"].replace('fileinto "GitHub";', "discard;")
    with pytest.raises(MailValidationError, match="does not match its approved action set"):
        case.client.create_sieve_filter(token)
    assert case.backend.created == [] and not case.log.path.exists()


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda params: params.update(script=params["script"] + "# extra\n"), "its preview"),
        (
            lambda params: params.update(
                script=params["script"] + "# extra\n",
                script_sha256=script_sha256(params["script"] + "# extra\n"),
            ),
            "its preview",
        ),
        (
            lambda params: params.update(
                script=params["script"].replace('fileinto "GitHub";', "discard;")
            ),
            "reviewed filter",
        ),
        (
            lambda params: params.update(conditions=[{"field": "subject", "contains": "x"}]),
            "reviewed filter",
        ),
        (lambda params: params.update(mark_read=True), "reviewed filter"),
        (lambda params: params.update(folder="Folders/Reviewed"), "reviewed filter"),
        (lambda params: params.update(script_sha256="0" * 64), "its preview"),
        (lambda params: params.update(conditions="from"), "conditions"),
        (lambda params: params.pop("backend"), "does not match Sieve filter creation"),
        (lambda params: params.update(extra=True), "does not match Sieve filter creation"),
    ],
    ids=[
        "comment",
        "comment-and-digest",
        "discard",
        "conditions",
        "mark_read",
        "folder",
        "digest",
        "malformed",
        "missing-key",
        "extra-key",
    ],
)
def test_forged_but_self_consistent_grants_still_apply_only_the_rendered_script(
    case, change, message
):
    token = preview(case)["token"]
    grant = stored(case)
    change(grant.params)
    grant.binding = approval_hash(ACTION, grant.params, grant.selector, grant.messages)
    with pytest.raises(MailValidationError, match=message):
        case.client.create_sieve_filter(token)
    assert case.backend.created == [] and not case.log.path.exists()


def test_tokens_of_other_actions_and_expired_tokens_cannot_create(case):
    folder_token = case.client.preview_create_folder("Folders/New")["token"]
    with pytest.raises(MailValidationError, match="does not match the requested action"):
        case.client.create_sieve_filter(folder_token)
    token = preview(case)["token"]
    with pytest.raises(MailValidationError, match="does not match the requested action"):
        case.client.create_folder(token)
    for value in (None, "", "forged", 7):
        with pytest.raises(MailValidationError):
            case.client.create_sieve_filter(value)
    now = [0.0]
    case.client.cleanup.tokens = TokenStore(ttl=1, clock=lambda: now[0])
    expiring = preview(case)["token"]
    now[0] = 2.0
    with pytest.raises(MailValidationError, match="expired"):
        case.client.create_sieve_filter(expiring)
    assert case.backend.created == [] and not case.log.path.exists()


def test_filter_grants_bind_no_messages_and_need_their_exact_keys():
    store = TokenStore()
    with pytest.raises(MailValidationError, match="cannot authorize message changes"):
        store.issue(ACTION, {}, {}, [{"message_id": "<a@example.test>"}])
    assert store.issue(ACTION, {"name": "x"}, {}, [])[0]


def test_unwritable_journal_refuses_before_any_apply_and_keeps_the_token(case, tmp_path):
    token = preview(case)["token"]
    blocked = tmp_path / "blocked-log"
    blocked.mkdir()
    case.client.cleanup.backend.log = ActionLog(blocked)
    with pytest.raises(MailError, match="action log is unavailable"):
        case.client.create_sieve_filter(token)
    assert case.backend.created == [] and case.tokens.inspect(token, ACTION)


def test_failed_intent_append_refuses_before_any_apply(case, monkeypatch):
    token = preview(case)["token"]

    def unavailable(*args, **kwargs):
        raise MailError("Synthetic unavailable journal")

    monkeypatch.setattr(LogTransaction, "append", unavailable)
    with pytest.raises(MailError):
        case.client.create_sieve_filter(token)
    assert case.backend.created == [] and case.tokens.inspect(token, ACTION)


def test_backend_failure_is_journaled_as_uncertain_without_its_text(case):
    token = preview(case)["token"]
    case.backend.failure = RuntimeError("private backend detail")
    result = case.client.create_sieve_filter(token)
    assert result["partial"] and result["token_consumed"] and not result["created"]
    assert result["attempted"] is True
    assert "outcome is uncertain; inspect the action log and the Proton filter" in result["error"]
    assert records(case)[0]["messages"][0]["status"] == "uncertain"
    assert "private backend detail" not in str(result) + case.log.path.read_text()
    assert case.backend.calls == ["filter_names", "filter_names", "create_filter"]


@pytest.mark.parametrize("failure", ["intent", "outcome"])
def test_journal_progress_failure_never_hides_the_apply_outcome(case, monkeypatch, failure):
    token = preview(case)["token"]
    append = LogTransaction.append

    def fail_progress(transaction, record):
        if record["action"] == "execute_progress" and record["message"]["status"] == (
            "uncertain" if failure == "intent" else "done"
        ):
            raise MailError("Synthetic journal failure")
        return append(transaction, record)

    monkeypatch.setattr(LogTransaction, "append", fail_progress)
    result = case.client.create_sieve_filter(token)
    assert result["partial"] and result["token_consumed"]
    assert result["created"] is result["attempted"] is (failure == "outcome")
    assert len(case.backend.created) == (1 if failure == "outcome" else 0)
    assert records(case)[0]["messages"][0]["status"] == (
        "uncertain" if failure == "outcome" else "not_attempted"
    )
    assert result["error"].startswith(
        "Sieve filter was created, but its outcome could not be journaled"
        if failure == "outcome"
        else "Sieve filter creation stopped before the apply backend was called, so no filter"
    )


def test_concurrent_cleanup_refuses_creation(case):
    token = preview(case)["token"]
    lock = case.client.cleanup._mutation_lock
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(MailValidationError, match="Another cleanup operation is running"):
            case.client.create_sieve_filter(token)
    finally:
        lock.release()
    assert case.backend.created == [] and case.tokens.inspect(token, ACTION)


def call(mcp, tool, arguments, context=None):
    return asyncio.run(mcp._tool_manager.call_tool(tool, arguments, context=context))


def audits(path):
    with ActionLog(path).transaction(readonly=True) as transaction:
        rows = [row for row in transaction.records() if row.get("action") == "audit"]
    calls = {}
    for row in rows:
        calls.setdefault(row["audit_id"], []).append(row)
    return list(calls.values())


ARGUMENTS = {"name": NAME, "folder": "Folders/GitHub", "conditions": GITHUB}


def test_tools_are_listed_with_their_annotations_and_exact_arguments(case):
    mcp = create_mcp(case.client.settings, case.client)
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    preview_tool, create_tool = tools["preview_sieve_filter"], tools[ACTION]
    assert preview_tool.annotations.readOnlyHint is True
    assert preview_tool.annotations.destructiveHint is False
    assert set(preview_tool.parameters["properties"]) == {
        "name",
        "folder",
        "conditions",
        "match",
        "mark_read",
    }
    assert set(preview_tool.parameters["required"]) == {"name", "folder", "conditions"}
    assert create_tool.annotations.readOnlyHint is False
    assert create_tool.annotations.destructiveHint is True
    assert create_tool.annotations.openWorldHint is False
    assert set(create_tool.parameters["properties"]) == {"token"}
    assert create_tool.parameters["additionalProperties"] is False
    assert "only call after explicit approval of its preview" in " ".join(
        create_tool.description.split()
    )
    for extra in ({"script": "discard;"}, {"name": "Other"}, {"confirm": True}):
        with pytest.raises(ToolError):
            call(mcp, ACTION, {"token": "A" * 43} | extra, Host())
    assert case.backend.calls == []


def test_default_server_refuses_before_any_form_and_audits_the_refusal(case):
    case.client.sieve_backend = None
    mcp = create_mcp(case.client.settings, case.client)
    previewed = call(mcp, "preview_sieve_filter", ARGUMENTS)
    assert previewed["apply"]["available"] is False and previewed["token"]
    host = Host()
    with pytest.raises(ToolError, match="Sieve filters cannot be applied: Proton Mail Bridge"):
        call(mcp, ACTION, {"token": previewed["token"]}, host)
    assert host.prompts == [] and case.backend.calls == []
    [(started, refused)] = audits(case.log.path)
    assert (started["outcome"], refused["outcome"]) == ("started", "refused")
    assert refused["tool"] == ACTION and "no other apply path" in refused["reason"]
    assert refused["action_hash"] == previewed["action_hash"]
    assert refused["token_id"] == sha256(previewed["token"])[:16]
    with case.log.transaction(readonly=True) as transaction:
        assert [row["action"] for row in transaction.records()] == ["audit", "audit"]
    # Still live and unconfirmed: the refusal neither consumed nor retired it.
    assert case.tokens.describe(previewed["token"], ACTION).confirmed is False
    assert case.client.list_actions(action=ACTION)["audit"][0]["outcome"] == "refused"


def test_host_form_shows_the_exact_script_before_the_single_apply(case):
    mcp = create_mcp(case.client.settings, case.client)
    previewed = call(mcp, "preview_sieve_filter", ARGUMENTS | {"mark_read": True})
    declined = Host("decline")
    with pytest.raises(ToolError, match="User confirmation was not accepted"):
        call(mcp, ACTION, {"token": previewed["token"]}, declined)
    assert case.backend.created == []
    previewed = call(mcp, "preview_sieve_filter", ARGUMENTS | {"mark_read": True})
    host = Host()
    result = call(mcp, ACTION, {"token": previewed["token"]}, host)
    assert result["created"] is True and case.backend.created == [(NAME, previewed["script"])]
    [prompt] = host.prompts
    sentence, details = prompt.split("\n", 1)
    assert previewed["action_hash"][:16] in sentence and '"Folders/GitHub"' in sentence
    assert "marking them read" in sentence
    shown = json.loads(details)
    assert shown["script"] == previewed["script"] and shown["mark_read"] is True
    assert shown["script_sha256"] == previewed["script_sha256"]
    assert shown["conditions"] == GITHUB and shown["backend"] == "fake"
    assert previewed["token"] not in prompt
    started, finished = audits(case.log.path)[-1]
    assert finished["outcome"] == "completed"
    assert finished["script_sha256"] == previewed["script_sha256"]
    assert finished["counts"] == {"approved": 1, "done": 1, "uncertain": 0, "remaining": 0}
    with pytest.raises(ToolError, match="already used"):
        call(mcp, ACTION, {"token": previewed["token"]}, Host())


def test_backend_failure_through_the_server_is_audited_as_partial(case):
    mcp = create_mcp(replace(case.client.settings, confirm_mutations=False), case.client)
    case.backend.failure = RuntimeError("private backend detail")
    previewed = call(mcp, "preview_sieve_filter", ARGUMENTS)
    result = call(mcp, ACTION, {"token": previewed["token"]}, Host(support=False))
    assert result["partial"] is True and result["created"] is False
    assert result["attempted"] is True
    finished = audits(case.log.path)[-1][-1]
    assert finished["outcome"] == "partial"
    assert finished["counts"] == {"approved": 1, "done": 0, "uncertain": 1, "remaining": 0}
    assert "private backend detail" not in Path(case.log.path).read_text()


# The audited outcome of one approval when a journal append fails: before the
# backend call (its intent record, then its uncertain progress line) or after the
# backend created the filter (its done line, then the call's execute_result).
JOURNAL_FAILURES = {
    ACTION: ("failed", None),
    "uncertain": ("partial", {"approved": 1, "done": 0, "uncertain": 0, "remaining": 1}),
    "done": ("partial", {"approved": 1, "done": 1, "uncertain": 0, "remaining": 0}),
    "execute_result": ("partial", {"approved": 1, "done": 1, "uncertain": 0, "remaining": 0}),
}


@pytest.mark.parametrize("failure", list(JOURNAL_FAILURES))
def test_journal_failures_around_the_apply_are_audited_once(case, monkeypatch, failure):
    mcp = create_mcp(case.client.settings, case.client)
    previewed = call(mcp, "preview_sieve_filter", ARGUMENTS)
    append = LogTransaction.append

    def fail_once(transaction, record):
        stage = record["action"]
        if stage == "execute_progress":
            stage = record["message"]["status"]
        if stage == failure:
            raise MailError("Synthetic journal failure")
        return append(transaction, record)

    monkeypatch.setattr(LogTransaction, "append", fail_once)
    host = Host()
    outcome, counts = JOURNAL_FAILURES[failure]
    if counts is None:
        with pytest.raises(ToolError, match="Mailbox operation failed"):
            call(mcp, ACTION, {"token": previewed["token"]}, host)
        # Nothing journaled the attempt, so the token was never claimed.
        assert case.tokens.inspect(previewed["token"], ACTION)
    else:
        result = call(mcp, ACTION, {"token": previewed["token"]}, host)
        assert result["partial"] is True and result["token_consumed"] is True
        assert result["created"] is (counts["done"] == 1)
        assert result["attempted"] is (counts["remaining"] == 0)
    assert len(host.prompts) == 1
    applied = [(NAME, previewed["script"])] if counts and counts["done"] else []
    assert case.backend.created == applied
    assert case.backend.calls.count("create_filter") == len(applied)
    started, finished = audits(case.log.path)[-1]
    assert (started["outcome"], finished["outcome"]) == ("started", outcome)
    assert finished["action_hash"] == previewed["action_hash"]
    assert finished.get("counts") == counts
    if counts is not None:
        # One filter counts once, as done, uncertain or remaining.
        assert counts["done"] + counts["uncertain"] + counts["remaining"] == 1
        assert finished["token_consumed"] is True
        assert finished["script_sha256"] == previewed["script_sha256"]


def test_deadline_expiring_just_before_the_apply_is_audited_as_remaining(case, monkeypatch):
    mcp = create_mcp(case.client.settings, case.client)
    previewed = call(mcp, "preview_sieve_filter", ARGUMENTS)
    deadline = case.client._deadline(execute=True)
    append = LogTransaction.append

    def expire_after_uncertain(transaction, record):
        appended = append(transaction, record)
        if record["action"] == "execute_progress" and record["message"]["status"] == "uncertain":
            deadline.end = 0  # The uncertain line is durable; the budget then runs out.
        return appended

    monkeypatch.setattr(case.client, "_deadline", lambda **_: deadline)
    monkeypatch.setattr(LogTransaction, "append", expire_after_uncertain)
    result = call(mcp, ACTION, {"token": previewed["token"]}, Host())
    # Only the preview's and the create's name checks reached the backend.
    assert case.backend.calls == ["filter_names", "filter_names"] and case.backend.created == []
    assert result["attempted"] is False and result["created"] is False
    assert result["partial"] is True and result["token_consumed"] is True
    assert result["error"].startswith("Sieve filter creation stopped before the apply backend")
    # The journal keeps its write-ahead uncertain line; the call itself never happened.
    [record] = [row for row in records(case) if row["action"] == ACTION]
    assert record["messages"][0]["status"] == "uncertain"
    started, finished = audits(case.log.path)[-1]
    assert (started["outcome"], finished["outcome"]) == ("started", "partial")
    assert finished["counts"] == {"approved": 1, "done": 0, "uncertain": 0, "remaining": 1}


@pytest.mark.parametrize("value", ["yes", "true", "1", 1, 0, None, [True]])
def test_mark_read_must_be_a_json_boolean_at_the_mcp_boundary(case, value):
    mcp = create_mcp(case.client.settings, case.client)
    tool = mcp._tool_manager.get_tool("preview_sieve_filter")
    metadata = tool.fn_metadata
    # The registered argument model, after the SDK's own JSON pre-parsing.
    with pytest.raises(ValidationError):
        metadata.arg_model.model_validate(metadata.pre_parse_json(ARGUMENTS | {"mark_read": value}))
    with pytest.raises(ToolError):
        call(mcp, "preview_sieve_filter", ARGUMENTS | {"mark_read": value})
    for flag in (True, False):
        assert metadata.arg_model.model_validate(ARGUMENTS | {"mark_read": flag}).mark_read is flag
    assert tool.parameters["properties"]["mark_read"]["type"] == "boolean"
    assert not case.sim.commands and not case.tokens._grants and case.backend.calls == []
