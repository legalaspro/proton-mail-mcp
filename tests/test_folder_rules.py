"""Reviewed connector rules exercise the real move journal against in-memory mail."""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from bridge_costs import envelope_from_headers
from resume_assertions import assert_bound_progress, identity, journal_identity
from test_cleanup_mutations import MUTATIONS, MutatingImap, uid_values
from test_mail import client, message

from proton_mail_mcp import guarded
from proton_mail_mcp.action_log import replay_records
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.folder_rules import FolderRulesService, load_rules
from proton_mail_mcp.mail import MailValidationError


def rule(field="from", contains="sender@example.com", folder="Folders/Review"):
    return {"field": field, "contains": contains, "folder": folder}


@pytest.fixture
def mailbox(tmp_path):
    def build(*, rules=None, count=1):
        path = tmp_path / "synthetic-rules.json"
        path.write_text(json.dumps({"version": 1, "rules": [rule()] if rules is None else rules}))
        items = {str(i): message(f"<rule-{i}@example.test>") for i in range(1, count + 1)}
        fake = MutatingImap(
            {
                "INBOX": items,
                "Folders/Review": {},
                "Folders/Other": {},
                "All Mail": dict(items),
                "Trash": {},
            }
        )
        settings = Settings(
            token="synthetic-folder-rules-" + "x" * 32,
            bridge_user="reader@example.test",
            call_timeout=300,
            action_log_path=str(tmp_path / "synthetic-actions.jsonl"),
        )
        cleanup = client(settings, fake).cleanup
        return SimpleNamespace(
            rules=FolderRulesService(cleanup, path), cleanup=cleanup, fake=fake, path=path
        )

    return build


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"version": True, "rules": []},
        {"version": 1, "rules": "not a list"},
        {"version": 1, "rules": [rule()] * 21},
        {"version": 1, "rules": [{**rule(), "mark_read": True}]},
        {"version": 1, "rules": [rule(field="text")]},
        {"version": 1, "rules": [rule(contains="")]},
        {"version": 1, "rules": [rule(contains="bad\nheader")]},
        *[
            {"version": 1, "rules": [rule(folder=target)]}
            for target in ("Trash", "Labels/Review", "Folders/../Trash", "Folders/")
        ],
    ],
)
def test_config_validation_fails_closed(tmp_path, data):
    path = tmp_path / "invalid-synthetic-rules.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="valid connector folder rules"):
        load_rules(path)


@pytest.mark.parametrize("data", [None, "{", '{"version":1,"version":1,"rules":[]}', " " * 65537])
def test_missing_or_invalid_json_refused(tmp_path, data):
    path = tmp_path / "synthetic-rules.json"
    if data is not None:
        path.write_text(data)
    with pytest.raises(ConfigError):
        load_rules(path)


@pytest.mark.parametrize(
    "field,contains", [("from", "SENDER@example.com"), ("to", "reader@"), ("subject", "hELLo")]
)
def test_preview_is_read_only_and_first_matching_rule_wins(mailbox, field, contains):
    case = mailbox(rules=[rule(field, contains), rule(folder="Folders/Other")])
    preview = case.rules.preview_rules()
    assert preview["token"] and preview["count"] == 1
    assert [item["count"] for item in preview["rules"]] == [1, 0]
    assert set(preview["rules"][0]["samples"][0]) - {"uidvalidity", "uid"} == {
        "id",
        "message_id",
        "from",
        "subject",
        "date",
        "folder",
    }
    assert not case.cleanup.backend.log.path.exists()
    assert all(call[0] not in MUTATIONS for call in case.fake.calls)
    assert all(call[2] is True for call in case.fake.calls if call[0] == "EXAMINE")
    assert all(
        "BODY"
        not in call[2].replace("BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SENDER REPLY-TO)]", "")
        for call in case.fake.calls
        if call[0] == "FETCH"
    )


