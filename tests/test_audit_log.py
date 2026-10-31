"""Every write tool call is audited in the action log; offline doubles only."""

import asyncio
import hashlib
import json
import re
import sys
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from merged_tools import tool_call
from test_approval_binding import bridge as bridge  # noqa: F401
from test_host_confirmation import TOOLS
from test_host_confirmation import case as case  # noqa: F401
from test_mail import FakeImap
from test_mail import client as fake_client
from test_send import BODY, DRAFT_ID, PASSWORD, SUBJECT, Host, local_certificate, records  # noqa: F401
from test_send import rig as _send_rig
from test_unsubscribe import URL

from proton_mail_mcp import action_log, rules_timer
from proton_mail_mcp.action_log import (
    WRITE_TOOLS,
    ActionLog,
    LogTransaction,
    audit_record,
    client_context,
    new_record,
    token_id,
)
from proton_mail_mcp.config import Settings
from proton_mail_mcp.guarded import TokenStore
from proton_mail_mcp.mail import MailError, MailValidationError, draft_audit
from proton_mail_mcp.server import _audit_result, create_mcp

rig = _send_rig
CLIENT = {"name": "offline-audit", "version": "9", "transport": "http"}
DRAFT = {"to": "Reader <reader@example.test>", "cc": ["copy@cc.example.test"]}
DRAFT |= {"subject": "Private audit subject", "body": "Private audit body text"}


def host(**options):
    """A host double whose initialized session reports its clientInfo."""
    double = Host(**options)
    double.session.client_params.clientInfo = SimpleNamespace(name="offline-audit", version="9")
    return double


def call(mcp, action, arguments, context=None):
    tool, arguments = tool_call(action, arguments)
    return asyncio.run(mcp._tool_manager.call_tool(tool, arguments, context=context))


def audits(path):
    """Audit records grouped per call, oldest first."""
    with ActionLog(path).transaction(readonly=True) as transaction:
        records = [row for row in transaction.records() if row.get("action") == "audit"]
    calls = {}
    for record in records:
        calls.setdefault(record["audit_id"], []).append(record)
    return list(calls.values())


def log_path(built):
    return Path(built.client.settings.action_log_path)


def test_audit_record_shape_and_token_ids_never_carry_the_token():
    token = "A" * 43
    with client_context(CLIENT):
        record = audit_record("archive", "started", audit_id="a" * 32, token=token, counts=None)
    assert record == {
        "action": "audit",
        "audit_id": "a" * 32,
        "timestamp": record["timestamp"],
        "client": CLIENT,
        "tool": "archive",
        "outcome": "started",
        "token_id": hashlib.sha256(token.encode()).hexdigest()[:16],
        "counts": None,
    }
    assert record["timestamp"].endswith("+00:00")
    for value in (None, "", "short", "A" * 44, "A" * 42 + "!", 7, ["A" * 43], PASSWORD):
        assert token_id(value) is None
    assert token not in json.dumps(record)


def test_client_field_is_on_every_top_level_record_and_resets():
    assert new_record("archive", {})["client"] is None
    with client_context(CLIENT):
        assert new_record("archive", {})["client"] == CLIENT
        with client_context(None):
            assert new_record("archive", {})["client"] is None
        assert new_record("archive", {})["client"] == CLIENT
    assert new_record("archive", {})["client"] is None


