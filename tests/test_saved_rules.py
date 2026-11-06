"""Saved custom rules: previews, bound tokens, host confirmation and journaled appends."""

import asyncio
import hashlib
import json
import os
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError
from sieve_fakes import FakeSieveBackend
from test_send import Host
from windows_skips import has_mode

from proton_mail_mcp import rules_store, saved_rules
from proton_mail_mcp.action_log import ActionLog, LogTransaction
from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.guarded import TokenStore, approval_hash
from proton_mail_mcp.mail import MailClient, MailError, MailValidationError
from proton_mail_mcp.rules_store import (
    MAX_SAVED_RULES,
    LocalRulesBackend,
    load_saved,
    rule_sha256,
    store_path,
)
from proton_mail_mcp.server import create_mcp
from proton_mail_mcp.sieve import (
    UnavailableSieveBackend,
    filter_spec,
    render_script,
    script_sha256,
)

ACTION = "add_rule"
GITHUB = [{"field": "from", "contains": "notifications@github.com"}]
NAME = "Private rule name"
FOLDERS = ("Folders/GitHub", "Folders/Reviewed", "Folders/Work", "Folders/Work/Receipts")
ARGUMENTS = {"name": NAME, "folder": "Folders/GitHub", "conditions": GITHUB}


@pytest.fixture
def case(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Saved rule tests must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    simulations = []

    def build(**options):
        sim = BridgeSim(**({"folders": FOLDERS, "labels": ("Labels/News",)} | options))
        simulations.append(sim)
        directory = tmp_path / f"case-{len(simulations)}"
        directory.mkdir()
        client = make_client(sim, directory, enable_rules=True)
        cleanup = client.cleanup
        return SimpleNamespace(
            sim=sim,
            client=client,
            tokens=cleanup.tokens,
            log=cleanup.backend.log,
            store=store_path(client.settings),
            directory=directory,
        )

    built = build()
    built.build = build
    yield built
    for sim in simulations:
        sim.close()
        assert not sim.errors and not sim.violations
        # Saving a rule never changes mail.
        assert not mutations(sim)


def preview(case, **options):
    return case.client.preview_add_rule(**(ARGUMENTS | options))


def spec(**options):
    return filter_spec(**(ARGUMENTS | {"match": "all", "mark_read": False} | options))


def records(case):
    with case.log.transaction(readonly=True) as transaction:
        return transaction.records()


def saved(case):
    return load_saved(case.store)[0]


def kinds(case):
    return {command.split()[0] for *_, command in case.sim.commands}


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def stored(case):
    [grant] = case.tokens._grants.values()
    return grant


def fill(case, count=MAX_SAVED_RULES):
    backend = LocalRulesBackend(case.store)
    for index in range(count):
        rule = filter_spec(f"Rule {index}", "Folders/Reviewed", [{"field": "subject", "is": "x"}])
        backend.create_filter(rule.name, render_script(rule), timeout=5)


def test_preview_shows_the_rule_and_binds_a_message_less_token_without_changes(case):
    result = preview(case)
    rule = spec()
    script = render_script(rule)
    assert result["action"] == ACTION
    assert {key: result[key] for key in rule.params()} == rule.params()
    assert result["rule_sha256"] == rule_sha256(rule)
    assert result["script"] == script and result["script_sha256"] == script_sha256(script)
    assert result["apply"] == {"backend": "local_rules", "available": True, "reason": None}
    assert result["token"] and result["expires_at"] and "matches" not in result
    grant = case.tokens.inspect(result["token"], ACTION)
    assert grant.messages == [] and grant.selector == {}
    assert grant.params == rule.params() | {
        "rule_sha256": rule_sha256(rule),
        "script": script,
        "script_sha256": script_sha256(script),
        "backend": "local_rules",
        "store": LocalRulesBackend(case.store).store_id,
    }
    assert grant.action_hash == result["action_hash"] == approval_hash(ACTION, grant.params, {}, [])
    assert not case.log.path.exists() and not case.store.exists()
    assert kinds(case) <= {"CAPABILITY", "STARTTLS", "LOGIN", "LIST", "LOGOUT"}


def github(case, index, *, sender="GitHub <notifications@github.com>", subject="PR merged"):
    return case.sim.add(Msg(hdr(f"<m{index}@x.test>", from_=sender, subject=subject)))


def test_include_matches_counts_current_inbox_mail_read_only_and_bounded(case, monkeypatch):
    messages = [github(case, index, subject=f"PR {index}") for index in range(4)]
    github(case, 10, sender='"notifications@github.com" <spoof@evil.example>')
    github(case, 11, sender="Friend <friend@elsewhere.example>")
    result = preview(case, include_matches=True)
    matches = result["matches"]
    assert {key: matches[key] for key in ("folder", "count", "candidates", "scanned")} == {
        "folder": "INBOX",
        "count": 4,
        "candidates": 5,
        "scanned": 5,
    }
    assert matches["total_is_exact"] is True
    # Newest first; the spoofed display name never counts as the address.
    assert [sample["subject"] for sample in matches["samples"]] == ["PR 3", "PR 2", "PR 1", "PR 0"]
    assert set(matches["samples"][0]) == {"from", "subject", "date", "folder", "uidvalidity", "uid"}
    assert [sample["uid"] for sample in matches["samples"]] == [
        str(case.sim.uid_of("INBOX", message.pid)) for message in reversed(messages)
    ]
    assert result["token"] and not case.log.path.exists()
    examined = [command for *_, command in case.sim.commands if command.startswith("SELECT")]
    assert examined == [] and "EXAMINE" in kinds(case)
    assert all(not message.flags for message in case.sim.messages.values())
    monkeypatch.setattr(saved_rules, "MATCH_SCAN", 2)
    narrowed = preview(case, include_matches=True)["matches"]
    assert (narrowed["scanned"], narrowed["candidates"], narrowed["count"]) == (2, 5, 1)
    assert narrowed["total_is_exact"] is False


def test_include_matches_never_blocks_the_preview(case, monkeypatch):
    github(case, 1)

    def failing(connection, criteria, literal=None):
        raise MailError("Synthetic search failure")

    monkeypatch.setattr(MailClient, "_search", staticmethod(failing))
    result = preview(case, include_matches=True)
    assert result["matches"]["partial"] is True and result["matches"]["total_is_exact"] is False
    assert result["token"] and case.tokens.inspect(result["token"], ACTION)


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": "bad\nname"},
        {"name": ""},
        {"name": "x" * 101},
        {"folder": "Trash"},
        {"folder": "Labels/News"},
        {"folder": "Folders/Inbox"},
        {"conditions": []},
        {"conditions": [{"field": "cc", "contains": "x"}]},
        {"conditions": [{"field": "subject", "contains": 'x"\ndiscard;'}]},
        {"conditions": [{"field": "from", "contains": "${1}"}]},
        {"conditions": [{"field": "subject", "contains": "a‮b"}]},
        {"conditions": GITHUB * 2},
        {"match": "most"},
        {"mark_read": "yes"},
        {"include_matches": "yes"},
    ],
)
def test_invalid_requests_are_refused_before_any_connection(case, arguments):
    with pytest.raises(MailValidationError):
        preview(case, **arguments)
    assert not case.sim.commands and not case.tokens._grants and not case.store.exists()