def test_apply_uses_chunked_moves_logs_outer_token_and_is_undoable(mailbox, monkeypatch):
    # One batched chunk would hold all 51 rows; keep several chunks.
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 21)
    case = mailbox(count=51)
    preview = case.rules.preview_rules()
    grant = case.cleanup.tokens.inspect(preview["token"], "apply_rules")
    source = dict(case.fake.folders["INBOX"])
    approved = {identity(row) for row in grant.messages}
    by_uid = {row["uid"]: identity(row) for row in grant.messages}
    apply = case.cleanup.backend.apply
    sizes = []
    actual = []
    original_uid = case.fake.uid

    def observe(command, *args):
        if command == "MOVE":
            for uid in uid_values(args[0]):
                bound = by_uid[uid]
                assert case.fake.current == bound[0]
                assert case.fake.folders["INBOX"][uid] is source[uid]
                assert bound not in actual
                actual.append(bound)
        return original_uid(command, *args)

    def recorded_apply(*args, **kwargs):
        result = apply(*args, **kwargs)
        sizes.append(len(args[4]))
        return result

    monkeypatch.setattr(case.fake, "uid", observe)
    monkeypatch.setattr(case.cleanup.backend, "apply", recorded_apply)
    token, results = preview["token"], []
    while token:
        result = case.rules.apply_rules(token)
        results.append(result)
        assert_bound_progress(case.cleanup, grant, results, actual)
        token = result["continuation_token"]
    assert sum(sizes) == 51 and all(0 < size <= 21 for size in sizes)
    assert sum(result["done"] for result in results) == 51 and not result["partial"]
    assert result["token_consumed"] and set(actual) == approved
    assert not case.fake.folders["INBOX"]
    records = replay_records(
        [json.loads(line) for line in case.cleanup.backend.log.path.read_text().splitlines()]
    )
    assert len(records) == len(sizes)
    assert all(record["action"] == "move_to_folder" for record in records)
    assert all(
        record["token_digest"] == hashlib.sha256(preview["token"].encode()).hexdigest()
        for record in records
    )
    assert {row["uid_after"] for record in records for row in record["messages"]} == set(
        case.fake.folders["Folders/Review"]
    )
    monkeypatch.setattr(case.fake, "uid", original_uid)
    restored = set()
    for record in reversed(records):
        entries = [row for row in record["messages"] if row["status"] == "done"]
        expected = {journal_identity(row) for row in entries}
        before = len(case.fake.calls)
        result = case.cleanup.undo_action(record["log_id"])
        assert result["count"] == len(expected) and not result["partial"]
        moved = [call for call in case.fake.calls[before:] if call[0] == "MOVE"]
        assert len(moved) == len(expected)
        assert {call[1] for call in moved} == {row["uid_after"] for row in entries}
        assert not restored.intersection(expected)
        restored.update(expected)
        assert {id(item) for item in case.fake.folders["INBOX"].values()} == {
            id(source[row[2]]) for row in restored
        }
        before_retry = len(case.fake.calls)
        with pytest.raises(MailValidationError, match="already.*undone"):
            case.cleanup.undo_action(record["log_id"])
        assert not any(call[0] in MUTATIONS for call in case.fake.calls[before_retry:])
    assert restored == approved
    assert len(case.fake.folders["INBOX"]) == 51
    assert not case.fake.folders["Folders/Review"]
    assert not any(call[0] in {"STORE", "EXPUNGE", "COPY"} for call in case.fake.calls)
    assert not any("\\Seen" in str(call) for call in case.fake.calls)
    with pytest.raises(MailValidationError, match="already used"):
        case.rules.apply_rules(preview["token"])


def test_multiple_destinations_share_one_claim_and_are_undoable(mailbox, monkeypatch):
    case = mailbox(rules=[rule("subject", "First"), rule(folder="Folders/Other")], count=2)
    case.fake.folders["INBOX"]["1"].headers = case.fake.folders["INBOX"]["1"].headers.replace(
        b"Hello", b"First"
    )
    original_claim = case.cleanup.tokens.claim
    claims = []

    def claim(*args, **kwargs):
        claims.append(args[1])
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(case.cleanup.tokens, "claim", claim)
    result = case.rules.apply_rules(case.rules.preview_rules()["token"])
    assert result["count"] == 2 and claims == ["apply_rules"]
    assert len(case.fake.folders["Folders/Review"]) == len(case.fake.folders["Folders/Other"]) == 1
    for log_id in result["log_ids"]:
        assert case.cleanup.undo_action(log_id)["count"] == 1
    assert len(case.fake.folders["INBOX"]) == 2