def test_draft_audit_describes_recipients_without_content(monkeypatch):
    details = draft_audit(**DRAFT, in_reply_to="<parent@example.test>")
    assert details == {
        "recipient_domains": ["cc.example.test", "example.test"],
        "recipient_count": 2,
        "subject_sha256": hashlib.sha256(DRAFT["subject"].encode()).hexdigest(),
        "body_sha256": hashlib.sha256(DRAFT["body"].encode()).hexdigest(),
    }
    # Lists are joined into one header, as create_draft writes them.
    listed = draft_audit(to=["A <a@Alpha.test>", "a@alpha.test, b@beta.test"])
    assert (listed["recipient_count"], listed["recipient_domains"]) == (
        3,
        ["alpha.test", "beta.test"],
    )
    unknown = {"recipient_domains": None, "recipient_count": None}
    assert draft_audit(to=7, cc={"x": 1}, subject=None, body=b"x") == unknown | {
        "subject_sha256": None,
        "body_sha256": None,
    }
    # Refused lists, including undecoded JSON text, have no count or domains.
    for to in (None, "", ["a@alpha.test", 7], '["a@alpha.test"]', "@b.test", "a@b..test"):
        assert draft_audit(to=to, cc="c@gamma.test").items() >= unknown.items()
    assert draft_audit(to="a@alpha.test", cc="not an address").items() >= unknown.items()
    assert draft_audit(to="(" * 500 + "a@b" + ")" * 500).items() >= unknown.items()

    def exhausted(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("proton_mail_mcp.mail.getaddresses", exhausted)
    assert draft_audit(**DRAFT).items() >= unknown.items()


def test_every_write_tool_is_audited_and_filterable(rig):
    settings = replace(rig.settings, action_log_path=str(rig.log.path))
    mcp = create_mcp(settings, mail_client=SimpleNamespace())
    write = {
        tool.name
        for tool in mcp._tool_manager.list_tools()
        if tool.annotations is not None and tool.annotations.readOnlyHint is False
    }
    assert write == WRITE_TOOLS


@pytest.mark.parametrize(
    "tool,arguments,result,outcome",
    [
        (
            "reindex_execute",
            {"token": "A" * 43},
            {"state": "running", "run_id": "b" * 32, "log_id": "c" * 32},
            "completed",
        ),
        (
            "reindex_execute",
            {"token": "A" * 43},
            {"error": "already running", "state": "running", "run_id": "b" * 32},
            "refused",
        ),
        (
            "reindex_cancel",
            {"run_id": "b" * 32},
            {"state": "running", "run_id": "b" * 32, "log_id": "d" * 32},
            "completed",
        ),
    ],
)
def test_local_reindex_calls_are_audited_without_cleanup_confirmation(
    tmp_path, tool, arguments, result, outcome
):
    settings = Settings(
        token="reindex-audit-token-" + "x" * 32,
        bridge_user="reader@example.test",
        action_log_path=str(tmp_path / "actions.jsonl"),
        confirm_mutations=True,
    )
    calls = []

    def action(**kwargs):
        calls.append(kwargs)
        return result

    mcp = create_mcp(settings, SimpleNamespace(**{tool: action}))
    context = host(action="decline")
    assert call(mcp, tool, arguments, context) == result
    assert calls == [arguments] and context.prompts == []
    [records] = audits(tmp_path / "actions.jsonl")
    assert [row["outcome"] for row in records] == ["started", outcome]
    assert records[-1]["run_id"] == result["run_id"]
    assert records[-1]["reindex_state"] == result["state"]
    assert records[-1]["log_ids"] == ([result["log_id"]] if "log_id" in result else [])
    assert "A" * 43 not in (tmp_path / "actions.jsonl").read_text()
    [entry] = ActionLog(settings.action_log_path).list_actions(action=tool)["audit"]
    assert entry["run_id"] == result["run_id"] and entry["outcome"] == outcome


def test_reindex_action_lookup_exposes_only_safe_non_undoable_metadata(tmp_path):
    log = ActionLog(tmp_path / "actions.jsonl")
    token = "A" * 43
    digest = hashlib.sha256(token.encode()).hexdigest()
    with client_context(CLIENT), log.transaction() as transaction:
        for action in ("reindex_preview", "reindex_execute", "reindex_cancel", "reindex_result"):
            transaction.append(
                new_record(action, {"private": "must-not-return"})
                | {
                    "run_id": "b" * 32,
                    "state": "succeeded" if action == "reindex_result" else "running",
                    "all_mail_count": 100,
                    "old_count": 80,
                    "new_count": 100,
                    "swap_outcome": "swapped",
                    "swap_reason": None,
                    **(
                        {"token_digest": digest}
                        if action in {"reindex_preview", "reindex_execute"}
                        else {}
                    ),
                    "last_error": "private diagnostic",
                }
            )
    found = log.list_actions(token=token, client=CLIENT["name"])["actions"]
    assert [row["action"] for row in found] == [
        "reindex_result",
        "reindex_cancel",
        "reindex_execute",
        "reindex_preview",
    ]
    assert all(
        row["undoable"] is False and re.fullmatch(r"[0-9a-f]{32}", row["log_id"]) for row in found
    )
    assert all(row["run_id"] == "b" * 32 for row in found)
    assert [row["action"] for row in log.list_actions(action="reindex_execute")["actions"]] == [
        "reindex_result",
        "reindex_execute",
    ]
    assert len(log.list_actions(action="reindex_preview")["actions"]) == 1
    visible = json.dumps(found)
    assert token not in visible and digest not in visible
    assert "must-not-return" not in visible and "private diagnostic" not in visible


@pytest.mark.parametrize("tool", TOOLS)
def test_token_tools_record_started_and_outcome_with_counts(case, tool):  # noqa: F811
    built = case(tool)
    token = built.preview["token"]
    result = call(built.mcp, tool, {"token": token}, host())
    assert built.changed()
    [(started, finished)] = audits(log_path(built))
    assert started["outcome"] == "started" and finished["outcome"] == "completed"
    for record in (started, finished):
        assert record["tool"] == tool and record["client"] == CLIENT
        assert record["token_id"] == hashlib.sha256(token.encode()).hexdigest()[:16]
    assert finished["action_hash"] == built.preview["action_hash"]
    assert finished["counts"]["done"] == finished["counts"]["approved"] >= 1
    assert finished["counts"]["uncertain"] == finished["counts"]["remaining"] == 0
    assert finished["token_consumed"] is True
    # Batch tools always return a list; single actions return exactly one string.
    single = tool in {"unsubscribe", "create_folder", "create_sieve_filter", "add_rule"}
    assert ("log_id" in result, "log_ids" in result) == (single, not single)
    expected_ids = [result["log_id"]] if single else result["log_ids"]
    assert isinstance(expected_ids, list) and expected_ids
    assert all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) for value in expected_ids
    )
    logged_preview = tool in {"unsubscribe", "move_to_trash", "archive", "move_to_folder"}
    assert ("log_id" in built.preview) is logged_preview
    assert "log_ids" not in built.preview
    found = call(built.mcp, "list_actions", {"token": token}, host())
    preview_ids = {built.preview["log_id"]} if logged_preview else set()
    assert {row["log_id"] for row in found["actions"]} == set(expected_ids) | preview_ids
    assert all("log_ids" not in row for row in found["actions"])
    for run in found.get("runs", []):
        assert "log_id" not in run and run["log_ids"] == expected_ids
    assert finished["log_ids"] == expected_ids
    text = log_path(built).read_text()
    assert token not in text and URL not in text and "synthetic-query-secret" not in text


