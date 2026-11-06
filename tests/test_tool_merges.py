"""Merged tools stay exactly as strict as the near-twin tools they replaced."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from merged_tools import tool_call
from test_approval_binding import bridge as bridge  # noqa: F401
from test_audit_log import audits
from test_host_confirmation import ROWS
from test_host_confirmation import case as case  # noqa: F401
from test_mail import client as mail_client
from test_mail import settings as settings  # noqa: F401
from test_reindex import ReindexImap, completed
from test_send import Host

from proton_mail_mcp.action_log import ActionLog, audit_record, new_record
from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.server import create_mcp

ACTIONS = [
    "move_to_trash",
    "archive",
    "move_to_folder",
    "mark_read",
    "mark_unread",
    "add_label",
    "remove_label",
]
REMOVED = {
    *ACTIONS,
    "reindex_preview",
    "reindex_execute",
    "reindex_cancel",
}
MERGED = {
    "move": {"move_to_trash", "archive", "move_to_folder"},
    "set_read_state": {"mark_read", "mark_unread"},
    "label": {"add_label", "remove_label"},
    "reindex": {"reindex_preview", "reindex_execute", "reindex_cancel"},
}
TOKEN = "tool-merges-test-token-" + "x" * 32


def call(mcp, action, arguments, host=None):
    tool, arguments = tool_call(action, arguments)
    return asyncio.run(mcp._tool_manager.call_tool(tool, arguments, context=host))


def log_of(built):
    return Path(built.client.settings.action_log_path)


def full_surface():
    settings = Settings(
        token=TOKEN,
        bridge_user="bridge@example.test",
        confirm_mutations=False,
        enable_send=True,
        enable_rules=True,
        send_from_allowlist=("bridge@example.test",),
    )
    mcp = create_mcp(settings, SimpleNamespace())
    return mcp, {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def test_merged_tools_replace_their_twins_and_the_rest_stay_separate():
    mcp, tools = full_surface()
    assert len(tools) == 32
    assert not REMOVED & set(tools)
    assert set(MERGED) <= set(tools)
    # A required selector: a merged write never has a default operation.
    for name, selector in (("move", "target"), ("set_read_state", "state"), ("label", "op")):
        assert set(tools[name].inputSchema["required"]) == {selector, "token"}
        assert tools[name].annotations.destructiveHint is True
        assert tools[name].annotations.readOnlyHint is False
    assert tools["reindex"].inputSchema["required"] == ["op"]
    # Deliberately not merged: reads stay read-only and views never hold a token.
    assert tools["reindex_status"].annotations.readOnlyHint is True
    assert tools["list_actions"].annotations.readOnlyHint is True
    assert tools["undo_action"].annotations.destructiveHint is True
    assert "resourceUri" in tools["review_send"].meta["ui"]
    assert "resourceUri" not in tools["preview_send"].meta["ui"]
    assert set(tools["preview_send"].inputSchema["properties"]) == {"draft_id"}
    # Models are told how to redeem and never pointed at a removed reindex tool.
    preview = tools["preview_action"].inputSchema["properties"]["action"]["description"]
    assert "move(target=" in preview and "set_read_state(state=" in preview
    assert "label(op=" in preview and "create_folder under their own names" in preview
    redeemers = "move, set_read_state, label, delete_draft, unsubscribe or create_folder"
    assert redeemers in " ".join(tools["preview_action"].description.split())
    for text in (mcp.instructions, *(tool.description for tool in tools.values())):
        assert not any(name in text for name in MERGED["reindex"])


@pytest.mark.parametrize("confirm", [True, False], ids=["form", "no-form"])
@pytest.mark.parametrize(
    "action,selector",
    [
        ("move_to_trash", {"target": "Archive"}),
        ("move_to_trash", {"target": "Folders/Reviewed"}),
        ("archive", {"target": "Trash"}),
        ("archive", {"target": "Folders/Archive"}),
        ("move_to_folder", {"target": "Trash"}),
        ("move_to_folder", {"target": "Archive"}),
        ("move_to_folder", {"target": "Folders/Other"}),
        ("move_to_folder", {"target": "Folders/reviewed"}),
        ("move_to_folder", {"target": "Folders/Reviewed/"}),
        ("move_to_folder", {"target": "Folders/Reviewed/Sub"}),
        ("mark_read", {"state": "unread"}),
        ("mark_unread", {"state": "read"}),
        ("add_label", {"op": "remove"}),
        ("remove_label", {"op": "add"}),
    ],
)
def test_a_selector_the_token_does_not_pin_refuses_and_keeps_the_token(
    case,  # noqa: F811
    action,
    selector,
    confirm,
):
    built = case(action, confirm_mutations=confirm)
    token = built.preview["token"]
    host = Host()
    with pytest.raises(ToolError, match="does not match the requested"):
        call(built.mcp, action, {**selector, "token": token}, host)
    assert host.prompts == [] and not built.changed()
    [(started, finished)] = audits(log_of(built))
    assert started["outcome"] == "started" and finished["outcome"] == "refused"
    assert "does not match the requested" in finished["reason"]
    # The refusal consumed nothing: the exact approved change still runs, once.
    second = Host()
    result = call(built.mcp, action, {"token": token}, second)
    assert built.changed() and result["done"] == result["approved_total"] == 2
    assert len(second.prompts) == int(confirm)
    with pytest.raises(ToolError, match="already used"):
        call(built.mcp, action, {"token": token}, Host())


@pytest.mark.parametrize(
    "action,tool,arguments",
    [
        ("mark_read", "label", {"op": "add"}),
        ("mark_read", "move", {"target": "Trash"}),
        ("add_label", "set_read_state", {"state": "read"}),
        ("add_label", "move", {"target": "Folders/Reviewed"}),
        ("archive", "label", {"op": "remove"}),
        ("archive", "set_read_state", {"state": "read"}),
        ("apply_rules", "move", {"target": "Folders/Reviewed"}),
        ("archive", "delete_draft", {}),
        ("move_to_folder", "apply_rules", {}),
        # A folder token redeems only with create_folder, and create_folder only a folder token.
        ("create_folder", "move", {"target": "Folders/Confirmed"}),
        ("create_folder", "set_read_state", {"state": "read"}),
        ("create_folder", "label", {"op": "add"}),
        ("create_folder", "delete_draft", {}),
        ("create_folder", "unsubscribe", {}),
        ("create_folder", "apply_rules", {}),
        ("create_folder", "add_rule", {}),
        ("archive", "create_folder", {}),
        ("move_to_folder", "create_folder", {}),
        ("mark_read", "create_folder", {}),
        ("add_label", "create_folder", {}),
        ("unsubscribe", "create_folder", {}),
        ("apply_rules", "create_folder", {}),
        ("add_rule", "create_folder", {}),
        ("create_sieve_filter", "create_folder", {}),
    ],
)
def test_no_token_crosses_to_another_merged_tool(case, action, tool, arguments):  # noqa: F811
    built = case(action, enable_rules=True)
    host = Host()
    with pytest.raises(ToolError, match="does not match"):
        call(built.mcp, tool, {**arguments, "token": built.preview["token"]}, host)
    assert host.prompts == [] and not built.changed()
    # Refused before the form, so the token is still live for its own tool.
    assert built.client.cleanup.tokens.describe(built.preview["token"], action).action == action


def test_a_draft_deletion_token_never_moves_mail(case):  # noqa: F811
    built = case("archive")
    token, _ = built.client.cleanup.tokens.issue(
        "delete_draft", {"folder": "Drafts"}, {"ids": [ROWS[0]["message_id"]]}, ROWS
    )
    host = Host()
    with pytest.raises(ToolError, match="does not match"):
        call(built.mcp, "move", {"target": "Trash", "token": token}, host)
    assert host.prompts == [] and not built.changed()


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("move", {}),
        ("move", {"target": 5}),
        ("move", {"target": ["Trash"]}),
        ("move", {"target": {"folder": "Trash"}}),
        ("move", {"target": None}),
        ("move", {"target": ""}),
        ("move", {"target": "trash"}),
        ("move", {"target": "TRASH"}),
        ("move", {"target": " Trash"}),
        ("move", {"target": "INBOX"}),
        ("move", {"target": "All Mail"}),
        ("move", {"target": "Labels/Review"}),
        ("set_read_state", {}),
        ("set_read_state", {"state": True}),
        ("set_read_state", {"state": ["read"]}),
        ("set_read_state", {"state": "seen"}),
        ("label", {}),
        ("label", {"op": {"add": True}}),
        ("label", {"op": "toggle"}),
        ("reindex", {}),
        ("reindex", {"op": 1}),
        ("reindex", {"op": ["preview"]}),
        ("reindex", {"op": "resume"}),
    ],
)
def test_arguments_selecting_no_action_refuse_and_audit_under_the_tool(
    case,  # noqa: F811
    tool,
    arguments,
):
    built = case("archive")
    token = built.preview["token"]
    host = Host()
    with pytest.raises(ToolError) as error:
        call(built.mcp, tool, {**arguments, "token": token}, host)
    assert token not in str(error.value)
    assert host.prompts == [] and not built.changed()
    [(started, finished)] = audits(log_of(built))
    assert started["tool"] == finished["tool"] == tool
    assert finished["outcome"] == "invalid_arguments"
    # Such a call is still found, by its tool's name.
    assert [row["tool"] for row in built.client.list_actions(action=tool)["audit"]] == [tool]
    call(built.mcp, "archive", {"token": token}, Host())
    assert built.changed()


@pytest.mark.parametrize(
    "tool,selector",
    [("move", "target"), ("set_read_state", "state"), ("label", "op"), ("reindex", "op")],
)
def test_refused_selectors_never_echo_their_value(case, tool, selector):  # noqa: F811
    built = case("archive")
    secret = "private-selector-value-never-echoed"
    with pytest.raises(ToolError) as error:
        call(built.mcp, tool, {selector: secret, "token": built.preview["token"]}, Host())
    assert secret not in str(error.value)
    assert secret not in log_of(built).read_text()


def test_folder_continuation_stays_pinned_to_its_target(case):  # noqa: F811
    built = case("move_to_folder")
    target = built.preview["target"]
    assert target == "Folders/Reviewed"
    first = call(
        built.mcp, "move", {"target": target, "token": built.preview["token"], "limit": 1}, Host()
    )
    assert first["done"] == 1 and first["remaining"] == 1
    token = first["continuation_token"]
    with pytest.raises(ToolError, match="does not match the requested move target"):
        call(built.mcp, "move", {"target": "Folders/Other", "token": token}, Host())
    with pytest.raises(ToolError, match="does not match"):
        call(built.mcp, "move", {"target": "Trash", "token": token}, Host())
    # The refusals moved nothing: a continuation needs no second form and finishes the set.
    host = Host()
    rest = call(built.mcp, "move", {"target": target, "token": token}, host)
    assert rest["run_done"] == 2 and rest["remaining"] == 0 and host.prompts == []


@pytest.mark.parametrize("confirm", [True, False], ids=["form", "no-form"])
@pytest.mark.parametrize(
    "action,target",
    [("move_to_trash", "Trash"), ("archive", "Archive"), ("move_to_folder", "Folders/Reviewed")],
)
def test_move_returns_the_target_its_continuation_must_state(case, action, target, confirm):  # noqa: F811
    built = case(action, confirm_mutations=confirm)
    assert built.preview["target"] == target
    first = call(
        built.mcp, "move", {"target": target, "token": built.preview["token"], "limit": 1}, Host()
    )
    assert first["target"] == target and first["remaining"] == 1 and "messages" not in first
    # The response alone finishes the set, for a caller that no longer has the preview.
    rest = call(
        built.mcp,
        "move",
        {"target": first["target"], "token": first["continuation_token"], "verbose": True},
        Host(),
    )
    assert rest["target"] == target and rest["run_done"] == 2 and rest["remaining"] == 0


def test_form_shows_the_stored_target_for_a_folder_created_by_the_approval(case):  # noqa: F811
    built = case("move_to_folder")
    create = built.client.preview_action(
        "move_to_folder",
        query={"folder": "INBOX"},
        params={"folder": "Folders/New", "create_folder_if_missing": True},
    )
    assert create["target"] == "Folders/New" and create["folder_creation"]["create"] is True
    with pytest.raises(ToolError, match="does not match the requested move target"):
        call(built.mcp, "move", {"target": "Folders/Reviewed", "token": create["token"]}, Host())
    host = Host("decline")
    with pytest.raises(ToolError, match="User confirmation was not accepted"):
        call(built.mcp, "move", {"target": "Folders/New", "token": create["token"]}, host)
    assert '"Folders/New"' in host.prompts[0] and "Create the folder" in host.prompts[0]
    assert not built.changed()


def test_pin_check_changes_nothing_and_needs_a_live_token(case):  # noqa: F811
    built = case("move_to_folder")
    token = built.preview["token"]
    check = built.client.check_move_target
    check("move_to_folder", token, "Folders/Reviewed")
    with pytest.raises(MailValidationError, match="requested move target"):
        check("move_to_folder", token, "Folders/Other")
    with pytest.raises(MailValidationError, match="requested action"):
        check("archive", token, "Archive")
    with pytest.raises(MailValidationError, match="already used"):
        check("move_to_folder", "not-a-preview-token", "Folders/Reviewed")
    # The check neither confirms nor consumes the approval.
    assert built.client.cleanup.tokens.describe(token, "move_to_folder").confirmed is False
    assert not built.changed()


@pytest.mark.parametrize("action", ACTIONS)
def test_every_merged_action_stays_audited_and_undoable(case, action):  # noqa: F811
    built = case(action)
    token = built.preview["token"]
    result = call(built.mcp, action, {"token": token}, Host())
    [(started, finished)] = audits(log_of(built))
    # Records name the exact action, with the counts its call produced.
    assert started["tool"] == finished["tool"] == action
    assert finished["outcome"] == "completed"
    assert finished["counts"]["done"] == finished["counts"]["approved"] == 2
    assert finished["log_ids"] == result["log_ids"]
    assert finished["action_hash"] == built.preview["action_hash"]
    with ActionLog(log_of(built)).transaction(readonly=True) as transaction:
        records = transaction.records(include_runs=True)
    assert next(row for row in records if row.get("run_id"))["action"] == action
    assert next(row for row in records if row["action"] == "execute_run")["run_action"] == action
    for log_id in result["log_ids"]:
        undone = call(built.mcp, "undo_action", {"log_id": log_id}, Host())
        assert undone["partial"] is False
        assert {row["status"] for row in undone["messages"]} == {"done"}


def test_lookup_by_a_merged_tool_finds_every_exact_action_old_and_new(tmp_path):
    log = ActionLog(tmp_path / "actions.jsonl")
    # Records written before and after the merge are the same shape.
    with log.transaction() as transaction:
        for action in sorted(REMOVED):
            transaction.append(new_record(action, {}))
            transaction.append(audit_record(action, "completed", audit_id=action.ljust(32, "x")))
        transaction.append(audit_record("move", "invalid_arguments", audit_id="m" * 32))
        preview = new_record("preview_action", {"action": "archive"})
        preview.update(
            status="previewed",
            count=1,
            blocked_count=0,
            excluded_count=0,
            total_is_exact=True,
            action_hash=None,
        )
        transaction.append(preview)
    for tool, actions in MERGED.items():
        found = log.list_actions(action=tool)
        moves = tool == "move"
        assert {row["tool"] for row in found["audit"]} == actions | ({"move"} if moves else set())
        assert {row["action"] for row in found["actions"]} == actions | (
            {"preview_action"} if moves else set()
        )
        # An exact action still finds only itself.
        for action in actions:
            exact = log.list_actions(action=action)
            assert {row["tool"] for row in exact["audit"]} == {action}
    assert {row["action"] for row in log.list_actions(action="preview_action")["actions"]} == {
        "preview_action"
    }
    with pytest.raises(MailValidationError, match="must name a write or preview tool"):
        log.list_actions(action="mark")


def test_merged_lookup_finds_a_real_run_and_its_preview(case):  # noqa: F811
    built = case("archive")
    call(built.mcp, "archive", {"token": built.preview["token"]}, Host())
    found = call(built.mcp, "list_actions", {"action": "move"}, Host())
    assert {row["action"] for row in found["actions"]} == {"archive", "preview_action"}
    assert [row["action"] for row in found["runs"]] == ["archive"]
    assert [row["tool"] for row in found["audit"]] == ["archive"]
    assert call(built.mcp, "list_actions", {"action": "label"}, Host())["actions"] == []


@pytest.fixture
def reindexing(settings):  # noqa: F811
    def build(*args, **kwargs):
        fake = ReindexImap(*args, **kwargs)
        options = replace(settings, reindex_batch_size=100, reindex_batch_sleep=0.01)
        reader = mail_client(options, fake)
        return SimpleNamespace(
            fake=fake,
            reader=reader,
            mcp=create_mcp(options, reader),
            log=ActionLog(options.action_log_path).path,
        )

    return build


def test_reindex_ops_share_the_single_use_token_without_a_form(reindexing):
    built = reindexing()
    host = Host("decline")
    preview = call(built.mcp, "reindex", {"op": "preview"}, host)
    assert preview["token"] and preview["log_id"] and preview["all_mail_count"] == 3
    assert built.reader.reindex.status()["state"] != "running"
    token = preview["token"]
    started = call(built.mcp, "reindex", {"op": "execute", "token": token}, host)
    assert started["state"] == "running" and started["token_consumed"] is True
    assert completed(built.reader.reindex)["state"] == "succeeded"
    with pytest.raises(ToolError, match="already used"):
        call(built.mcp, "reindex", {"op": "execute", "token": token}, host)
    assert host.prompts == []
    # Every op is audited under its exact name, and the issued token is never logged.
    assert [(call[0]["tool"], call[-1]["outcome"]) for call in audits(built.log)] == [
        ("reindex_preview", "completed"),
        ("reindex_execute", "completed"),
        ("reindex_execute", "refused"),
    ]
    assert token not in built.log.read_text()
    assert audits(built.log)[0][-1]["log_ids"] == [preview["log_id"]]
    assert audits(built.log)[1][-1]["run_id"] == started["run_id"]


def test_reindex_cancel_stops_only_the_named_run(reindexing):
    built = reindexing(block=True)
    token = call(built.mcp, "reindex", {"op": "preview"})["token"]
    started = call(built.mcp, "reindex", {"op": "execute", "token": token})
    assert built.fake.entered.wait(5)
    # A preview during that run issues nothing, and its audit says so.
    running = call(built.mcp, "reindex", {"op": "preview"})
    assert running["run_id"] == started["run_id"] and "token" not in running
    outcome = audits(built.log)[-1][-1]
    assert (outcome["tool"], outcome["outcome"]) == ("reindex_preview", "refused")
    assert outcome["reindex_state"] == "running" and outcome["run_id"] == started["run_id"]
    with pytest.raises(ToolError, match="not running"):
        call(built.mcp, "reindex", {"op": "cancel", "run_id": "0" * 32})
    cancelled = call(built.mcp, "reindex", {"op": "cancel", "run_id": started["run_id"]})
    assert cancelled["cancel_requested"] is True
    built.fake.release.set()
    assert completed(built.reader.reindex)["state"] == "cancelled"
    assert call(built.mcp, "reindex_status", {})["state"] == "cancelled"


def test_reindex_status_stays_outside_the_write_audit(reindexing):
    built = reindexing()
    call(built.mcp, "reindex_status", {})
    assert not built.log.exists() or audits(built.log) == []


def test_reindex_and_cleanup_tokens_never_cross(case):  # noqa: F811
    built = case("archive")
    cleanup_token = built.preview["token"]
    with pytest.raises(ToolError, match="invalid, expired or already used"):
        call(built.mcp, "reindex", {"op": "execute", "token": cleanup_token}, Host())
    reindex_token = call(built.mcp, "reindex", {"op": "preview"}, Host())["token"]
    for tool, arguments in (
        ("move", {"target": "Archive"}),
        ("set_read_state", {"state": "read"}),
        ("label", {"op": "add"}),
    ):
        with pytest.raises(ToolError, match="already used"):
            call(built.mcp, tool, {**arguments, "token": reindex_token}, Host())
    assert not built.changed()
    # Both tokens are still live for their own tools.
    call(built.mcp, "move", {"target": "Archive", "token": cleanup_token}, Host())
    assert built.changed()
    started = call(built.mcp, "reindex", {"op": "execute", "token": reindex_token}, Host())
    assert started["token_consumed"] is True
    built.client.reindex._thread.join(10)
