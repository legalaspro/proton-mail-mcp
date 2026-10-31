"""Rules source boundaries use synthetic command-recording mailboxes only."""

import json
from copy import deepcopy

import pytest
from test_folder_rules import rule
from test_rules_timer import arrive, baseline, changed, mark
from test_rules_timer import timer_case as timer_case

from proton_mail_mcp import rules_timer
from proton_mail_mcp.folder_rules import FolderRulesService, load_rules
from proton_mail_mcp.mail import MailValidationError

DISALLOWED_SOURCES = [
    "Drafts",
    "Sent",
    "drafts",
    "sEnT",
    " Drafts ",
    "\tSent\n",
    "Ｄｒａｆｔｓ",
    "Ｓｅｎｔ",
    "Drаfts",  # Cyrillic a.
    "Sеnt",  # Cyrillic e.
    "Draftſ",
    "Dr\u200bafts",
    "Folders/Drafts",
    "Folders/Sent",
    "Folders/Ｄｒａｆｔｓ",
    "Folders/Drаfts",
    "folders/Sent",
    "Folders/Other",
    "Folders/Brouillons",
    "Folders/Envoyes",
    "Brouillons",
    "Envoyés",
    "All Mail",
    "Starred",
    "Labels/Review",
    "Trash",
    "Spam",
    "Archive",
    "Unknown",
    "Folders/Unknown",
    " INBOX",
    "INBOX ",
    "Folders/INBOX",
    "INBOX/Child",
    " INBOX ",
    "ＩＮＢＯＸ",
    "ІNBOX",  # Cyrillic I.
    r"\Drafts",
    r"\Sent",
    r"\All",
    r"\Flagged",
    r"\Noselect",
]
BAD_FLAGS = [
    r"\Drafts",
    r"\dRaFtS",
    r"\Sent",
    r"\sEnT",
    r"\All",
    r"\Flagged",
    r"\Noselect",
    r"\Trash",
    r"\Junk",
    r"\Archive",
    r"\NonExistent",
]


def source_case(timer_case, source="INBOX", flags=None):
    case = timer_case()
    baseline(case)
    if source != "INBOX":
        # Use an existing synthetic mailbox for even unknown source identities:
        # refusing must depend on policy, not accidental mailbox absence.
        case.fake.folders.setdefault(source, {})
    arrival = arrive(case, 3)
    if source != "INBOX":
        case.fake.folders[source]["3"] = arrival
        del case.fake.folders["INBOX"]["3"]
    if flags:
        case.fake.folder_flags[source] = flags
    before = case.state.read_bytes()
    case.fake.calls.clear()
    return case, before


def assert_untouched(case, before):
    assert case.state.read_bytes() == before
    assert changed(case) == []
    assert not any(call[0] in {"SELECT", "EXAMINE"} for call in case.fake.calls)


def run_cli(case, monkeypatch, source):
    monkeypatch.setattr("sys.argv", ["proton-mail-mcp-rules-run", "--folder", source])
    monkeypatch.setattr(
        rules_timer.Settings, "from_env", staticmethod(lambda values: case.cleanup.settings)
    )
    monkeypatch.setattr(rules_timer, "MailClient", lambda settings: case.cleanup.client)
    monkeypatch.setattr(rules_timer, "RulesTimer", lambda cleanup: case.timer)
    with pytest.raises(SystemExit) as result:
        rules_timer.main()
    return result.value.code


@pytest.mark.parametrize("source", DISALLOWED_SOURCES)
def test_runner_refuses_source_before_select_and_preserves_mark(timer_case, caplog, source):
    case, before = source_case(timer_case, source)
    with pytest.raises(MailValidationError, match="source"):
        case.timer.run(folder=source)
    assert_untouched(case, before)
    assert "source" in caplog.text.lower()