@pytest.mark.parametrize("tool", TOOLS)
def test_refused_tokens_are_audited_with_their_reason(case, tool):  # noqa: F811
    built = case(tool)
    with pytest.raises(ToolError):
        call(built.mcp, tool, {"token": "not-a-preview-token"}, host())
    with pytest.raises(ToolError):
        call(built.mcp, tool, {"token": built.preview["token"]}, host(action="decline"))
    with pytest.raises(ToolError):
        call(built.mcp, tool, {"token": built.preview["token"]}, host())
    invalid, declined, replayed = [records[-1] for records in audits(log_path(built))]
    assert invalid["outcome"] == "refused" and invalid["token_id"] is None
    assert "already used" in invalid["reason"] and invalid["action_hash"] is None
    assert declined["outcome"] == "confirmation_declined"
    assert declined["action_hash"] == built.preview["action_hash"]
    assert replayed["outcome"] == "refused" and "already used" in replayed["reason"]
    assert not built.changed()


def test_invalid_arguments_and_backend_failures_are_audited(case):  # noqa: F811
    built = case("archive")
    with pytest.raises(ToolError):
        call(built.mcp, "archive", {"token": built.preview["token"], "confirm": True}, host())
    built.client._session = lambda **kwargs: (_ for _ in ()).throw(RuntimeError(PASSWORD))
    with pytest.raises(ToolError) as error:
        call(built.mcp, "archive", {"token": built.preview["token"]}, host())
    assert PASSWORD not in str(error.value)
    invalid, failed = [records for records in audits(log_path(built))]
    assert [row["outcome"] for row in invalid] == ["started", "invalid_arguments"]
    assert [row["outcome"] for row in failed] == ["started", "failed"]
    assert failed[-1]["action_hash"] == built.preview["action_hash"]
    assert PASSWORD not in log_path(built).read_text()


