"""Draft deletion uses exact UIDs against an entirely in-memory IMAP fake."""

import json
import re
import socket

import pytest
from test_mail import FakeImap, client, message, unquote

from proton_mail_mcp.action_log import replay_records
from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.protected import ProtectedPolicy
from proton_mail_mcp.security import Credentials

OWN_ADDRESS = "reader@proton.me"


class DraftImap(FakeImap):
    capabilities = (b"IMAP4rev1", b"UIDPLUS")

    def select(self, folder, *, readonly):
        self.current = unquote(folder)
        self.calls.append(("EXAMINE" if readonly else "SELECT", folder, readonly))
        return "OK", [str(len(self.folders[self.current])).encode()]

    def uid(self, command, *args):
        if command in {"STORE", "EXPUNGE"}:
            self.calls.append((command, *args))
            uids = args[0].split(",")
            assert all(re.fullmatch(r"[1-9][0-9]*", uid) for uid in uids)
            if command == "STORE":
                assert args[1:] == ("+FLAGS.SILENT", r"(\Deleted)")
                for uid in uids:
                    item = self.folders[self.current][uid]
                    item.flags = set(getattr(item, "flags", ())) | {r"\Deleted"}
            else:
                assert len(args) == 1
                for uid in uids:
                    item = self.folders[self.current][uid]
                    assert r"\Deleted" in item.flags
                    del self.folders[self.current][uid]
            return "OK", []
        return super().uid(command, *args)

    def expunge(self):
        pytest.fail("Draft deletion must never issue global EXPUNGE")