def test_missing_view_or_shadowed_folders_refuse_the_preview(case, monkeypatch):
    with pytest.raises(MailValidationError, match="existing real folder"):
        preview(case, folder="Folders/Missing")
    handle = case.sim.handle

    def special_use(tag, op, uid_op, words, session, command):
        response = handle(tag, op, uid_op, words, session, command)
        if op == "LIST":
            response = b'* LIST (\\Trash) "/" "Folders/Renamed"\r\n' + response
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
        assert not built.store.exists()


def test_add_saves_exactly_the_previewed_rule_once_after_a_durable_intent(case, monkeypatch):
    conditions = [{"field": "to", "is": "me@example.test"}, {"field": "subject", "contains": "Rx"}]
    result = preview(case, conditions=conditions, match="any", mark_read=True)
    backend = LocalRulesBackend(case.store)
    create = backend.create_filter

    def during(name, script, *, timeout):
        raw = [json.loads(line) for line in case.log.path.read_text().splitlines()]
        assert raw[0]["action"] == ACTION and raw[0]["status"] == "in_progress"
        assert raw[-1]["action"] == "execute_progress"
        assert raw[-1]["message"]["status"] == "uncertain"
        with pytest.raises(MailValidationError, match="already used"):
            case.tokens.inspect(result["token"], ACTION)
        assert not case.store.exists() and script == result["script"]
        return create(name, script, timeout=timeout)

    backend.create_filter = during
    monkeypatch.setattr(saved_rules, "LocalRulesBackend", lambda path: backend)
    added = case.client.add_rule(result["token"])
    assert added == {
        "log_id": added["log_id"],
        "name": NAME,
        "folder": "Folders/GitHub",
        "rule_sha256": result["rule_sha256"],
        "backend": "local_rules",
        "added": True,
        "attempted": True,
        "partial": False,
        "token_consumed": True,
    }
    rule = spec(conditions=conditions, match="any", mark_read=True)
    assert saved(case) == (rule,) and has_mode(case.store, 0o600)
    [record] = records(case)
    assert record["log_id"] == added["log_id"] and record["status"] == "completed"
    assert record["params"] == {
        "name_sha256": sha256(NAME),
        "folder": "Folders/GitHub",
        "match": "any",
        "fields": ["to:is", "subject:contains"],
        "mark_read": True,
        "rule_sha256": rule_sha256(rule),
        "script_sha256": result["script_sha256"],
        "backend": "local_rules",
        "store": backend.store_id,
    }
    assert record["messages"] == [{"folder": "Folders/GitHub", "status": "done", "result": "added"}]
    assert record["token_digest"] == sha256(result["token"])
    text = case.log.path.read_text()
    for private in (result["token"], NAME, "me@example.test", "fileinto", str(case.store)):
        assert private not in text
    [entry] = case.client.list_actions(token=result["token"])["actions"]
    assert (entry["action"], entry["status"], entry["done"]) == (ACTION, "completed", 1)
    with pytest.raises(MailValidationError, match="already used"):
        case.client.add_rule(result["token"])
    with pytest.raises(MailValidationError, match="cannot be undone"):
        case.client.undo_action(added["log_id"])
    assert saved(case) == (rule,)