def draft_server(tmp_path):
    """A server whose real MailClient saves drafts to a fake IMAP server."""
    settings = Settings(
        token="audit-draft-token-" + "x" * 32,
        bridge_user="reader@example.com",
        action_log_path=str(tmp_path / "actions.jsonl"),
        confirm_mutations=False,
    )
    fake = FakeImap({"INBOX": {}, "Drafts": {}})
    return create_mcp(settings, fake_client(settings, fake)), fake


def saved_recipients(fake):
    """Every To and Cc address of the draft in the APPEND payload."""
    draft = BytesParser(policy=policy.default).parsebytes(fake.appended[-1][3])
    return [address for name in ("To", "Cc") if draft[name] for address in draft[name].addresses]


def test_create_draft_and_undo_are_audited_without_content(tmp_path):
    mcp, fake = draft_server(tmp_path)
    message_id = call(mcp, "create_draft", DRAFT, host())
    with pytest.raises(ToolError):
        call(mcp, "undo_action", {"log_id": "0" * 32}, host())
    draft, undo = audits(tmp_path / "actions.jsonl")
    assert [row["outcome"] for row in draft] == ["started", "completed"]
    for record in draft:
        assert record["recipient_domains"] == ["cc.example.test", "example.test"]
        assert record["recipient_count"] == 2 and record["token_id"] is None
        assert record["body_sha256"] == hashlib.sha256(DRAFT["body"].encode()).hexdigest()
    assert draft[-1]["message_id"] == message_id and fake.appended
    assert draft[-1]["counts"] == {"approved": 1, "done": 1}
    assert [row["outcome"] for row in undo] == ["started", "refused"]
    assert undo[-1]["tool"] == "undo_action"
    text = (tmp_path / "actions.jsonl").read_text()
    for secret in ("reader@example.test", "copy@cc.example.test", "Private audit"):
        assert secret not in text


@pytest.mark.parametrize(
    "recipients,count,domains",
    [
        # The SDK decodes JSON-encoded lists for these str | list[str] fields.
        (
            {
                "to": json.dumps(["Ann Example <ann@alpha.test>", "Bob Example <bob@beta.test>"]),
                "cc": json.dumps(["carol@gamma.test"]),
            },
            3,
            ["alpha.test", "beta.test", "gamma.test"],
        ),
        (
            {
                "to": json.dumps(["dan@Delta.TEST"]),
                "cc": "Eve Example <eve@alpha.test>, frank@x.test",
            },
            3,
            ["alpha.test", "delta.test", "x.test"],
        ),
        ({"to": ["Ann Example <ann@alpha.test>", "ann@alpha.test"]}, 2, ["alpha.test"]),
    ],
    ids=["encoded-to-and-cc", "encoded-to-plain-cc", "plain-list"],
)
def test_draft_audit_describes_the_recipients_the_draft_was_saved_with(
    tmp_path, recipients, count, domains
):
    mcp, fake = draft_server(tmp_path)
    call(mcp, "create_draft", recipients | {"subject": "Private s", "body": "Private b"}, host())
    saved = saved_recipients(fake)
    assert len(fake.appended) == 1 and len(saved) == count
    assert sorted({address.domain.lower() for address in saved}) == domains
    [records] = audits(tmp_path / "actions.jsonl")
    assert [row["outcome"] for row in records] == ["started", "completed"]
    for record in records:
        assert (record["recipient_count"], record["recipient_domains"]) == (count, domains)
    text = (tmp_path / "actions.jsonl").read_text()
    for address in saved:
        assert address.addr_spec not in text
        assert not address.display_name or address.display_name not in text
    assert "Private" not in text