@pytest.mark.parametrize("source", DISALLOWED_SOURCES)
def test_real_cli_refuses_source_nonzero_with_no_mailbox_changes(
    timer_case, monkeypatch, capsys, source
):
    case, before = source_case(timer_case, source)
    assert run_cli(case, monkeypatch, source) != 0
    output = capsys.readouterr()
    assert not output.out and "failed" in output.err.lower()
    assert_untouched(case, before)


@pytest.mark.parametrize("source", ["Drafts", "Sent"])
def test_cli_source_guard_runs_before_settings_or_runner(timer_case, monkeypatch, source):
    case, before = source_case(timer_case, source)
    reached = []
    monkeypatch.setattr("sys.argv", ["proton-mail-mcp-rules-run", "--folder", source])

    def settings(values):
        reached.append("settings")
        return case.cleanup.settings

    def permissive_run(**kwargs):
        reached.append("runner")
        return {"status": "completed", "count": 0, "log_ids": []}

    monkeypatch.setattr(rules_timer.Settings, "from_env", staticmethod(settings))
    monkeypatch.setattr(rules_timer, "MailClient", lambda settings: case.cleanup.client)
    monkeypatch.setattr(rules_timer, "RulesTimer", lambda cleanup: case.timer)
    monkeypatch.setattr(case.timer, "run", permissive_run)
    with pytest.raises(SystemExit) as result:
        rules_timer.main()
    assert result.value.code != 0 and reached == []
    assert_untouched(case, before)


@pytest.mark.parametrize("source", ["Drafts", "Sent"])
def test_runner_guard_precedes_private_run_and_first_baseline(timer_case, monkeypatch, source):
    case = timer_case()
    reached = []

    def permissive_run(*args):
        reached.append(args)
        return {"status": "initialized", "count": 0, "log_ids": []}

    monkeypatch.setattr(case.timer, "_run", permissive_run)
    with pytest.raises(MailValidationError, match="source"):
        case.timer.run(folder=source)
    assert reached == [] and changed(case) == [] and not case.state.exists()


@pytest.mark.parametrize("flags", BAD_FLAGS)
@pytest.mark.parametrize("entrypoint", ["runner", "cli", "preview", "apply"])
def test_inbox_special_use_flags_refuse_every_rules_path_before_select(
    timer_case, monkeypatch, capsys, flags, entrypoint
):
    case = timer_case()
    service = FolderRulesService(case.cleanup, case.rules)
    token = service.preview_rules()["token"] if entrypoint == "apply" else None
    baseline(case)
    arrive(case, 3)
    before = case.state.read_bytes()
    case.fake.folder_flags["INBOX"] = flags
    case.fake.calls.clear()
    if entrypoint == "cli":
        assert run_cli(case, monkeypatch, "INBOX") != 0
        output = capsys.readouterr()
        assert not output.out and "failed" in output.err.lower()
    elif entrypoint == "preview":
        preview = service.preview_rules()
        assert preview["blocked"] and not preview["token"] and preview["count"] == 0
    else:
        with pytest.raises(MailValidationError, match="source"):
            if entrypoint == "runner":
                case.timer.run()
            else:
                service.apply_rules(token)
    assert_untouched(case, before)


@pytest.mark.parametrize(
    "source,flags",
    [("Folders/Brouillons", r"\Drafts"), ("Folders/Envoyes", r"\Sent")],
)
def test_renamed_localized_special_folders_never_become_cli_sources(
    timer_case, monkeypatch, capsys, source, flags
):
    case, before = source_case(timer_case, source, flags)
    assert run_cli(case, monkeypatch, source) != 0
    assert "failed" in capsys.readouterr().err.lower()
    assert_untouched(case, before)