def test_names_in_use_are_refused_in_any_case_and_existing_rules_never_replaced(case):
    case.client.add_rule(preview(case)["token"])
    before = case.store.read_bytes()
    for name in (NAME, NAME.upper(), NAME.casefold(), "Ｐrivate rule name"):
        with pytest.raises(MailValidationError, match="already exists; rules are never replaced"):
            preview(case, name=name, folder="Folders/Reviewed")
    assert not case.tokens._grants
    # A name taken between preview and confirmation refuses before the journal.
    token = preview(case, name="Second")["token"]
    taken = spec(name="SECOND", folder="Folders/Reviewed")
    LocalRulesBackend(case.store).create_filter(taken.name, render_script(taken), timeout=5)
    middle = case.store.read_bytes()
    with pytest.raises(MailValidationError, match="already exists"):
        case.client.add_rule(token)
    assert case.store.read_bytes() == middle != before and len(records(case)) == 1
    assert case.tokens.inspect(token, ACTION)
    assert [rule.name for rule in saved(case)] == [NAME, "SECOND"]


def test_list_rules_shows_built_in_then_saved_rules_without_mail_access(case, monkeypatch):
    packaged = case.directory / "packaged.json"
    packaged.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [{"field": "subject", "contains": "Invoice", "folder": "Folders/Work"}],
            }
        )
    )
    monkeypatch.setattr(FolderRulesService.__init__, "__defaults__", (packaged,))
    empty = case.client.list_rules()
    assert empty["saved_count"] == 0 and empty["built_in_count"] == 1
    case.client.add_rule(preview(case, mark_read=True)["token"])
    commands = len(case.sim.commands)
    listed = case.client.list_rules()
    assert len(case.sim.commands) == commands
    assert listed == {
        "rules": [
            {
                "rule": 1,
                "kind": "built_in",
                "field": "subject",
                "contains": "Invoice",
                "target": "Folders/Work",
            },
            {
                "rule": 2,
                "kind": "saved",
                "name": NAME,
                "conditions": GITHUB,
                "match": "all",
                "mark_read": True,
                "target": "Folders/GitHub",
                "rule_sha256": rule_sha256(spec(mark_read=True)),
            },
        ],
        "built_in_count": 1,
        "saved_count": 1,
        "max_saved_rules": MAX_SAVED_RULES,
        "apply": {"backend": "local_rules", "available": True, "reason": None},
    }


def test_a_changed_stored_rule_is_refused_by_its_binding(case):
    token = preview(case)["token"]
    grant = stored(case)
    grant.params["folder"] = "Folders/Reviewed"
    with pytest.raises(MailValidationError, match="does not match its approved action set"):
        case.client.add_rule(token)
    assert not case.store.exists() and not case.log.path.exists()


def rescript(params, **changes):
    """A grant changed consistently: its rule fields with a matching script and digests."""
    params.update(changes)
    rule = filter_spec(**{key: params[key] for key in ("name", "folder", "conditions")})
    params.update(script=render_script(rule), script_sha256=script_sha256(render_script(rule)))


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
        (lambda params: params.update(mark_read=True), "reviewed filter"),
        (lambda params: params.update(rule_sha256="0" * 64), "its preview"),
        (lambda params: params.update(script_sha256="0" * 64), "its preview"),
        # The rule, script and script digest all agree; only rule_sha256 is stale.
        (lambda params: rescript(params, folder="Folders/Reviewed"), "its preview"),
        (lambda params: rescript(params, name="Other name"), "its preview"),
        (
            lambda params: rescript(params, conditions=[{"field": "subject", "is": "x"}]),
            "its preview",
        ),
        (lambda params: params.update(conditions="from"), "conditions"),
        (lambda params: params.update(backend="fake"), "store changed after preview"),
        (lambda params: params.update(store="0" * 64), "store changed after preview"),
        (lambda params: params.pop("store"), "does not match saving a rule"),
        (lambda params: params.update(extra=True), "does not match saving a rule"),
    ],
    ids=[
        "comment",
        "comment-and-digest",
        "discard",
        "mark_read",
        "rule-digest",
        "script-digest",
        "folder",
        "name",
        "conditions",
        "malformed",
        "backend",
        "store",
        "missing-key",
        "extra-key",
    ],
)
def test_forged_but_self_consistent_grants_still_save_only_the_previewed_rule(
    case, change, message
):
    token = preview(case)["token"]
    grant = stored(case)
    change(grant.params)
    grant.binding = approval_hash(ACTION, grant.params, grant.selector, grant.messages)
    with pytest.raises(MailValidationError, match=message):
        case.client.add_rule(token)
    assert not case.store.exists() and not case.log.path.exists()