def test_later_rule_refuses_newly_protected_membership_after_first_move(mailbox, monkeypatch):
    case = mailbox(rules=[rule("subject", "First"), rule(folder="Folders/Other")], count=2)
    case.fake.folders["INBOX"]["1"].headers = case.fake.folders["INBOX"]["1"].headers.replace(
        b"Hello", b"First"
    )
    second = deepcopy(case.fake.folders["INBOX"]["2"])
    token = case.rules.preview_rules()["token"]
    apply = case.cleanup.backend.apply

    def protect_remaining_membership(*args, **kwargs):
        result = apply(*args, **kwargs)
        protected = deepcopy(second)
        protected.headers = protected.headers.replace(b"sender@example.com", b"relative@gmail.com")
        case.fake.folders["All Mail"]["2"] = protected
        return result

    monkeypatch.setattr(case.cleanup.backend, "apply", protect_remaining_membership)
    result = case.rules.apply_rules(token)

    assert result["count"] == result["done"] == result["remaining"] == 1
    assert result["partial"] and result["token_consumed"] and result["uncertain"] == 0
    assert len(result["log_ids"]) == 1 and "interrupted" in result["error"]
    assert set(case.fake.folders["INBOX"]) == {"2"}
    assert case.fake.folders["INBOX"]["2"] == second
    assert len(case.fake.folders["Folders/Review"]) == 1
    assert not case.fake.folders["Folders/Other"]
    assert [call[0] for call in case.fake.calls if call[0] in MUTATIONS] == ["MOVE"]
    with pytest.raises(MailValidationError, match="already used"):
        case.rules.apply_rules(token)


def test_protected_sender_skipped_and_cannot_fall_through(mailbox):
    case = mailbox(rules=[rule("subject", "Hello"), rule(folder="Folders/Other")], count=2)
    case.fake.folders["INBOX"]["1"].headers = case.fake.folders["INBOX"]["1"].headers.replace(
        b"sender@example.com", b"relative@gmail.com"
    )
    preview = case.rules.preview_rules()
    assert preview["count"] == 1 and preview["matched_count"] == 2
    assert len(preview["rules"][0]["blocked"]) == 1
    assert preview["rules"][1]["count"] == 0
    assert case.rules.apply_rules(preview["token"])["count"] == 1
    assert set(case.fake.folders["INBOX"]) == {"1"}


def protected_membership_case(mailbox, malformed=None, *, count=1):
    case = mailbox(rules=[rule("subject", "Hello")], count=count)
    protected = message("<rule-1@example.test>")
    protected.headers = protected.headers.replace(b"sender@example.com", b"relative@gmail.com")
    case.fake.folders["All Mail"]["1"] = protected
    if malformed is not None:
        peer = message("<malformed-peer@example.test>")
        peer.headers = peer.headers.replace(
            b"Message-ID: <malformed-peer@example.test>\r\n", malformed
        )
        case.fake.folders["INBOX"]["2"] = peer
    return case


@pytest.mark.parametrize("count", [1, 2])
def test_protected_membership_skipped_after_complete_resolution(mailbox, count):
    case = protected_membership_case(mailbox, count=count)
    protected = deepcopy(case.fake.folders["All Mail"]["1"])
    preview = case.rules.preview_rules()
    assert preview["count"] == count - 1 and preview["matched_count"] == count
    assert len(preview["rules"][0]["blocked"]) == 1
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)
    if count == 1:
        assert preview["token"] is None
    else:
        assert case.rules.apply_rules(preview["token"])["count"] == 1
        assert set(case.fake.folders["INBOX"]) == {"1"}
        assert len(case.fake.folders["Folders/Review"]) == 1
    assert case.fake.folders["All Mail"]["1"] == protected


def matching_membership_case(mailbox, same_rule, *, protected_source=False):
    case = mailbox(
        rules=[rule("subject", "First"), rule("subject", "Second", "Folders/Other")], count=2
    )
    for uid, subject in [("1", "First"), ("2", "First" if same_rule else "Second")]:
        case.fake.folders["INBOX"][uid].headers = case.fake.folders["INBOX"][uid].headers.replace(
            b"Subject: Hello", b"Subject: " + subject.encode()
        )
    case.fake.folders["All Mail"]["2"] = message(
        "<rule-2@example.test>", subject="First" if same_rule else "Second"
    )
    if protected_source:
        case.fake.folders["INBOX"]["2"].headers = case.fake.folders["INBOX"]["2"].headers.replace(
            b"sender@example.com", b"relative@gmail.com"
        )
    return case


def corrupt_matching_membership(case, malformed):
    peer = case.fake.folders["All Mail"]["2"]
    peer.headers = peer.headers.replace(
        b"Message-ID: <rule-2@example.test>", b"Message-ID: <rule-2@example.test> " + malformed
    )


