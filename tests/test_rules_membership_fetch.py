"""All Mail UID ranges handle holes without capability-specific search paths."""

from dataclasses import replace

import pytest
from test_folder_rules import mailbox as mailbox
from test_mail import message
from test_rules_matched_membership import matched_case

from proton_mail_mcp.mail import MailValidationError


def tiny_all_mail(mailbox, monkeypatch, uid=9):
    case = matched_case(mailbox, count=1)
    case.cleanup.client.settings = replace(
        case.cleanup.client.settings, identity_index_enabled=False
    )
    messages = case.fake.folders["All Mail"]
    messages.pop("1000")
    messages[str(uid)] = messages.pop("1")
    messages["2"] = message("<unrelated@example.test>")
    command = case.fake.uid
    searches = []

    def record(operation, *args):
        if operation == "SEARCH" and case.fake.current == "All Mail":
            assert "ALL" not in args
            searches.append(args)
        return command(operation, *args)

    monkeypatch.setattr(case.fake, "uid", record)
    return case, searches


@pytest.mark.parametrize("uid", [16, 17, 200, 201])
def test_bounded_membership_fetch_handles_uid_holes_across_200_uid_ranges(
    mailbox, monkeypatch, uid
):
    case, searches = tiny_all_mail(mailbox, monkeypatch, uid)
    metadata = case.cleanup.client._metadata
    fetched = []

    def record(connection, uids, **options):
        if case.fake.current == "All Mail":
            fetched.extend(uids)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.cleanup.client, "_metadata", record)
    result = case.rules.preview_rules()

    assert result["token"] and result["count"] == 1
    grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
    assert [
        row["uid"] for row in grant.messages[0]["memberships"] if row["folder"] == "All Mail"
    ] == [str(uid)]
    assert not searches
    assert fetched == [str(value) for value in range(1, uid + 1)]
    assert b"CONDSTORE" not in case.fake.capabilities


def test_bounded_membership_fetch_refuses_hidden_second_identity(mailbox, monkeypatch):
    case, searches = tiny_all_mail(mailbox, monkeypatch)
    peer = case.fake.folders["All Mail"]["2"]
    peer.headers = peer.headers[:-2] + b"Message-ID: <rule-1@example.test>\r\n\r\n"

    with pytest.raises(MailValidationError, match="unresolved"):
        case.rules.preview_rules()

    assert not searches and not case.cleanup.tokens._grants


def test_bounded_membership_fetch_recovers_after_omitted_row(mailbox, monkeypatch):
    case, searches = tiny_all_mail(mailbox, monkeypatch)
    metadata = case.cleanup.client._metadata
    omitted = False

    def omit_once(connection, uids, **options):
        nonlocal omitted
        rows = metadata(connection, uids, **options)
        if case.fake.current == "All Mail" and not omitted:
            rows.pop("9")
            omitted = True
        return rows

    monkeypatch.setattr(case.cleanup.client, "_metadata", omit_once)
    with pytest.raises(MailValidationError, match="incomplete"):
        case.rules.preview_rules()

    assert not case.cleanup.tokens._grants
    result = case.rules.preview_rules()

    assert result["token"] and result["count"] == 1
    grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
    assert [
        row["uid"] for row in grant.messages[0]["memberships"] if row["folder"] == "All Mail"
    ] == ["9"]
    assert not searches


def test_empty_allocated_all_mail_needs_no_fetch_or_search(mailbox, monkeypatch):
    case, searches = tiny_all_mail(mailbox, monkeypatch)
    case.fake.folders["All Mail"].clear()
    case.fake.next_uids["All Mail"] = 10
    metadata = case.cleanup.client._metadata

    def record(connection, uids, **options):
        assert case.fake.current != "All Mail"
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.cleanup.client, "_metadata", record)
    result = case.rules.preview_rules()

    assert result["token"] and result["count"] == 1
    assert not searches


@pytest.mark.parametrize("change", ["omitted", "arrival"])
def test_bounded_membership_fetch_refuses_incomplete_selection_and_resumes_after_arrival(
    mailbox, monkeypatch, change
):
    case, searches = tiny_all_mail(mailbox, monkeypatch)
    metadata = case.cleanup.client._metadata

    def disrupt(connection, uids, **options):
        rows = metadata(connection, uids, **options)
        if case.fake.current == "All Mail":
            if change == "omitted":
                rows.pop("9")
            else:
                case.fake.folders["All Mail"]["3"] = message("<arrival@example.test>")
        return rows

    monkeypatch.setattr(case.cleanup.client, "_metadata", disrupt)
    if change == "omitted":
        with pytest.raises(MailValidationError, match="incomplete"):
            case.rules.preview_rules()
        assert not case.cleanup.tokens._grants
    else:
        result = case.rules.preview_rules()
        assert result["partial"] and not result["total_is_exact"]
        assert result["next_cursor"] and result["token"] is None
        assert "changed" in result["reason"] and "continue preview_rules" in result["reason"]
        assert not case.cleanup.tokens._grants
        monkeypatch.setattr(case.cleanup.client, "_metadata", metadata)

        resumed = case.rules.preview_rules(cursor=result["next_cursor"])

        assert resumed["token"] and resumed["count"] == 1
        assert resumed["next_cursor"] is None
    assert not searches