def test_tokens_of_other_actions_invalid_and_expired_tokens_cannot_save(case):
    folder_token = case.client.preview_create_folder("Folders/New")["token"]
    sieve_token = case.client.preview_sieve_filter(**ARGUMENTS)["token"]
    for other in (folder_token, sieve_token):
        with pytest.raises(MailValidationError, match="does not match the requested action"):
            case.client.add_rule(other)
    token = preview(case)["token"]
    with pytest.raises(MailValidationError, match="does not match the requested action"):
        case.client.create_folder(token)
    for value in (None, "", "forged", 7, "x" * 300):
        with pytest.raises(MailValidationError):
            case.client.add_rule(value)
    now = [0.0]
    case.client.cleanup.tokens = TokenStore(ttl=1, clock=lambda: now[0])
    expiring = preview(case)["token"]
    now[0] = 2.0
    with pytest.raises(MailValidationError, match="expired"):
        case.client.add_rule(expiring)
    assert not case.store.exists() and not case.log.path.exists()


def test_rule_grants_bind_no_messages():
    tokens = TokenStore()
    with pytest.raises(MailValidationError, match="cannot authorize message changes"):
        tokens.issue(ACTION, {}, {}, [{"message_id": "<a@example.test>"}])
    assert tokens.issue(ACTION, {"name": "x"}, {}, [])[0]


def test_folder_removed_after_preview_refuses_and_keeps_the_token(case):
    token = preview(case)["token"]
    case.sim.custom_folders.remove("Folders/GitHub")
    with pytest.raises(MailValidationError, match="existing real folder"):
        case.client.add_rule(token)
    assert not case.store.exists() and records(case) == []
    assert case.tokens.inspect(token, ACTION)
    case.sim.custom_folders.append("Folders/GitHub")
    assert case.client.add_rule(token)["added"] is True and saved(case) == (spec(),)


@pytest.mark.parametrize("state", ["full", "invalid"])
def test_a_full_or_invalid_store_refuses_before_any_journal_or_change(case, state):
    token = preview(case)["token"]
    if state == "full":
        fill(case)
        reason = "the saved rules store already holds 20 rules"
    else:
        case.store.parent.mkdir(parents=True, exist_ok=True)
        case.store.write_text('{"version": 1, "rules": [{"name": "x"}]}')
        case.store.chmod(0o600)
        reason = "the saved rules store is invalid"
    before = case.store.read_bytes()
    refusal = f"Rules cannot be saved: {reason}; nothing was changed"
    with pytest.raises(MailValidationError, match=refusal):
        preview(case, name="Another")
    with pytest.raises(MailValidationError, match=refusal):
        case.client.add_rule_ready()
    with pytest.raises(MailValidationError, match=refusal):
        case.client.add_rule(token)
    assert case.store.read_bytes() == before and not case.log.path.exists()
    assert case.tokens.inspect(token, ACTION)


def test_a_changed_store_target_after_preview_refuses(case, monkeypatch):
    token = preview(case)["token"]
    monkeypatch.setattr(
        saved_rules,
        "LocalRulesBackend",
        lambda path: LocalRulesBackend(case.directory / "elsewhere.json"),
    )
    with pytest.raises(MailValidationError, match="store changed after preview; preview again"):
        case.client.add_rule(token)
    assert not case.store.exists() and not (case.directory / "elsewhere.json").exists()
    assert case.tokens.inspect(token, ACTION) and not case.log.path.exists()


def configure(case, path):
    """Keep saved rules at path, as PROTON_MCP_SAVED_RULES does."""
    settings = replace(case.client.settings, saved_rules_path=str(path))
    case.client.settings = case.client.cleanup.settings = settings
    case.store = path


def point(link, target):
    link.unlink()
    link.symlink_to(target)


def linked_store(case):
    """The configured store directory is the link selected -> first; second is empty."""
    first, second = case.directory / "first", case.directory / "second"
    first.mkdir()
    second.mkdir()
    selected = case.directory / "selected"
    selected.symlink_to(first)
    configure(case, selected / "saved-rules.json")
    return selected, first, second


@pytest.mark.parametrize("previewed", ["plain", "dotted"])
def test_a_link_and_dotdot_spelling_of_another_store_cannot_redeem_its_token(case, previewed):
    first, nested = case.directory / "first", case.directory / "second" / "nested"
    first.mkdir()
    nested.mkdir(parents=True)
    (first / "linked").symlink_to(nested)
    # One path to abspath; first's and second's store to the kernel.
    spellings = {
        "plain": first / "saved-rules.json",
        "dotted": first / "linked" / ".." / "saved-rules.json",
    }
    configure(case, spellings[previewed])
    token = preview(case)["token"]
    configure(case, spellings["dotted" if previewed == "plain" else "plain"])
    with pytest.raises(MailValidationError, match="store changed after preview; preview again"):
        case.client.add_rule(token)
    assert os.listdir(first) == ["linked"] and os.listdir(nested.parent) == ["nested"]
    assert case.tokens.inspect(token, ACTION) and not case.log.path.exists()