@pytest.mark.parametrize("same_rule", [True, False], ids=["same-rule", "later-rule"])
@pytest.mark.parametrize("malformed", [b"<other@example.test>", b"(added by relay)"])
@pytest.mark.parametrize("protected_source", [False, True])
def test_unresolved_matching_membership_refuses_entire_preview(
    mailbox, monkeypatch, same_rule, malformed, protected_source
):
    case = matching_membership_case(mailbox, same_rule, protected_source=protected_source)
    corrupt_matching_membership(case, malformed)
    before = deepcopy(case.fake.folders)

    def unexpected_issue(*args, **kwargs):
        pytest.fail("Unresolved matching membership must not issue any approval")

    monkeypatch.setattr(case.cleanup.tokens, "issue", unexpected_issue)
    with pytest.raises(MailValidationError):
        case.rules.preview_rules()
    assert case.fake.folders == before
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)
    assert not case.cleanup.backend.log.path.exists()


@pytest.mark.parametrize("same_rule", [True, False], ids=["same-rule", "later-rule"])
@pytest.mark.parametrize("malformed", [b"<other@example.test>", b"(added by relay)"])
@pytest.mark.parametrize("reduced_approval", [False, True], ids=["full-approval", "safe-peer-only"])
@pytest.mark.parametrize("protected_source", [False, True])
def test_unresolved_matching_membership_refuses_entire_confirmation(
    mailbox, same_rule, malformed, reduced_approval, protected_source
):
    case = matching_membership_case(mailbox, same_rule, protected_source=protected_source)
    token = case.rules.preview_rules()["token"]
    if reduced_approval:
        # Reproduce a legacy approval that skipped the unresolved row. The
        # remaining row still matches, so fingerprint checks alone cannot help.
        grant = case.cleanup.tokens.inspect(token, "apply_rules")
        token, _ = case.cleanup.tokens.issue(
            "apply_rules",
            grant.params,
            grant.selector,
            [row for row in grant.messages if row["message_id"] == "<rule-1@example.test>"],
        )
    corrupt_matching_membership(case, malformed)
    before = deepcopy(case.fake.folders)
    with pytest.raises(MailValidationError):
        case.rules.apply_rules(token)
    assert case.fake.folders == before
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)
    assert not case.cleanup.backend.log.path.exists()


@pytest.mark.parametrize("same_rule", [True, False], ids=["same-rule", "later-rule"])
def test_verified_protected_source_skipped_after_complete_resolution(mailbox, same_rule):
    case = matching_membership_case(mailbox, same_rule, protected_source=True)
    protected = deepcopy(case.fake.folders["INBOX"]["2"])
    preview = case.rules.preview_rules()
    assert preview["count"] == 1 and preview["matched_count"] == 2
    blocked = [row for result in preview["rules"] for row in result["blocked"]]
    assert len(blocked) == 1 and blocked[0]["unresolved"] is False
    result = case.rules.apply_rules(preview["token"])
    assert result["done"] == 1 and not result["partial"]
    assert set(case.fake.folders["INBOX"]) == {"2"}
    assert case.fake.folders["INBOX"]["2"] == protected
    assert len(case.fake.folders["Folders/Review"]) == 1
    assert not case.fake.folders["Folders/Other"]


@pytest.mark.parametrize("change", ["date", "size", "sender", "missing"])
def test_confirmation_binds_skipped_protected_candidate_fingerprint(mailbox, change):
    case = matching_membership_case(mailbox, same_rule=True, protected_source=True)
    preview = case.rules.preview_rules()
    assert preview["count"] == 1 and preview["matched_count"] == 2
    grant = case.cleanup.tokens.inspect(preview["token"], "apply_rules")
    assert {row["uid"] for row in grant.selector["reviewed_rows"]} == {"1", "2"}
    protected = case.fake.folders["INBOX"]["2"]
    if change == "date":
        protected.headers = protected.headers.replace(b"10:00:00", b"11:00:00")
    elif change == "size":
        protected.sections["1"] += b"changed"
    elif change == "sender":
        protected.headers = protected.headers.replace(b"relative@gmail.com", b"relative@yahoo.com")
    else:
        del case.fake.folders["INBOX"]["2"]
    case.fake.calls.clear()
    with pytest.raises(MailValidationError):
        case.rules.apply_rules(preview["token"])
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)
    assert not case.cleanup.backend.log.path.exists()
    # Confirmation refreshes the approved/reviewed UIDs; it cannot run rules
    # selectors again and add newly arrived matching messages to the plan.
    assert not any(
        call[0] == "SEARCH" and any(key in call for key in ("FROM", "SUBJECT", "TO"))
        for call in case.fake.calls
    )