@pytest.fixture
def case(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("Draft deletion tests must not open network connections")

    monkeypatch.setattr(socket, "create_connection", no_network)
    settings = Settings(
        token="offline-test-token-at-least-thirty-two-characters",
        bridge_user="reader@example.com",
        action_log_path=str(tmp_path / "actions.jsonl"),
        identity_index_enabled=False,
    )
    fake = DraftImap(
        {
            "INBOX": {"8": message("<outside@example.com>")},
            "Drafts": {
                "1": message("<first@example.com>", flags={r"\Draft"}),
                "3": message("<second@example.com>", flags={r"\Draft"}),
                "9": message("<untargeted@example.com>", flags={r"\Deleted"}),
            },
            "Archive": {},
            "Trash": {},
        },
        folder_flags={"Drafts": r"\HasNoChildren \Drafts"},
    )
    return client(settings, fake), fake


def mutations(fake):
    return [call for call in fake.calls if call[0] in {"STORE", "EXPUNGE", "MOVE", "COPY"}]


def records(client):
    with client.cleanup.backend.log.transaction(readonly=True) as transaction:
        return transaction.records()


@pytest.fixture
def own_case(case):
    mail_client, fake = case
    mail_client.credentials_provider = lambda _: Credentials(OWN_ADDRESS, "offline-test-password")
    return mail_client, fake


def set_draft_sender(fake, sender=OWN_ADDRESS, *, extra_headers=b""):
    item = fake.folders["Drafts"]["1"]
    item.headers = extra_headers + item.headers.replace(
        b"Sender <sender@example.com>", sender.encode("ascii")
    )
    return item


@pytest.mark.parametrize("renamed", [False, True])
def test_delete_draft_allows_own_personal_provider_in_verified_drafts(own_case, renamed):
    mail_client, fake = own_case
    set_draft_sender(
        fake,
        "Author <READER@PROTON.ME>",
        extra_headers=f"Sender: {OWN_ADDRESS}\r\nReply-To: {OWN_ADDRESS}\r\n".encode(),
    )
    folder = "Drafts"
    if renamed:
        folder = "Unsent"
        fake.folders[folder] = fake.folders.pop("Drafts")
        fake.folder_flags = {folder: r"\HasNoChildren \Drafts"}
    policy = mail_client.cleanup.policy
    original_allowlist = policy.rules["personal_provider_allowlist"]
    preview = mail_client.preview_action("delete_draft", ids=["<first@example.com>"])
    assert preview["token"] and not preview["blocked"]
    assert preview["personal_provider_bypass_count"] == 1
    assert {bypass["header"] for bypass in preview["samples"][0]["personal_provider_bypasses"]} == {
        "From",
        "Sender",
        "Reply-To",
    }
    result = mail_client.delete_draft(preview["token"])
    assert result["count"] == 1 and not result["partial"]
    assert "1" not in fake.folders[folder]
    assert policy is mail_client.cleanup.policy
    assert policy.rules["personal_provider_allowlist"] == original_allowlist
    assert policy.check({"from": OWN_ADDRESS}) == ["Personal mail provider in From"]


@pytest.mark.parametrize("folder", ["INBOX", "Drafts"])
def test_delete_draft_own_exception_never_applies_to_other_tools(own_case, folder):
    mail_client, fake = own_case
    item = set_draft_sender(fake)
    if folder != "Drafts":
        fake.folders[folder]["11"] = fake.folders["Drafts"].pop("1")
    preview = mail_client.preview_action("move_to_trash", query={"folder": folder})
    assert preview["blocked"] and preview["token"] is None
    assert "Personal mail provider" in str(preview["blocked"])
    assert not mutations(fake)
    assert item in fake.folders[folder].values()


def test_delete_draft_refuses_own_message_outside_drafts(own_case):
    mail_client, fake = own_case
    set_draft_sender(fake)
    fake.folders["INBOX"]["11"] = fake.folders["Drafts"].pop("1")
    with pytest.raises(MailValidationError, match="Drafts"):
        mail_client.preview_action("delete_draft", ids=["<first@example.com>"])
    assert not mutations(fake) and not mail_client.cleanup.tokens._grants


@pytest.mark.parametrize("header", ["Sender", "Reply-To"])
def test_delete_draft_own_author_does_not_exempt_other_personal_addresses(own_case, header):
    mail_client, fake = own_case
    set_draft_sender(fake, extra_headers=f"{header}: other@proton.me\r\n".encode())
    preview = mail_client.preview_action("delete_draft", ids=["<first@example.com>"])
    assert preview["blocked"] and preview["token"] is None
    assert f"Personal mail provider in {header}" in str(preview["blocked"])
    assert not mutations(fake)


@pytest.mark.parametrize(
    "sender,extra_headers,protected_header",
    [
        ("other@proton.me", f"Sender: {OWN_ADDRESS}\r\n".encode(), "From"),
        ("other@proton.me", f"Reply-To: {OWN_ADDRESS}\r\n".encode(), "From"),
        (f"{OWN_ADDRESS}, other@proton.me", b"", "From"),
        ("external@example.com", f"Sender: {OWN_ADDRESS}\r\n".encode(), "Sender"),
        ("external@example.com", f"Reply-To: {OWN_ADDRESS}\r\n".encode(), "Reply-To"),
    ],
)
def test_delete_draft_requires_one_verified_own_from(
    own_case, sender, extra_headers, protected_header
):
    mail_client, fake = own_case
    set_draft_sender(fake, sender, extra_headers=extra_headers)
    preview = mail_client.preview_action("delete_draft", ids=["<first@example.com>"])
    assert preview["blocked"] and preview["token"] is None
    assert f"Personal mail provider in {protected_header}" in str(preview["blocked"])
    assert not mutations(fake)


@pytest.mark.parametrize(
    "rules,reason",
    [
        ({"addresses": [OWN_ADDRESS]}, "Protected personal address"),
        ({"domains": ["proton.me"]}, "Protected sender domain"),
        ({"domain_patterns": ["proton.*"]}, "Protected financial"),
        ({"keywords": ["author"]}, "Protected sender display name"),
    ],
)
def test_delete_draft_own_provider_exception_preserves_other_policy_rules(
    own_case, tmp_path, rules, reason
):
    mail_client, fake = own_case
    set_draft_sender(fake, f"Author <{OWN_ADDRESS}>")
    path = tmp_path / "synthetic-protected.json"
    path.write_text(json.dumps(rules))
    mail_client.cleanup.policy = ProtectedPolicy(str(path))
    preview = mail_client.preview_action("delete_draft", ids=["<first@example.com>"])
    assert preview["blocked"] and preview["token"] is None
    assert reason in str(preview["blocked"])
    assert not mutations(fake)


def test_delete_draft_rechecks_own_author_headers_before_writing(own_case):
    mail_client, fake = own_case
    item = set_draft_sender(fake)
    preview = mail_client.preview_action("delete_draft", ids=["<first@example.com>"])
    assert preview["token"]
    item.headers = b"Reply-To: other@proton.me\r\n" + item.headers
    with pytest.raises(MailValidationError, match="protected|changed"):
        mail_client.delete_draft(preview["token"])
    assert not mutations(fake) and "1" in fake.folders["Drafts"]


def test_delete_draft_rechecks_own_author_after_writable_select(own_case, monkeypatch):
    mail_client, fake = own_case
    item = set_draft_sender(fake)
    preview = mail_client.preview_action("delete_draft", ids=["<first@example.com>"])
    assert preview["token"]
    select = fake.select

    def changed_selection(folder, *, readonly):
        response = select(folder, readonly=readonly)
        if not readonly:
            item.headers = b"Reply-To: other@proton.me\r\n" + item.headers
        return response

    monkeypatch.setattr(fake, "select", changed_selection)
    with pytest.raises(MailValidationError, match="changed"):
        mail_client.delete_draft(preview["token"])
    assert not mutations(fake) and "1" in fake.folders["Drafts"]


def test_delete_draft_preview_host_confirmation_execute_and_single_use(case, monkeypatch):
    client, fake = case
    client.require_host_confirmation()
    preview = client.preview_action(
        "delete_draft", ids=["<first@example.com>", "<second@example.com>"]
    )
    assert preview["count"] == 2 and preview["token"] and preview["log_id"]
    assert not preview["blocked"] and not mutations(fake)
    [logged] = records(client)
    assert logged["log_id"] == preview["log_id"]
    assert logged["action"] == "preview_action" and logged["params"]["action"] == "delete_draft"
    [listed] = client.list_actions(action="delete_draft")["actions"]
    assert listed["log_id"] == preview["log_id"] and listed["preview_action"] == "delete_draft"

    with pytest.raises(MailValidationError, match="confirmation"):
        client.delete_draft(preview["token"])
    assert not mutations(fake)
    approval = client.approval("delete_draft", preview["token"])
    assert approval["folder"] == "Drafts" and approval["count"] == 2
    with pytest.raises(MailValidationError, match="changed during confirmation"):
        client.confirm_approval("delete_draft", preview["token"], "0" * 64)
    client.confirm_approval("delete_draft", preview["token"], approval["action_hash"])

    uid = fake.uid

    def journal_before_write(command, *args):
        if command in {"STORE", "EXPUNGE"}:
            logged = replay_records(
                [
                    json.loads(line)
                    for line in client.cleanup.backend.log.path.read_text().splitlines()
                ]
            )[-1]
            assert logged["action"] == "delete_draft" and logged["status"] == "in_progress"
            assert {row["uid_before"] for row in logged["messages"]} == {"1", "3"}
            assert all(row["status"] == "uncertain" for row in logged["messages"])
            with pytest.raises(MailValidationError, match="already used"):
                client.cleanup.tokens.inspect(preview["token"], "delete_draft")
        return uid(command, *args)

    monkeypatch.setattr(fake, "uid", journal_before_write)

    result = client.delete_draft(preview["token"])
    assert result["count"] == 2 and result["token_consumed"] and not result["partial"]
    assert result["log_id"] and result["log_id"] != preview["log_id"]
    assert set(fake.folders["Drafts"]) == {"9"}
    assert fake.folders["Drafts"]["9"].flags == {r"\Deleted"}
    assert set(fake.folders["INBOX"]) == {"8"}
    assert mutations(fake) == [
        ("STORE", "1,3", "+FLAGS.SILENT", r"(\Deleted)"),
        ("EXPUNGE", "1,3"),
    ]
    assert all(unquote(call[1]) == "Drafts" for call in fake.calls if call[0] == "SELECT")
    executed = next(record for record in records(client) if record["action"] == "delete_draft")
    assert executed["log_id"] == result["log_id"] and executed["status"] == "completed"
    assert all(entry["status"] == "done" for entry in executed["messages"])
    before = list(mutations(fake))
    with pytest.raises(MailValidationError, match="already used"):
        client.delete_draft(preview["token"])
    with pytest.raises(MailValidationError, match="cannot be undone"):
        client.undo_action(result["log_id"])
    assert mutations(fake) == before


@pytest.mark.parametrize(
    "ids", [["<outside@example.com>"], ["<first@example.com>", "<outside@example.com>"]]
)
def test_delete_draft_refuses_outside_drafts_and_mixed_batches(case, ids):
    client, fake = case
    with pytest.raises(MailValidationError, match="Drafts"):
        client.preview_action("delete_draft", ids=ids)
    assert not mutations(fake) and not client.cleanup.tokens._grants
    assert set(fake.folders["Drafts"]) == {"1", "3", "9"}
    assert set(fake.folders["INBOX"]) == {"8"}


@pytest.mark.parametrize("sender", ["reader@proton.me", "alerts@bank.example.org"])
def test_delete_draft_protected_sender_blocks_the_whole_batch(case, sender):
    client, fake = case
    protected = fake.folders["Drafts"]["3"]
    protected.headers = protected.headers.replace(b"sender@example.com", sender.encode("ascii"))
    preview = client.preview_action(
        "delete_draft", ids=["<first@example.com>", "<second@example.com>"]
    )
    assert preview["count"] == 2 and preview["blocked"]
    assert preview["token"] is None and preview["log_id"]
    assert not mutations(fake) and not client.cleanup.tokens._grants
    assert set(fake.folders["Drafts"]) == {"1", "3", "9"}
    assert r"\Deleted" not in fake.folders["Drafts"]["1"].flags
    [logged] = records(client)
    assert logged["log_id"] == preview["log_id"] and logged["status"] == "blocked"


def test_delete_draft_rechecks_entire_batch_when_one_draft_moves_after_preview(case):
    client, fake = case
    preview = client.preview_action(
        "delete_draft", ids=["<first@example.com>", "<second@example.com>"]
    )
    fake.folders["INBOX"]["10"] = fake.folders["Drafts"].pop("3")
    with pytest.raises(MailValidationError, match="Drafts|changed"):
        client.delete_draft(preview["token"])
    assert not mutations(fake)
    assert set(fake.folders["Drafts"]) == {"1", "9"}
    assert r"\Deleted" not in fake.folders["Drafts"]["1"].flags
    assert set(fake.folders["INBOX"]) == {"8", "10"}


@pytest.mark.parametrize("change", ["uidvalidity", "identity"])
def test_delete_draft_rechecks_writable_selection_before_any_store(case, monkeypatch, change):
    client, fake = case
    preview = client.preview_action(
        "delete_draft", ids=["<first@example.com>", "<second@example.com>"]
    )
    select = fake.select

    def changed_selection(folder, *, readonly):
        response = select(folder, readonly=readonly)
        if not readonly:
            if change == "uidvalidity":
                fake.uidvalidity = "2"
            else:
                fake.folders["Drafts"]["3"] = message("<replacement@example.com>")
        return response

    monkeypatch.setattr(fake, "select", changed_selection)
    with pytest.raises(MailValidationError, match="Drafts|changed"):
        client.delete_draft(preview["token"])
    assert not mutations(fake) and set(fake.folders["Drafts"]) == {"1", "3", "9"}
    assert r"\Deleted" not in fake.folders["Drafts"]["1"].flags


def test_delete_draft_prefers_renamed_special_use_folder(case):
    client, fake = case
    fake.folders["Unsent"] = fake.folders.pop("Drafts")
    fake.folders["Drafts"] = {"3": message("<ordinary-folder@example.com>")}
    fake.folder_flags = {"Unsent": r"\HasNoChildren \Drafts"}
    preview = client.preview_action("delete_draft", ids=["<first@example.com>"])
    assert client.cleanup.tokens.inspect(preview["token"], "delete_draft").params == {
        "folder": "Unsent"
    }
    result = client.delete_draft(preview["token"])
    assert result["count"] == 1 and "1" not in fake.folders["Unsent"]
    assert set(fake.folders["Drafts"]) == {"3"}
    assert all(unquote(call[1]) == "Unsent" for call in fake.calls if call[0] == "SELECT")


def test_delete_draft_falls_back_to_configured_drafts_without_special_use(case):
    client, fake = case
    fake.folder_flags = {}
    preview = client.preview_action("delete_draft", ids=["<first@example.com>"])
    result = client.delete_draft(preview["token"])
    assert result["count"] == 1 and not result["partial"]
    assert set(fake.folders["Drafts"]) == {"3", "9"}
    assert all(unquote(call[1]) == "Drafts" for call in fake.calls if call[0] == "SELECT")


@pytest.mark.parametrize("conflict", [r"\All", r"\Sent", r"\Trash", r"\Noselect"])
def test_delete_draft_refuses_conflicting_drafts_attributes(case, conflict):
    client, fake = case
    fake.folder_flags["Drafts"] = r"\Drafts " + conflict
    with pytest.raises(MailValidationError, match="Drafts.*selectable.*conflicting"):
        client.preview_action("delete_draft", ids=["<first@example.com>"])
    assert not mutations(fake) and not client.cleanup.tokens._grants
    assert set(fake.folders["Drafts"]) == {"1", "3", "9"}


def test_delete_draft_requires_uidplus_before_any_mutation(case):
    client, fake = case
    preview = client.preview_action("delete_draft", ids=["<first@example.com>"])
    fake.capabilities = (b"IMAP4rev1",)
    with pytest.raises(MailValidationError, match="UIDPLUS|UID EXPUNGE"):
        client.delete_draft(preview["token"])
    assert not mutations(fake) and set(fake.folders["Drafts"]) == {"1", "3", "9"}
