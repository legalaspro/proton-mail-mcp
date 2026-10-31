"""Proton-shaped mailboxes through real sockets, imaplib and production deadlines.

Literal-work counters retain the conservative Gluon model. The default timing
model prices command round trips and fetched metadata, since server-side SEARCH
work is implementation-dependent. A separate worst-case literal-load test keeps
that limitation visible. None of these timings are a real Bridge benchmark.
"""

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest
from action_records import execution_records as replay_records
from bridge_costs import (
    DEFAULT_COSTS,
    LITERAL_SEARCH_KEYS,
    PESSIMISTIC_COSTS,
    ChargedThreading,
    WorkCounters,
    envelope_from_headers,
    search_keys,
)
from mock_imap_cleanup import CleanupIMAPPeer
from resume_assertions import assert_bound_progress, identity, journal_identity

from proton_mail_mcp import mail
from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import MailClient, MailValidationError
from proton_mail_mcp.security import Credentials

SYNTHETIC_BATCH = json.loads(
    Path(__file__).with_name("fixtures").joinpath("preview_batch.json").read_text()
)
BUDGET = 45.0


class WorkClock:
    """Swap only mail.time, leaving threading's own clock and timers untouched."""

    def __init__(self):
        self.elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    def __getattr__(self, name):
        return getattr(time, name)


def headers(identifier, *, sender="offers@updates.example.invalid", date="17 Oct 2026"):
    return (
        f"Message-ID: {identifier}\r\n"
        f"From: Newsletter <{sender}>\r\n"
        "To: Example Reader <reader@example.invalid>, Other Reader <other@example.invalid>, "
        "Third Reader <third@example.invalid>\r\n"
        "Cc: Newsletter Team <team@example.invalid>\r\n"
        "Subject: Synthetic preview fixture with a realistic newsletter summary, "
        "delivery updates, upcoming events and account information\r\n"
        f"Date: {date} 09:00:00 +0000\r\n\r\n"
    ).encode()