@pytest.mark.parametrize("later_group", [False, True], ids=["next-chunk", "next-rule"])
def test_skipped_reviewed_candidate_is_rechecked_before_later_mutations(
    mailbox, monkeypatch, later_group
):
    # One batched chunk would hold all 51 eligible rows; keep a next chunk.
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 21)
    case = mailbox(
        rules=(
            [rule("subject", "First"), rule("subject", "Second", "Folders/Other")]
            if later_group
            else [rule("subject", "Hello")]
        ),
        count=3 if later_group else 52,
    )
    if later_group:
        for uid, subject in [("1", "First"), ("2", "Second"), ("3", "First")]:
            row = case.fake.folders["INBOX"][uid]
            row.headers = row.headers.replace(b"Subject: Hello", b"Subject: " + subject.encode())
    skipped_uid = "3" if later_group else "52"
    skipped = case.fake.folders["INBOX"][skipped_uid]
    skipped.headers = skipped.headers.replace(b"sender@example.com", b"relative@gmail.com")
    preview = case.rules.preview_rules()
    assert preview["count"] == (2 if later_group else 51) and preview["token"]
    original_apply = case.cleanup.backend.apply
    grant = case.cleanup.tokens.inspect(preview["token"], "apply_rules")
    original = dict(case.fake.folders["INBOX"])
    chunks = []

    def apply(*args, **kwargs):
        result = original_apply(*args, **kwargs)
        chunks.append([identity(row) for row in args[4]])
        skipped.headers = skipped.headers.replace(b"10:00:00", b"11:00:00")
        return result

    monkeypatch.setattr(case.cleanup.backend, "apply", apply)
    result = case.rules.apply_rules(preview["token"])
    if later_group:
        assert result["partial"] and result["count"] == 1
        assert set(case.fake.folders["INBOX"]) == {"2", "3"}
    else:
        assert len(chunks) == 1 and 0 < len(chunks[0]) <= 21
        actual = chunks[0]
        assert result["partial"] and result["count"] == len(actual)
        assert "chunk preparation" in result["error"]
        assert_bound_progress(case.cleanup, grant, [result], actual)
        moved_uids = {row[2] for row in actual}
        assert set(case.fake.folders["INBOX"]) == set(original) - moved_uids
        assert {id(item) for item in case.fake.folders["Folders/Review"].values()} == {
            id(original[uid]) for uid in moved_uids
        }
        moves = [call for call in case.fake.calls if call[0] == "MOVE"]
        assert len(moves) == 1 and set(uid_values(moves[0][1])) == moved_uids
        assert skipped_uid not in moved_uids
        before_retry = len(case.fake.calls)
        with pytest.raises(MailValidationError, match="already used"):
            case.rules.apply_rules(preview["token"])
        with pytest.raises(MailValidationError, match="changed"):
            case.rules.apply_rules(result["continuation_token"])
        assert not any(call[0] in MUTATIONS for call in case.fake.calls[before_retry:])


@pytest.mark.parametrize("same_rule", [True, False], ids=["same-rule", "later-rule"])
@pytest.mark.parametrize("protection", ["ordinary", "source", "peer"])
@pytest.mark.parametrize(
    "candidate", ["shared-message", "membership-only", "Archive", "Folders/Side"]
)
@pytest.mark.parametrize("confirm", [False, True], ids=["preview", "valid-confirmation"])
def test_duplicate_raw_identity_refuses_entire_batch(
    mailbox, monkeypatch, same_rule, protection, candidate, confirm
):
    case = matching_membership_case(mailbox, same_rule, protected_source=protection == "source")
    if candidate != "membership-only":
        case.fake.folders["All Mail"]["2"] = case.fake.folders["INBOX"]["2"]
    peer = case.fake.folders["All Mail"]["2"]
    second_id = b"<other@example.test>"
    if candidate in {"Archive", "Folders/Side"}:
        # A separate physical message hides the source ID behind its first ID.
        # Its real and virtual memberships expose the same object and raw bytes.
        peer = message("<independent-peer@example.test>")
        case.fake.folders[candidate] = {"3": peer}
        case.fake.folders["All Mail"]["3"] = peer
        second_id = b"<rule-2@example.test>"
        assert case.fake.folders[candidate]["3"] is case.fake.folders["All Mail"]["3"]
    if protection == "peer":
        peer.headers = peer.headers.replace(b"sender@example.com", b"relative@gmail.com")
    peer.headers = peer.headers[:-2] + b"X-Pad-Data: " + second_id + b"\r\n\r\n"
    headers = peer.headers
    token = case.rules.preview_rules()["token"] if confirm else None
    if confirm:
        assert token
    peer.headers = peer.headers.replace(b"X-Pad-Data:", b"Message-ID:")
    assert len(peer.headers) == len(headers)
    assert peer.headers.count(b"Message-ID:") == 2
    assert envelope_from_headers(peer.headers) == envelope_from_headers(headers)
    before = deepcopy(case.fake.folders)

    def unexpected_issue(*args, **kwargs):
        pytest.fail("Duplicate raw Message-ID fields must not issue any approval")

    monkeypatch.setattr(case.cleanup.tokens, "issue", unexpected_issue)
    with pytest.raises(MailValidationError):
        case.rules.apply_rules(token) if confirm else case.rules.preview_rules()
    assert case.fake.folders == before
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)
    assert not case.cleanup.backend.log.path.exists()
    if confirm:
        assert case.cleanup.tokens.inspect(token, "apply_rules")


