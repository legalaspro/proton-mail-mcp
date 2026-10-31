"""Apply reuses occurrence discovery while keeping each authorization read fresh."""

from dataclasses import replace

import pytest
from test_cleanup_mutations import MUTATIONS
from test_folder_rules import mailbox as mailbox
from test_mail import message
from test_rules_matched_membership import matched_case

from proton_mail_mcp import cleanup_lookup, mail
from proton_mail_mcp.mail import CallDeadlineExceeded, MailValidationError


class PreflightComplete(MailValidationError):
    """Stop at the mutation boundary: GluonSearchImap deliberately has no MOVE."""


def prepared(mailbox):
    case = matched_case(mailbox, count=2)
    preview = case.rules.preview_rules()
    assert preview["token"] and preview["batch_count"] == 2
    return case, preview["token"]


def all_mail_entry(case):
    return next(
        entry
        for entry in case.cleanup.client._rules_raw_identity_snapshots.values()
        if entry["inventory"]["snapshot"]["folder"] == "All Mail"
    )


def stop_before_mutation(case, monkeypatch):
    boundaries = []

    def stop(connection, deadline, action, params, rows, **options):
        boundaries.append(connection)
        raise PreflightComplete("test stopped after the complete rules preflight")

    monkeypatch.setattr(case.cleanup.backend, "apply", stop)
    return boundaries


def raw_reads(case, monkeypatch):
    reads = []
    metadata = case.cleanup.client._metadata

    def record(connection, uids, **options):
        if case.fake.current == "All Mail" and options.get("identity_headers_only"):
            # Range reconciliation can also probe unallocated or expunged UIDs;
            # count messages whose headers can actually be read.
            reads.extend(uid for uid in uids if uid in case.fake.folders["All Mail"])
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.cleanup.client, "_metadata", record)
    return reads


def assert_unclaimed(case, token):
    assert case.cleanup.tokens.inspect(token, "apply_rules")
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)
    assert not case.cleanup.backend.log.path.exists()


def test_finished_audit_reads_only_matching_rows_at_apply(mailbox, monkeypatch):
    case, token = prepared(mailbox)
    reads = raw_reads(case, monkeypatch)
    boundaries = stop_before_mutation(case, monkeypatch)

    with pytest.raises(PreflightComplete):
        case.rules.apply_rules(token)

    assert len(boundaries) == 1
    assert set(reads) == {"1", "2"}
    assert_unclaimed(case, token)


@pytest.mark.parametrize(
    "invalid",
    ["checksum", "account", "endpoint", "source", "rules-digest", "mark-read"],
)
def test_mismatched_audit_binding_requires_a_new_raw_walk(mailbox, monkeypatch, invalid):
    case, token = prepared(mailbox)
    inventory = all_mail_entry(case)["inventory"]
    if invalid == "checksum":
        inventory["checksum"] = "0" * 64
    elif invalid in {"account", "endpoint"}:
        setting = "bridge_user" if invalid == "account" else "imap_host"
        case.cleanup.client.settings = replace(
            case.cleanup.client.settings,
            **{setting: "different@example.test" if invalid == "account" else "different.test"},
        )
    else:
        options = list(inventory["options"])
        index = {"source": 0, "rules-digest": 1, "mark-read": 2}[invalid]
        options[index] = not options[index] if invalid == "mark-read" else "different"
        inventory["options"] = tuple(options)
        inventory["checksum"] = cleanup_lookup._preview_inventory_checksum(inventory)
    reads = raw_reads(case, monkeypatch)
    boundaries = stop_before_mutation(case, monkeypatch)

    with pytest.raises(PreflightComplete):
        case.rules.apply_rules(token)

    assert "1000" in reads, "an audit from a different binding must not hide unrelated UIDs"
    assert len(boundaries) == 1
    assert_unclaimed(case, token)


@pytest.mark.parametrize("missing", ["expired", "removed"])
def test_unavailable_audit_falls_back_to_a_complete_read(mailbox, monkeypatch, missing):
    case, token = prepared(mailbox)
    if missing == "expired":
        all_mail_entry(case)["expires"] = mail.time.monotonic() - 1
    else:
        case.cleanup.client._rules_raw_identity_snapshots.clear()
    reads = raw_reads(case, monkeypatch)
    boundaries = stop_before_mutation(case, monkeypatch)

    with pytest.raises(PreflightComplete):
        case.rules.apply_rules(token)

    assert "1000" in reads and len(boundaries) == 1
    assert_unclaimed(case, token)