@pytest.mark.parametrize("alias", ["INBOX", "inbox", "InBoX"])
@pytest.mark.parametrize("flags", [r"\Drafts", r"\Sent"])
def test_conflicting_duplicate_list_flags_cannot_hide_protected_inbox(
    timer_case, monkeypatch, alias, flags
):
    case, before = source_case(timer_case)
    original = case.fake.list

    def conflicting():
        status, entries = original()
        return status, [*entries, f'({flags}) "/" "{alias}"'.encode()]

    monkeypatch.setattr(case.fake, "list", conflicting)
    with pytest.raises(MailValidationError, match="source"):
        case.timer.run()
    assert_untouched(case, before)


def test_nonexistent_inbox_is_refused_before_select(timer_case):
    case, before = source_case(timer_case)
    del case.fake.folders["INBOX"]
    with pytest.raises(MailValidationError, match="source"):
        case.timer.run()
    assert_untouched(case, before)


@pytest.mark.parametrize("source", DISALLOWED_SOURCES)
def test_preview_lists_disallowed_source_as_blocked_without_token(timer_case, source):
    case, before = source_case(timer_case, source)
    preview = FolderRulesService(case.cleanup, case.rules).preview_rules(folder=source)
    assert preview["blocked"] and "source" in preview["blocked"][0]["reason"].lower()
    assert preview["token"] is None and preview["count"] == 0
    assert preview["matched_count"] == 0
    assert_untouched(case, before)


@pytest.mark.parametrize("source", ["Drafts", "Sent", "Folders/Other", "Labels/Review"])
def test_apply_refuses_legacy_disallowed_selector_without_consuming_token(timer_case, source):
    case = timer_case()
    service = FolderRulesService(case.cleanup, case.rules)
    preview = service.preview_rules()
    grant = case.cleanup.tokens.inspect(preview["token"], "apply_rules")
    token, _ = case.cleanup.tokens.issue(
        "apply_rules", grant.params, {"folder": source}, grant.messages
    )
    baseline(case)
    before = case.state.read_bytes()
    case.fake.calls.clear()
    with pytest.raises(MailValidationError, match="source"):
        service.apply_rules(token)
    assert case.cleanup.tokens.inspect(token, "apply_rules")
    assert_untouched(case, before)


@pytest.mark.parametrize("source", ["Drafts", "Sent"])
@pytest.mark.parametrize("source_key", ["folder", "source_folder"])
def test_apply_refuses_entire_mixed_plan_before_execution(
    timer_case, monkeypatch, source, source_key
):
    case = timer_case()
    service = FolderRulesService(case.cleanup, case.rules)
    grant = case.cleanup.tokens.inspect(service.preview_rules()["token"], "apply_rules")
    rows = deepcopy(grant.messages)
    rows[1][source_key] = source
    token, _ = case.cleanup.tokens.issue("apply_rules", grant.params, grant.selector, rows)
    reached = []

    def reviewed_legacy_plan(*args, **kwargs):
        return [rows], [{"matched_count": len(rows)}]

    def execution(*args, **kwargs):
        reached.append(len(args[4]))
        return {"done": len(rows), "uncertain": 0, "partial": False, "log_ids": [], "messages": []}

    # Reproduce an already-issued mixed plan, independently of the new planner:
    # confirmation must validate every source before handing any group to execution.
    monkeypatch.setattr(service, "_plan", reviewed_legacy_plan)
    monkeypatch.setattr(case.cleanup, "_execute_chunks", execution)
    baseline(case)
    before = case.state.read_bytes()
    case.fake.calls.clear()
    with pytest.raises(MailValidationError, match="source"):
        service.apply_rules(token)
    assert reached == [] and case.cleanup.tokens.inspect(token, "apply_rules")
    assert_untouched(case, before)