@pytest.mark.parametrize("raw_id", [b"", b"Message-ID: invalid\x00\r\n"])
@pytest.mark.parametrize("confirm", [False, True], ids=["preview", "confirmation"])
def test_unusable_unrelated_message_does_not_require_full_folder_audit(mailbox, raw_id, confirm):
    case = mailbox()
    peer = message("<unrelated@example.test>", subject="Unrelated")
    case.fake.folders["Archive"] = {"3": peer}
    case.fake.folders["All Mail"]["3"] = peer
    token = case.rules.preview_rules()["token"] if confirm else None
    peer.headers = peer.headers.replace(b"Message-ID: <unrelated@example.test>\r\n", raw_id)
    result = case.rules.apply_rules(token) if confirm else case.rules.preview_rules()
    assert result["count"] > 0
    if not confirm:
        assert result["token"]
        assert not any(call[0] in MUTATIONS for call in case.fake.calls)
    assert case.fake.folders["Archive"]["3"].headers == peer.headers


@pytest.mark.parametrize("header_name", [b"Message-ID", b"mEsSaGe-Id"])
def test_duplicate_raw_message_id_fields_with_same_value_still_refused(mailbox, header_name):
    case = matching_membership_case(mailbox, same_rule=False, protected_source=True)
    peer = case.fake.folders["All Mail"]["2"]
    peer.headers = peer.headers[:-2] + header_name + b": <rule-2@example.test>\r\n\r\n"
    with pytest.raises(MailValidationError):
        case.rules.preview_rules()
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)
    assert not case.cleanup.backend.log.path.exists()


@pytest.mark.parametrize("folder", ["INBOX", "All Mail"], ids=["source", "membership"])
@pytest.mark.parametrize("failure", ["missing-uid", "mismatched-id"])
@pytest.mark.parametrize("confirm", [False, True], ids=["preview", "confirmation"])
def test_incomplete_raw_identity_evidence_refuses_entire_batch(
    mailbox, monkeypatch, folder, failure, confirm
):
    case = matching_membership_case(mailbox, same_rule=False, protected_source=True)
    token = case.rules.preview_rules()["token"] if confirm else None
    original_metadata = case.cleanup.client._metadata

    def metadata(connection, uids, **kwargs):
        result = original_metadata(connection, uids, **kwargs)
        if connection.current == folder and kwargs.get("identity_headers_only") and result:
            uid = next(iter(result))
            if failure == "missing-uid":
                del result[uid]
            else:
                result[uid]["headers"].replace_header("Message-ID", "<wrong@example.test>")
        return result

    monkeypatch.setattr(case.cleanup.client, "_metadata", metadata)
    before = deepcopy(case.fake.folders)
    with pytest.raises(MailValidationError):
        case.rules.apply_rules(token) if confirm else case.rules.preview_rules()
    assert case.fake.folders == before
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)
    assert not case.cleanup.backend.log.path.exists()
    if confirm:
        assert case.cleanup.tokens.inspect(token, "apply_rules")