def test_a_store_link_retargeted_during_confirmation_refuses_and_keeps_the_token(case):
    selected, first, second = linked_store(case)
    mcp = create_mcp(case.client.settings, case.client)
    previewed = call(mcp, "preview_add_rule", ARGUMENTS)
    host = Host(during=lambda: point(selected, second))
    with pytest.raises(ToolError, match="store changed after preview; preview again"):
        call(mcp, ACTION, {"token": previewed["token"]}, host)
    assert len(host.prompts) == 1
    # Refused before the journal, the claim or any write, in either directory.
    assert os.listdir(first) == [] and os.listdir(second) == []
    assert [row for row in records(case) if row["action"] == ACTION] == []
    started, refused = audits(case.log.path)[-1]
    assert (started["outcome"], refused["outcome"]) == ("started", "refused")
    assert case.tokens.describe(previewed["token"], ACTION)
    # Unconsumed, the token still saves into exactly the store it bound.
    point(selected, first)
    result = call(mcp, ACTION, {"token": previewed["token"]}, Host())
    assert result["added"] is True and result["token_consumed"] is True
    assert saved(case) == (spec(),) and os.listdir(second) == []


def test_a_store_link_retargeted_during_the_save_cannot_move_it(case, monkeypatch):
    selected, first, second = linked_store(case)
    token = preview(case)["token"]
    # second already uses the name, so reading there instead would refuse.
    LocalRulesBackend(second / "saved-rules.json").create_filter(
        NAME, render_script(spec()), timeout=5
    )
    before = (second / "saved-rules.json").read_bytes()
    read = LogTransaction.records

    def retarget(transaction):
        # The first journal read comes just after the store was bound.
        point(selected, second)
        return read(transaction)

    monkeypatch.setattr(LogTransaction, "records", retarget)
    result = case.client.add_rule(token)
    assert result["added"] is True and result["partial"] is False
    assert load_saved(first / "saved-rules.json")[0] == (spec(),)
    assert (second / "saved-rules.json").read_bytes() == before


def test_a_store_directory_replaced_after_preview_refuses_and_keeps_the_token(case):
    state = case.directory / "state"
    state.mkdir()
    configure(case, state / "saved-rules.json")
    token = preview(case)["token"]
    state.rename(case.directory / "old-state")
    state.mkdir()
    with pytest.raises(MailValidationError, match="store changed after preview; preview again"):
        case.client.add_rule(token)
    assert os.listdir(state) == [] and os.listdir(case.directory / "old-state") == []
    assert case.tokens.inspect(token, ACTION) and not case.log.path.exists()


def test_a_store_directory_missing_at_preview_is_created_by_the_save(case):
    configure(case, case.directory / "new" / "state" / "saved-rules.json")
    token = preview(case)["token"]
    assert not (case.directory / "new").exists()
    assert case.client.add_rule(token)["added"] is True
    assert saved(case) == (spec(),) and has_mode(case.store.parent, 0o700)


def replace_directory(directory, moved):
    """Move a directory away and make another one in its place."""
    directory.rename(moved)
    directory.mkdir()


def missing_store(case):
    """The store's directory parent/missing-child is missing; parent exists."""
    parent = case.directory / "parent"
    parent.mkdir()
    configure(case, parent / "missing-child" / "saved-rules.json")
    return parent, case.directory / "old-parent"


def test_a_missing_store_directorys_parent_replaced_during_confirmation_refuses(case):
    parent, old = missing_store(case)
    mcp = create_mcp(case.client.settings, case.client)
    previewed = call(mcp, "preview_add_rule", ARGUMENTS)
    host = Host(during=lambda: replace_directory(parent, old))
    with pytest.raises(ToolError, match="store changed after preview; preview again"):
        call(mcp, ACTION, {"token": previewed["token"]}, host)
    assert len(host.prompts) == 1
    # Refused before the journal, the claim or any write, in either directory.
    assert os.listdir(parent) == [] and os.listdir(old) == []
    assert [row for row in records(case) if row["action"] == ACTION] == []
    started, refused = audits(case.log.path)[-1]
    assert (started["outcome"], refused["outcome"]) == ("started", "refused")
    assert case.tokens.describe(previewed["token"], ACTION)
    # Unconsumed, the token still saves below exactly the directory it bound.
    parent.rmdir()
    old.rename(parent)
    result = call(mcp, ACTION, {"token": previewed["token"]}, Host())
    assert result["added"] is True and result["token_consumed"] is True
    assert saved(case) == (spec(),)


@pytest.mark.parametrize("moment", ["bound", "write"])
def test_a_missing_store_directorys_parent_replaced_during_the_save_cannot_move_it(
    case, monkeypatch, moment
):
    parent, old = missing_store(case)
    token = preview(case)["token"]
    replaced = []

    def replace():
        if not replaced:
            replace_directory(parent, old)
            # Its store already uses the name, so reading or saving there would refuse.
            LocalRulesBackend(case.store).create_filter(NAME, render_script(spec()), timeout=5)
            replaced.append(case.store.read_bytes())

    if moment == "bound":
        read = LogTransaction.records

        def bound_then_replaced(transaction):
            # The first journal read comes just after the store was bound.
            replace()
            return read(transaction)

        monkeypatch.setattr(LogTransaction, "records", bound_then_replaced)
    else:
        backend = LocalRulesBackend(case.store)
        create = backend.create_filter

        def replaced_before_the_write(name, script, *, timeout):
            # After the bind, the journaled intent and the claim.
            replace()
            return create(name, script, timeout=timeout)

        backend.create_filter = replaced_before_the_write
        monkeypatch.setattr(saved_rules, "LocalRulesBackend", lambda path: backend)
    result = case.client.add_rule(token)
    assert result["added"] is True and result["partial"] is False
    assert load_saved(old / "missing-child" / "saved-rules.json")[0] == (spec(),)
    assert case.store.read_bytes() == replaced[0]