@pytest.mark.parametrize("source", ["Drafts", "Sent", "Folders/Other", "Labels/Review"])
@pytest.mark.parametrize("source_key", ["source", "source_folder"])
def test_rules_file_source_fields_block_preview_and_entire_apply(timer_case, source, source_key):
    case = timer_case()
    service = FolderRulesService(case.cleanup, case.rules)
    token = service.preview_rules()["token"]
    baseline(case)
    before = case.state.read_bytes()
    case.rules.write_text(
        json.dumps({"version": 1, "rules": [rule(), {**rule(), source_key: source}]})
    )
    case.fake.calls.clear()
    preview = FolderRulesService(case.cleanup, case.rules).preview_rules()
    assert preview["blocked"] and not preview["token"] and preview["count"] == 0
    with pytest.raises(MailValidationError, match="source"):
        service.apply_rules(token)
    with pytest.raises(MailValidationError, match="source"):
        load_rules(case.rules)
    assert_untouched(case, before)


@pytest.mark.parametrize("source_key", ["source", "source_folder", "folder"])
def test_document_source_override_cannot_escape_preview_or_apply(timer_case, source_key):
    case = timer_case()
    service = FolderRulesService(case.cleanup, case.rules)
    token = service.preview_rules()["token"]
    baseline(case)
    before = case.state.read_bytes()
    case.rules.write_text(json.dumps({"version": 1, "rules": [rule()], source_key: "Drafts"}))
    case.fake.calls.clear()
    preview = service.preview_rules()
    assert preview["blocked"] and not preview["token"] and preview["count"] == 0
    with pytest.raises(MailValidationError, match="source"):
        service.apply_rules(token)
    assert_untouched(case, before)


@pytest.mark.parametrize("failure", ["missing", r"\Drafts", r"\Sent", r"\NonExistent"])
def test_empty_rules_still_validate_inbox_metadata(timer_case, failure):
    case = timer_case(rules=[])
    service = FolderRulesService(case.cleanup, case.rules)
    if failure == "missing":
        del case.fake.folders["INBOX"]
    else:
        case.fake.folder_flags["INBOX"] = failure
    preview = service.preview_rules()
    assert preview["blocked"] and not preview["token"] and preview["count"] == 0
    assert changed(case) == [] and not case.state.exists()
    assert not any(call[0] in {"SELECT", "EXAMINE"} for call in case.fake.calls)


def test_refusal_logs_do_not_echo_untrusted_source(timer_case, monkeypatch, capsys, caplog):
    source = "synthetic-private-marker\nforged-log-entry"
    case, before = source_case(timer_case, source)
    assert run_cli(case, monkeypatch, source) != 0
    output = capsys.readouterr()
    assert "source" in caplog.text.lower()
    for value in (source, "synthetic-private-marker", "forged-log-entry"):
        assert value not in output.out + output.err + caplog.text
    assert_untouched(case, before)


def test_rules_file_cannot_override_source_even_with_inbox(timer_case):
    case = timer_case(rules=[{**rule(), "source": "INBOX"}])
    preview = FolderRulesService(case.cleanup, case.rules).preview_rules()
    assert preview["blocked"] and not preview["token"] and preview["count"] == 0
    assert changed(case) == [] and case.fake.calls == []


def test_real_cli_inbox_moves_only_matching_new_message(timer_case, monkeypatch, capsys):
    case = timer_case(rules=[rule("subject", "Wanted")])
    case.fake.folders["INBOX"]["1"].headers = case.fake.folders["INBOX"]["1"].headers.replace(
        b"Hello", b"Wanted"
    )
    baseline(case)
    assert run_cli(case, monkeypatch, "INBOX") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    old = dict(case.fake.folders["INBOX"])
    matching = arrive(case, 3, subject="Wanted", seen=False)
    unmatched = arrive(case, 4, subject="Unrelated", seen=True)
    case.fake.calls.clear()
    assert run_cli(case, monkeypatch, "INBOX") == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1
    assert case.fake.folders["INBOX"] == {**old, "4": unmatched}
    assert list(case.fake.folders["Folders/Review"].values()) == [matching]
    assert [call[0] for call in changed(case)] == ["MOVE"]
    assert changed(case)[0][1] == "3" and mark(case)["uid"] == 4
    assert matching.flags == set() and unmatched.flags == {r"\Seen"}