@pytest.mark.parametrize("confirm", [False, True], ids=["preview", "confirmation"])
def test_uidvalidity_change_during_raw_identity_checks_refuses_entire_batch(
    mailbox, monkeypatch, confirm
):
    case = matching_membership_case(mailbox, same_rule=False, protected_source=True)
    token = case.rules.preview_rules()["token"] if confirm else None
    original_metadata = case.cleanup.client._metadata

    def metadata(connection, uids, **kwargs):
        result = original_metadata(connection, uids, **kwargs)
        if kwargs.get("identity_headers_only") and connection.current == "INBOX" and "2" in result:
            case.fake.uidvalidity = "2"
        return result

    monkeypatch.setattr(case.cleanup.client, "_metadata", metadata)
    before = deepcopy(case.fake.folders)
    with pytest.raises(MailValidationError):
        case.rules.apply_rules(token) if confirm else case.rules.preview_rules()
    assert case.fake.folders == before
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)
    assert not case.cleanup.backend.log.path.exists()
    if confirm:
        assert case.cleanup.tokens.inspect(token, "apply_rules")


@pytest.mark.parametrize("folder", ["INBOX", "All Mail"], ids=["source", "membership"])
@pytest.mark.parametrize("confirm", [False, True], ids=["preview", "confirmation"])
def test_unresolved_sender_refuses_entire_rules_batch(mailbox, monkeypatch, folder, confirm):
    case = matching_membership_case(mailbox, same_rule=False)
    token = case.rules.preview_rules()["token"] if confirm else None
    peer = case.fake.folders[folder]["2"]
    peer.headers = peer.headers.replace(b"From: Sender <sender@example.com>", b"From: invalid")
    before = deepcopy(case.fake.folders)

    def unexpected_issue(*args, **kwargs):
        pytest.fail("Unresolved sender must not issue any approval")

    monkeypatch.setattr(case.cleanup.tokens, "issue", unexpected_issue)
    with pytest.raises(MailValidationError, match="cannot verify protected memberships"):
        if confirm:
            case.rules.apply_rules(token)
        else:
            case.rules.preview_rules()
    assert case.fake.folders == before
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)
    assert not case.cleanup.backend.log.path.exists()


@pytest.mark.parametrize("malformed", [b"", b"Message-ID: invalid\x00\r\n"])
def test_malformed_peer_refuses_preview_before_token_issue(mailbox, monkeypatch, malformed):
    case = protected_membership_case(mailbox, malformed)

    def unexpected_issue(*args, **kwargs):
        pytest.fail("Incomplete rules plan must not issue an approval")

    monkeypatch.setattr(case.cleanup.tokens, "issue", unexpected_issue)
    with pytest.raises(MailValidationError, match="cannot verify protected memberships"):
        case.rules.preview_rules()
    assert set(case.fake.folders["INBOX"]) == {"1", "2"}
    assert not case.fake.folders["Folders/Review"]
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)
    assert not case.cleanup.backend.log.path.exists()


@pytest.mark.parametrize("malformed", [b"", b"Message-ID: invalid\x00\r\n"])
def test_malformed_peer_refuses_confirmation_of_incomplete_approval(mailbox, malformed):
    case = protected_membership_case(mailbox, malformed)
    cleanup = case.cleanup
    # Reproduce an approval issued by the old planner, which dropped the bad
    # peer and approved a row whose protected memberships were never checked.
    deadline = cleanup.client._deadline()
    with cleanup.client._session(deadline=deadline) as (connection, _):
        rows = cleanup._resolve(
            connection,
            deadline,
            {"query": {"folder": "INBOX", "subject": "Hello"}},
            "move_to_folder",
            {"folder": "Folders/Review"},
            cleanup.client._folders(connection),
        )
    _, digest = load_rules(case.path)
    token, _ = cleanup.tokens.issue(
        "apply_rules",
        {"rules_digest": digest},
        {"folder": "INBOX"},
        [row for row in rows if not row.get("identity_invalid")],
    )
    with pytest.raises(MailValidationError, match="cannot verify protected memberships"):
        case.rules.apply_rules(token)
    assert set(case.fake.folders["INBOX"]) == {"1", "2"}
    assert not case.fake.folders["Folders/Review"]
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)
    assert not cleanup.backend.log.path.exists()


@pytest.mark.parametrize("token", [None, "", "forged"])
def test_apply_requires_preview_token_before_connection(mailbox, token):
    case = mailbox()
    with pytest.raises(MailValidationError, match="Invalid or already used"):
        case.rules.apply_rules(token)
    assert case.fake.calls == [] and not case.cleanup.backend.log.path.exists()


