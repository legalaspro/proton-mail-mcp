"""Only ASCII INBOX aliases with benign LIST metadata authorize rules."""

import json
import re

import pytest
from test_cleanup_mutations import uid_values
from test_rules_timer import arrive, baseline, changed
from test_rules_timer import timer_case as timer_case

from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.rules_source import require_rules_source

LIST_INBOX = [
    ("INBOX", b'(\\HasNoChildren) "/" "INBOX"'),
    ("INBOX", b'(\\HasNoChildren \\Unmarked) "/" "INBOX"'),
    ("Inbox", b'(\\Unmarked) "." "Inbox"'),
    ("INBOX", b'(\\HasChildren \\Marked) "/" INBOX'),
    ("inbox", b'(\\Inbox \\HasChildren \\UnknownUse) "/" "inbox"'),
]
REFUSED_FLAGS = [
    r"\Drafts",
    r"\Sent",
    r"\Junk",
    r"\Trash",
    r"\All",
    r"\Noselect",
    r"\NonExistent",
    r"\Archive",
    r"\Flagged",
]


@pytest.mark.parametrize("flag", REFUSED_FLAGS)
def test_rules_source_rejects_each_special_use_before_select(timer_case, flag):
    case = timer_case()
    case.fake.folder_flags["INBOX"] = flag
    with pytest.raises(MailValidationError, match="source"):
        require_rules_source("INBOX", case.fake)
    assert not any(call[0] in {"SELECT", "EXAMINE"} for call in case.fake.calls)


def inbox_reply(case, monkeypatch, name, reply):
    """Keep SELECT strict so callers must use the mailbox name supplied by LIST."""
    fake = case.fake
    fake.folders[name] = fake.folders.pop("INBOX")
    original_list, original_uid = fake.list, fake.uid

    def listing():
        status, entries = original_list()
        return status, [
            entry for entry in entries if not entry.endswith(('"' + name + '"').encode())
        ] + [reply]

    def uid(command, *args):
        if command == "STORE":
            fake.calls.append((command, *args))
            assert args[1:] == ("+FLAGS.SILENT", r"(\Seen)")
            # A batched STORE names an exact UID set.
            for value in uid_values(args[0]):
                item = fake.folders[fake.current][value]
                item.flags = getattr(item, "flags", set()) | {r"\Seen"}
            return "OK", []
        status, data = original_uid(command, *args)
        if command != "FETCH" or "FLAGS" not in args[1]:
            return status, data
        result = []
        for part in data:
            metadata = part[0] if isinstance(part, tuple) else part
            match = re.search(rb"UID (\d+)", metadata)
            if match:
                item = fake.folders[fake.current][match[1].decode()]
                flags = " ".join(sorted(getattr(item, "flags", set()))).encode()
                metadata = metadata.replace(match[0], match[0] + b" FLAGS (" + flags + b")")
            result.append((metadata, part[1]) if isinstance(part, tuple) else metadata)
        return status, result

    monkeypatch.setattr(fake, "list", listing)
    monkeypatch.setattr(fake, "uid", uid)


@pytest.mark.parametrize("caller", ["INBOX", "Inbox", "inbox"])
@pytest.mark.parametrize("name,reply", LIST_INBOX)
def test_bridge_list_rules_preview_apply_read_state(timer_case, monkeypatch, caller, name, reply):
    case = timer_case(count=1)
    item = case.fake.folders["INBOX"]["1"]
    inbox_reply(case, monkeypatch, name, reply)
    service = FolderRulesService(case.cleanup, case.rules)
    preview = service.preview_rules(folder=caller, mark_read=True)
    assert preview["token"] and not preview["blocked"]
    assert preview["count"] == preview["matched_count"] == 1
    assert preview["rules"][0]["count"] == 1
    assert changed(case) == [] and not getattr(item, "flags", set())
    grant = case.cleanup.tokens.inspect(preview["token"], "apply_rules")
    assert {row["folder"] for row in grant.messages} == {name}
    result = service.apply_rules(preview["token"])
    assert result["count"] == 1 and not result["partial"]
    assert not case.fake.folders[name]
    assert list(case.fake.folders["Folders/Review"].values()) == [item]
    assert item.flags == {r"\Seen"}
    selected = [call[1] for call in case.fake.calls if call[0] in {"EXAMINE", "SELECT"}]
    assert '"' + name + '"' in selected
    assert not any(call[0] in {"COPY", "EXPUNGE", "CREATE", "DELETE"} for call in changed(case))


@pytest.mark.parametrize("name,reply", LIST_INBOX)
def test_bridge_list_timer_keeps_canonical_high_water(timer_case, monkeypatch, name, reply):
    case = timer_case(count=1)
    assert baseline(case)["status"] == "completed"
    old = case.fake.folders["INBOX"]["1"]
    added = arrive(case, 2)
    inbox_reply(case, monkeypatch, name, reply)
    result = case.timer.run(folder="iNbOx")
    assert result["status"] == "completed" and result["count"] == 1
    assert case.fake.folders[name] == {"1": old}
    assert list(case.fake.folders["Folders/Review"].values()) == [added]
    state = json.loads(case.state.read_text())
    assert set(state["mailboxes"]) == {"INBOX"}
    assert state["mailboxes"]["INBOX"]["uid"] == 2
    assert not getattr(added, "flags", set())
    assert not any(call[0] == "STORE" for call in changed(case))


@pytest.mark.parametrize("flag", REFUSED_FLAGS)
@pytest.mark.parametrize("entrypoint", ["preview", "apply", "timer"])
def test_inbox_alias_special_use_conflict_refuses_every_phase(
    timer_case, monkeypatch, flag, entrypoint
):
    case = timer_case(count=1)
    inbox_reply(case, monkeypatch, "INBOX", LIST_INBOX[0][1])
    service = FolderRulesService(case.cleanup, case.rules)
    token = service.preview_rules(mark_read=True)["token"]
    original_list = case.fake.list

    def listing():
        status, entries = original_list()
        return status, [*entries, f'({flag.swapcase()}) "/" "Inbox"'.encode()]

    monkeypatch.setattr(case.fake, "list", listing)
    case.fake.calls.clear()
    if entrypoint == "preview":
        result = service.preview_rules(mark_read=True)
        assert result["token"] is None and result["blocked"] and result["count"] == 0
    else:
        with pytest.raises(MailValidationError, match="source"):
            service.apply_rules(token) if entrypoint == "apply" else case.timer.run()
    assert changed(case) == []
    assert not any(call[0] in {"EXAMINE", "SELECT"} for call in case.fake.calls)
    assert not case.state.exists()