def test_rules_previewed_while_the_store_directory_is_missing_all_save_there(case):
    parent, old = missing_store(case)
    first, second, third = (preview(case, name=name)["token"] for name in ("1st", "2nd", "3rd"))
    assert case.client.add_rule(first)["added"] is True
    assert has_mode(case.store.parent, 0o700)
    # Previewed before the first save made the directory, below the same parent.
    assert case.client.add_rule(second)["added"] is True
    assert [rule.name for rule in saved(case)] == ["1st", "2nd"]
    # That parent replaced since: refused before the journal, keeping the token.
    replace_directory(parent, old)
    with pytest.raises(MailValidationError, match="store changed after preview; preview again"):
        case.client.add_rule(third)
    assert case.tokens.inspect(third, ACTION) and os.listdir(parent) == []
    assert len([row for row in records(case) if row["action"] == ACTION]) == 2


def test_rules_previewed_before_a_fresh_state_directory_exists_all_save(case):
    # The default store beside a journal whose directory is missing: the first
    # add's own audit record makes that directory, after both previews.
    journal = case.directory / "fresh" / "state" / "actions.jsonl"
    client = make_client(case.sim, case.directory, action_log=journal, enable_rules=True)
    mcp = create_mcp(client.settings, client)
    tokens = [
        call(mcp, "preview_add_rule", ARGUMENTS | {"name": name})["token"]
        for name in ("1st", "2nd")
    ]
    assert not (case.directory / "fresh").exists()
    for token in tokens:
        result = call(mcp, ACTION, {"token": token}, Host())
        assert result["added"] is True and result["partial"] is False
    store = store_path(client.settings)
    assert store == journal.with_name("saved-rules.json")
    assert [rule.name for rule in load_saved(store)[0]] == ["1st", "2nd"]
    assert has_mode(store.parent, 0o700)


def test_the_sieve_backend_stays_unavailable_and_fails_closed(case, monkeypatch):
    token = preview(case)["token"]
    monkeypatch.setattr(saved_rules, "LocalRulesBackend", lambda path: UnavailableSieveBackend())
    refusal = "Rules cannot be saved: Proton Mail Bridge offers mail clients only IMAP and SMTP"
    for attempt in (
        lambda: preview(case, name="Other"),
        case.client.add_rule_ready,
        lambda: case.client.add_rule(token),
    ):
        with pytest.raises(MailValidationError, match=refusal):
            attempt()
    assert not case.store.exists() and not case.log.path.exists()
    assert case.tokens.inspect(token, ACTION)
    # Sieve filter creation itself still refuses, unchanged.
    case.client.sieve_backend = None
    sieve_token = case.client.preview_sieve_filter(**ARGUMENTS)["token"]
    with pytest.raises(MailValidationError, match="Sieve filters cannot be applied"):
        case.client.create_sieve_filter(sieve_token)


def test_a_selected_sieve_backend_would_receive_exactly_the_reviewed_script(case, monkeypatch):
    backend = FakeSieveBackend()
    monkeypatch.setattr(saved_rules, "LocalRulesBackend", lambda path: backend)
    result = preview(case)
    assert result["apply"]["backend"] == "fake"
    assert case.tokens.inspect(result["token"], ACTION).params["store"] is None
    added = case.client.add_rule(result["token"])
    assert added["added"] is True and added["backend"] == "fake"
    assert backend.created == [(NAME, result["script"])] and not case.store.exists()


def test_unwritable_journal_refuses_before_any_save_and_keeps_the_token(case):
    token = preview(case)["token"]
    blocked = case.directory / "blocked-log"
    blocked.mkdir()
    case.client.cleanup.backend.log = ActionLog(blocked)
    with pytest.raises(MailError, match="action log is unavailable"):
        case.client.add_rule(token)
    assert not case.store.exists() and case.tokens.inspect(token, ACTION)


def test_failed_intent_append_refuses_before_any_save(case, monkeypatch):
    token = preview(case)["token"]

    def unavailable(*args, **kwargs):
        raise MailError("Synthetic unavailable journal")

    monkeypatch.setattr(LogTransaction, "append", unavailable)
    with pytest.raises(MailError):
        case.client.add_rule(token)
    assert not case.store.exists() and case.tokens.inspect(token, ACTION)


def test_a_failed_store_write_is_journaled_as_uncertain_without_its_text(case, monkeypatch):
    token = preview(case)["token"]

    def broken(*args, **kwargs):
        raise OSError("private disk detail")

    monkeypatch.setattr(rules_store.os, "replace", broken)
    result = case.client.add_rule(token)
    assert result["partial"] and result["token_consumed"] and not result["added"]
    assert result["attempted"] is True
    assert result["error"].startswith("Saving the rule had an uncertain outcome; check list_rules")
    assert records(case)[0]["messages"][0]["status"] == "uncertain"
    assert "private disk detail" not in str(result) + case.log.path.read_text()
    assert not case.store.exists()