@pytest.mark.parametrize("change", ["config", "uid", "protection"])
def test_revalidation_refuses_changes_before_mutation(mailbox, change):
    case = mailbox()
    token = case.rules.preview_rules()["token"]
    if change == "config":
        case.path.write_text(json.dumps({"version": 1, "rules": [rule(folder="Folders/Other")]}))
    elif change == "uid":
        case.fake.folders["INBOX"]["2"] = case.fake.folders["INBOX"].pop("1")
    else:
        case.fake.folders["INBOX"]["1"].headers = case.fake.folders["INBOX"]["1"].headers.replace(
            b"sender@example.com", b"relative@gmail.com"
        )
    with pytest.raises(MailValidationError, match="changed"):
        case.rules.apply_rules(token)
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)
    assert not case.cleanup.backend.log.path.exists()


def test_confirmation_leaves_new_matching_arrival_outside_approval(mailbox):
    case = mailbox()
    token = case.rules.preview_rules()["token"]
    arrival = message("<arrival@example.test>")
    case.fake.folders["INBOX"]["2"] = arrival
    result = case.rules.apply_rules(token)
    assert result["count"] == 1 and not result["partial"]
    assert case.fake.folders["INBOX"] == {"2": arrival}
    assert len(case.fake.folders["Folders/Review"]) == 1


def test_total_batch_cap_spans_rules(mailbox):
    case = mailbox(rules=[rule("subject", "First"), rule(folder="Folders/Other")], count=201)
    for item in list(case.fake.folders["INBOX"].values())[:100]:
        item.headers = item.headers.replace(b"Hello", b"First")
    preview = case.rules.preview_rules()
    assert preview["count"] == 200 and preview["token"]
    assert preview["total_matches"] == 201 and preview["remaining"] == 1
    assert "200" in preview["reason"]
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)


def test_missing_or_virtual_destination_refused(mailbox):
    case = mailbox()
    case.fake.folder_flags["Folders/Review"] = r"\All"
    with pytest.raises(MailValidationError, match="destination folders"):
        case.rules.preview_rules()
    del case.fake.folders["Folders/Review"]
    with pytest.raises(MailValidationError, match="destination folders"):
        case.rules.preview_rules()


def test_empty_rules_validate_source_without_selecting_mail(mailbox):
    case = mailbox(rules=[])
    result = case.rules.preview_rules()
    assert result["count"] == 0 and result["token"] is None
    assert any(call[0] == "LIST" for call in case.fake.calls)
    assert not any(call[0] in MUTATIONS | {"SELECT", "EXAMINE"} for call in case.fake.calls)


def test_apply_reports_rows_its_batched_final_check_left_untouched(mailbox, monkeypatch):
    from proton_mail_mcp.cleanup_response import cleanup_response

    case = mailbox(count=5)
    preview = case.rules.preview_rules()
    assert preview["token"] and preview["count"] == 5
    fake, edits = case.fake, []
    original = fake.select

    def select(folder, *, readonly):
        result = original(folder, readonly=readonly)
        # After preflight's writable SELECT, before the batched final FETCH.
        if not readonly and not edits and fake.current == "INBOX":
            item = fake.folders["INBOX"]["4"]
            item.headers = item.headers.replace(b"Wed, 14 Oct 2026", b"Thu, 15 Oct 2026")
            edits.append(True)
        return result

    monkeypatch.setattr(fake, "select", select)
    result = case.rules.apply_rules(preview["token"])
    assert edits and result["done"] == 4 and result["partial"]
    assert [(row["uid"], row["status"]) for row in result["mismatched"]] == [("4", "not_attempted")]
    assert "fingerprint changed" in result["mismatched"][0]["reason"]
    assert cleanup_response(dict(result))["mismatched_ids"] == ["<rule-4@example.test>"]


@pytest.mark.parametrize("mark_read", [False, True])
def test_preview_plans_a_mark_read_rules_move_as_store_and_move(mailbox, monkeypatch, mark_read):
    case = mailbox(count=2)
    planned = []
    for name in ("_estimated_calls", "_preview_has_headroom"):
        original = getattr(case.cleanup, name)

        def spy(*args, _original=original, _name=name, **kwargs):
            planned.append((_name, kwargs.get("params")))
            return _original(*args, **kwargs)

        monkeypatch.setattr(case.cleanup, name, spy)
    assert case.rules.preview_rules(mark_read=mark_read)["token"]
    assert planned == [
        ("_estimated_calls", {"mark_read": mark_read}),
        ("_preview_has_headroom", {"mark_read": mark_read}),
    ]