@pytest.mark.parametrize("missing", ["expired", "removed"])
def test_cold_audit_deadline_keeps_the_approval_unclaimed(mailbox, monkeypatch, missing):
    case, token = prepared(mailbox)
    if missing == "expired":
        all_mail_entry(case)["expires"] = mail.time.monotonic() - 1
    else:
        case.cleanup.client._rules_raw_identity_snapshots.clear()
    boundaries = stop_before_mutation(case, monkeypatch)
    clock = [mail.time.monotonic()]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    metadata = case.cleanup.client._metadata

    def exhaust(connection, uids, **options):
        if case.fake.current == "All Mail" and options.get("identity_headers_only"):
            clock[0] = options["deadline"].io_end
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.cleanup.client, "_metadata", exhaust)
    with pytest.raises(CallDeadlineExceeded):
        case.rules.apply_rules(token)

    assert not boundaries
    assert_unclaimed(case, token)


def test_missing_select_uidnext_retains_the_original_full_raw_reread(mailbox, monkeypatch):
    case, token = prepared(mailbox)
    # Without a current SELECT allocation witness, even a kept audit cannot
    # replace the original full raw scan and its hidden-field refusal.
    case.fake.reports_uidnext = False
    peer = case.fake.folders["All Mail"]["1000"]
    peer.headers = peer.headers[:-2] + b"Message-ID: <rule-1@example.test>\r\n\r\n"
    reads = raw_reads(case, monkeypatch)
    boundaries = stop_before_mutation(case, monkeypatch)

    with pytest.raises(MailValidationError, match="unresolved|ambiguous|Message-ID"):
        case.rules.apply_rules(token)

    assert "1000" in reads and not boundaries
    assert_unclaimed(case, token)


@pytest.mark.parametrize("replace_old", [False, True], ids=["new-uid", "same-count-edit"])
def test_changed_allocation_discovers_a_new_hidden_duplicate_before_claim(
    mailbox, monkeypatch, replace_old
):
    case, token = prepared(mailbox)
    if replace_old:
        del case.fake.folders["All Mail"]["1000"]
    peer = message("<independent-peer@example.test>")
    peer.headers = peer.headers[:-2] + b"Message-ID: <rule-1@example.test>\r\n\r\n"
    case.fake.folders["All Mail"]["1001"] = peer
    reads = raw_reads(case, monkeypatch)
    boundaries = stop_before_mutation(case, monkeypatch)

    with pytest.raises(MailValidationError, match="unresolved|ambiguous|Message-ID"):
        case.rules.apply_rules(token)

    assert "1001" in reads and not boundaries
    assert_unclaimed(case, token)


def test_expunge_between_preview_and_apply_reconciles_the_kept_audit(mailbox, monkeypatch):
    case, token = prepared(mailbox)
    del case.fake.folders["All Mail"]["1000"]
    reads = raw_reads(case, monkeypatch)
    boundaries = stop_before_mutation(case, monkeypatch)

    with pytest.raises(PreflightComplete):
        case.rules.apply_rules(token)

    assert len(boundaries) == 1 and set(reads) == {"1", "2"}
    assert {row["uid"] for row in all_mail_entry(case)["inventory"]["rows"]} == {"1", "2"}
    assert_unclaimed(case, token)


def test_apply_audit_state_belongs_to_each_call_proxy(mailbox, monkeypatch):
    case, token = prepared(mailbox)
    reads = raw_reads(case, monkeypatch)
    boundaries = stop_before_mutation(case, monkeypatch)

    for _ in range(2):
        before = len(reads)
        with pytest.raises(PreflightComplete):
            case.rules.apply_rules(token)
        assert set(reads[before:]) == {"1", "2"}
        assert "_rules_apply_state" not in vars(case.fake)
        assert "_rules_preview_state" not in vars(case.fake)

    first, second = boundaries
    assert first is not second
    assert first._rules_apply_state is not second._rules_apply_state
    assert getattr(first, "_rules_preview_state", None) is None
    assert getattr(second, "_rules_preview_state", None) is None
    assert_unclaimed(case, token)