@pytest.mark.parametrize("failure", ["intent", "outcome"])
def test_journal_progress_failure_never_hides_the_save_outcome(case, monkeypatch, failure):
    token = preview(case)["token"]
    append = LogTransaction.append

    def fail_progress(transaction, record):
        if record["action"] == "execute_progress" and record["message"]["status"] == (
            "uncertain" if failure == "intent" else "done"
        ):
            raise MailError("Synthetic journal failure")
        return append(transaction, record)

    monkeypatch.setattr(LogTransaction, "append", fail_progress)
    result = case.client.add_rule(token)
    assert result["partial"] and result["token_consumed"]
    assert result["added"] is result["attempted"] is (failure == "outcome")
    assert saved(case) == ((spec(),) if failure == "outcome" else ())
    assert result["error"].startswith(
        "Rule was saved, but its outcome could not be journaled"
        if failure == "outcome"
        else "Saving stopped before the rules backend was called, so no rule was saved"
    )


def test_concurrent_cleanup_refuses_saving(case):
    token = preview(case)["token"]
    lock = case.client.cleanup._mutation_lock
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(MailValidationError, match="Another cleanup operation is running"):
            case.client.add_rule(token)
    finally:
        lock.release()
    assert not case.store.exists() and case.tokens.inspect(token, ACTION)


def call(mcp, tool, arguments, context=None):
    return asyncio.run(mcp._tool_manager.call_tool(tool, arguments, context=context))


def audits(path):
    with ActionLog(path).transaction(readonly=True) as transaction:
        rows = [row for row in transaction.records() if row.get("action") == "audit"]
    calls = {}
    for row in rows:
        calls.setdefault(row["audit_id"], []).append(row)
    return list(calls.values())


def test_tools_are_listed_with_their_annotations_and_exact_arguments(case):
    mcp = create_mcp(case.client.settings, case.client)
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    previewing, adding, listing = tools["preview_add_rule"], tools[ACTION], tools["list_rules"]
    assert previewing.annotations.readOnlyHint is True
    assert previewing.annotations.destructiveHint is False
    assert set(previewing.parameters["properties"]) == {
        "name",
        "folder",
        "conditions",
        "match",
        "mark_read",
        "include_matches",
    }
    assert set(previewing.parameters["required"]) == {"name", "folder", "conditions"}
    assert listing.annotations.readOnlyHint is True and listing.parameters["properties"] == {}
    assert adding.annotations.readOnlyHint is False
    assert adding.annotations.destructiveHint is True
    assert adding.annotations.openWorldHint is False
    assert set(adding.parameters["properties"]) == {"token"}
    assert adding.parameters["additionalProperties"] is False
    assert "only call after explicit approval of its preview" in " ".join(
        adding.description.split()
    )
    for extra in ({"name": "Other"}, {"conditions": []}, {"confirm": True}):
        with pytest.raises(ToolError):
            call(mcp, ACTION, {"token": "A" * 43} | extra, Host())
    assert not case.store.exists()


def test_host_form_shows_the_exact_rule_before_the_single_save(case):
    mcp = create_mcp(case.client.settings, case.client)
    declined = Host("decline")
    previewed = call(mcp, "preview_add_rule", ARGUMENTS | {"mark_read": True})
    with pytest.raises(ToolError, match="User confirmation was not accepted"):
        call(mcp, ACTION, {"token": previewed["token"]}, declined)
    assert not case.store.exists()
    with pytest.raises(ToolError, match="already used"):
        call(mcp, ACTION, {"token": previewed["token"]}, Host())
    previewed = call(mcp, "preview_add_rule", ARGUMENTS | {"mark_read": True})
    host = Host()
    result = call(mcp, ACTION, {"token": previewed["token"]}, host)
    assert result["added"] is True and saved(case) == (spec(mark_read=True),)
    [prompt] = host.prompts
    sentence, details = prompt.split("\n", 1)
    assert previewed["action_hash"][:16] in sentence
    assert f'Save the folder rule "{NAME}": when rules run, matching INBOX mail' in sentence
    assert 'moves to "Folders/GitHub" (apply_rules also marks it read).' in sentence
    shown = json.loads(details)
    assert {key: shown[key] for key in ("name", "folder", "conditions", "match", "mark_read")} == (
        spec(mark_read=True).params()
    )
    assert shown["rule_sha256"] == previewed["rule_sha256"] and shown["backend"] == "local_rules"
    assert previewed["token"] not in prompt and "script" not in shown
    started, finished = audits(case.log.path)[-1]
    assert (started["outcome"], finished["outcome"]) == ("started", "completed")
    assert finished["rule_sha256"] == previewed["rule_sha256"]
    assert finished["counts"] == {"approved": 1, "done": 1, "uncertain": 0, "remaining": 0}
    assert finished["action_hash"] == previewed["action_hash"]
    assert case.client.list_actions(action=ACTION)["audit"][0]["outcome"] == "completed"
    with pytest.raises(ToolError, match="already used"):
        call(mcp, ACTION, {"token": previewed["token"]}, Host())