@pytest.mark.parametrize(
    "to",
    [
        # Deep enough to exhaust the recursive address parser.
        "(" * 500 + "a@b" + ")" * 500,
        "@b.test",
        "a@b..test",
    ],
    ids=["nested-comments", "empty-local-part", "empty-domain-label"],
)
def test_malformed_recipients_are_audited_as_refused_attempts(tmp_path, to):
    mcp, fake = draft_server(tmp_path)
    with pytest.raises(ToolError, match="Validation error: Recipients must be valid email"):
        call(mcp, "create_draft", {"to": to, "subject": "Example", "body": "Text"}, host())
    assert fake.calls == [] and fake.appended == []
    [records] = audits(tmp_path / "actions.jsonl")
    assert [row["outcome"] for row in records] == ["started", "refused"]
    assert records[-1]["reason"] == "Recipients must be valid email addresses"
    for record in records:
        assert record["recipient_count"] is None and record["recipient_domains"] is None
        assert record["subject_sha256"] == hashlib.sha256(b"Example").hexdigest()


def test_recipient_parser_failures_never_skip_the_started_record(tmp_path, monkeypatch):
    def exhausted(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("proton_mail_mcp.mail.getaddresses", exhausted)
    mcp, fake = draft_server(tmp_path)
    with pytest.raises(ToolError, match="Recipients must be valid email addresses"):
        call(mcp, "create_draft", DRAFT, host())
    assert fake.calls == []
    [records] = audits(tmp_path / "actions.jsonl")
    assert [row["outcome"] for row in records] == ["started", "refused"]
    assert all(record["recipient_count"] is None for record in records)


def test_undecodable_recipient_json_is_audited_before_the_sdk_refuses_it(tmp_path):
    mcp, fake = draft_server(tmp_path)
    nested = "[" * 5000 + "]" * 5000  # Exhausts the SDK's JSON decoding.
    with pytest.raises(ToolError):
        call(mcp, "create_draft", {"to": nested, "subject": "s", "body": "b"}, host())
    assert fake.calls == []
    [records] = audits(tmp_path / "actions.jsonl")
    assert [row["outcome"] for row in records] == ["started", "invalid_arguments"]
    assert all(record["recipient_count"] is None for record in records)


def test_rejected_draft_arguments_are_described_after_sdk_decoding(tmp_path):
    mcp, fake = draft_server(tmp_path)
    arguments = {"to": json.dumps(["ann@alpha.test"]), "subject": "s", "body": "b"}
    with pytest.raises(ToolError):
        call(mcp, "create_draft", arguments | {"bcc": "hidden@beta.test"}, host())
    assert fake.calls == []
    [records] = audits(tmp_path / "actions.jsonl")
    assert [row["outcome"] for row in records] == ["started", "invalid_arguments"]
    for record in records:
        assert (record["recipient_count"], record["recipient_domains"]) == (1, ["alpha.test"])


def test_draft_outcome_describes_the_arguments_create_draft_received(tmp_path, monkeypatch):
    mcp, fake = draft_server(tmp_path)
    tool = mcp._tool_manager.get_tool("create_draft")
    original = tool.fn

    async def renormalized(**arguments):
        # Stands in for SDK argument handling the audit wrapper cannot foresee.
        return await original(**(arguments | {"cc": "late@zeta.test"}))

    monkeypatch.setattr(tool, "fn", renormalized)
    call(mcp, "create_draft", {"to": "ann@alpha.test", "subject": "s", "body": "b"}, host())
    assert {address.domain for address in saved_recipients(fake)} == {"alpha.test", "zeta.test"}
    started, completed = audits(tmp_path / "actions.jsonl")[0]
    assert (started["recipient_count"], started["recipient_domains"]) == (1, ["alpha.test"])
    assert completed["recipient_count"] == 2
    assert completed["recipient_domains"] == ["alpha.test", "zeta.test"]


def test_send_is_audited_without_addresses_or_content(rig, monkeypatch):
    monkeypatch.setattr("proton_mail_mcp.server.SendService", lambda *args: rig.service)
    mcp = create_mcp(rig.settings, rig.client)
    preview = rig.service.preview_send(DRAFT_ID)
    result = call(mcp, "send_draft", {"token": preview["token"]}, host())
    assert result["sent"] is True
    with pytest.raises(ToolError):
        call(mcp, "send_draft", {"token": preview["token"]}, host())
    with pytest.raises(ToolError):
        call(mcp, "send_draft", {"token": preview["token"], "confirm": True}, host())
    sent, replayed, invalid = audits(rig.log.path)
    assert [row["outcome"] for row in sent] == ["started", "completed"]
    assert sent[-1]["action_hash"] == preview["action_hash"]
    assert sent[-1]["send_result"] == "sent" and sent[-1]["message_id"] == DRAFT_ID
    assert "log_ids" not in result and re.fullmatch(r"[0-9a-f]{32}", result["log_id"])
    assert "log_id" not in preview and "log_ids" not in preview
    assert sent[-1]["log_ids"] == [result["log_id"]]
    assert replayed[-1]["outcome"] == "refused"
    assert invalid[-1]["outcome"] == "invalid_arguments"
    text = rig.log.path.read_text()
    for secret in (BODY, SUBJECT, PASSWORD, preview["token"], "recipient@example.test"):
        assert secret not in text


def test_unwritable_audit_log_refuses_writes_before_any_change(case, tmp_path):  # noqa: F811
    built = case("archive")
    blocked = tmp_path / "blocked-log"
    blocked.mkdir()
    mcp = create_mcp(replace(built.client.settings, action_log_path=str(blocked)), built.client)
    with pytest.raises(ToolError, match="Action log is busy or unavailable; no change was made"):
        call(mcp, "archive", {"token": built.preview["token"]}, host())
    assert not built.changed()
    listed = call(mcp, "preview_action", {"action": "archive", "query": {}}, host())
    assert listed["count"] == 2 and listed["log_id"]


def test_busy_log_is_waited_out_briefly(tmp_path, monkeypatch):
    log = ActionLog(tmp_path / "actions.jsonl")
    attempts = []
    original = ActionLog.transaction

    def transaction(self, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise action_log._LogBusy("Cleanup action log is busy; retry the operation")
        return original(self, **kwargs)

    monkeypatch.setattr(ActionLog, "transaction", transaction)
    log.audit(audit_record("archive", "started", audit_id="b" * 32))
    assert len(attempts) == 3
    assert [row["audit_id"] for row in audits(log.path)[0]] == ["b" * 32]

    def busy(self, **kwargs):
        raise action_log._LogBusy("Cleanup action log is busy; retry the operation")

    monkeypatch.setattr(ActionLog, "transaction", busy)
    with pytest.raises(action_log._LogBusy):
        log.audit(audit_record("archive", "started", audit_id="c" * 32), wait=0)


def test_list_actions_folds_and_filters_audits_without_tokens(case):  # noqa: F811
    built = case("archive")
    token = built.preview["token"]
    call(built.mcp, "archive", {"token": token}, host())
    second = case("move_to_trash")
    with ActionLog(log_path(second)).transaction() as transaction:
        with client_context({**CLIENT, "name": "other-client"}):
            transaction.append(audit_record("move_to_trash", "started", audit_id="d" * 32))
    lookup = built.client.list_actions
    [entry] = lookup(action="archive")["audit"]
    assert entry["tool"] == "archive" and entry["outcome"] == "completed"
    assert entry["client"] == CLIENT and entry["action_hash"] == built.preview["action_hash"]
    assert set(entry) == {"audit_id", "timestamp", "tool", "outcome", "client"} | {
        "action_hash",
        "counts",
    }
    assert lookup(token=token)["audit"] == [entry]
    assert lookup(token=token + "x")["audit"] == []
    assert [row["tool"] for row in lookup(client="offline-audit")["audit"]] == ["archive"]
    others = second.client.list_actions(client="other-client")
    assert [(row["tool"], row["outcome"]) for row in others["audit"]] == [
        ("move_to_trash", "started")
    ]
    assert lookup(action="archive")["actions"] and not lookup(action="unsubscribe")["actions"]
    assert lookup(before=entry["timestamp"])["audit"] == []
    visible = json.dumps(lookup())
    assert token not in visible and hashlib.sha256(token.encode()).hexdigest()[:16] not in visible
    for kwargs in ({"action": "search_mail"}, {"action": 7}, {"client": ""}, {"client": "x\n"}):
        with pytest.raises(MailValidationError):
            lookup(**kwargs)


def test_cleanup_journal_records_name_the_client(case):  # noqa: F811
    built = case("archive")
    call(built.mcp, "archive", {"token": built.preview["token"]}, host())
    with ActionLog(log_path(built)).transaction(readonly=True) as transaction:
        records = transaction.records(include_runs=True)
    chunk = next(row for row in records if row.get("action") == "archive")
    run = next(row for row in records if row.get("action") == "execute_run")
    assert chunk["client"] == CLIENT and run["client"] == CLIENT
    raw = [json.loads(line) for line in log_path(built).read_text().splitlines()]
    events = [row for row in raw if row["action"] in {"run_start", "run_end"}]
    assert events and all(row["client"] == CLIENT for row in events)


def test_rules_timer_cli_records_name_the_cli(monkeypatch, capsys):
    seen = []

    class Timer:
        def __init__(self, cleanup):
            pass

        def run(self, folder):
            seen.append(action_log._CLIENT.get())
            return {"status": "completed", "count": 0, "log_ids": []}

    monkeypatch.setattr(rules_timer, "RulesTimer", Timer)
    monkeypatch.setattr(sys, "argv", ["proton-mail-mcp-rules-run"])
    monkeypatch.setenv("PROTON_BRIDGE_USER", "reader@example.test")
    with pytest.raises(SystemExit) as exit_code:
        rules_timer.main()
    assert exit_code.value.code == 0
    assert seen == [{"name": "proton-mail-mcp-rules-run", "version": None, "transport": "cli"}]
    assert action_log._CLIENT.get() is None


def test_interrupted_call_reads_as_started(case):  # noqa: F811
    built = case("move_to_trash")
    with ActionLog(log_path(built)).transaction() as transaction:
        transaction.append(audit_record("move_to_trash", "started", audit_id="e" * 32))
    [entry] = built.client.list_actions(action="move_to_trash")["audit"]
    assert entry["outcome"] == "started" and "counts" in entry and entry["counts"] is None


def test_existing_folder_is_audited_as_a_no_op(case):  # noqa: F811
    built = case("create_folder")
    # Another approval creates the same folder first.
    other = built.client.preview_create_folder("Folders/Confirmed")["token"]
    call(built.mcp, "create_folder", {"token": other}, host())
    result = call(built.mcp, "create_folder", {"token": built.preview["token"]}, host())
    assert result["exists"] is True and result["created"] is False
    assert result["token_consumed"] is False
    created, existed = [records[-1] for records in audits(log_path(built))]
    assert created["counts"] == {"approved": 1, "done": 1, "uncertain": 0, "remaining": 0}
    assert created["exists"] is False and created["token_consumed"] is True
    assert existed["counts"] == {"approved": 1, "done": 0, "uncertain": 0, "remaining": 0}
    assert existed["exists"] is True and existed["outcome"] == "completed"
    assert existed["token_consumed"] is False


def test_created_folder_with_an_unjournaled_outcome_counts_once(case, monkeypatch):  # noqa: F811
    built = case("create_folder")
    append = LogTransaction.append

    def fail_done(transaction, record):
        if record["action"] == "execute_progress" and record["message"]["status"] == "done":
            raise MailError("Synthetic journal failure")
        return append(transaction, record)

    monkeypatch.setattr(LogTransaction, "append", fail_done)
    result = call(built.mcp, "create_folder", {"token": built.preview["token"]}, host())
    assert result["created"] is True and result["partial"] is True and built.changed()
    finished = audits(log_path(built))[-1][-1]
    assert finished["outcome"] == "partial"
    assert finished["counts"] == {"approved": 1, "done": 1, "uncertain": 0, "remaining": 0}


@pytest.mark.parametrize(
    "tool,result,counts",
    [
        ("create_sieve_filter", {"created": True, "attempted": True, "partial": False}, (1, 0, 0)),
        ("create_sieve_filter", {"created": True, "attempted": True, "partial": True}, (1, 0, 0)),
        ("create_sieve_filter", {"created": True, "partial": True}, (1, 0, 0)),
        ("create_sieve_filter", {"created": False, "attempted": True, "partial": True}, (0, 1, 0)),
        ("create_sieve_filter", {"created": False, "partial": True}, (0, 1, 0)),
        ("create_sieve_filter", {"created": False, "attempted": False, "partial": True}, (0, 0, 1)),
        ("add_rule", {"added": True, "attempted": True, "partial": False}, (1, 0, 0)),
        ("add_rule", {"added": True, "attempted": True, "partial": True}, (1, 0, 0)),
        ("add_rule", {"added": False, "attempted": True, "partial": True}, (0, 1, 0)),
        ("add_rule", {"added": False, "attempted": False, "partial": True}, (0, 0, 1)),
        # add_rule reports added, never created.
        ("add_rule", {"created": True, "attempted": True, "partial": True}, (0, 1, 0)),
        ("create_folder", {"created": True, "partial": True}, (1, 0, 0)),
        ("create_folder", {"created": False, "partial": True}, (0, 1, 0)),
        ("create_folder", {"created": False, "exists": True, "partial": False}, (0, 0, 0)),
    ],
)
def test_one_created_object_is_counted_at_most_once(tool, result, counts):
    summary = _audit_result(tool, result | {"token_consumed": True})
    done, uncertain, remaining = counts
    expected = {"approved": 1, "done": done, "uncertain": uncertain, "remaining": remaining}
    assert summary["counts"] == expected
    assert summary["outcome"] == ("partial" if result["partial"] else "completed")


def test_audit_hash_peek_has_no_side_effects():
    now = [1000.0]
    tokens = TokenStore(10, clock=lambda: now[0])
    rows = [{"folder": "INBOX", "uidvalidity": "1", "uid": "1", "message_id": "<a@example.test>"}]
    token, _ = tokens.issue("archive", {}, {}, rows)
    expected = tokens.describe(token, "archive").action_hash
    now[0] += 10
    assert tokens.action_hash(token) == expected
    assert tokens.action_hash("unknown") is None and tokens.action_hash(7) is None
    with pytest.raises(MailValidationError, match="^Preview token expired; request a new preview$"):
        tokens.inspect(token, "archive")


def test_expired_tokens_keep_their_refusal_through_the_audited_tools(rig, case, monkeypatch):  # noqa: F811
    monkeypatch.setattr("proton_mail_mcp.server.SendService", lambda *args: rig.service)
    mcp = create_mcp(rig.settings, rig.client)
    preview = rig.service.preview_send(DRAFT_ID)
    rig.now[0] += rig.tokens.ttl
    with pytest.raises(ToolError):
        call(mcp, "send_draft", {"token": preview["token"]}, host())
    send_records = [row for row in records(rig) if row.get("action") == "send_draft"]
    assert send_records[-1]["result"] == "token_expired"
    assert audits(rig.log.path)[-1][-1]["action_hash"] == preview["action_hash"]

    built = case("archive", confirm_mutations=False)
    tokens = built.client.cleanup.tokens
    now = [tokens.clock() + tokens.ttl]
    tokens.clock = lambda: now[0]
    with pytest.raises(ToolError, match="Preview token expired; request a new preview"):
        call(built.mcp, "archive", {"token": built.preview["token"]}, host())
    expired = audits(log_path(built))[-1][-1]
    assert expired["outcome"] == "refused"
    assert expired["action_hash"] == built.preview["action_hash"]


@pytest.mark.parametrize(
    "tool,arguments,result",
    [
        ("create_folder", {"token": "A" * 43}, {"log_id": "synthetic-log", "partial": False}),
        ("undo_action", {"log_id": "0" * 32}, {"messages": [1, 2]}),
        ("undo_action", {"log_id": "0" * 32}, "odd"),
    ],
)
def test_unexpected_results_still_return_and_are_audited(tmp_path, tool, arguments, result):
    settings = Settings(
        token="audit-shape-token-" + "x" * 32,
        bridge_user="reader@example.test",
        action_log_path=str(tmp_path / "actions.jsonl"),
        confirm_mutations=False,
    )
    backend = SimpleNamespace(**{tool: lambda **kwargs: result})
    mcp = create_mcp(settings, backend)
    assert call(mcp, tool, arguments, host()) == result
    [records] = audits(tmp_path / "actions.jsonl")
    assert [row["outcome"] for row in records] == ["started", "completed"]