class ProtonShapePeer(CleanupIMAPPeer):
    def __init__(self, clock, identifiers, *, mailbox_size=20_027, costs=DEFAULT_COSTS):
        super().__init__()
        self.clock = clock
        self.work = WorkCounters(clock, costs)
        self.folders = {
            name: {}
            for name in (
                "INBOX",
                "Drafts",
                "Sent",
                "Starred",
                "Archive",
                "Spam",
                "Trash",
                "All Mail",
                "Folders/Reviewed",
                "Folders/Receipts",
                "Folders/Old",
                "Labels/Review",
                "Labels/Keep",
                "Labels/Receipts",
            )
        }
        for index in range(mailbox_size - len(identifiers)):
            raw = headers(f"<filler{index:08d}@example.invalid>", date="01 Jan 2020")
            self.folders["Archive"][str(index + 1)] = raw
            self.folders["All Mail"][str(index + 1)] = raw
        for index, identifier in enumerate(identifiers):
            # Independent folder UID spaces deliberately collide.
            folder = "INBOX" if index % 2 == 0 else "Spam"
            uid = str(index // 2 + 1)
            self.folders[folder][uid] = headers(identifier)
            self.folders["All Mail"][str(mailbox_size - index)] = headers(identifier)
        self.uidvalidities = {name: str(index + 100) for index, name in enumerate(self.folders)}
        self.next_uid = {name: mailbox_size + 100 for name in self.folders}
        self.deleted = {name: set() for name in self.folders}
        self.header_scans = []
        self.fetched_rows = 0
        self.command_hook = self.charge
        self.expire_in = None

    def charge(self, folder, operation, command):
        if folder == self.expire_in and operation == "FETCH":
            self.clock.elapsed += BUDGET + 1
        rows = 0
        if operation == "SEARCH":
            rows = len(self.folders[folder])
            if search_keys(command) & LITERAL_SEARCH_KEYS:
                self.header_scans.append((folder, rows))
        elif operation == "FETCH":
            rows = len(self.uids(command.split()[2].decode(), self.folders[folder]))
            self.fetched_rows += rows
        self.work.charge(operation, command, rows)

    @staticmethod
    def matches(raw, criteria):
        terms = list(criteria)
        if terms[:2] == ["CHARSET", "UTF-8"]:
            terms = terms[2:]
        if terms == ["ALL"]:
            # An indexed UID inventory does not examine message headers.
            return True
        # SEARCH dates model indexed INTERNALDATE; fixtures deliberately set it
        # to Date. Adversarial protected-copy tests use old dates in other views.
        dates = [
            (term, terms[index + 1])
            for index, term in enumerate(terms)
            if term in ("SINCE", "BEFORE")
        ]
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        day = parsedate_to_datetime(str(parsed["Date"])).date()
        if dates:
            for term, value in dates:
                limit = datetime.strptime(value, "%d-%b-%Y").date()
                if term == "SINCE" and day < limit or term == "BEFORE" and day >= limit:
                    return False
        if "ON" in terms:
            # Canonical lookups send only an OR tree of ON days.
            words = [term for term in terms if term != "OR"]
            assert len(words) % 2 == 0 and set(words[::2]) == {"ON"}
            return day in {datetime.strptime(value, "%d-%b-%Y").date() for value in words[1::2]}
        return CleanupIMAPPeer.matches(raw, criteria)


@pytest.fixture
def shape_mailbox(tmp_path, monkeypatch):
    peers = []

    def build(
        identifiers=SYNTHETIC_BATCH,
        *,
        mailbox_size=20_027,
        production=True,
        costs=DEFAULT_COSTS,
        model_literal_work=False,
    ):
        clock = WorkClock()
        monkeypatch.setattr(mail, "time", clock)
        timing_costs = costs if model_literal_work else replace(costs, literal_load=0)
        peer = ProtonShapePeer(clock, identifiers, mailbox_size=mailbox_size, costs=timing_costs)
        monkeypatch.setattr(mail, "threading", ChargedThreading(peer.work))
        peers.append(peer)
        factory = peer.factory()
        if production:
            # Exercise the actual guarded read/readline overrides, including the
            # Python 3.14 imaplib implementation, with only the endpoint replaced.
            monkeypatch.setattr(mail._DeadlineIMAP4, "_create_socket", factory._create_socket)
            monkeypatch.setattr(mail._DeadlineIMAP4, "starttls", factory.starttls)
            factory = mail._DeadlineIMAP4
        settings = Settings(
            token="offline-proton-shape-" + "x" * 32,
            bridge_user="reader@example.invalid",
            imap_port=31337,
            action_log_path=str(tmp_path / "synthetic-actions.jsonl"),
            timeout=30,
            call_timeout=BUDGET,
        )
        client = MailClient(
            settings, lambda _: Credentials("reader@example.invalid", "dummy"), factory
        )
        return client, peer, clock

    yield build
    for peer in peers:
        peer.close()


def assert_header_only(peer):
    for entry in peer.commands:
        command = entry[-1]
        if isinstance(command, str):
            command = command.encode()
        if command.startswith(b"UID FETCH"):
            assert not re.search(rb"BODY\[|BODY\.PEEK\[(?!HEADER)|RFC822(?!\.SIZE)", command)
    return True


def assert_success(client, peer, clock, identifiers):
    started = time.monotonic()
    result = client.preview_action("move_to_trash", ids=identifiers)
    wall_elapsed = time.monotonic() - started
    # Never include the result/token/IDs in assertion output, including when the
    # optional locally supplied batch is used.
    assert bool(result["token"])
    assert result["count"] == 27
    assert result["partial"] is False
    assert clock.elapsed < BUDGET
    assert wall_elapsed < BUDGET
    assert peer.work.client_cpu_seconds > 0
    assert not peer.header_scans
    assert assert_header_only(peer)
    assert not peer.mutations
    return {
        "modeled_seconds": clock.elapsed,
        "wall_seconds": wall_elapsed,
        "fetched_db_rows": peer.fetched_rows,
        **peer.work.summary(),
        "token_returned": True,
    }


def test_proton_shape_27_id_preview_finishes(shape_mailbox, request):
    client, peer, clock = shape_mailbox(production=True)
    assert len(peer.folders["All Mail"]) >= 20_000
    assert len(SYNTHETIC_BATCH) == 27
    assert len(envelope_from_headers(headers(SYNTHETIC_BATCH[0]))) >= 512
    assert all(re.fullmatch(r"<[a-z0-9]{37}@example\.invalid>", value) for value in SYNTHETIC_BATCH)
    request.node.user_properties.extend(
        assert_success(client, peer, clock, SYNTHETIC_BATCH).items()
    )


def test_proton_shape_optional_real_batch(shape_mailbox, request):
    # Explicit opt-in path; no real identifiers are copied into the repository.
    path = os.environ.get("PMM_REAL_BATCH")
    if not path:
        pytest.skip("Optional real batch path not configured")
    if not Path(path).is_file():
        pytest.skip("Optional real batch file is unavailable")
    identifiers = json.loads(Path(path).read_text())
    if not isinstance(identifiers, list) or len(identifiers) != 27:
        pytest.fail("Optional real batch does not have 27 entries", pytrace=False)
    client, peer, clock = shape_mailbox(identifiers)
    request.node.user_properties.extend(assert_success(client, peer, clock, identifiers).items())


@pytest.mark.parametrize("folder", ["INBOX", "Spam", "Trash"])
@pytest.mark.parametrize("costs", [DEFAULT_COSTS, PESSIMISTIC_COSTS], ids=["calibrated", "db-3x"])
def test_one_day_query_preview(shape_mailbox, folder, costs):
    client, peer, clock = shape_mailbox(costs=costs)
    if folder == "Trash":
        peer.folders[folder]["1"] = peer.folders["INBOX"].pop("1")
    result = client.preview_action(
        "move_to_trash" if folder != "Trash" else "archive",
        query={"folder": folder, "since": "2026-10-17", "before": "2026-10-18"},
    )
    assert bool(result["token"])
    assert result["count"] > 0 and result["partial"] is False
    assert clock.elapsed < BUDGET * 0.8
    assert assert_header_only(peer)
    assert not peer.mutations


def test_archive_preview(shape_mailbox):
    client, peer, _ = shape_mailbox()
    result = client.preview_action("archive", ids=SYNTHETIC_BATCH)
    assert bool(result["token"])
    assert result["count"] == 27 and result["partial"] is False
    assert assert_header_only(peer)
    assert not peer.mutations


def test_all_mail_default_search_returns_nonzero_partial(shape_mailbox, monkeypatch):
    client, peer, clock = shape_mailbox(production=True)
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-17")
    result = client.search_mail(folder="All Mail", limit=100, include_snippets=False)
    assert len(result["messages"]) == 27 and result["total"] == 27
    assert result["partial"] is True and result["total_is_exact"] is False
    assert result["applied_since"] == "2026-09-17"
    assert clock.elapsed < BUDGET
    searches = [command for _, command in peer.commands if command.startswith(b"UID SEARCH")]
    assert b"SINCE 17-Sep-2026" in searches[0]
    # The canonical lookup only adds indexed ON-day searches, never HEADER scans.
    assert searches[1:] and all(
        re.fullmatch(rb"UID SEARCH CHARSET UTF-8 ON 17-Oct-2026", command)
        for command in searches[1:]
    )
    assert not peer.header_scans and not peer.mutations
    # Each All Mail hit reports its real INBOX or Spam handle; All Mail is the alias.
    for item in result["messages"]:
        real = "INBOX" if item["message_id"] in SYNTHETIC_BATCH[::2] else "Spam"
        assert item["folder"] == real
        assert peer.folders[real][item["uid"]] == peer.folders["All Mail"][item["alias"]["uid"]]
        assert item["uidvalidity"] == peer.uidvalidities[real]
        assert item["alias"]["folder"] == "All Mail"
        assert item["alias"]["uidvalidity"] == peer.uidvalidities["All Mail"]


@pytest.mark.parametrize("folder", ["All Mail", "Labels/Keep"])
def test_old_protected_copy_anywhere_blocks(shape_mailbox, folder):
    client, peer, _ = shape_mailbox(mailbox_size=80)
    identifier = SYNTHETIC_BATCH[0]
    uid = "80" if folder == "All Mail" else "1"
    peer.folders[folder][uid] = headers(
        identifier, sender="security@mail.bank.example.com", date="01 Jan 2000"
    )
    result = client.preview_action("move_to_trash", ids=[identifier])
    assert not result["token"]
    assert result["count"] == 1 and result["blocked"]
    assert result["blocked"][0]["id"] == identifier
    assert result["blocked"][0]["folder"] == "INBOX"
    assert result["partial"] is False
    assert not peer.mutations


@pytest.mark.parametrize("folder", ["INBOX", "All Mail", "Labels/Keep"])
def test_ambiguous_id_is_refused(shape_mailbox, folder):
    client, peer, _ = shape_mailbox(mailbox_size=80)
    identifier = SYNTHETIC_BATCH[0]
    peer.folders[folder]["999"] = headers(identifier)
    if folder.startswith("Labels/"):
        peer.folders[folder]["998"] = headers(identifier)
    result = client.preview_action("move_to_trash", ids=[identifier])
    assert result["token"] is None and result["excluded_ids"] == [identifier]
    assert result["excluded_count"] == 1
    assert not peer.mutations


def test_deadline_mid_membership_never_issues_token(shape_mailbox):
    client, peer, _ = shape_mailbox(mailbox_size=80)
    peer.folders["Labels/Keep"]["1"] = headers(SYNTHETIC_BATCH[0])
    peer.expire_in = "Labels/Keep"
    result = client.preview_action("move_to_trash", ids=SYNTHETIC_BATCH)
    assert result["partial"] is True
    assert result["token"] is None and result["expires_at"] is None
    assert any(stage in result["reason"].lower() for stage in ("membership", "inventory"))
    assert "Labels/Keep" in result["reason"]
    assert "Synthetic preview fixture" not in result["reason"]
    assert not any(identifier in result["reason"] for identifier in SYNTHETIC_BATCH)
    assert not peer.mutations


def test_colliding_uids_keep_message_and_folder_identity(shape_mailbox):
    client, peer, _ = shape_mailbox(mailbox_size=80)
    assert "1" in peer.folders["INBOX"] and "1" in peer.folders["Spam"]
    result = client.preview_action("archive", ids=SYNTHETIC_BATCH[:2])
    assert bool(result["token"])
    assert {(row["id"], row["folder"]) for row in result["samples"]} == {
        (SYNTHETIC_BATCH[0], "INBOX"),
        (SYNTHETIC_BATCH[1], "Spam"),
    }
    grant = client.cleanup.tokens.claim(result["token"], "archive")
    assert {(row["message_id"], row["folder"]) for row in grant.messages} == {
        (SYNTHETIC_BATCH[0], "INBOX"),
        (SYNTHETIC_BATCH[1], "Spam"),
    }
    assert not peer.mutations


@pytest.mark.parametrize("size", [1, 2, 27])
@pytest.mark.parametrize("costs", [DEFAULT_COSTS, PESSIMISTIC_COSTS], ids=["calibrated", "db-3x"])
def test_id_shapes_have_bounded_header_fetches_with_budget_margin(
    shape_mailbox, size, costs, request
):
    client, peer, clock = shape_mailbox(costs=costs)
    result = client.preview_action("move_to_trash", ids=SYNTHETIC_BATCH[:size])
    assert bool(result["token"])
    assert result["count"] == size and result["partial"] is False
    assert assert_header_only(peer)
    assert clock.elapsed < BUDGET * 0.8
    request.node.user_properties.extend(
        {
            "modeled_seconds": clock.elapsed,
            "margin_seconds": BUDGET - clock.elapsed,
            **peer.work.summary(),
        }.items()
    )
    # Candidate inventories are reused by source and memberships.
    inventories = [folder for folder, command in peer.commands if command.endswith(b" ALL")]
    assert len(inventories) == len(set(inventories))


@pytest.mark.parametrize("action", ["move_to_trash", "archive", "remove_label"])
def test_full_preview_execute_undo_flow_fetches_only_headers(tmp_path, action):
    from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations

    peer = BridgeSim(copyuid=False)
    try:
        client = make_client(peer, tmp_path)
        identifier = "<flow@example.invalid>"
        peer.add(Msg(hdr(identifier), labels={"Labels/Review"}))
        kwargs = {"ids": [identifier]}
        if action == "remove_label":
            kwargs = {"query": {"folder": "Labels/Review"}, "params": {"label": "Review"}}
        preview = client.preview_action(action, **kwargs)
        assert bool(preview["token"])
        assert assert_header_only(peer)
        executed = getattr(client, action)(preview["token"])
        assert executed["count"] == 1 and executed["partial"] is False
        assert assert_header_only(peer)
        if action != "remove_label":
            # Without COPYUID the move still pinned its new copy, the evidence
            # that counted it done; undo restores by that handle, from Archive too.
            assert executed["messages"][0]["uid_after"]
        moves = len(mutations(peer))
        undone = client.undo_action(executed["log_ids"][0])
        assert undone["count"] == 1 and undone["partial"] is False
        assert len(mutations(peer)) > moves
        assert assert_header_only(peer)
        assert not peer.violations and not peer.errors
    finally:
        peer.close()


@pytest.mark.parametrize("action", ["move_to_trash", "archive", "remove_label"])
def test_27_row_execution_reresolves_within_pessimistic_budget(shape_mailbox, action, request):
    client, peer, clock = shape_mailbox(costs=PESSIMISTIC_COSTS)
    kwargs = {"ids": SYNTHETIC_BATCH}
    if action == "remove_label":
        peer.folders["Labels/Review"] = {
            str(index + 1): headers(identifier) for index, identifier in enumerate(SYNTHETIC_BATCH)
        }
        kwargs = {"query": {"folder": "Labels/Review"}, "params": {"label": "Review"}}
    preview = client.preview_action(action, **kwargs)
    assert bool(preview["token"])
    # Only a lookup of All Mail and every real folder names a label sample's real
    # folder. This pessimistic approval leaves too little of the 80% preview
    # budget for one, so it never starts: samples keep their label alias and the
    # preview is partial, while the approval itself is complete.
    labels = action == "remove_label"
    assert preview["partial"] is labels
    assert not any(b" ON " in command for _, command in peer.commands)
    if labels:
        assert {sample["folder"] for sample in preview["samples"]} == {None}
        assert {sample["alias"]["folder"] for sample in preview["samples"]} == {"Labels/Review"}
    preview_seconds = clock.elapsed
    assert preview_seconds < BUDGET * 0.8
    peer.commands.clear()
    # Preserve the 45 s inventory-cost regression, while reserving the mutation
    # allowance on top of its read-only resolution cost.
    client.settings = replace(client.settings, call_timeout=120, execute_timeout=120)
    client.cleanup.settings = client.settings
    grant = client.cleanup.tokens.inspect(preview["token"], action)
    approved = {identity(row) for row in grant.messages}
    by_identifier = {row["message_id"]: row for row in grant.messages}
    original_folders = {name: dict(rows) for name, rows in peer.folders.items()}
    charge = peer.command_hook
    actual = []

    def observe(folder, operation, command):
        charge(folder, operation, command)
        if operation != ("EXPUNGE" if action == "remove_label" else "MOVE"):
            return
        for uid in peer.uids(command.split()[2].decode(), peer.folders[folder]):
            raw = peer.folders[folder][uid]
            identifier = str(BytesParser(policy=policy.default).parsebytes(raw)["Message-ID"])
            row = by_identifier[identifier]
            if action == "remove_label":
                assert (folder, peer.uidvalidities[folder], uid) in {
                    (member["folder"], member["uidvalidity"], member["uid"])
                    for member in row["memberships"]
                }
            else:
                assert (folder, peer.uidvalidities[folder], uid) == identity(row)[:3]
            bound = identity(row)
            assert bound not in actual
            actual.append(bound)

    peer.command_hook = observe
    token, results, execute_seconds = preview["token"], [], []
    while token:
        started = clock.elapsed
        command_start = len(peer.commands)
        executed = getattr(client, action)(token)
        results.append(executed)
        execute_seconds.append(clock.elapsed - started)
        assert execute_seconds[-1] < BUDGET
        assert_bound_progress(client.cleanup, grant, results, actual)
        assert len(results) <= preview["estimated_calls"]
        assert assert_header_only(peer)
        if action == "remove_label":
            all_inventories = [
                command
                for folder, command in peer.commands[command_start:]
                if folder == "All Mail" and command.endswith(b" ALL")
            ]
            assert len(all_inventories) <= 1
        token = executed["continuation_token"]
    assert sum(result["count"] for result in results) == 27 and executed["partial"] is False
    assert set(actual) == approved
    # Per-message label removal sends STORE and EXPUNGE for each message; batched
    # MOVEs name every approved UID exactly once (observed above).
    if action == "remove_label":
        assert peer.mutations == 54
    else:
        assert len(actual) == 27 and peer.mutations <= 27
    chunks = list(
        reversed(
            replay_records(
                [
                    json.loads(line)
                    for line in client.cleanup.backend.log.path.read_text().splitlines()
                ]
            )
        )
    )
    # Every physical log keeps its exact done set and is undone newest-first.
    assert len(chunks) == sum(len(result["log_ids"]) for result in results)
    before_retry = peer.mutations
    with pytest.raises(MailValidationError, match="already used"):
        getattr(client, action)(preview["token"])
    assert peer.mutations == before_retry
    peer.command_hook = charge
    undo_seconds = []
    restored_identities = set()
    for chunk in chunks:
        expected = {journal_identity(row) for row in chunk["messages"] if row["status"] == "done"}
        assert expected and not restored_identities.intersection(expected)
        undo_actual = []

        def observe_undo(folder, operation, command):
            charge(folder, operation, command)
            if operation not in {"MOVE", "COPY"}:
                return
            for uid in peer.uids(command.split()[2].decode(), peer.folders[folder]):
                identifier = str(
                    BytesParser(policy=policy.default).parsebytes(peer.folders[folder][uid])[
                        "Message-ID"
                    ]
                )
                bound = identity(by_identifier[identifier])
                assert bound in expected and bound not in undo_actual
                undo_actual.append(bound)

        peer.command_hook = observe_undo
        started = clock.elapsed
        restored = client.undo_action(chunk["log_id"])
        undo_seconds.append(clock.elapsed - started)
        assert restored["count"] == len(expected) and restored["partial"] is False
        assert set(undo_actual) == expected and len(undo_actual) == len(expected)
        assert undo_seconds[-1] < BUDGET
        assert assert_header_only(peer)
        restored_identities.update(expected)
        before_retry = peer.mutations
        with pytest.raises(MailValidationError, match="already.*undone"):
            client.undo_action(chunk["log_id"])
        assert peer.mutations == before_retry
    assert restored_identities == approved
    for folder, rows in original_folders.items():
        assert sorted(peer.folders[folder].values()) == sorted(rows.values())
    request.node.user_properties.extend(
        {
            "preview_seconds": preview_seconds,
            "execute_seconds": execute_seconds,
            "undo_seconds": undo_seconds,
            **peer.work.summary(),
        }.items()
    )


@pytest.mark.parametrize("folder", ["All Mail", "Labels/Keep"])
@pytest.mark.parametrize("position", ["oldest", "newest"])
def test_protected_copy_beyond_first_500_blocks(shape_mailbox, folder, position):
    client, peer, _ = shape_mailbox(mailbox_size=1_200)
    if folder == "All Mail":
        del peer.folders[folder]["1200"]
    else:
        peer.folders[folder] = {
            str(uid): headers(f"<keep{uid}@example.invalid>") for uid in range(2, 1_201)
        }
    peer.folders[folder]["1" if position == "oldest" else "1201"] = headers(
        SYNTHETIC_BATCH[0], sender="security@mail.bank.example.com", date="01 Jan 2000"
    )
    result = client.preview_action("move_to_trash", ids=SYNTHETIC_BATCH[:1])
    assert not result["token"] and result["blocked"]
    assert assert_header_only(peer) and not peer.mutations


def test_ambiguous_copy_beyond_first_500_is_refused(shape_mailbox):
    client, peer, _ = shape_mailbox(mailbox_size=1_200)
    peer.folders["All Mail"]["2"] = headers(SYNTHETIC_BATCH[0], date="01 Jan 2000")
    result = client.preview_action("move_to_trash", ids=SYNTHETIC_BATCH[:1])
    assert result["token"] is None and result["excluded_ids"] == SYNTHETIC_BATCH[:1]
    assert result["excluded_reasons"] == {"ambiguous_membership": 1}
    assert assert_header_only(peer) and not peer.mutations


@pytest.mark.parametrize(
    "sender",
    [None, "", "garbled", "group:;"],
)
@pytest.mark.parametrize("folder", ["All Mail", "Labels/Keep"])
def test_unknown_envelope_sender_anywhere_is_protected(shape_mailbox, sender, folder):
    client, peer, _ = shape_mailbox(mailbox_size=80)
    raw = headers(SYNTHETIC_BATCH[0])
    raw = re.sub(rb"From:[^\r]+\r\n", b"", raw)
    if sender is not None:
        raw = f"From: {sender}\r\n".encode() + raw
    peer.folders[folder]["80" if folder == "All Mail" else "1"] = raw
    result = client.preview_action("move_to_trash", ids=SYNTHETIC_BATCH[:1])
    assert not result["token"] and result["blocked"]
    assert any(
        "protect" in row["reason"].lower() or "sender" in row["reason"].lower()
        for row in result["blocked"]
    )
    assert assert_header_only(peer) and not peer.mutations


@pytest.mark.parametrize("folder", ["All Mail", "Labels/Keep"])
@pytest.mark.parametrize("consistent", [False, True], ids=["changed-copy", "matching-copies"])
def test_utf8_display_name_is_usable_only_with_consistent_memberships(
    shape_mailbox, folder, consistent
):
    client, peer, _ = shape_mailbox(mailbox_size=80)
    raw = re.sub(rb"From:[^\r]+\r\n", b"", headers(SYNTHETIC_BATCH[0]))
    raw = b"From: =?utf-8?Q?B=C3=A4nk?= <x@example.invalid>\r\n" + raw
    peer.folders[folder]["80" if folder == "All Mail" else "1"] = raw
    if consistent:
        peer.folders["INBOX"]["1"] = raw
        peer.folders["All Mail"]["80"] = raw
    result = client.preview_action("move_to_trash", ids=SYNTHETIC_BATCH[:1])
    assert not result["blocked"]  # A valid UTF-8 display name is not ambiguous.
    if consistent:
        assert result["token"] and result["count"] == 1
        assert result["excluded_count"] == 0
        assert result["samples"][0]["from"] == "Bänk <x@example.invalid>"
    else:
        assert result["token"] is None and result["count"] == 0
        assert result["excluded_count"] == 1
        assert result["excluded_reasons"] == {"inconsistent_sender_headers": 1}
    assert assert_header_only(peer) and not peer.mutations


@pytest.mark.parametrize("folder", ["INBOX", "All Mail", "Labels/Keep"])
def test_nil_envelope_message_id_blocks_only_source_candidates(shape_mailbox, folder):
    client, peer, _ = shape_mailbox(mailbox_size=80)
    raw = headers(SYNTHETIC_BATCH[0])
    raw = re.sub(rb"Message-ID:[^\r]+\r\n", b"", raw)
    peer.folders[folder]["1" if folder == "INBOX" else "500"] = raw
    if folder == "INBOX":
        result = client.preview_action("move_to_trash", query={"folder": "INBOX"})
        assert result["token"] and result["excluded_count"] == 1
        assert result["excluded_reasons"] == {"invalid_identity": 1}
        grant = client.cleanup.tokens.inspect(result["token"], "move_to_trash")
        assert all(row["message_id"] for row in grant.messages)
    else:
        result = client.preview_action("move_to_trash", query={"folder": "INBOX"})
        assert result["token"] and not result["blocked"]
    assert assert_header_only(peer) and not peer.mutations


def test_one_envelope_with_two_requested_ids_is_ambiguous(shape_mailbox):
    client, peer, _ = shape_mailbox(mailbox_size=80)
    peer.folders["Labels/Keep"]["1"] = headers(" ".join(SYNTHETIC_BATCH[:2]))
    result = client.preview_action("move_to_trash", ids=SYNTHETIC_BATCH[:2])
    assert result["token"] is None and set(result["excluded_ids"]) == set(SYNTHETIC_BATCH[:2])
    assert result["excluded_reasons"] == {"ambiguous_identity": 2}
    assert assert_header_only(peer) and not peer.mutations


def test_archived_main_reproduces_reported_timeouts():
    source = os.environ.get("PMM_BASELINE_PACKAGE")
    if not source:
        pytest.skip("Optional full archived main package is not configured")
    tests = Path(__file__).resolve().parent
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((source, str(tests))))
    script = str(tests / "tools" / "bridge_model.py")
    for scenario, expected in (("all", "timeout"), ("report2", "report2")):
        # The only child is this fixed offline harness, inheriting netiso. Use
        # Popen because conftest forbids general subprocess.run network helpers.
        with subprocess.Popen(
            [
                sys.executable,
                script,
                "--label",
                "main281",
                "--mode",
                "calibrated",
                "--scenario",
                scenario,
                "--expect",
                expected,
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            try:
                stdout, _ = process.communicate(timeout=240)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                pytest.fail("Archived baseline calibration timed out", pytrace=False)
            # Never emit subprocess output on failure.
            assert process.returncode == 0, "Archived baseline calibration failed"
        summaries = [json.loads(line) for line in stdout.splitlines()]
        assert len(summaries) == (8 if scenario == "all" else 1)
        assert all(row["partial"] and not row["token"] for row in summaries)


def test_mailbox_literal_search_cost_does_not_block_identity_preview(shape_mailbox):
    # Charge whole-message decryption for any HEADER SEARCH. Identity discovery
    # must avoid that work while retaining raw PEEK evidence for candidates.
    client, peer, clock = shape_mailbox(
        costs=DEFAULT_COSTS,
        model_literal_work=True,
    )
    result = client.preview_action("move_to_trash", ids=SYNTHETIC_BATCH)
    assert result["partial"] is False and result["token"]
    assert clock.elapsed < BUDGET * 0.8
    assert not peer.header_scans
    assert assert_header_only(peer) and not peer.mutations


def test_query_address_and_subject_filters_use_search_and_header_metadata(shape_mailbox):
    client, peer, _ = shape_mailbox(mailbox_size=80)
    result = client.preview_action(
        "move_to_trash",
        query={
            "folder": "INBOX",
            "from": "offers@updates.example.invalid",
            "to": "reader@example.invalid",
            "subject": "Synthetic preview fixture",
        },
    )
    assert bool(result["token"]) and result["partial"] is False
    assert result["count"] == 14 and assert_header_only(peer)


def test_cleanup_body_text_query_is_refused_without_literal_fallback(shape_mailbox):
    client, peer, _ = shape_mailbox(mailbox_size=80)
    with pytest.raises(MailValidationError, match="message bodies"):
        client.preview_action("move_to_trash", query={"folder": "INBOX", "text": "content"})
    assert assert_header_only(peer) and not peer.mutations