def test_a_full_store_refuses_before_any_form_and_audits_the_refusal(case):
    mcp = create_mcp(case.client.settings, case.client)
    previewed = call(mcp, "preview_add_rule", ARGUMENTS)
    fill(case)
    host = Host()
    with pytest.raises(ToolError, match="Rules cannot be saved: the saved rules store already"):
        call(mcp, ACTION, {"token": previewed["token"]}, host)
    assert host.prompts == []
    [(started, refused)] = audits(case.log.path)
    assert (started["outcome"], refused["outcome"]) == ("started", "refused")
    assert refused["tool"] == ACTION and "already holds 20 rules" in refused["reason"]
    assert refused["action_hash"] == previewed["action_hash"]
    # Still live and unconfirmed: the refusal neither consumed nor retired it.
    assert case.tokens.describe(previewed["token"], ACTION).confirmed is False


def test_a_store_failure_through_the_server_is_audited_as_uncertain(case, monkeypatch):
    mcp = create_mcp(replace(case.client.settings, confirm_mutations=False), case.client)
    previewed = call(mcp, "preview_add_rule", ARGUMENTS)

    def broken(*args, **kwargs):
        raise OSError("private disk detail")

    monkeypatch.setattr(rules_store.os, "replace", broken)
    result = call(mcp, ACTION, {"token": previewed["token"]}, Host(support=False))
    assert result["partial"] is True and result["added"] is False and result["attempted"] is True
    finished = audits(case.log.path)[-1][-1]
    assert finished["outcome"] == "partial"
    assert finished["counts"] == {"approved": 1, "done": 0, "uncertain": 1, "remaining": 0}
    assert "private disk detail" not in case.log.path.read_text()


# The audited outcome of one approval when a journal append fails: before the
# save (its intent record, then its uncertain progress line) or after the store
# kept the rule (its done line, then the call's execute_result).
JOURNAL_FAILURES = {
    ACTION: ("failed", None),
    "uncertain": ("partial", {"approved": 1, "done": 0, "uncertain": 0, "remaining": 1}),
    "done": ("partial", {"approved": 1, "done": 1, "uncertain": 0, "remaining": 0}),
    "execute_result": ("partial", {"approved": 1, "done": 1, "uncertain": 0, "remaining": 0}),
}


@pytest.mark.parametrize("failure", list(JOURNAL_FAILURES))
def test_journal_failures_around_the_save_are_audited_once(case, monkeypatch, failure):
    mcp = create_mcp(case.client.settings, case.client)
    previewed = call(mcp, "preview_add_rule", ARGUMENTS)
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
        assert result["added"] is (counts["done"] == 1)
        assert result["attempted"] is (counts["remaining"] == 0)
    assert len(host.prompts) == 1
    assert saved(case) == ((spec(),) if counts and counts["done"] else ())
    started, finished = audits(case.log.path)[-1]
    assert (started["outcome"], finished["outcome"]) == ("started", outcome)
    assert finished["action_hash"] == previewed["action_hash"]
    assert finished.get("counts") == counts
    if counts is not None:
        assert counts["done"] + counts["uncertain"] + counts["remaining"] == 1
        assert finished["rule_sha256"] == previewed["rule_sha256"]


def test_deadline_expiring_just_before_the_save_is_audited_as_remaining(case, monkeypatch):
    mcp = create_mcp(case.client.settings, case.client)
    previewed = call(mcp, "preview_add_rule", ARGUMENTS)
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
    assert result["attempted"] is False and result["added"] is False
    assert result["partial"] is True and result["token_consumed"] is True
    assert result["error"].startswith("Saving stopped before the rules backend was called")
    assert not case.store.exists()
    [record] = [row for row in records(case) if row["action"] == ACTION]
    assert record["messages"][0]["status"] == "uncertain"
    finished = audits(case.log.path)[-1][-1]
    assert finished["counts"] == {"approved": 1, "done": 0, "uncertain": 0, "remaining": 1}


@pytest.mark.parametrize("option", ["mark_read", "include_matches"])
@pytest.mark.parametrize("value", ["yes", "true", "1", 1, 0, None, [True]])
def test_switches_must_be_json_booleans_at_the_mcp_boundary(case, option, value):
    mcp = create_mcp(case.client.settings, case.client)
    tool = mcp._tool_manager.get_tool("preview_add_rule")
    metadata = tool.fn_metadata
    with pytest.raises(ValidationError):
        metadata.arg_model.model_validate(metadata.pre_parse_json(ARGUMENTS | {option: value}))
    with pytest.raises(ToolError):
        call(mcp, "preview_add_rule", ARGUMENTS | {option: value})
    for flag in (True, False):
        model = metadata.arg_model.model_validate(ARGUMENTS | {option: flag})
        assert getattr(model, option) is flag
    assert tool.parameters["properties"][option]["type"] == "boolean"
    assert not case.sim.commands and not case.tokens._grants
